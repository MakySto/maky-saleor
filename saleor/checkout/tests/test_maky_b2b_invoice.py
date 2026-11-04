from io import StringIO

import pytest
from django.core.management import call_command

from .. import maky_b2b, maky_b2b_invoice
from ..maky_b2b_invoice import (
    KEY_BILLING_ADDRESS,
    KEY_COMPANY_NAME,
    KEY_RECORD,
    KEY_REGISTRATION_ID,
    KEY_SHIPPING_ADDRESS,
    KEY_TAX_ID,
    KEY_TAX_REGIME,
    KEY_VAT_ID,
    KEY_VIES,
    RowsStatus,
)

VAT_ID = "HU24224471"
COMPANY = "PLASTIC PLATE KFT."
REQUEST_IDENTIFIER = "WAPIAAAAZ1example"


def _address(**overrides) -> dict:
    return {
        "company_name": COMPANY,
        "first_name": "Balázs",
        "last_name": "Bertus",
        "street_address_1": "Alkotmány utca 41.",
        "street_address_2": "",
        "city": "Komló",
        "city_area": "",
        "postal_code": "7300",
        "country_area": "",
        "country": "HU",
        **overrides,
    }


def _snapshot(**overrides) -> dict:
    snapshot = {
        "schema": 1,
        "policyVersion": maky_b2b.POLICY_VERSION,
        "recordedAt": "2026-11-04T07:19:02.123456+00:00",
        "managed": True,
        "decision": maky_b2b.Decision.EXEMPT,
        "effectiveTaxExemption": True,
        "taxReason": "EU_ICS_43",
        "frozenPaymentSnapshot": True,
        "contextHash": "hash",
        "stateRevision": 3,
        "input": {
            "isBusiness": True,
            "vatId": VAT_ID,
            "registrationId": "",
            "taxId": "",
        },
        "companyName": COMPANY,
        "billingAddress": _address(),
        "shippingAddress": _address(
            street_address_1="Hegyelő utca 10/B",
            city="Hosszúhetény",
            postal_code="7694",
        ),
        "delivery": {"kind": "address", "country": "HU", "pickupId": None},
        "attestation": {
            "source": "VIES",
            "vatId": VAT_ID,
            "status": maky_b2b.VerificationStatus.VALID,
            "checkedAt": "2026-11-04T08:18:30+01:00",
            "requestIdentifier": REQUEST_IDENTIFIER,
            "name": COMPANY,
            "identity": maky_b2b.Identity.MATCH,
        },
        "paymentLock": None,
        "checkoutTotal": {"net": "47552.04", "gross": "47552.04", "currency": "HUF"},
        **overrides,
    }
    snapshot["sig"] = maky_b2b.sign(snapshot)
    return snapshot


def test_invoice_fields_for_exempt_order():
    # given
    snapshot = _snapshot()

    # when
    fields = maky_b2b_invoice.invoice_fields(snapshot)

    # then
    assert fields == {
        KEY_TAX_REGIME: (
            "Oslobodené od DPH – dodanie tovaru do iného členského štátu EÚ, "
            "§ 43 ods. 1 písm. a) zákona č. 222/2004 Z. z. (EU_ICS_43)"
        ),
        KEY_VAT_ID: VAT_ID,
        KEY_VIES: (
            f"VALID, overené 2026-11-04 07:18 UTC; IČ DPH {VAT_ID}; "
            f"číslo konzultácie {REQUEST_IDENTIFIER}; "
            f"názov vo VIES {COMPANY} (zhoduje sa s obchodným menom)"
        ),
        KEY_COMPANY_NAME: COMPANY,
        KEY_BILLING_ADDRESS: (
            f"{COMPANY}, Balázs Bertus, Alkotmány utca 41., 7300 Komló, HU"
        ),
        KEY_SHIPPING_ADDRESS: (
            f"{COMPANY}, Balázs Bertus, Hegyelő utca 10/B, 7694 Hosszúhetény, HU"
        ),
        KEY_REGISTRATION_ID: maky_b2b_invoice.NOT_PROVIDED,
        KEY_TAX_ID: maky_b2b_invoice.NOT_PROVIDED,
        KEY_RECORD: (
            "podpis platný; zapísané 2026-11-04 07:19 UTC; rozhodnutie EXEMPT; "
            f"{maky_b2b.POLICY_VERSION}; zdroj {maky_b2b.ORDER_SNAPSHOT_KEY}"
        ),
    }
    assert list(fields) == sorted(fields)


def test_invoice_fields_for_taxed_order_without_verification():
    # given - business customer with pickup in SK and no VIES check
    snapshot = _snapshot(
        decision=maky_b2b.Decision.TAXED_DOMESTIC_DELIVERY,
        effectiveTaxExemption=False,
        taxReason="DOMESTIC_VAT",
        input={
            "isBusiness": True,
            "vatId": "",
            "registrationId": "12345678",
            "taxId": "2020123456",
        },
        delivery={"kind": "pickup", "country": "SK", "pickupId": "BA-01"},
        attestation=None,
    )

    # when
    fields = maky_b2b_invoice.invoice_fields(snapshot)

    # then
    assert fields[KEY_TAX_REGIME] == (
        "S DPH – doručenie alebo odber na Slovensku (TAXED_DOMESTIC_DELIVERY)"
    )
    assert fields[KEY_VAT_ID] == maky_b2b_invoice.NOT_PROVIDED
    assert fields[KEY_VIES] == "Neoverené – objednávka nemá záznam z VIES"
    assert fields[KEY_SHIPPING_ADDRESS] == (
        "Osobný odber, výdajné miesto BA-01, krajina SK"
    )
    assert fields[KEY_REGISTRATION_ID] == "12345678"
    assert fields[KEY_TAX_ID] == "2020123456"


def test_invoice_fields_without_request_identifier_and_vies_name():
    # given
    snapshot = _snapshot()
    attestation = {
        **snapshot["attestation"],
        "requestIdentifier": None,
        "name": None,
        "identity": maky_b2b.Identity.NOT_PROVIDED,
    }
    snapshot = _snapshot(attestation=attestation)

    # when
    fields = maky_b2b_invoice.invoice_fields(snapshot)

    # then
    assert fields[KEY_VIES] == (
        f"VALID, overené 2026-11-04 07:18 UTC; IČ DPH {VAT_ID}; "
        "číslo konzultácie — (VIES nevrátil)"
    )


def test_invoice_fields_flag_tampered_snapshot():
    # given
    snapshot = {**_snapshot(), "taxReason": "EU_ICS_43", "decision": "EXEMPT"}
    snapshot["input"] = {**snapshot["input"], "vatId": "HU99999999"}

    # when
    fields = maky_b2b_invoice.invoice_fields(snapshot)

    # then
    assert fields[KEY_RECORD].startswith(
        "PODPIS NESEDÍ – údaje nepoužívaj bez kontroly; "
    )


@pytest.fixture
def order_with_b2b_snapshot(order):
    order.private_metadata = {
        "other": "kept",
        maky_b2b.ORDER_SNAPSHOT_KEY: _snapshot(),
    }
    order.save(update_fields=["private_metadata"])
    return order


def test_command_dry_run_lists_order_without_writing(order_with_b2b_snapshot):
    # given
    metadata_before = dict(order_with_b2b_snapshot.private_metadata)
    out = StringIO()

    # when
    call_command("maky_b2b_invoice_fields", stdout=out)

    # then
    assert out.getvalue().splitlines() == [
        f"order={order_with_b2b_snapshot.number} decision=EXEMPT "
        f"rows={RowsStatus.MISSING}",
        "mode=dry-run orders=1 pending=1 updated=0 broken=0",
    ]
    order_with_b2b_snapshot.refresh_from_db(fields=["private_metadata"])
    assert order_with_b2b_snapshot.private_metadata == metadata_before


def test_command_apply_adds_rows_and_keeps_snapshot(order_with_b2b_snapshot):
    # given
    snapshot = _snapshot()
    out = StringIO()

    # when
    call_command("maky_b2b_invoice_fields", "--apply", stdout=out)

    # then
    assert out.getvalue().splitlines() == [
        f"order={order_with_b2b_snapshot.number} decision=EXEMPT "
        f"rows={RowsStatus.MISSING} updated",
        "mode=apply orders=1 pending=1 updated=1 broken=0",
    ]
    order_with_b2b_snapshot.refresh_from_db(fields=["private_metadata"])
    assert order_with_b2b_snapshot.private_metadata == {
        "other": "kept",
        maky_b2b.ORDER_SNAPSHOT_KEY: snapshot,
        **maky_b2b_invoice.invoice_fields(snapshot),
    }


def test_command_apply_replaces_outdated_rows(order_with_b2b_snapshot):
    # given
    stale_key = f"{maky_b2b_invoice.INVOICE_KEY_PREFIX}99 Old row"
    order_with_b2b_snapshot.private_metadata = {
        **order_with_b2b_snapshot.private_metadata,
        KEY_VAT_ID: "HU00000000",
        stale_key: "old",
    }
    order_with_b2b_snapshot.save(update_fields=["private_metadata"])

    # when
    call_command("maky_b2b_invoice_fields", "--apply", stdout=StringIO())
    out = StringIO()
    call_command("maky_b2b_invoice_fields", stdout=out)

    # then
    order_with_b2b_snapshot.refresh_from_db(fields=["private_metadata"])
    assert stale_key not in order_with_b2b_snapshot.private_metadata
    assert order_with_b2b_snapshot.private_metadata[KEY_VAT_ID] == VAT_ID
    assert out.getvalue().splitlines() == [
        f"order={order_with_b2b_snapshot.number} decision=EXEMPT "
        f"rows={RowsStatus.CURRENT}",
        "mode=dry-run orders=1 pending=0 updated=0 broken=0",
    ]


def test_command_leaves_broken_snapshot_untouched(order):
    # given
    order.private_metadata = {maky_b2b.ORDER_SNAPSHOT_KEY: "not an object"}
    order.save(update_fields=["private_metadata"])
    out = StringIO()

    # when
    call_command("maky_b2b_invoice_fields", "--apply", stdout=out)

    # then
    assert out.getvalue().splitlines() == [
        f"order={order.number} decision=- rows={RowsStatus.BROKEN_SNAPSHOT}",
        "mode=apply orders=1 pending=0 updated=0 broken=1",
    ]
    order.refresh_from_db(fields=["private_metadata"])
    assert order.private_metadata == {maky_b2b.ORDER_SNAPSHOT_KEY: "not an object"}


def test_command_ignores_orders_without_snapshot(order):
    # given
    order.private_metadata = {"other": "kept"}
    order.save(update_fields=["private_metadata"])
    out = StringIO()

    # when
    call_command("maky_b2b_invoice_fields", "--apply", stdout=out)

    # then
    assert out.getvalue().splitlines() == [
        "mode=apply orders=0 pending=0 updated=0 broken=0"
    ]
    order.refresh_from_db(fields=["private_metadata"])
    assert order.private_metadata == {"other": "kept"}
