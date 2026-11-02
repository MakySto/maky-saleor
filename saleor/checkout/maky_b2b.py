"""MAKY B2B intra-community supply (§ 43 SK VAT act) exemption — MAKY-B2B-V1.

This module is a MAKY fork patch. It does not calculate taxes. Saleor's native
tax calculation stays the single source of amounts; this module only decides
whether the native ``Checkout.tax_exemption`` flag may be *effective* for a
checkout managed by the MAKY B2B workflow, and keeps that decision consistent
across the price cache, payment initialization and order creation.

Storage (no new tables):

* public checkout metadata ``maky.b2b.input.v1`` — customer input only, never a
  grant by itself;
* private checkout metadata ``maky.b2b.state.v1`` — the managed marker,
  attestation, revision and payment lock. Written only by this module, signed
  with an HMAC derived from ``SECRET_KEY``; generic metadata mutations cannot
  write or delete keys in the ``maky.b2b.`` namespace;
* private order metadata ``maky.b2b.order.v1`` — immutable signed snapshot of
  what was actually used for the order (data for the manual invoice).

The decision is fail-safe: whenever anything does not match (context,
signature, time, backend mode, payment snapshot), the checkout is taxed.
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..account.models import Address
from ..core.prices import quantize_price
from ..payment import ChargeStatus, TransactionEventType
from ..payment.models import Payment, TransactionEvent, TransactionItem
from ..warehouse.models import Warehouse
from .fetch import fetch_checkout_info, fetch_checkout_lines
from .models import Checkout, CheckoutMetadata

if TYPE_CHECKING:
    from ..app.models import App
    from .fetch import CheckoutInfo

logger = logging.getLogger(__name__)

POLICY_VERSION = "MAKY-B2B-V1"
STATE_SCHEMA = 1

RESERVED_PREFIX = "maky.b2b."
INPUT_KEY = "maky.b2b.input.v1"
STATE_KEY = "maky.b2b.state.v1"
ORDER_SNAPSHOT_KEY = "maky.b2b.order.v1"
# Keys of the superseded first design (helper + allowlist). Their presence makes
# the checkout "managed but unverified" — they never grant an exemption.
LEGACY_PRIVATE_KEYS = ("b2b_vies_valid", "b2b_vat_country")

SELLER_COUNTRY = "SK"
ATTESTATION_TTL = datetime.timedelta(hours=24)
ALLOWED_CLOCK_SKEW = datetime.timedelta(minutes=5)
MIN_CHECKED_AT = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
MAX_EVENTS = 20

MODE_ON = "on"
MODE_OFF = "off"

# EU member states (ISO 3166-1 alpha-2). VAT prefix for Greece is "EL".
EU_COUNTRIES = frozenset(
    {
        "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR",
        "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
        "SE", "SI", "SK",
    }
)  # fmt: skip
VAT_PREFIX_TO_COUNTRY = {code: code for code in EU_COUNTRIES if code != "GR"}
VAT_PREFIX_TO_COUNTRY["EL"] = "GR"

VAT_ID_RE = re.compile(r"^[A-Z]{2}[A-Z0-9+*]{2,13}$")
MAX_INPUT_FIELD_LENGTH = 64
MAX_REQUEST_IDENTIFIER_LENGTH = 128
MAX_VIES_TEXT_LENGTH = 512


class Decision:
    EXEMPT = "EXEMPT"
    TAXED_NOT_COMPANY = "TAXED_NOT_COMPANY"
    TAXED_NO_VAT_ID = "TAXED_NO_VAT_ID"
    TAXED_SK_VAT = "TAXED_SK_VAT"
    TAXED_DOMESTIC_DELIVERY = "TAXED_DOMESTIC_DELIVERY"
    TAXED_COUNTRY_MISMATCH = "TAXED_COUNTRY_MISMATCH"
    TAXED_UNVERIFIED = "TAXED_UNVERIFIED"
    TAXED_INVALID = "TAXED_INVALID"
    TAXED_UNAVAILABLE = "TAXED_UNAVAILABLE"
    TAXED_IDENTITY_REVIEW = "TAXED_IDENTITY_REVIEW"
    TAXED_STALE = "TAXED_STALE"
    TAXED_DISABLED = "TAXED_DISABLED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"

    CHOICES = (
        EXEMPT,
        TAXED_NOT_COMPANY,
        TAXED_NO_VAT_ID,
        TAXED_SK_VAT,
        TAXED_DOMESTIC_DELIVERY,
        TAXED_COUNTRY_MISMATCH,
        TAXED_UNVERIFIED,
        TAXED_INVALID,
        TAXED_UNAVAILABLE,
        TAXED_IDENTITY_REVIEW,
        TAXED_STALE,
        TAXED_DISABLED,
        OUT_OF_SCOPE,
    )


class VerificationStatus:
    VALID = "VALID"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"

    CHOICES = (VALID, INVALID, UNAVAILABLE)


class Identity:
    MATCH = "MATCH"
    NOT_PROVIDED = "NOT_PROVIDED"
    MISMATCH = "MISMATCH"


class B2bErrorCode:
    """Machine codes shared by the B2B mutation and the guarded mutations."""

    STALE_CONTEXT = "stale_context"
    INVALID_ATTESTATION = "invalid_attestation"
    CHECKOUT_LOCKED = "checkout_locked"
    UNMANAGED_EXEMPTION_CONFLICT = "unmanaged_exemption_conflict"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    # codes added to existing Saleor error enums
    B2B_TAX_CONTEXT_CHANGED = "b2b_tax_context_changed"
    B2B_CHECKOUT_LOCKED = "b2b_checkout_locked"
    B2B_MANAGED_CHECKOUT = "b2b_managed_checkout"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def get_mode() -> str:
    value = str(getattr(settings, "MAKY_B2B_VAT_EXEMPTION_MODE", MODE_OFF))
    return MODE_ON if value.strip().lower() == MODE_ON else MODE_OFF


def get_payment_lock_ttl() -> datetime.timedelta:
    seconds = int(getattr(settings, "MAKY_B2B_PAYMENT_LOCK_TTL_SECONDS", 1200))
    return datetime.timedelta(seconds=max(seconds, 60))


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_vat_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s.\-_/]", "", value).upper()


def vat_id_country(vat_id: str) -> str | None:
    if not VAT_ID_RE.match(vat_id or ""):
        return None
    return VAT_PREFIX_TO_COUNTRY.get(vat_id[:2])


def _clean_str(value: Any, max_length: int = MAX_INPUT_FIELD_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_length]


@dataclass(frozen=True)
class B2bInput:
    is_business: bool
    vat_id: str
    registration_id: str
    tax_id: str
    malformed: bool = False

    def as_dict(self) -> dict:
        return {
            "isBusiness": self.is_business,
            "vatId": self.vat_id,
            "registrationId": self.registration_id,
            "taxId": self.tax_id,
        }


EMPTY_INPUT = B2bInput(False, "", "", "")


def parse_input(raw: Any) -> B2bInput:
    """Parse the public customer input. Never trusts truthy strings."""
    if raw in (None, ""):
        return EMPTY_INPUT
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except ValueError:
            return B2bInput(False, "", "", "", malformed=True)
    if not isinstance(data, dict) or data.get("schema") != 1:
        return B2bInput(False, "", "", "", malformed=True)
    return B2bInput(
        is_business=data.get("isBusiness") is True,
        vat_id=normalize_vat_id(data.get("vatId"))[:MAX_INPUT_FIELD_LENGTH],
        registration_id=_clean_str(data.get("registrationId")),
        tax_id=_clean_str(data.get("taxId")),
    )


ADDRESS_FIELDS = (
    "company_name",
    "first_name",
    "last_name",
    "street_address_1",
    "street_address_2",
    "city",
    "city_area",
    "postal_code",
    "country_area",
)


def _address_dict(address: Address | None) -> dict | None:
    if address is None:
        return None
    data = {field: getattr(address, field, "") or "" for field in ADDRESS_FIELDS}
    data["country"] = address.country.code if address.country else ""
    return data


# Legal-form tokens removed before the deterministic identity comparison.
LEGAL_FORM_TOKENS = frozenset(
    {
        "ag", "as", "a", "s", "bv", "co", "cv", "eg", "ek", "ev", "gbr", "gmbh",
        "inc", "kft", "kg", "kgaa", "ks", "ltd", "limited", "nv", "oy", "oyj",
        "ohg", "plc", "sa", "sas", "sarl", "sl", "spa", "srl", "sro", "r", "o",
        "sp", "z", "oo", "spzoo", "ug", "haftungsbeschrankt", "zrt", "nyrt",
        "bt", "ab", "aps", "se", "sc", "snc", "scs", "kda", "doo", "dd", "ad",
        "eood", "ood", "und", "and", "et", "cie", "the",
    }
)  # fmt: skip
VIES_PLACEHOLDERS = frozenset({"", "---", "--", "-", "n/a", "na", "unknown"})


def _identity_tokens(value: str) -> list[str]:
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = re.sub(r"[^0-9a-z]+", " ", text)
    return [token for token in text.split() if token not in LEGAL_FORM_TOKENS]


def compare_identity(vies_name: str | None, company_name: str | None) -> str:
    """Deterministic, conservative company-name comparison (no fuzzy matching).

    Missing/placeholder name from the registry is NOT a negative result. A
    usable name matches when, after removing diacritics, punctuation, case and
    legal-form tokens, all tokens of the shorter name occur in the longer one.
    """
    if (vies_name or "").strip().casefold() in VIES_PLACEHOLDERS:
        return Identity.NOT_PROVIDED
    vies_tokens = _identity_tokens(vies_name or "")
    company_tokens = _identity_tokens(company_name or "")
    if not vies_tokens:
        return Identity.NOT_PROVIDED
    if not company_tokens:
        return Identity.MISMATCH
    shorter, longer = sorted((vies_tokens, company_tokens), key=len)
    if set(shorter) <= set(longer):
        return Identity.MATCH
    return Identity.MISMATCH


# ---------------------------------------------------------------------------
# Signing and context
# ---------------------------------------------------------------------------


def _signing_key() -> bytes:
    return hashlib.sha256(f"maky-b2b-state:{settings.SECRET_KEY}".encode()).digest()


def _canonical(data: Any) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode()


def sign(data: dict) -> str:
    payload = {key: value for key, value in data.items() if key != "sig"}
    return hmac.new(_signing_key(), _canonical(payload), hashlib.sha256).hexdigest()


def is_signature_valid(data: Any) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("sig"), str):
        return False
    return hmac.compare_digest(sign(data), data["sig"])


@dataclass(frozen=True)
class Delivery:
    kind: str  # "address" | "pickup"
    pickup_id: str | None = None
    pickup_country: str | None = None


@dataclass(frozen=True)
class Context:
    checkout_token: str
    channel_slug: str
    currency: str
    b2b_input: B2bInput
    billing: dict | None
    shipping: dict | None
    delivery: Delivery

    @property
    def delivery_country(self) -> str | None:
        if self.delivery.kind == "pickup":
            return self.delivery.pickup_country
        if self.shipping and self.shipping.get("country"):
            return self.shipping["country"]
        return None

    @property
    def company_name(self) -> str:
        return (self.billing or {}).get("company_name", "").strip()

    def canonical(self) -> dict:
        return {
            "policy": POLICY_VERSION,
            "checkout": self.checkout_token,
            "channel": self.channel_slug,
            "currency": self.currency,
            "input": self.b2b_input.as_dict(),
            "inputMalformed": self.b2b_input.malformed,
            "billing": self.billing,
            "shipping": self.shipping,
            "delivery": {
                "kind": self.delivery.kind,
                "pickup": self.delivery.pickup_id,
            },
            "origin": SELLER_COUNTRY,
        }

    @property
    def hash(self) -> str:
        return hmac.new(
            _signing_key(), _canonical(self.canonical()), hashlib.sha256
        ).hexdigest()


def build_context(
    *,
    checkout: Checkout,
    channel_slug: str,
    public_metadata: dict | None,
    billing_address: Address | None,
    shipping_address: Address | None,
    collection_point=None,
) -> Context:
    if collection_point is not None:
        warehouse_address = getattr(collection_point, "address", None)
        delivery = Delivery(
            kind="pickup",
            pickup_id=str(collection_point.pk),
            pickup_country=(
                warehouse_address.country.code
                if warehouse_address is not None and warehouse_address.country
                else None
            ),
        )
    else:
        delivery = Delivery(kind="address")
    return Context(
        checkout_token=str(checkout.token),
        channel_slug=channel_slug,
        currency=checkout.currency,
        b2b_input=parse_input((public_metadata or {}).get(INPUT_KEY)),
        billing=_address_dict(billing_address),
        shipping=_address_dict(shipping_address),
        delivery=delivery,
    )


def context_from_checkout_info(
    checkout_info: "CheckoutInfo", metadata: CheckoutMetadata | None
) -> Context:
    return build_context(
        checkout=checkout_info.checkout,
        channel_slug=checkout_info.channel.slug,
        public_metadata=metadata.metadata if metadata else {},
        billing_address=checkout_info.billing_address,
        shipping_address=checkout_info.shipping_address,
        collection_point=checkout_info.collection_point,
    )


def make_context_version(revision: int, context_hash: str) -> str:
    raw = f"1:{revision}:{context_hash[:32]}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class State:
    managed: bool
    verified: bool  # signature valid
    revision: int
    data: dict

    @property
    def attestation(self) -> dict | None:
        if not self.verified:
            return None
        attestation = self.data.get("attestation")
        return attestation if isinstance(attestation, dict) else None

    @property
    def payment_lock(self) -> dict | None:
        if not self.verified:
            return None
        lock = self.data.get("paymentLock")
        return lock if isinstance(lock, dict) else None


def read_state(private_metadata: dict | None) -> State:
    private_metadata = private_metadata or {}
    raw = private_metadata.get(STATE_KEY)
    legacy = any(key in private_metadata for key in LEGACY_PRIVATE_KEYS)
    if raw is None:
        return State(managed=legacy, verified=False, revision=0, data={})
    if not isinstance(raw, dict):
        return State(managed=True, verified=False, revision=0, data={})
    verified = is_signature_valid(raw) and raw.get("schema") == STATE_SCHEMA
    raw_revision = raw.get("revision")
    revision = raw_revision if isinstance(raw_revision, int) else 0
    if not verified:
        logger.warning("MAKY B2B: checkout state signature is not valid.")
    return State(managed=True, verified=verified, revision=revision, data=raw)


def parse_dt(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def structural_decision(context: Context) -> str | None:
    """Return a TAXED_*/OUT_OF_SCOPE decision not depending on the attestation.

    ``None`` means the context is eligible for automatic exemption provided a
    usable attestation exists and the backend mode is on.
    """
    delivery_country = context.delivery_country
    if context.delivery.kind == "address" and delivery_country:
        if delivery_country not in EU_COUNTRIES:
            # Export (US/CA): existing regime, not assessed by this workflow.
            return Decision.OUT_OF_SCOPE
    b2b_input = context.b2b_input
    if not b2b_input.is_business:
        return Decision.TAXED_NOT_COMPANY
    if not b2b_input.vat_id:
        return Decision.TAXED_NO_VAT_ID
    if b2b_input.vat_id.startswith(SELLER_COUNTRY):
        return Decision.TAXED_SK_VAT
    vat_country = vat_id_country(b2b_input.vat_id)
    if vat_country is None:
        return Decision.TAXED_UNVERIFIED
    if context.delivery.kind == "pickup" or delivery_country == SELLER_COUNTRY:
        return Decision.TAXED_DOMESTIC_DELIVERY
    if not delivery_country or not context.company_name or not context.billing:
        return Decision.TAXED_UNVERIFIED
    if vat_country != delivery_country:
        return Decision.TAXED_COUNTRY_MISMATCH
    return None


def attestation_decision(
    context: Context, attestation: dict | None, now: datetime.datetime
) -> str:
    if not attestation:
        return Decision.TAXED_UNVERIFIED
    if (
        attestation.get("vatId") != context.b2b_input.vat_id
        or attestation.get("contextHash") != context.hash
    ):
        return Decision.TAXED_STALE
    status = attestation.get("status")
    if status == VerificationStatus.INVALID:
        return Decision.TAXED_INVALID
    if status == VerificationStatus.UNAVAILABLE:
        return Decision.TAXED_UNAVAILABLE
    if status != VerificationStatus.VALID:
        return Decision.TAXED_UNVERIFIED
    if attestation.get("identity") == Identity.MISMATCH:
        return Decision.TAXED_IDENTITY_REVIEW
    expires_at = parse_dt(attestation.get("expiresAt"))
    checked_at = parse_dt(attestation.get("checkedAt"))
    if expires_at is None or checked_at is None or now >= expires_at:
        return Decision.TAXED_STALE
    return Decision.EXEMPT


def live_decision(
    context: Context,
    state: State,
    now: datetime.datetime,
    mode: str | None = None,
) -> str:
    structural = structural_decision(context)
    if structural is not None:
        return structural
    if (mode or get_mode()) != MODE_ON:
        return Decision.TAXED_DISABLED
    return attestation_decision(context, state.attestation, now)


# ---------------------------------------------------------------------------
# Payment lock
# ---------------------------------------------------------------------------

_TERMINAL_EVENTS = (
    TransactionEventType.AUTHORIZATION_FAILURE,
    TransactionEventType.CHARGE_FAILURE,
    TransactionEventType.CANCEL_SUCCESS,
)


def _transaction_has_money(item: TransactionItem) -> bool:
    return any(
        value > 0
        for value in (
            item.authorized_value,
            item.charged_value,
            item.authorize_pending_value,
            item.charge_pending_value,
        )
    )


def _payment_has_money(payment: Payment) -> bool:
    return payment.is_active and (
        payment.charge_status != ChargeStatus.NOT_CHARGED or payment.captured_amount > 0
    )


def payment_lock_is_active(
    checkout: Checkout,
    lock: dict | None,
    now: datetime.datetime,
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> bool:
    """Return whether the frozen payment snapshot still governs the checkout.

    Never released while Saleor knows about authorized, charged or pending
    money on a locked attempt. Without money it is released after a terminal
    failure/cancel event of every attempt, or after the lock TTL.
    """
    if not lock:
        return False
    attempts = lock.get("attempts") or []
    keys = [a.get("idempotencyKey") for a in attempts if a.get("type") == "transaction"]
    payment_ids = [a.get("id") for a in attempts if a.get("type") == "payment"]
    transactions = list(
        TransactionItem.objects.using(database_connection_name).filter(
            checkout_id=checkout.pk, idempotency_key__in=[k for k in keys if k]
        )
    )
    payments = list(
        Payment.objects.using(database_connection_name).filter(
            checkout_id=checkout.pk, pk__in=[p for p in payment_ids if p]
        )
    )
    if any(_transaction_has_money(t) for t in transactions):
        return True
    if any(_payment_has_money(p) for p in payments):
        return True
    created_at = parse_dt(lock.get("createdAt"))
    if created_at is None:
        return False
    if now - created_at >= get_payment_lock_ttl():
        return False
    if not transactions and not payments:
        # The provider was never reached (creation failed) -> nothing to wait for
        # once the attempt is older than a short grace period.
        return now - created_at < datetime.timedelta(minutes=2)
    terminal_ids = set(
        TransactionEvent.objects.using(database_connection_name)
        .filter(
            transaction_id__in=[t.pk for t in transactions], type__in=_TERMINAL_EVENTS
        )
        .values_list("transaction_id", flat=True)
    )
    all_transactions_terminal = all(t.pk in terminal_ids for t in transactions)
    all_payments_inactive = all(not p.is_active for p in payments)
    return not (all_transactions_terminal and all_payments_inactive)


def checkout_has_money_in_flight(
    checkout: Checkout,
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> bool:
    for item in TransactionItem.objects.using(database_connection_name).filter(
        checkout_id=checkout.pk
    ):
        if _transaction_has_money(item):
            return True
    for payment in Payment.objects.using(database_connection_name).filter(
        checkout_id=checkout.pk, is_active=True
    ):
        if _payment_has_money(payment):
            return True
    return False


# ---------------------------------------------------------------------------
# Effective exemption (price path, payment, order)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evaluation:
    managed: bool
    decision: str
    effective_exemption: bool
    context: Context | None
    state: State
    frozen: bool  # decision taken from an active payment snapshot

    @property
    def context_version(self) -> str | None:
        if self.context is None:
            return None
        return make_context_version(self.state.revision, self.context.hash)


def evaluate(
    *,
    checkout: Checkout,
    context: Context,
    state: State,
    now: datetime.datetime | None = None,
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> Evaluation:
    now = now or timezone.now()
    if not state.managed:
        decision = live_decision(context, state, now)
        # Unmanaged checkout: native flag semantics are untouched.
        return Evaluation(
            managed=False,
            decision=decision,
            effective_exemption=checkout.tax_exemption,
            context=context,
            state=state,
            frozen=False,
        )
    lock = state.payment_lock
    if lock and payment_lock_is_active(checkout, lock, now, database_connection_name):
        decision = str(lock.get("decision"))
        exempt = bool(lock.get("exempt")) and checkout.tax_exemption
        if lock.get("contextHash") != context.hash or decision not in Decision.CHOICES:
            decision, exempt = Decision.TAXED_STALE, False
        elif decision == Decision.EXEMPT and not exempt:
            decision = Decision.TAXED_STALE
        return Evaluation(
            managed=True,
            decision=decision,
            effective_exemption=exempt,
            context=context,
            state=state,
            frozen=True,
        )
    decision = live_decision(context, state, now)
    if decision == Decision.EXEMPT and not checkout.tax_exemption:
        # Native flag was revoked/repaired -> a new attestation write is needed.
        decision = Decision.TAXED_STALE
    return Evaluation(
        managed=True,
        decision=decision,
        effective_exemption=decision == Decision.EXEMPT,
        context=context,
        state=state,
        frozen=False,
    )


def _load_metadata(
    checkout: Checkout, database_connection_name: str
) -> CheckoutMetadata | None:
    return (
        CheckoutMetadata.objects.using(database_connection_name)
        .filter(checkout_id=checkout.pk)
        .first()
    )


def get_effective_tax_exemption(
    checkout_info: "CheckoutInfo",
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> bool:
    """Effective native exemption used by the checkout price calculation.

    Fast path: a checkout without the native flag is always taxed here and costs
    no extra query (regular B2C traffic). Only flagged checkouts load metadata.
    """
    checkout = checkout_info.checkout
    if not checkout.tax_exemption:
        return False
    metadata = _load_metadata(checkout, database_connection_name)
    state = read_state(metadata.private_metadata if metadata else None)
    if not state.managed:
        return True
    context = context_from_checkout_info(checkout_info, metadata)
    return evaluate(
        checkout=checkout,
        context=context,
        state=state,
        database_connection_name=database_connection_name,
    ).effective_exemption


def evaluate_checkout_info(
    checkout_info: "CheckoutInfo",
    database_connection_name: str = settings.DATABASE_CONNECTION_DEFAULT_NAME,
) -> Evaluation:
    checkout = checkout_info.checkout
    metadata = _load_metadata(checkout, database_connection_name)
    state = read_state(metadata.private_metadata if metadata else None)
    context = context_from_checkout_info(checkout_info, metadata)
    return evaluate(
        checkout=checkout,
        context=context,
        state=state,
        database_connection_name=database_connection_name,
    )


# ---------------------------------------------------------------------------
# Writer helpers (always under the checkout row lock)
# ---------------------------------------------------------------------------


def _lock_checkout(checkout_pk) -> Checkout | None:
    return (
        Checkout.objects.select_for_update(of=("self",)).filter(pk=checkout_pk).first()
    )


def _lock_metadata(checkout: Checkout) -> CheckoutMetadata:
    metadata = (
        CheckoutMetadata.objects.select_for_update(of=("self",))
        .filter(checkout_id=checkout.pk)
        .first()
    )
    if metadata is None:
        CheckoutMetadata.objects.get_or_create(checkout=checkout)
        metadata = CheckoutMetadata.objects.select_for_update(of=("self",)).get(
            checkout_id=checkout.pk
        )
    return metadata


def _locked_context(checkout: Checkout, metadata: CheckoutMetadata) -> Context:
    address_ids = [
        pk for pk in (checkout.billing_address_id, checkout.shipping_address_id) if pk
    ]
    addresses = {
        address.pk: address
        for address in Address.objects.select_for_update(of=("self",))
        .filter(pk__in=address_ids)
        .order_by("pk")
    }
    collection_point = None
    if checkout.collection_point_id:
        collection_point = (
            Warehouse.objects.select_related("address")
            .filter(pk=checkout.collection_point_id)
            .first()
        )
    return build_context(
        checkout=checkout,
        channel_slug=checkout.channel.slug,
        public_metadata=metadata.metadata,
        billing_address=(
            addresses.get(checkout.billing_address_id)
            if checkout.billing_address_id
            else None
        ),
        shipping_address=(
            addresses.get(checkout.shipping_address_id)
            if checkout.shipping_address_id
            else None
        ),
        collection_point=collection_point,
    )


def _write_state(
    metadata: CheckoutMetadata,
    previous: State,
    *,
    decision: str,
    attestation: dict | None,
    payment_lock: dict | None,
    app: Optional["App"],
    event: str,
    now: datetime.datetime,
    bump_revision: bool = True,
) -> dict:
    events = list(previous.data.get("events") or []) if previous.verified else []
    events.append(
        {
            "at": now.isoformat(),
            "event": event,
            "decision": decision,
            "appId": app.pk if app else None,
        }
    )
    revision = previous.revision + 1 if bump_revision else previous.revision
    data = {
        "schema": STATE_SCHEMA,
        "policyVersion": POLICY_VERSION,
        "managed": True,
        "revision": revision,
        "decision": decision,
        "attestation": attestation,
        "paymentLock": payment_lock,
        "updatedAt": now.isoformat(),
        "appId": app.pk if app else None,
        "events": events[-MAX_EVENTS:],
    }
    data["sig"] = sign(data)
    metadata.private_metadata = dict(metadata.private_metadata or {})
    metadata.private_metadata[STATE_KEY] = data
    for key in LEGACY_PRIVATE_KEYS:
        metadata.private_metadata.pop(key, None)
    metadata.save(update_fields=["private_metadata"])
    return data


def _invalidate_prices(checkout: Checkout, tax_exemption: bool, now) -> None:
    checkout.tax_exemption = tax_exemption
    checkout.price_expiration = now
    checkout.discount_expiration = now
    checkout.last_change = now
    checkout.save(
        update_fields=[
            "tax_exemption",
            "price_expiration",
            "discount_expiration",
            "last_change",
        ]
    )


@dataclass
class AttestationResult:
    checkout: Checkout | None
    error_field: str | None = None
    error_code: str | None = None
    error_message: str | None = None


def _error(field, code, message) -> AttestationResult:
    return AttestationResult(
        checkout=None, error_field=field, error_code=code, error_message=message
    )


def validate_verification_input(
    verification: dict, now: datetime.datetime
) -> tuple[dict | None, AttestationResult | None]:
    vat_id = normalize_vat_id(verification.get("vat_id"))
    status = verification.get("status")
    checked_at = verification.get("checked_at")
    request_identifier = verification.get("request_identifier")
    name = verification.get("name")
    address = verification.get("address")

    def invalid(field, message):
        return None, _error(
            f"verification.{field}", B2bErrorCode.INVALID_ATTESTATION, message
        )

    if vat_id != verification.get("vat_id") or not VAT_ID_RE.match(vat_id):
        return invalid("vatId", "VAT ID must be the full normalized number.")
    if status not in VerificationStatus.CHOICES:
        return invalid("status", "Unsupported verification status.")
    if not isinstance(checked_at, datetime.datetime) or checked_at.tzinfo is None:
        return invalid("checkedAt", "checkedAt must be a timezone-aware datetime.")
    if checked_at > now + ALLOWED_CLOCK_SKEW:
        return invalid("checkedAt", "checkedAt cannot be in the future.")
    if checked_at < MIN_CHECKED_AT:
        return invalid("checkedAt", "checkedAt is not plausible.")
    if request_identifier is not None and (
        not isinstance(request_identifier, str)
        or not request_identifier.strip()
        or len(request_identifier) > MAX_REQUEST_IDENTIFIER_LENGTH
    ):
        return invalid("requestIdentifier", "Invalid request identifier.")
    for field, value in (("name", name), ("address", address)):
        if value is not None and (
            not isinstance(value, str) or len(value) > MAX_VIES_TEXT_LENGTH
        ):
            return invalid(field, f"Invalid {field}.")
    return (
        {
            "source": "VIES",
            "vatId": vat_id,
            "status": status,
            "checkedAt": checked_at.astimezone(datetime.UTC).isoformat(),
            "expiresAt": (
                (checked_at + ATTESTATION_TTL).astimezone(datetime.UTC).isoformat()
                if status == VerificationStatus.VALID
                else None
            ),
            "requestIdentifier": request_identifier.strip()
            if request_identifier
            else None,
            "name": name,
            "address": address,
        },
        None,
    )


def apply_attestation(
    *,
    checkout_pk,
    expected_context_version: str,
    verification: dict | None,
    app: Optional["App"],
    now: datetime.datetime | None = None,
) -> AttestationResult:
    """Atomically verify freshness, store the attestation and set the native flag.

    VIES must be called by the caller *before* this function; no remote call is
    made while the locks are held.
    """
    now = now or timezone.now()
    attestation_base = None
    if verification is not None:
        attestation_base, error = validate_verification_input(verification, now)
        if error:
            return error

    with transaction.atomic():
        checkout = _lock_checkout(checkout_pk)
        if checkout is None:
            return _error("id", B2bErrorCode.NOT_FOUND, "Checkout not found.")
        metadata = _lock_metadata(checkout)
        state = read_state(metadata.private_metadata)
        context = _locked_context(checkout, metadata)
        current_version = make_context_version(state.revision, context.hash)
        if expected_context_version != current_version:
            return _error(
                "expectedContextVersion",
                B2bErrorCode.STALE_CONTEXT,
                "Checkout context changed. Reload b2bTaxState and retry.",
            )
        if payment_lock_is_active(
            checkout, state.payment_lock, now
        ) or checkout_has_money_in_flight(checkout):
            return _error(
                "id",
                B2bErrorCode.CHECKOUT_LOCKED,
                "A payment for this checkout is in progress.",
            )
        if checkout.tax_exemption and not state.managed:
            return _error(
                "id",
                B2bErrorCode.UNMANAGED_EXEMPTION_CONFLICT,
                "Checkout has a tax exemption not managed by the B2B workflow.",
            )

        structural = structural_decision(context)
        if structural == Decision.OUT_OF_SCOPE:
            if state.managed:
                # Revoke only our own approval; never touch export regime.
                _write_state(
                    metadata,
                    state,
                    decision=Decision.OUT_OF_SCOPE,
                    attestation=None,
                    payment_lock=None,
                    app=app,
                    event="out_of_scope",
                    now=now,
                )
                if checkout.tax_exemption:
                    _invalidate_prices(checkout, False, now)
            return AttestationResult(checkout=checkout)

        attestation = None
        if attestation_base is not None:
            if attestation_base["vatId"] != context.b2b_input.vat_id:
                return _error(
                    "verification.vatId",
                    B2bErrorCode.INVALID_ATTESTATION,
                    "Verified VAT ID does not match the checkout input.",
                )
            attestation = {
                **attestation_base,
                "identity": compare_identity(
                    attestation_base.get("name"), context.company_name
                ),
                "companyName": context.company_name,
                "contextHash": context.hash,
                "acceptedAt": now.isoformat(),
                "appId": app.pk if app else None,
                "policyVersion": POLICY_VERSION,
            }

        new_state_preview = State(
            managed=True,
            verified=True,
            revision=state.revision + 1,
            data={"attestation": attestation},
        )
        decision = live_decision(context, new_state_preview, now)
        exempt = decision == Decision.EXEMPT
        _write_state(
            metadata,
            state,
            decision=decision,
            attestation=attestation,
            payment_lock=None,
            app=app,
            event="attestation" if verification is not None else "revoke",
            now=now,
        )
        _invalidate_prices(checkout, exempt, now)
    logger.info(
        "MAKY B2B attestation stored",
        extra={"checkout_id": str(checkout.pk), "decision": decision},
    )
    return AttestationResult(checkout=checkout)


def revoke_for_native_flag(
    *, checkout: Checkout, tax_exemption: bool, app=None
) -> str | None:
    """Apply native ``taxExemptionManage`` on a managed checkout.

    Returns an error code or ``None``. ``None`` with a managed checkout means
    the caller must not touch the flag itself (already handled here).
    Returns "unmanaged" when the native behaviour should be used.
    """
    now = timezone.now()
    with transaction.atomic():
        locked = _lock_checkout(checkout.pk)
        if locked is None:
            return B2bErrorCode.NOT_FOUND
        metadata = _lock_metadata(locked)
        state = read_state(metadata.private_metadata)
        if not state.managed:
            return "unmanaged"
        if payment_lock_is_active(
            locked, state.payment_lock, now
        ) or checkout_has_money_in_flight(locked):
            return B2bErrorCode.B2B_CHECKOUT_LOCKED
        context = _locked_context(locked, metadata)
        if tax_exemption:
            if live_decision(context, state, now) == Decision.EXEMPT:
                if not locked.tax_exemption:
                    _invalidate_prices(locked, True, now)
                checkout.tax_exemption = True
                return None
            return B2bErrorCode.B2B_MANAGED_CHECKOUT
        _write_state(
            metadata,
            state,
            decision=live_decision(context, State(True, True, 0, {}), now),
            attestation=None,
            payment_lock=None,
            app=app,
            event="native_revoke",
            now=now,
        )
        _invalidate_prices(locked, False, now)
        checkout.tax_exemption = False
    return None


def guard_context_change(checkout: Checkout) -> None:
    """Reject tax-relevant changes while a B2B payment snapshot is active.

    Called by address, delivery and metadata mutations inside their atomic
    block. Unmanaged checkouts cost one indexed metadata query.
    """
    metadata = _load_metadata(checkout, settings.DATABASE_CONNECTION_DEFAULT_NAME)
    state = read_state(metadata.private_metadata if metadata else None)
    if not state.managed or not state.payment_lock:
        return
    with transaction.atomic():
        locked = _lock_checkout(checkout.pk)
        if locked is None:
            return
        metadata = _load_metadata(locked, settings.DATABASE_CONNECTION_DEFAULT_NAME)
        state = read_state(metadata.private_metadata if metadata else None)
        if payment_lock_is_active(locked, state.payment_lock, timezone.now()):
            raise B2bLockedError()


class B2bLockedError(Exception):
    message = (
        "A payment for this checkout is in progress. Finish or cancel it before "
        "changing company, VAT, address or delivery data."
    )


class B2bContextChangedError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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
    # Imported here: `calculations` imports this module (price path hook).
    from .calculations import fetch_checkout_data

    metadata = _load_metadata(checkout, settings.DATABASE_CONNECTION_DEFAULT_NAME)
    if not read_state(metadata.private_metadata if metadata else None).managed:
        return None
    now = timezone.now()
    with transaction.atomic():
        locked = _lock_checkout(checkout.pk)
        if locked is None:
            raise B2bContextChangedError("Checkout does not exist anymore.")
        metadata = _lock_metadata(locked)
        state = read_state(metadata.private_metadata)
        context = _locked_context(locked, metadata)
        lock = state.payment_lock
        lock_active = bool(lock) and payment_lock_is_active(locked, lock, now)

        lines, _ = fetch_checkout_lines(locked)
        checkout_info = fetch_checkout_info(locked, lines, manager)
        checkout_info, _ = fetch_checkout_data(
            checkout_info, manager, lines, requestor=None, force_update=True
        ).get()
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

        if lock_active:
            assert lock is not None
            if (
                lock.get("contextHash") == context.hash
                and lock.get("totalGross") == str(total_gross)
                and lock.get("currency") == fresh.currency
            ):
                # Retry of the same quote: attach the attempt to the same snapshot.
                attempts = list(lock.get("attempts") or [])
                attempts.append({**attempt, "at": now.isoformat()})
                _write_state(
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
        _write_state(
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
                "attempts": [{**attempt, "at": now.isoformat()}],
            },
            app=None,
            event="payment_lock",
            now=now,
            bump_revision=False,
        )
    return amount


# ---------------------------------------------------------------------------
# Order creation
# ---------------------------------------------------------------------------


def validate_and_build_order_snapshot(
    checkout_info: "CheckoutInfo",
    metadata: CheckoutMetadata | None,
    *,
    order_total_gross: Decimal | None = None,
) -> tuple[bool, dict | None]:
    """Validate the B2B state right before the order is created.

    Returns ``(order_tax_exemption, snapshot_or_None)``. Raises
    ``B2bContextChangedError`` if the checkout is not consistent with the
    frozen payment snapshot or its tax decision.
    """
    checkout = checkout_info.checkout
    now = timezone.now()
    state = read_state(metadata.private_metadata if metadata else None)
    context = context_from_checkout_info(checkout_info, metadata)
    if not state.managed and not context.b2b_input.is_business:
        return checkout.tax_exemption, None
    evaluation = evaluate(checkout=checkout, context=context, state=state, now=now)
    lock = state.payment_lock
    if state.managed:
        db_checkout = (
            Checkout.objects.filter(pk=checkout.pk).only("tax_exemption").first()
        )
        if db_checkout is not None and (
            db_checkout.tax_exemption != checkout.tax_exemption
        ):
            raise B2bContextChangedError(
                "Checkout changed during completion. Reload the checkout."
            )
        if evaluation.frozen and lock and lock.get("contextHash") != context.hash:
            raise B2bContextChangedError(
                "Checkout data differ from the started payment."
            )
    exempt = evaluation.effective_exemption
    reason = None
    if evaluation.decision == Decision.OUT_OF_SCOPE:
        reason = "OUT_OF_SCOPE_EXPORT"
    elif exempt:
        reason = "EU_ICS_43"
    else:
        reason = "DOMESTIC_VAT"
    attestation = state.attestation
    snapshot = {
        "schema": 1,
        "policyVersion": POLICY_VERSION,
        "recordedAt": now.isoformat(),
        "managed": state.managed,
        "decision": evaluation.decision,
        "effectiveTaxExemption": exempt,
        "taxReason": reason,
        "frozenPaymentSnapshot": evaluation.frozen,
        "contextHash": context.hash,
        "stateRevision": state.revision,
        "input": context.b2b_input.as_dict(),
        "companyName": context.company_name,
        "billingAddress": context.billing,
        "shippingAddress": context.shipping,
        "delivery": {
            "kind": context.delivery.kind,
            "country": context.delivery_country,
            "pickupId": context.delivery.pickup_id,
        },
        "attestation": attestation,
        "paymentLock": lock if evaluation.frozen else None,
        "checkoutTotal": {
            "net": str(checkout.total_net_amount),
            "gross": str(
                order_total_gross
                if order_total_gross is not None
                else checkout.total_gross_amount
            ),
            "currency": checkout.currency,
        },
    }
    snapshot["sig"] = sign(snapshot)
    return (exempt if state.managed else checkout.tax_exemption), snapshot


# ---------------------------------------------------------------------------
# Metadata key protection
# ---------------------------------------------------------------------------


def find_forbidden_keys(keys, *, private: bool) -> list[str]:
    forbidden = []
    for key in keys:
        if not isinstance(key, str):
            continue
        if not key.startswith(RESERVED_PREFIX):
            if private and key in LEGACY_PRIVATE_KEYS:
                forbidden.append(key)
            continue
        if not private and key == INPUT_KEY:
            continue
        forbidden.append(key)
    return forbidden


def validate_input_payload(raw_value: Any) -> bool:
    """Public input must be a JSON object with schema 1 (or empty to clear)."""
    if raw_value in (None, ""):
        return True
    if not isinstance(raw_value, str):
        return False
    try:
        data = json.loads(raw_value)
    except ValueError:
        return False
    if not isinstance(data, dict) or data.get("schema") != 1:
        return False
    if "isBusiness" in data and not isinstance(data["isBusiness"], bool):
        return False
    for field in ("registrationId", "taxId", "vatId"):
        if field in data and not isinstance(data[field], str):
            return False
        if len(data.get(field) or "") > MAX_INPUT_FIELD_LENGTH:
            return False
    return True
