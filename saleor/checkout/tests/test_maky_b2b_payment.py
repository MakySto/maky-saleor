import datetime
from decimal import Decimal

import pytest
from django.db import transaction
from django.utils import timezone

from ...giftcard.const import GIFT_CARD_PAYMENT_GATEWAY_ID
from ...payment import TransactionEventType
from ...plugins.manager import get_plugins_manager
from .. import maky_b2b
from ..calculations import fetch_checkout_data
from ..fetch import fetch_checkout_info, fetch_checkout_lines
from ..models import CheckoutMetadata
from .fixtures.maky_b2b import maky_b2b_context_version, maky_b2b_verification


def _lock_with_attempt(idempotency_key: str, at: datetime.datetime) -> dict:
    return {
        "attempts": [
            {
                "type": "transaction",
                "idempotencyKey": idempotency_key,
                "at": at.isoformat(),
            }
        ]
    }


def test_attempt_without_transaction_is_being_created_during_grace(
    maky_b2b_checkout, settings
):
    # given
    settings.MAKY_B2B_PAYMENT_ATTEMPT_GRACE_SECONDS = 300
    now = timezone.now()
    lock = _lock_with_attempt("key-1", now - datetime.timedelta(seconds=299))

    # when
    attempts = maky_b2b.get_payment_attempts(maky_b2b_checkout.pk, lock, now)

    # then
    assert attempts.status == maky_b2b.PaymentAttempts.UNRESOLVED
    assert attempts.being_created == 1


def test_attempt_without_transaction_after_grace_never_reached_provider(
    maky_b2b_checkout, settings
):
    # given
    settings.MAKY_B2B_PAYMENT_ATTEMPT_GRACE_SECONDS = 300
    now = timezone.now()
    lock = _lock_with_attempt("key-1", now - datetime.timedelta(seconds=300))

    # when
    attempts = maky_b2b.get_payment_attempts(maky_b2b_checkout.pk, lock, now)

    # then
    assert attempts.status == maky_b2b.PaymentAttempts.NONE
    assert attempts.being_created == 0


@pytest.mark.parametrize(
    ("app_identifier", "event_type", "expected"),
    [
        ("maky.stripe.test", None, maky_b2b.PaymentAttempts.UNRESOLVED),
        (
            "maky.stripe.test",
            TransactionEventType.CHARGE_FAILURE,
            maky_b2b.PaymentAttempts.UNRESOLVED,
        ),
        (
            "maky.stripe.test",
            TransactionEventType.AUTHORIZATION_FAILURE,
            maky_b2b.PaymentAttempts.UNRESOLVED,
        ),
        (
            "maky.stripe.test",
            TransactionEventType.CANCEL_SUCCESS,
            maky_b2b.PaymentAttempts.NONE,
        ),
        (
            "maky.stripe.test",
            TransactionEventType.REFUND_SUCCESS,
            maky_b2b.PaymentAttempts.NONE,
        ),
        (
            GIFT_CARD_PAYMENT_GATEWAY_ID,
            TransactionEventType.CHARGE_FAILURE,
            maky_b2b.PaymentAttempts.NONE,
        ),
    ],
)
def test_attempt_without_money_is_final_only_after_confirmed_event(
    maky_b2b_checkout, transaction_item_generator, app_identifier, event_type, expected
):
    # given
    item = transaction_item_generator(
        checkout_id=maky_b2b_checkout.pk, app_identifier=app_identifier
    )
    if event_type:
        item.events.create(
            type=event_type, amount_value=Decimal(0), currency=item.currency
        )

    # when
    attempts = maky_b2b.get_payment_attempts(
        maky_b2b_checkout.pk, None, timezone.now() + datetime.timedelta(days=30)
    )

    # then
    assert attempts.status == expected


def test_attempt_with_money_is_funded_even_after_cancel_event(
    maky_b2b_checkout, transaction_item_generator
):
    # given
    item = transaction_item_generator(
        checkout_id=maky_b2b_checkout.pk, authorized_value=Decimal("126.00")
    )
    item.events.create(
        type=TransactionEventType.CANCEL_SUCCESS,
        amount_value=Decimal(0),
        currency=item.currency,
    )

    # when
    attempts = maky_b2b.get_payment_attempts(maky_b2b_checkout.pk, None, timezone.now())

    # then
    assert attempts.status == maky_b2b.PaymentAttempts.FUNDS
    assert attempts.funded == [item]


def test_guard_rejects_change_while_snapshot_attempt_is_unresolved(
    maky_b2b_checkout, app
):
    # given - managed checkout with a snapshot whose transaction is being created
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=maky_b2b_verification(),
        app=app,
    )
    assert result.error_code is None
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    state = metadata.private_metadata[maky_b2b.STATE_KEY]
    state["paymentLock"] = _lock_with_attempt("key-1", timezone.now())
    state["sig"] = maky_b2b.sign({k: v for k, v in state.items() if k != "sig"})
    metadata.save(update_fields=["private_metadata"])

    # when / then
    with transaction.atomic(), pytest.raises(maky_b2b.B2bLockedError):
        maky_b2b.guard_context_change(maky_b2b_checkout.pk)


@pytest.mark.parametrize(
    ("lock_changes", "mismatch"),
    [
        ({"linesHash": "other-goods-same-total"}, "lines"),
        ({"voucherCode": "SUMMER"}, "voucher"),
        ({"currency": "EUR"}, "currency"),
        ({"totalGross": "113.40"}, "total"),
        ({"contextHash": "other-context"}, "tax context"),
    ],
)
def test_order_must_match_confirmed_offer(
    maky_b2b_checkout, app, transaction_item_generator, lock_changes, mismatch
):
    # given - exempt checkout with an active snapshot of the current offer
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=maky_b2b_verification(),
        app=app,
    )
    assert result.error_code is None
    maky_b2b_checkout.refresh_from_db()
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(maky_b2b_checkout)
    checkout_info, _ = fetch_checkout_data(
        fetch_checkout_info(maky_b2b_checkout, lines, manager),
        manager,
        lines,
        requestor=None,
        force_update=True,
    ).get()
    assert checkout_info.checkout.total_gross_amount == Decimal("126.00")
    evaluation = maky_b2b.evaluate_checkout_info(checkout_info)
    goods = maky_b2b.checkout_lines_fingerprint(lines)
    item = transaction_item_generator(
        checkout_id=maky_b2b_checkout.pk, charged_value=Decimal("126.00")
    )
    item.idempotency_key = "key-1"
    item.save(update_fields=["idempotency_key"])
    lock = {
        "createdAt": timezone.now().isoformat(),
        "contextHash": evaluation.context.hash,
        "decision": maky_b2b.Decision.EXEMPT,
        "exempt": True,
        "totalGross": "126.00",
        "currency": maky_b2b_checkout.currency,
        "linesHash": goods,
        "voucherCode": None,
        "attempts": [
            {"type": "transaction", "idempotencyKey": "key-1", "at": "2026-11-02"}
        ],
    }
    lock.update(lock_changes)
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    state = metadata.private_metadata[maky_b2b.STATE_KEY]
    state["paymentLock"] = lock
    state["sig"] = maky_b2b.sign({k: v for k, v in state.items() if k != "sig"})
    metadata.save(update_fields=["private_metadata"])

    # when / then
    with pytest.raises(maky_b2b.B2bContextChangedError) as error:
        maky_b2b.validate_and_build_order_snapshot(
            checkout_info,
            metadata,
            order_total_gross=checkout_info.checkout.total_gross_amount,
            lines_hash=goods,
        )
    assert error.value.message == (
        "Checkout differs from the offer confirmed by the started payment "
        f"({mismatch})."
    )


def test_lines_fingerprint_ignores_line_split_but_not_swap():
    # given
    split = [("1", 1), ("1", 2), ("2", 1)]
    merged = [("2", 1), ("1", 3)]
    swapped = [("3", 3), ("2", 1)]

    # when / then
    assert maky_b2b.lines_fingerprint(split) == maky_b2b.lines_fingerprint(merged)
    assert maky_b2b.lines_fingerprint(swapped) != maky_b2b.lines_fingerprint(merged)


def test_order_rejects_money_not_started_for_confirmed_offer(
    maky_b2b_checkout, app, transaction_item_generator
):
    # given - money from an attempt started before the checkout became managed
    item = transaction_item_generator(
        checkout_id=maky_b2b_checkout.pk, charged_value=Decimal("154.98")
    )
    item.idempotency_key = "started-before-attestation"
    item.save(update_fields=["idempotency_key"])
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    state = {
        "schema": maky_b2b.STATE_SCHEMA,
        "policyVersion": maky_b2b.POLICY_VERSION,
        "managed": True,
        "revision": 1,
        "decision": maky_b2b.Decision.EXEMPT,
        "attestation": None,
        "paymentLock": None,
        "updatedAt": timezone.now().isoformat(),
        "appId": app.pk,
        "events": [],
    }
    state["sig"] = maky_b2b.sign(state)
    metadata.private_metadata = {maky_b2b.STATE_KEY: state}
    metadata.save(update_fields=["private_metadata"])
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(maky_b2b_checkout)
    checkout_info = fetch_checkout_info(maky_b2b_checkout, lines, manager)

    # when / then
    with pytest.raises(maky_b2b.B2bContextChangedError) as error:
        maky_b2b.validate_and_build_order_snapshot(
            checkout_info,
            metadata,
            order_total_gross=maky_b2b_checkout.total_gross_amount,
            lines_hash=maky_b2b.checkout_lines_fingerprint(lines),
        )
    assert error.value.message == (
        "A payment of this checkout was not started for the confirmed offer."
    )
