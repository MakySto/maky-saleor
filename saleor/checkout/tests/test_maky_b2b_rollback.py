import datetime
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from freezegun import freeze_time

from .. import maky_b2b, maky_b2b_rollback
from ..maky_b2b_rollback import RollbackNotAllowedError, UnmanageStatus
from ..models import CheckoutMetadata
from .fixtures.maky_b2b import (
    maky_b2b_context_version,
    maky_b2b_input,
    maky_b2b_verification,
)


def _attest(checkout, app, **verification_overrides):
    result = maky_b2b.apply_attestation(
        checkout_pk=checkout.pk,
        expected_context_version=maky_b2b_context_version(checkout),
        verification=maky_b2b_verification(**verification_overrides),
        app=app,
    )
    assert result.error_code is None
    checkout.refresh_from_db()
    return checkout


def _store_payment_attempt(checkout, idempotency_key: str, at: datetime.datetime):
    metadata = CheckoutMetadata.objects.get(checkout=checkout)
    state = metadata.private_metadata[maky_b2b.STATE_KEY]
    state["paymentLock"] = {
        "attempts": [
            {
                "type": "transaction",
                "idempotencyKey": idempotency_key,
                "at": at.isoformat(),
            }
        ]
    }
    state["sig"] = maky_b2b.sign({k: v for k, v in state.items() if k != "sig"})
    metadata.save(update_fields=["private_metadata"])


def _stored_state(checkout):
    return CheckoutMetadata.objects.get(checkout=checkout).private_metadata.get(
        maky_b2b.STATE_KEY
    )


def test_inventory_includes_snapshot_attempt_without_transaction(
    maky_b2b_checkout, app
):
    # given - the payment snapshot exists, its transaction is still being created
    checkout = _attest(maky_b2b_checkout, app)
    now = timezone.now()
    _store_payment_attempt(checkout, "key-1", now)

    # when
    managed = maky_b2b_rollback.inventory(now)

    # then
    assert len(managed) == 1
    assert managed[0].checkout_pk == checkout.pk
    assert managed[0].attempts.status == maky_b2b.PaymentAttempts.UNRESOLVED
    assert managed[0].attempts.being_created == 1
    assert managed[0].blocks_rollback is True


def test_inventory_includes_managed_checkout_without_exemption(
    maky_b2b_checkout, app, transaction_item_generator
):
    # given - managed checkout taxed after a VIES INVALID answer, payment started
    checkout = _attest(
        maky_b2b_checkout, app, status=maky_b2b.VerificationStatus.INVALID
    )
    assert checkout.tax_exemption is False
    item = transaction_item_generator(checkout_id=checkout.pk)

    # when
    managed = maky_b2b_rollback.inventory()

    # then
    assert len(managed) == 1
    assert managed[0].tax_exemption is False
    assert managed[0].decision == maky_b2b.Decision.TAXED_INVALID
    assert managed[0].attempts.status == maky_b2b.PaymentAttempts.UNRESOLVED
    assert managed[0].attempts.unresolved == [item]


def test_inventory_ignores_generic_exemption(maky_b2b_checkout):
    # given - native exemption set outside the B2B workflow
    maky_b2b_checkout.tax_exemption = True
    maky_b2b_checkout.save(update_fields=["tax_exemption"])

    # when
    managed = maky_b2b_rollback.inventory()

    # then
    assert managed == []


def test_unmanage_requires_drain_mode(maky_b2b_checkout, app, settings):
    # given
    checkout = _attest(maky_b2b_checkout, app)
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_OFF

    # when / then
    with pytest.raises(RollbackNotAllowedError):
        maky_b2b_rollback.unmanage_checkout(checkout.pk)
    checkout.refresh_from_db(fields=["tax_exemption"])
    assert checkout.tax_exemption is True
    assert _stored_state(checkout)["decision"] == maky_b2b.Decision.EXEMPT


@freeze_time("2026-11-03 10:00")
def test_unmanage_turns_checkout_into_ordinary_checkout(
    maky_b2b_checkout, app, settings
):
    # given
    checkout = _attest(maky_b2b_checkout, app)
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN

    # when
    result = maky_b2b_rollback.unmanage_checkout(checkout.pk)

    # then
    assert result.status == UnmanageStatus.UNMANAGED
    metadata = CheckoutMetadata.objects.get(checkout=checkout)
    assert maky_b2b.STATE_KEY not in metadata.private_metadata
    assert metadata.metadata == {maky_b2b.INPUT_KEY: maky_b2b_input()}
    checkout.refresh_from_db(fields=["tax_exemption", "price_expiration"])
    assert checkout.tax_exemption is False
    assert checkout.price_expiration == timezone.now()
    assert maky_b2b_rollback.inventory() == []


@pytest.mark.parametrize(
    ("attempt", "expected_status"),
    [
        ("funded", maky_b2b.PaymentAttempts.FUNDS),
        ("unresolved", maky_b2b.PaymentAttempts.UNRESOLVED),
        ("being_created", maky_b2b.PaymentAttempts.UNRESOLVED),
    ],
)
def test_unmanage_leaves_checkout_with_open_payment_attempt(
    maky_b2b_checkout,
    app,
    settings,
    transaction_item_generator,
    attempt,
    expected_status,
):
    # given
    checkout = _attest(maky_b2b_checkout, app)
    if attempt == "funded":
        transaction_item_generator(
            checkout_id=checkout.pk, authorized_value=Decimal("126.00")
        )
    elif attempt == "unresolved":
        transaction_item_generator(checkout_id=checkout.pk)
    else:
        _store_payment_attempt(checkout, "key-1", timezone.now())
    state_before = _stored_state(checkout)
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN

    # when
    result = maky_b2b_rollback.unmanage_checkout(checkout.pk)

    # then
    assert result.status == UnmanageStatus.BLOCKED
    assert result.attempts.status == expected_status
    assert _stored_state(checkout) == state_before
    checkout.refresh_from_db(fields=["tax_exemption"])
    assert checkout.tax_exemption is True


def test_attestation_in_drain_mode_does_not_manage_checkout(
    maky_b2b_checkout, app, settings
):
    # given
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN

    # when
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=maky_b2b_verification(),
        app=app,
    )

    # then
    assert result.error_code is None
    assert _stored_state(maky_b2b_checkout) is None
    maky_b2b_checkout.refresh_from_db(fields=["tax_exemption"])
    assert maky_b2b_checkout.tax_exemption is False
    assert maky_b2b_rollback.inventory() == []


def test_check_command_fails_outside_drain_mode(db, settings):
    # given
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_OFF

    # when / then
    with pytest.raises(CommandError) as error:
        call_command("maky_b2b_rollback", "check", stdout=StringIO())
    assert str(error.value) == (
        "NOT READY: mode is 'off'. Set MAKY_B2B_VAT_EXEMPTION_MODE=drain in every "
        "Core process first."
    )


def test_check_command_lists_checkout_with_open_payment_attempt(
    maky_b2b_checkout, app, settings
):
    # given
    checkout = _attest(maky_b2b_checkout, app)
    _store_payment_attempt(checkout, "key-1", timezone.now())
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN
    out = StringIO()

    # when / then
    with pytest.raises(CommandError) as error:
        call_command("maky_b2b_rollback", "check", stdout=out)
    assert str(error.value) == (
        "NOT READY: 1 managed checkout(s), 1 with payment attempts to resolve."
    )
    assert out.getvalue().splitlines() == [
        "mode=drain managed_checkouts=1",
        f"checkout={checkout.pk} tax_exemption=True "
        f"decision={maky_b2b.Decision.EXEMPT} state_signature_valid=True "
        "attempts=UNRESOLVED funded=- unresolved=- being_created=1",
    ]


def test_unmanage_command_then_check_is_ready(maky_b2b_checkout, app, settings):
    # given
    _attest(maky_b2b_checkout, app)
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN
    unmanage_out, check_out = StringIO(), StringIO()

    # when
    call_command("maky_b2b_rollback", "unmanage", stdout=unmanage_out)
    call_command("maky_b2b_rollback", "check", stdout=check_out)

    # then
    assert unmanage_out.getvalue().splitlines() == [
        f"checkout={maky_b2b_checkout.pk} {UnmanageStatus.UNMANAGED}",
        "unmanaged=1 blocked=0 skipped=0",
    ]
    assert check_out.getvalue().splitlines() == [
        "mode=drain managed_checkouts=0",
        "READY: no managed checkout is left.",
    ]
