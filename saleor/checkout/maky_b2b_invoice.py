"""MAKY-B2B-V1: readable invoice data of a B2B order for the staff.

The signed order snapshot ``maky.b2b.order.v1`` is one nested object, which the
Dashboard cannot show in a readable way. The rows built here are derived from
it and stored next to it in the order's private metadata, one value per row, so
the staff can read them and copy them to the manual invoice.

* The snapshot stays the only source of truth and is never modified.
* The keys share the protected ``maky.b2b.`` namespace, so generic metadata
  mutations cannot change or delete them.
* The keys are numbered: the API returns metadata sorted by key, so the
  Dashboard shows the rows in reading order.
* Nothing is guessed: a value missing in the snapshot is shown as missing.
"""

import datetime
import logging
from collections.abc import Iterator
from dataclasses import dataclass

from django.db import transaction

from ..order.lock_objects import order_qs_select_for_update
from ..order.models import Order
from .maky_b2b import (
    ORDER_SNAPSHOT_KEY,
    Decision,
    Identity,
    is_signature_valid,
)

logger = logging.getLogger(__name__)

INVOICE_KEY_PREFIX = "maky.b2b.faktura."
KEY_TAX_REGIME = f"{INVOICE_KEY_PREFIX}01 Daňový režim"
KEY_VAT_ID = f"{INVOICE_KEY_PREFIX}02 IČ DPH odberateľa"
KEY_VIES = f"{INVOICE_KEY_PREFIX}03 Overenie VIES"
KEY_COMPANY_NAME = f"{INVOICE_KEY_PREFIX}04 Obchodné meno"
KEY_BILLING_ADDRESS = f"{INVOICE_KEY_PREFIX}05 Fakturačná adresa"
KEY_SHIPPING_ADDRESS = f"{INVOICE_KEY_PREFIX}06 Dodacia adresa"
KEY_REGISTRATION_ID = f"{INVOICE_KEY_PREFIX}07 IČO / registračné číslo"
KEY_TAX_ID = f"{INVOICE_KEY_PREFIX}08 DIČ / daňové číslo"
KEY_RECORD = f"{INVOICE_KEY_PREFIX}09 Záznam"

NOT_PROVIDED = "— (nezadané)"

TAX_REASON_EXEMPT = "EU_ICS_43"
TAX_REASON_EXPORT = "OUT_OF_SCOPE_EXPORT"

EXEMPT_REGIME = (
    "Oslobodené od DPH – dodanie tovaru do iného členského štátu EÚ, "
    "§ 43 ods. 1 písm. a) zákona č. 222/2004 Z. z."
)
EXPORT_REGIME = (
    "Doručenie mimo EÚ – B2B oslobodenie sa neuplatňuje, DPH podľa nastavenia kanála"
)
TAXED_REASONS = {
    Decision.TAXED_NOT_COMPANY: "nákup nebol označený ako firemný",
    Decision.TAXED_NO_VAT_ID: "firma nezadala IČ DPH",
    Decision.TAXED_SK_VAT: "slovenské IČ DPH",
    Decision.TAXED_DOMESTIC_DELIVERY: "doručenie alebo odber na Slovensku",
    Decision.TAXED_COUNTRY_MISMATCH: "krajina IČ DPH sa líši od krajiny doručenia",
    Decision.TAXED_UNVERIFIED: "IČ DPH nebolo overené",
    Decision.TAXED_INVALID: "VIES označil IČ DPH za neplatné",
    Decision.TAXED_UNAVAILABLE: "VIES nebol dostupný",
    Decision.TAXED_IDENTITY_REVIEW: "názov firmy vo VIES sa nezhoduje so zadaným",
    Decision.TAXED_STALE: "overenie vypršalo alebo nesedí na údaje objednávky",
    Decision.TAXED_DISABLED: "B2B oslobodenie bolo na backende vypnuté",
}
IDENTITY_LABELS = {
    Identity.MATCH: "zhoduje sa s obchodným menom",
    Identity.MISMATCH: "NEZHODUJE sa s obchodným menom",
    Identity.NOT_PROVIDED: "VIES názov neuvádza",
}


def _text(value, missing: str = NOT_PROVIDED) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return missing


def _timestamp(value) -> str:
    if not isinstance(value, str):
        return NOT_PROVIDED
    try:
        moment = datetime.datetime.fromisoformat(value)
    except ValueError:
        return value
    if moment.tzinfo is None:
        return value
    return moment.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")


def _address(address) -> str:
    if not isinstance(address, dict):
        return NOT_PROVIDED

    def field(name: str) -> str:
        value = address.get(name)
        return value.strip() if isinstance(value, str) else ""

    person = " ".join(
        part for part in (field("first_name"), field("last_name")) if part
    )
    city = " ".join(part for part in (field("postal_code"), field("city")) if part)
    parts = (
        field("company_name"),
        person,
        field("street_address_1"),
        field("street_address_2"),
        field("city_area"),
        city,
        field("country_area"),
        field("country"),
    )
    return ", ".join(part for part in parts if part) or NOT_PROVIDED


def _tax_regime(snapshot: dict) -> str:
    reason = snapshot.get("taxReason")
    decision = snapshot.get("decision")
    if reason == TAX_REASON_EXEMPT:
        return f"{EXEMPT_REGIME} ({reason})"
    if reason == TAX_REASON_EXPORT:
        return f"{EXPORT_REGIME} ({reason})"
    why = TAXED_REASONS.get(str(decision), "dôvod neuvedený")
    return f"S DPH – {why} ({decision})"


def _vies(attestation) -> str:
    if not isinstance(attestation, dict):
        return "Neoverené – objednávka nemá záznam z VIES"
    parts = [
        f"{_text(attestation.get('status'))}, overené "
        f"{_timestamp(attestation.get('checkedAt'))}",
        f"IČ DPH {_text(attestation.get('vatId'))}",
        "číslo konzultácie "
        + _text(attestation.get("requestIdentifier"), "— (VIES nevrátil)"),
    ]
    name = _text(attestation.get("name"), "")
    if name:
        identity = IDENTITY_LABELS.get(str(attestation.get("identity")))
        parts.append(f"názov vo VIES {name}" + (f" ({identity})" if identity else ""))
    return "; ".join(parts)


def _shipping(snapshot: dict) -> str:
    delivery = snapshot.get("delivery")
    if isinstance(delivery, dict) and delivery.get("kind") == "pickup":
        return (
            f"Osobný odber, výdajné miesto {_text(delivery.get('pickupId'))}, "
            f"krajina {_text(delivery.get('country'))}"
        )
    return _address(snapshot.get("shippingAddress"))


def _record(snapshot: dict) -> str:
    signature = (
        "podpis platný"
        if is_signature_valid(snapshot)
        else "PODPIS NESEDÍ – údaje nepoužívaj bez kontroly"
    )
    return (
        f"{signature}; zapísané {_timestamp(snapshot.get('recordedAt'))}; "
        f"rozhodnutie {_text(snapshot.get('decision'))}; "
        f"{_text(snapshot.get('policyVersion'))}; "
        f"zdroj {ORDER_SNAPSHOT_KEY}"
    )


def invoice_fields(snapshot: dict) -> dict[str, str]:
    """Return the readable rows for an order snapshot ``maky.b2b.order.v1``."""
    customer_input = snapshot.get("input")
    if not isinstance(customer_input, dict):
        customer_input = {}
    return {
        KEY_TAX_REGIME: _tax_regime(snapshot),
        KEY_VAT_ID: _text(customer_input.get("vatId")),
        KEY_VIES: _vies(snapshot.get("attestation")),
        KEY_COMPANY_NAME: _text(snapshot.get("companyName")),
        KEY_BILLING_ADDRESS: _address(snapshot.get("billingAddress")),
        KEY_SHIPPING_ADDRESS: _shipping(snapshot),
        KEY_REGISTRATION_ID: _text(customer_input.get("registrationId")),
        KEY_TAX_ID: _text(customer_input.get("taxId")),
        KEY_RECORD: _record(snapshot),
    }


def with_invoice_fields(private_metadata: dict) -> dict:
    """Return private metadata with fresh rows for its snapshot.

    Rows from an earlier version of this module are replaced; metadata without
    a snapshot is returned without any rows.
    """
    result = {
        key: value
        for key, value in private_metadata.items()
        if not key.startswith(INVOICE_KEY_PREFIX)
    }
    snapshot = private_metadata.get(ORDER_SNAPSHOT_KEY)
    if isinstance(snapshot, dict):
        result.update(invoice_fields(snapshot))
    return result


class RowsStatus:
    # The rows match the snapshot.
    CURRENT = "CURRENT"
    # The order has no rows yet (created before this module existed).
    MISSING = "MISSING"
    # The rows differ from what this version builds from the snapshot.
    OUTDATED = "OUTDATED"
    # The snapshot is not an object; nothing can be derived, left untouched.
    BROKEN_SNAPSHOT = "BROKEN_SNAPSHOT"


@dataclass(frozen=True)
class OrderRows:
    number: int
    decision: str | None
    status: str
    updated: bool


def _rows_status(private_metadata: dict) -> str:
    snapshot = private_metadata.get(ORDER_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        return RowsStatus.BROKEN_SNAPSHOT
    current = {
        key: value
        for key, value in private_metadata.items()
        if key.startswith(INVOICE_KEY_PREFIX)
    }
    if not current:
        return RowsStatus.MISSING
    if current != invoice_fields(snapshot):
        return RowsStatus.OUTDATED
    return RowsStatus.CURRENT


def _snapshot_orders():
    return Order.objects.filter(private_metadata__has_key=ORDER_SNAPSHOT_KEY)


def _batches(batch_size: int) -> Iterator[list]:
    last_pk = None
    while True:
        qs = _snapshot_orders().order_by("pk")
        if last_pk is not None:
            qs = qs.filter(pk__gt=last_pk)
        pks = list(qs.values_list("pk", flat=True)[:batch_size])
        if not pks:
            return
        yield pks
        last_pk = pks[-1]


def refresh_order_rows(*, apply: bool, batch_size: int = 100) -> list[OrderRows]:
    """List orders with a snapshot and, with ``apply``, write missing rows.

    Only the ``maky.b2b.faktura.`` rows change; the snapshot and every other
    metadata key stay as they are. Each batch is one short transaction holding
    the order row locks, so a concurrent metadata write is not lost.
    """
    result = []
    for pks in _batches(batch_size):
        with transaction.atomic():
            qs = order_qs_select_for_update() if apply else Order.objects
            orders = list(
                qs.filter(pk__in=pks).only("pk", "number", "private_metadata")
            )
            changed = []
            for order in orders:
                metadata = order.private_metadata or {}
                status = _rows_status(metadata)
                update = apply and status in (RowsStatus.MISSING, RowsStatus.OUTDATED)
                if update:
                    order.private_metadata = with_invoice_fields(metadata)
                    changed.append(order)
                snapshot = metadata.get(ORDER_SNAPSHOT_KEY)
                result.append(
                    OrderRows(
                        number=order.number,
                        decision=(
                            snapshot.get("decision")
                            if isinstance(snapshot, dict)
                            else None
                        ),
                        status=status,
                        updated=update,
                    )
                )
            if changed:
                Order.objects.bulk_update(changed, ["private_metadata"])
    if apply:
        logger.info(
            "MAKY B2B invoice rows refreshed",
            extra={"orders_updated": sum(row.updated for row in result)},
        )
    return sorted(result, key=lambda row: row.number)
