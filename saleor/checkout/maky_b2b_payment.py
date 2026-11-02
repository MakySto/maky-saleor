"""MAKY-B2B-V1 payment side: payment snapshot and release of payment attempts.

Separate from ``maky_b2b`` because it depends on ``calculations`` and the
payment gateway, while ``calculations`` itself imports ``maky_b2b`` for the
price path.
"""

import datetime
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..core.prices import quantize_price
from ..giftcard.const import GIFT_CARD_PAYMENT_GATEWAY_ID
from ..payment import PaymentError, TransactionAction, TransactionEventType
from ..payment.gateway import request_cancelation_action
from ..payment.models import TransactionEvent, TransactionItem
from .calculations import fetch_checkout_data
from .fetch import fetch_checkout_info, fetch_checkout_lines
from .maky_b2b import (
    CANCEL_REQUEST_DEBOUNCE,
    B2bContextChangedError,
    B2bErrorCode,
    B2bLockedError,
    Decision,
    PaymentAttempts,
    checkout_lines_fingerprint,
    evaluate,
    get_payment_attempts,
    load_metadata,
    lock_checkout,
    lock_metadata,
    locked_context,
    payment_lock_is_active,
    read_state,
    write_state,
)
from .models import Checkout

if TYPE_CHECKING:
    from ..app.models import App

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Payment initialization snapshot
# ---------------------------------------------------------------------------


def _remaining_amount(checkout: Checkout, total_gross: Decimal) -> Decimal:
    """Return the amount to pay, same rule as `TransactionSessionBase.get_amount`."""
    amount = total_gross
    for item in TransactionItem.objects.filter(checkout_id=checkout.pk):
        amount -= max(item.authorized_value, item.charged_value)
        amount -= item.authorize_pending_value
        amount -= item.charge_pending_value
    return max(amount, Decimal(0))


def prepare_payment_attempt(
    *,
    checkout: Checkout,
    manager,
    requested_amount: Decimal | None,
    attempt: dict,
    allow_partial: bool = False,
) -> Decimal | None:
    """Freeze tax regime and amount for a payment attempt of a managed checkout.

    Runs before the provider is contacted. Under the checkout row lock it
    recalculates prices from primary data (forced, so no cache can hide a
    changed context), evaluates the attestation (TTL is assessed here), checks
    the requested amount against the server amount and stores a payment
    snapshot bound to the attempt. Returns the server amount to request, or
    ``None`` for checkouts outside the workflow (native behaviour).

    Raises ``B2bLockedError`` or ``B2bContextChangedError``.
    """
    now = timezone.now()
    with transaction.atomic():
        # The row lock comes first, also for checkouts outside the workflow: an
        # attestation committed after an unlocked "not managed" read would
        # otherwise let this attempt start without a snapshot.
        locked = lock_checkout(checkout.pk)
        if locked is None:
            raise B2bContextChangedError("Checkout does not exist anymore.")
        metadata = load_metadata(locked, settings.DATABASE_CONNECTION_DEFAULT_NAME)
        if not read_state(metadata.private_metadata if metadata else None).managed:
            return None
        metadata = lock_metadata(locked)
        state = read_state(metadata.private_metadata)
        context = locked_context(locked, metadata)
        lock = state.payment_lock
        lock_active = bool(lock) and payment_lock_is_active(locked, lock, now)

        lines, _ = fetch_checkout_lines(locked)
        checkout_info = fetch_checkout_info(locked, lines, manager)
        checkout_info, fresh_lines = fetch_checkout_data(
            checkout_info, manager, lines, requestor=None, force_update=True
        ).get()
        lines = list(fresh_lines)
        fresh = checkout_info.checkout
        total_gross = quantize_price(fresh.total_gross_amount, fresh.currency)
        server_amount = quantize_price(
            _remaining_amount(fresh, total_gross), fresh.currency
        )
        if total_gross != quantize_price(checkout.total_gross_amount, fresh.currency):
            raise B2bContextChangedError(
                "Checkout total changed. Reload the summary and confirm the amount "
                "again."
            )
        if requested_amount is not None:
            requested_amount = quantize_price(requested_amount, fresh.currency)
            if allow_partial:
                if requested_amount > server_amount:
                    raise B2bContextChangedError(
                        "Requested amount exceeds the amount to pay."
                    )
            elif requested_amount != server_amount:
                raise B2bContextChangedError(
                    "Requested amount differs from the amount to pay. Reload the "
                    "summary and confirm the amount again."
                )
        amount = requested_amount if requested_amount is not None else server_amount

        goods = checkout_lines_fingerprint(lines)
        voucher_code = fresh.voucher_code or None
        if lock_active:
            assert lock is not None
            if (
                lock.get("contextHash") == context.hash
                and lock.get("totalGross") == str(total_gross)
                and lock.get("currency") == fresh.currency
                and lock.get("linesHash") == goods
                and lock.get("voucherCode") == voucher_code
            ):
                # Retry of the same quote: attach the attempt to the same snapshot.
                attempts = list(lock.get("attempts") or [])
                attempts.append({**attempt, "at": now.isoformat()})
                write_state(
                    metadata,
                    state,
                    decision=state.data.get("decision", Decision.TAXED_UNVERIFIED),
                    attestation=state.attestation,
                    payment_lock={**lock, "attempts": attempts[-10:]},
                    app=None,
                    event="payment_retry",
                    now=now,
                    bump_revision=False,
                )
                return amount
            raise B2bLockedError()

        # New attempt: live evaluation on the freshly locked data.
        evaluation = evaluate(checkout=fresh, context=context, state=state, now=now)
        write_state(
            metadata,
            state,
            decision=evaluation.decision,
            attestation=state.attestation,
            payment_lock={
                "createdAt": now.isoformat(),
                "contextHash": context.hash,
                "revision": state.revision,
                "decision": evaluation.decision,
                "exempt": evaluation.effective_exemption,
                "totalGross": str(total_gross),
                "totalNet": str(quantize_price(fresh.total_net_amount, fresh.currency)),
                "amount": str(amount),
                "currency": fresh.currency,
                "linesHash": goods,
                "voucherCode": voucher_code,
                "attempts": [{**attempt, "at": now.isoformat()}],
            },
            app=None,
            event="payment_lock",
            now=now,
            bump_revision=False,
        )
    return amount


# ---------------------------------------------------------------------------
# Releasing payment attempts (recovery when the customer wants to change data)
# ---------------------------------------------------------------------------


class ReleaseStatus:
    # Nothing can take money anymore; tax-relevant changes are allowed again.
    RELEASED = "RELEASED"
    # Cancelation of attempts without money was requested from the payment
    # app; poll b2bTaxState.paymentLocked.
    CANCELATION_REQUESTED = "CANCELATION_REQUESTED"
    # transactionInitialize is still running; retry shortly.
    ATTEMPT_BEING_CREATED = "ATTEMPT_BEING_CREATED"
    # Money is authorized, charged or pending: finish the started payment.
    PAYMENT_HAS_FUNDS = "PAYMENT_HAS_FUNDS"

    CHOICES = (
        RELEASED,
        CANCELATION_REQUESTED,
        ATTEMPT_BEING_CREATED,
        PAYMENT_HAS_FUNDS,
    )


@dataclass
class ReleaseResult:
    checkout: Checkout | None
    status: str | None = None
    error_code: str | None = None


def _create_cancel_request_events(
    items: list[TransactionItem], app: Optional["App"], now: datetime.datetime
) -> list[tuple[TransactionItem, TransactionEvent]]:
    """Create CANCEL_REQUEST events, skipping items asked very recently."""
    recently_requested = set(
        TransactionEvent.objects.filter(
            transaction_id__in=[item.pk for item in items],
            type=TransactionEventType.CANCEL_REQUEST,
            created_at__gte=now - CANCEL_REQUEST_DEBOUNCE,
        ).values_list("transaction_id", flat=True)
    )
    requests = []
    for item in items:
        if item.pk in recently_requested:
            continue
        if item.app_identifier == GIFT_CARD_PAYMENT_GATEWAY_ID:
            # Synchronous internal gateway: a pending gift card attempt cannot
            # exist after its request finished; nothing to cancel remotely.
            continue
        event = TransactionEvent.objects.create(
            transaction=item,
            type=TransactionEventType.CANCEL_REQUEST,
            amount_value=item.authorized_value,
            currency=item.currency,
            app=app,
            app_identifier=app.identifier if app else None,
            idempotency_key=str(uuid.uuid4()),
            message="MAKY B2B: cancel unpaid attempt before checkout change",
        )
        requests.append((item, event))
    return requests


def release_payment_attempts(
    *,
    checkout_pk,
    manager,
    app: Optional["App"],
    now: datetime.datetime | None = None,
) -> ReleaseResult:
    """Recovery for a started payment the customer wants to abandon.

    Attempts holding money are never touched (no automatic refunds). Attempts
    without money that may still succeed are asked to cancel through their
    payment app (``TRANSACTION_CANCELATION_REQUESTED``); the lock is released
    only when the app confirms ``CANCEL_SUCCESS``. Safe to call repeatedly.
    """
    now = now or timezone.now()
    with transaction.atomic():
        locked = lock_checkout(checkout_pk)
        if locked is None:
            return ReleaseResult(checkout=None, error_code=B2bErrorCode.NOT_FOUND)
        metadata = load_metadata(locked, settings.DATABASE_CONNECTION_DEFAULT_NAME)
        state = read_state(metadata.private_metadata if metadata else None)
        attempts = get_payment_attempts(locked.pk, state.payment_lock, now)
        if attempts.status == PaymentAttempts.FUNDS:
            return ReleaseResult(
                checkout=locked, status=ReleaseStatus.PAYMENT_HAS_FUNDS
            )
        if attempts.status == PaymentAttempts.NONE:
            if metadata is not None and state.managed and state.payment_lock:
                metadata = lock_metadata(locked)
                write_state(
                    metadata,
                    state,
                    decision=state.data.get("decision", Decision.TAXED_UNVERIFIED),
                    attestation=state.attestation,
                    payment_lock=None,
                    app=app,
                    event="payment_lock_released",
                    now=now,
                    bump_revision=False,
                )
            return ReleaseResult(checkout=locked, status=ReleaseStatus.RELEASED)
        requests = _create_cancel_request_events(attempts.unresolved, app, now)
        status = (
            ReleaseStatus.CANCELATION_REQUESTED
            if attempts.unresolved
            else ReleaseStatus.ATTEMPT_BEING_CREATED
        )
    # Outside the row lock: payload creation; delivery runs in Celery after commit.
    channel_slug = locked.channel.slug
    for item, request_event in requests:
        try:
            request_cancelation_action(
                transaction=item,
                manager=manager,
                cancel_value=item.authorized_value,
                request_event=request_event,
                channel_slug=channel_slug,
                user=None,
                app=app,
                action=TransactionAction.CANCEL,
            )
        except PaymentError:
            # Saleor already stored CANCEL_FAILURE on the request event; the
            # attempt stays unresolved (see operations: manual reconciliation).
            logger.warning(
                "MAKY B2B: cancelation request failed",
                extra={"checkout_id": str(locked.pk), "transaction_id": item.pk},
            )
    logger.info(
        "MAKY B2B payment attempts release",
        extra={"checkout_id": str(locked.pk), "status": status},
    )
    return ReleaseResult(checkout=locked, status=status)
