"""MAKY-B2B-V1 preparation of a rollback to a Core without this patch.

A Core without the patch ignores the B2B state: it would honor every native
``tax_exemption`` set by the B2B workflow and it knows nothing about payment
snapshots. Before the code is switched back, every managed checkout must
therefore be resolved and turned into an ordinary checkout.

The procedure runs with ``MAKY_B2B_VAT_EXEMPTION_MODE=drain``. In that mode no
checkout can become managed, and a payment snapshot is only ever written for a
managed checkout, so once ``inventory`` finds no managed checkout, no B2B
payment attempt can start before the code is switched.

Payment attempts are classified by ``get_payment_attempts``, the same function
that guards checkout changes, so the inventory sees exactly what the backend
treats as active or unresolved.
"""

import datetime
import logging
from collections.abc import Iterator
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .maky_b2b import (
    LEGACY_PRIVATE_KEYS,
    MODE_DRAIN,
    STATE_KEY,
    AttemptsSnapshot,
    PaymentAttempts,
    State,
    get_mode,
    get_payment_attempts,
    invalidate_prices,
    lock_checkout,
    read_state,
)
from .models import CheckoutMetadata

logger = logging.getLogger(__name__)


class RollbackNotAllowedError(Exception):
    message = (
        "Unmanaging B2B checkouts requires MAKY_B2B_VAT_EXEMPTION_MODE=drain in "
        "every Core process."
    )


class UnmanageStatus:
    # B2B state removed; the checkout is an ordinary checkout now.
    UNMANAGED = "UNMANAGED"
    # A payment attempt may still take money or holds money; left untouched.
    BLOCKED = "BLOCKED"
    # Checkout no longer exists or is not managed anymore.
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class ManagedCheckout:
    checkout_pk: object
    tax_exemption: bool
    decision: str | None
    state_verified: bool
    attempts: AttemptsSnapshot

    @property
    def blocks_rollback(self) -> bool:
        return self.attempts.status != PaymentAttempts.NONE


@dataclass(frozen=True)
class UnmanageResult:
    checkout_pk: object
    status: str
    attempts: AttemptsSnapshot | None = None


def _rollback_lock(state: State) -> dict | None:
    """Return the payment snapshot, also from a state with an invalid signature.

    The guard ignores an unverified snapshot. Here it only adds attempts to
    wait for, so reading it can never let a rollback through too early.
    """
    lock = state.data.get("paymentLock")
    return lock if isinstance(lock, dict) else None


def _managed_metadata() -> Iterator[CheckoutMetadata]:
    return (
        CheckoutMetadata.objects.filter(
            Q(private_metadata__has_key=STATE_KEY)
            | Q(private_metadata__has_any_keys=list(LEGACY_PRIVATE_KEYS))
        )
        .select_related("checkout")
        .only("checkout_id", "private_metadata", "checkout__tax_exemption")
        .order_by("checkout_id")
        .iterator(chunk_size=500)
    )


def inventory(now: datetime.datetime | None = None) -> list[ManagedCheckout]:
    """List managed checkouts and their payment attempts (read-only)."""
    now = now or timezone.now()
    result = []
    for metadata in _managed_metadata():
        state = read_state(metadata.private_metadata)
        if not state.managed:
            continue
        result.append(
            ManagedCheckout(
                checkout_pk=metadata.checkout_id,
                tax_exemption=metadata.checkout.tax_exemption,
                decision=state.data.get("decision"),
                state_verified=state.verified,
                attempts=get_payment_attempts(
                    metadata.checkout_id, _rollback_lock(state), now
                ),
            )
        )
    return result


def unmanage_checkout(
    checkout_pk, now: datetime.datetime | None = None
) -> UnmanageResult:
    """Turn a managed checkout without payment attempts into an ordinary one.

    Runs under the checkout row lock that ``transactionInitialize`` also takes,
    so a payment attempt cannot start between the check and the change. Removes
    the B2B state and revokes the native exemption the workflow had set. The
    customer's B2B input (public metadata) and orders are not touched.
    """
    if get_mode() != MODE_DRAIN:
        raise RollbackNotAllowedError()
    now = now or timezone.now()
    with transaction.atomic():
        checkout = lock_checkout(checkout_pk)
        if checkout is None:
            return UnmanageResult(checkout_pk, UnmanageStatus.SKIPPED)
        metadata = (
            CheckoutMetadata.objects.select_for_update(of=("self",))
            .filter(checkout_id=checkout.pk)
            .first()
        )
        state = read_state(metadata.private_metadata if metadata else None)
        if metadata is None or not state.managed:
            return UnmanageResult(checkout_pk, UnmanageStatus.SKIPPED)
        attempts = get_payment_attempts(checkout.pk, _rollback_lock(state), now)
        if attempts.status != PaymentAttempts.NONE:
            return UnmanageResult(checkout_pk, UnmanageStatus.BLOCKED, attempts)
        private_metadata = dict(metadata.private_metadata)
        for key in (STATE_KEY, *LEGACY_PRIVATE_KEYS):
            private_metadata.pop(key, None)
        metadata.private_metadata = private_metadata
        metadata.save(update_fields=["private_metadata"])
        if checkout.tax_exemption:
            invalidate_prices(checkout, False, now)
    logger.info(
        "MAKY B2B checkout unmanaged for rollback",
        extra={"checkout_id": str(checkout_pk)},
    )
    return UnmanageResult(checkout_pk, UnmanageStatus.UNMANAGED, attempts)


def unmanage_all(now: datetime.datetime | None = None) -> list[UnmanageResult]:
    """Unmanage every managed checkout without payment attempts.

    Each checkout is handled in its own short transaction; checkouts whose
    attempts are not resolved are reported as ``BLOCKED`` and left untouched.
    """
    if get_mode() != MODE_DRAIN:
        raise RollbackNotAllowedError()
    checkout_ids = [metadata.checkout_id for metadata in _managed_metadata()]
    return [unmanage_checkout(checkout_pk, now) for checkout_pk in checkout_ids]
