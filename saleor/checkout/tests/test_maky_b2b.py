import datetime
import json
from decimal import Decimal

import pytest
from django.utils import timezone
from freezegun import freeze_time

from ...plugins.manager import get_plugins_manager
from .. import maky_b2b
from ..calculations import fetch_checkout_data
from ..fetch import fetch_checkout_info, fetch_checkout_lines
from ..models import CheckoutMetadata
from .fixtures.maky_b2b import (
    MAKY_B2B_COMPANY,
    maky_b2b_address,
    maky_b2b_context_version,
    maky_b2b_input,
    maky_b2b_verification,
)


def _priced(checkout):
    checkout.refresh_from_db()
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, manager)
    checkout_info, _ = fetch_checkout_data(
        checkout_info, manager, lines, requestor=None
    ).get()
    return checkout_info


def _evaluate(checkout):
    checkout.refresh_from_db()
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, manager)
    return maky_b2b.evaluate_checkout_info(checkout_info)


def _attest(checkout, app, **verification):
    result = maky_b2b.apply_attestation(
        checkout_pk=checkout.pk,
        expected_context_version=maky_b2b_context_version(checkout),
        verification=maky_b2b_verification(**verification),
        app=app,
    )
    checkout.refresh_from_db()
    return result


def _set_input(checkout, **overrides):
    metadata = CheckoutMetadata.objects.get(checkout=checkout)
    metadata.metadata[maky_b2b.INPUT_KEY] = maky_b2b_input(**overrides)
    metadata.save(update_fields=["metadata"])


def _state(checkout):
    metadata = CheckoutMetadata.objects.get(checkout=checkout)
    return maky_b2b.read_state(metadata.private_metadata)


# --- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("de 123.456-789", "DE123456789"),
        (" el 123456789 ", "EL123456789"),
        (123456789, ""),
        (None, ""),
    ],
)
def test_normalize_vat_id(raw, expected):
    assert maky_b2b.normalize_vat_id(raw) == expected


def test_vat_id_country_maps_greece_and_rejects_unknown_prefix():
    assert maky_b2b.vat_id_country("EL123456789") == "GR"
    assert maky_b2b.vat_id_country("DE123456789") == "DE"
    assert maky_b2b.vat_id_country("XI123456789") is None
    assert maky_b2b.vat_id_country("GB123456789") is None


@pytest.mark.parametrize(
    ("raw", "is_business", "malformed"),
    [
        (json.dumps({"schema": 1, "isBusiness": "true", "vatId": "DE1"}), False, False),
        (json.dumps({"schema": 1, "isBusiness": True, "vatId": "DE1"}), True, False),
        (json.dumps({"schema": 2, "isBusiness": True}), False, True),
        ("not json", False, True),
    ],
)
def test_parse_input_never_trusts_truthy_strings(raw, is_business, malformed):
    parsed = maky_b2b.parse_input(raw)
    assert parsed.is_business is is_business
    assert parsed.malformed is malformed


@pytest.mark.parametrize(
    ("vies_name", "company", "expected"),
    [
        ("---", MAKY_B2B_COMPANY, maky_b2b.Identity.NOT_PROVIDED),
        ("", MAKY_B2B_COMPANY, maky_b2b.Identity.NOT_PROVIDED),
        ("MUSTER HANDEL GMBH", MAKY_B2B_COMPANY, maky_b2b.Identity.MATCH),
        ("Müller & Söhne GmbH", "Muller Sohne", maky_b2b.Identity.MATCH),
        ("Muster Handel", "Muster Handel GmbH & Co. KG", maky_b2b.Identity.MATCH),
        ("Totally Different AG", MAKY_B2B_COMPANY, maky_b2b.Identity.MISMATCH),
        ("Muster Handel GmbH", "", maky_b2b.Identity.MISMATCH),
    ],
)
def test_compare_identity(vies_name, company, expected):
    assert maky_b2b.compare_identity(vies_name, company) == expected


def test_signature_detects_tampering():
    data = {"schema": 1, "revision": 1, "attestation": {"status": "VALID"}}
    data["sig"] = maky_b2b.sign(data)
    assert maky_b2b.is_signature_valid(data) is True
    data["attestation"]["status"] = "INVALID"
    assert maky_b2b.is_signature_valid(data) is False


def test_forged_state_is_managed_but_never_verified():
    forged = {
        "schema": 1,
        "revision": 7,
        "attestation": {"status": "VALID"},
        "sig": "0" * 64,
    }
    state = maky_b2b.read_state({maky_b2b.STATE_KEY: forged})
    assert state.managed is True
    assert state.verified is False
    assert state.attestation is None


def test_legacy_keys_mean_managed_unverified():
    state = maky_b2b.read_state({"b2b_vies_valid": "true", "b2b_vat_country": "DE"})
    assert state.managed is True
    assert state.attestation is None


# --- decision matrix ---------------------------------------------------------


def test_valid_de_vat_and_de_delivery_is_exempt_with_expected_amounts(
    maky_b2b_checkout, app
):
    # given
    taxed = _priced(maky_b2b_checkout).checkout
    assert taxed.total.gross.amount == Decimal("154.98")
    assert taxed.total.net.amount == Decimal("126.00")

    # when
    result = _attest(maky_b2b_checkout, app)

    # then
    assert result.error_code is None
    checkout = _priced(maky_b2b_checkout).checkout
    assert checkout.subtotal.gross.amount == Decimal("121.13")
    assert checkout.shipping_price.gross.amount == Decimal("4.87")
    assert checkout.total.gross.amount == Decimal("126.00")
    assert checkout.total.net.amount == Decimal("126.00")
    assert checkout.tax_exemption is True
    evaluation = _evaluate(maky_b2b_checkout)
    assert evaluation.decision == maky_b2b.Decision.EXEMPT
    assert evaluation.effective_exemption is True


def test_sk_company_with_sk_delivery_pays_vat(maky_b2b_checkout, app):
    # given
    _set_input(maky_b2b_checkout, vatId="SK2020123456")
    maky_b2b_checkout.billing_address = maky_b2b_address("SK", "Firma s.r.o.")
    maky_b2b_checkout.shipping_address = maky_b2b_address("SK", "Firma s.r.o.")
    maky_b2b_checkout.save()

    # when
    result = _attest(maky_b2b_checkout, app, vat_id="SK2020123456")

    # then
    assert result.error_code is None
    assert _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.TAXED_SK_VAT
    checkout = _priced(maky_b2b_checkout).checkout
    assert checkout.total.gross.amount == Decimal("154.98")
    assert checkout.tax_exemption is False


@pytest.mark.parametrize(
    ("shipping_country", "expected"),
    [
        ("SK", maky_b2b.Decision.TAXED_DOMESTIC_DELIVERY),
        ("AT", maky_b2b.Decision.TAXED_COUNTRY_MISMATCH),
        ("US", maky_b2b.Decision.OUT_OF_SCOPE),
    ],
)
def test_vat_and_delivery_country_rules(
    maky_b2b_checkout, app, shipping_country, expected
):
    # given
    maky_b2b_checkout.shipping_address = maky_b2b_address(shipping_country)
    maky_b2b_checkout.save()

    # when
    result = _attest(maky_b2b_checkout, app)

    # then
    assert result.error_code is None
    assert _evaluate(maky_b2b_checkout).decision == expected
    assert maky_b2b_checkout.tax_exemption is False


def test_out_of_scope_without_managed_state_writes_nothing(maky_b2b_checkout, app):
    # given
    maky_b2b_checkout.shipping_address = maky_b2b_address("US")
    maky_b2b_checkout.save()

    # when
    _attest(maky_b2b_checkout, app)

    # then
    assert _state(maky_b2b_checkout).managed is False


def test_pickup_is_domestic(maky_b2b_checkout, app, warehouse_for_cc):
    # given
    maky_b2b_checkout.collection_point = warehouse_for_cc
    maky_b2b_checkout.assigned_delivery = None
    maky_b2b_checkout.save()

    # when
    _attest(maky_b2b_checkout, app)

    # then
    decision = _evaluate(maky_b2b_checkout).decision
    assert decision in (
        maky_b2b.Decision.TAXED_DOMESTIC_DELIVERY,
        maky_b2b.Decision.TAXED_UNVERIFIED,
    )
    assert maky_b2b_checkout.tax_exemption is False


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"isBusiness": False}, maky_b2b.Decision.TAXED_NOT_COMPANY),
        ({"vatId": ""}, maky_b2b.Decision.TAXED_NO_VAT_ID),
    ],
)
def test_company_without_exemption_keeps_company_data(
    maky_b2b_checkout, overrides, expected
):
    # given
    _set_input(maky_b2b_checkout, **overrides)

    # when
    evaluation = _evaluate(maky_b2b_checkout)

    # then
    assert evaluation.decision == expected
    assert evaluation.context.company_name == MAKY_B2B_COMPANY


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (maky_b2b.VerificationStatus.INVALID, maky_b2b.Decision.TAXED_INVALID),
        (maky_b2b.VerificationStatus.UNAVAILABLE, maky_b2b.Decision.TAXED_UNAVAILABLE),
    ],
)
def test_invalid_and_unavailable_are_distinct(maky_b2b_checkout, app, status, expected):
    # when
    _attest(maky_b2b_checkout, app, status=status, request_identifier=None)

    # then
    assert _evaluate(maky_b2b_checkout).decision == expected
    assert _priced(maky_b2b_checkout).checkout.total.gross.amount == Decimal("154.98")


def test_identity_mismatch_needs_review(maky_b2b_checkout, app):
    # when
    _attest(maky_b2b_checkout, app, name="Totally Different AG")

    # then
    assert (
        _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.TAXED_IDENTITY_REVIEW
    )
    assert maky_b2b_checkout.tax_exemption is False


def test_mode_off_does_not_grant(maky_b2b_checkout, app, settings):
    # given
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = "off"

    # when
    _attest(maky_b2b_checkout, app)

    # then
    assert _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.TAXED_DISABLED
    assert maky_b2b_checkout.tax_exemption is False


def test_mode_switched_off_invalidates_unpaid_cached_exemption(
    maky_b2b_checkout, app, settings
):
    # given
    _attest(maky_b2b_checkout, app)
    assert _priced(maky_b2b_checkout).checkout.total.gross.amount == Decimal("126.00")
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.price_expiration > timezone.now()

    # when
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = "off"
    checkout = _priced(maky_b2b_checkout).checkout

    # then
    assert checkout.total.gross.amount == Decimal("154.98")
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False


@pytest.mark.parametrize(
    "change",
    ["vat_id", "company", "billing_street", "not_business"],
)
def test_fresh_price_cache_does_not_survive_context_change(
    maky_b2b_checkout, app, change
):
    # given
    _attest(maky_b2b_checkout, app)
    assert _priced(maky_b2b_checkout).checkout.total.gross.amount == Decimal("126.00")
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.price_expiration > timezone.now()

    # when - change made directly in DB, bypassing all mutations and invalidation
    if change == "vat_id":
        _set_input(maky_b2b_checkout, vatId="DE999999999")
    elif change == "company":
        maky_b2b_checkout.billing_address.company_name = "Other GmbH"
        maky_b2b_checkout.billing_address.save()
    elif change == "billing_street":
        maky_b2b_checkout.billing_address.street_address_1 = "Andere Strasse 5"
        maky_b2b_checkout.billing_address.save()
    else:
        _set_input(maky_b2b_checkout, isBusiness=False)
    maky_b2b_checkout.refresh_from_db()

    # then
    checkout = _priced(maky_b2b_checkout).checkout
    assert checkout.total.gross.amount == Decimal("154.98")
    assert _evaluate(maky_b2b_checkout).effective_exemption is False


def test_attestation_expires_after_24h_from_original_check(maky_b2b_checkout, app):
    # given
    checked_at = timezone.now() - datetime.timedelta(hours=23)
    _attest(maky_b2b_checkout, app, checked_at=checked_at)
    assert _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.EXEMPT

    # when - reusing the same cached result later must not extend validity
    with freeze_time(timezone.now() + datetime.timedelta(hours=2)):
        result = _attest(maky_b2b_checkout, app, checked_at=checked_at)
        evaluation = _evaluate(maky_b2b_checkout)
        checkout = _priced(maky_b2b_checkout).checkout

    # then
    assert result.error_code is None
    assert evaluation.decision == maky_b2b.Decision.TAXED_STALE
    assert checkout.total.gross.amount == Decimal("154.98")


def test_stale_context_rejects_late_vies_answer(maky_b2b_checkout, app):
    # given - storefront reads version, calls VIES...
    version = maky_b2b_context_version(maky_b2b_checkout)
    # ...meanwhile second tab switches to another company with the same VAT country
    maky_b2b_checkout.billing_address.company_name = "Other GmbH"
    maky_b2b_checkout.billing_address.save()

    # when - late VIES answer arrives
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=version,
        verification=maky_b2b_verification(),
        app=app,
    )

    # then
    assert result.error_code == maky_b2b.B2bErrorCode.STALE_CONTEXT
    assert _state(maky_b2b_checkout).managed is False
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False


def test_concurrent_attestation_writes_conflict_on_revision(maky_b2b_checkout, app):
    # given
    version = maky_b2b_context_version(maky_b2b_checkout)
    first = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=version,
        verification=maky_b2b_verification(),
        app=app,
    )

    # when
    second = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=version,
        verification=None,
        app=app,
    )

    # then
    assert first.error_code is None
    assert second.error_code == maky_b2b.B2bErrorCode.STALE_CONTEXT
    assert _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.EXEMPT


def test_new_revision_does_not_invalidate_own_fresh_proof(maky_b2b_checkout, app):
    # when
    _attest(maky_b2b_checkout, app)

    # then
    state = _state(maky_b2b_checkout)
    assert state.revision == 1
    assert _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.EXEMPT


def test_null_verification_revokes(maky_b2b_checkout, app):
    # given
    _attest(maky_b2b_checkout, app)

    # when
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=None,
        app=app,
    )

    # then
    assert result.error_code is None
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False
    assert _evaluate(maky_b2b_checkout).decision == maky_b2b.Decision.TAXED_UNVERIFIED
    assert _priced(maky_b2b_checkout).checkout.total.gross.amount == Decimal("154.98")


@pytest.mark.parametrize(
    ("verification", "field"),
    [
        ({"vat_id": "DE 123456789"}, "verification.vatId"),
        ({"vat_id": "DE987654321"}, "verification.vatId"),
        ({"status": "true"}, "verification.status"),
        ({"checked_at": "2026-01-01"}, "verification.checkedAt"),
        ({"request_identifier": "x" * 200}, "verification.requestIdentifier"),
    ],
)
def test_invalid_attestation_input(maky_b2b_checkout, app, verification, field):
    # when
    result = _attest(maky_b2b_checkout, app, **verification)

    # then
    assert result.error_code == maky_b2b.B2bErrorCode.INVALID_ATTESTATION
    assert result.error_field == field
    assert _state(maky_b2b_checkout).managed is False


def test_future_checked_at_is_rejected(maky_b2b_checkout, app):
    # when
    result = _attest(
        maky_b2b_checkout,
        app,
        checked_at=timezone.now() + datetime.timedelta(hours=1),
    )

    # then
    assert result.error_code == maky_b2b.B2bErrorCode.INVALID_ATTESTATION
    assert result.error_field == "verification.checkedAt"


def test_unmanaged_native_exemption_is_not_taken_over(maky_b2b_checkout, app):
    # given
    maky_b2b_checkout.tax_exemption = True
    maky_b2b_checkout.save(update_fields=["tax_exemption"])

    # when
    result = _attest(maky_b2b_checkout, app)

    # then
    assert result.error_code == maky_b2b.B2bErrorCode.UNMANAGED_EXEMPTION_CONFLICT
    assert maky_b2b_checkout.tax_exemption is True
    assert _state(maky_b2b_checkout).managed is False


def test_generic_unmanaged_exemption_keeps_native_behaviour(maky_b2b_checkout):
    # given
    maky_b2b_checkout.tax_exemption = True
    maky_b2b_checkout.save(update_fields=["tax_exemption"])

    # when
    checkout = _priced(maky_b2b_checkout).checkout

    # then
    assert checkout.total.gross.amount == Decimal("126.00")


def test_forged_private_state_does_not_grant(maky_b2b_checkout, app):
    # given - valid flow first, then tamper the stored attestation
    _attest(maky_b2b_checkout, app, status=maky_b2b.VerificationStatus.INVALID)
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    state = metadata.private_metadata[maky_b2b.STATE_KEY]
    state["attestation"]["status"] = "VALID"
    metadata.save(update_fields=["private_metadata"])
    maky_b2b_checkout.tax_exemption = True
    maky_b2b_checkout.save(update_fields=["tax_exemption"])

    # when
    checkout = _priced(maky_b2b_checkout).checkout

    # then
    assert checkout.total.gross.amount == Decimal("154.98")
    assert _evaluate(maky_b2b_checkout).effective_exemption is False


def test_b2c_checkout_price_path_has_no_extra_query(
    checkout_with_item, django_assert_num_queries
):
    # given
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout_with_item)
    checkout_info = fetch_checkout_info(checkout_with_item, lines, manager)
    assert checkout_with_item.tax_exemption is False

    # when / then
    with django_assert_num_queries(0):
        assert maky_b2b.get_effective_tax_exemption(checkout_info) is False
