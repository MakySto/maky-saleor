import datetime
from decimal import Decimal
from unittest import mock

import graphene
import pytest
from django.utils import timezone
from freezegun import freeze_time

from ....checkout import maky_b2b
from ....checkout.error_codes import CheckoutErrorCode
from ....checkout.models import Checkout, CheckoutMetadata
from ....checkout.tests.fixtures.maky_b2b import (
    MAKY_B2B_COMPANY,
    MAKY_B2B_VAT_ID,
    maky_b2b_context_version,
    maky_b2b_verification,
)
from ....order.models import Order
from ....payment.interface import TransactionSessionResult
from ....payment.models import TransactionItem
from ...core.enums import TransactionInitializeErrorCode
from ...tests.utils import get_graphql_content

APP_IDENTIFIER = "maky.stripe.test"

TRANSACTION_INITIALIZE = """
mutation($id: ID!, $amount: PositiveDecimal, $paymentGateway: PaymentGatewayToInitialize!) {
  transactionInitialize(id: $id, amount: $amount, paymentGateway: $paymentGateway) {
    transaction { id authorizedAmount { amount } chargedAmount { amount } }
    errors { field code message }
  }
}
"""

BILLING_UPDATE = """
mutation($id: ID!, $address: AddressInput!) {
  checkoutBillingAddressUpdate(id: $id, billingAddress: $address) {
    errors { field code message }
  }
}
"""

SHIPPING_UPDATE = """
mutation($id: ID!, $address: AddressInput!) {
  checkoutShippingAddressUpdate(id: $id, shippingAddress: $address) {
    errors { field code message }
  }
}
"""

CHECKOUT_COMPLETE = """
mutation($id: ID!) {
  checkoutComplete(id: $id) {
    order { id total { gross { amount } net { amount } } }
    errors { field code message }
  }
}
"""

STATE_QUERY = """
query($id: ID!) {
  checkout(id: $id) {
    b2bTaxState { decision effectiveTaxExemption paymentLocked }
    totalPrice { gross { amount } }
  }
}
"""

SK_ADDRESS = {
    "firstName": "Jan",
    "lastName": "Novak",
    "companyName": "Firma s.r.o.",
    "streetAddress1": "Hlavna 1",
    "city": "Bratislava",
    "postalCode": "81101",
    "country": "SK",
}


def _id(checkout):
    return graphene.Node.to_global_id("Checkout", checkout.token)


@pytest.fixture
def stripe_test_app(webhook_app):
    webhook_app.identifier = APP_IDENTIFIER
    webhook_app.save(update_fields=["identifier"])
    return webhook_app


@pytest.fixture
def exempt_checkout(maky_b2b_checkout, app):
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=maky_b2b_verification(),
        app=app,
    )
    assert result.error_code is None
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is True
    return maky_b2b_checkout


def _session_result(result: str, amount: str | None = None):
    response = {
        "pspReference": "pi_test_123",
        "data": {"clientSecret": "pi_test_123_secret"},
        "result": result,
        "amount": amount or "126.00",
    }
    return TransactionSessionResult(app_identifier=APP_IDENTIFIER, response=response)


def _initialize(api_client, checkout, amount=None):
    variables = {
        "id": _id(checkout),
        "amount": amount,
        "paymentGateway": {"id": APP_IDENTIFIER, "data": None},
    }
    content = get_graphql_content(
        api_client.post_graphql(TRANSACTION_INITIALIZE, variables)
    )
    return content["data"]["transactionInitialize"]


def _state(api_client, checkout):
    content = get_graphql_content(
        api_client.post_graphql(STATE_QUERY, {"id": _id(checkout)})
    )
    return content["data"]["checkout"]


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_initialize_uses_server_amount_and_freezes_snapshot(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")

    # when
    data = _initialize(api_client, exempt_checkout)

    # then
    assert data["errors"] == []
    session_data = mocked_initialize.call_args[0][0]
    assert session_data.action.amount == Decimal("126.00")
    metadata = CheckoutMetadata.objects.get(checkout=exempt_checkout)
    lock = metadata.private_metadata[maky_b2b.STATE_KEY]["paymentLock"]
    assert lock["exempt"] is True
    assert lock["decision"] == maky_b2b.Decision.EXEMPT
    assert lock["totalGross"] == "126.00"
    assert len(lock["attempts"]) == 1
    assert _state(api_client, exempt_checkout)["b2bTaxState"]["paymentLocked"] is True


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_initialize_with_stale_client_amount_is_rejected(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # when - storefront still shows the taxed amount
    data = _initialize(api_client, exempt_checkout, amount=154.98)

    # then
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == (
        TransactionInitializeErrorCode.B2B_TAX_CONTEXT_CHANGED.name
    )
    assert data["errors"][0]["field"] == "amount"
    mocked_initialize.assert_not_called()
    assert (
        TransactionItem.objects.filter(checkout_id=exempt_checkout.pk).exists() is False
    )


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        (BILLING_UPDATE, "checkoutBillingAddressUpdate"),
        (SHIPPING_UPDATE, "checkoutShippingAddressUpdate"),
    ],
)
def test_address_change_during_payment_is_locked(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app, mutation, field
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    _initialize(api_client, exempt_checkout)
    billing_before = exempt_checkout.billing_address.as_data()
    shipping_before = exempt_checkout.shipping_address.as_data()

    # when - second tab / direct API switches to SK
    content = get_graphql_content(
        api_client.post_graphql(
            mutation, {"id": _id(exempt_checkout), "address": SK_ADDRESS}
        )
    )

    # then
    errors = content["data"][field]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == CheckoutErrorCode.B2B_CHECKOUT_LOCKED.name
    assert errors[0]["message"] == maky_b2b.B2bLockedError.message
    exempt_checkout.refresh_from_db()
    assert exempt_checkout.billing_address.as_data() == billing_before
    assert exempt_checkout.shipping_address.as_data() == shipping_before


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_attestation_write_during_payment_is_locked(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app, app
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    _initialize(api_client, exempt_checkout)

    # when
    result = maky_b2b.apply_attestation(
        checkout_pk=exempt_checkout.pk,
        expected_context_version=maky_b2b_context_version(exempt_checkout),
        verification=None,
        app=app,
    )

    # then
    assert result.error_code == maky_b2b.B2bErrorCode.CHECKOUT_LOCKED
    exempt_checkout.refresh_from_db()
    assert exempt_checkout.tax_exemption is True


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_retry_of_same_quote_reuses_snapshot(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    _initialize(api_client, exempt_checkout)

    # when
    data = _initialize(api_client, exempt_checkout)

    # then
    assert data["errors"] == []
    lock = CheckoutMetadata.objects.get(checkout=exempt_checkout).private_metadata[
        maky_b2b.STATE_KEY
    ]["paymentLock"]
    assert len(lock["attempts"]) == 2


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_complete_creates_order_with_exempt_snapshot(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_SUCCESS", "126.00")
    assert _initialize(api_client, exempt_checkout)["errors"] == []

    # when
    content = get_graphql_content(
        api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(exempt_checkout)})
    )

    # then
    data = content["data"]["checkoutComplete"]
    assert data["errors"] == []
    assert data["order"]["total"]["gross"]["amount"] == 126.0
    order = Order.objects.get()
    assert order.tax_exemption is True
    assert order.total_gross_amount == Decimal("126.00")
    snapshot = order.private_metadata[maky_b2b.ORDER_SNAPSHOT_KEY]
    assert maky_b2b.is_signature_valid(snapshot) is True
    assert snapshot["taxReason"] == "EU_ICS_43"
    assert snapshot["decision"] == maky_b2b.Decision.EXEMPT
    assert snapshot["input"]["vatId"] == MAKY_B2B_VAT_ID
    assert snapshot["companyName"] == MAKY_B2B_COMPANY
    assert snapshot["frozenPaymentSnapshot"] is True
    assert snapshot["attestation"]["status"] == maky_b2b.VerificationStatus.VALID
    assert snapshot["paymentLock"]["totalGross"] == "126.00"


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_frozen_snapshot_survives_mode_off_and_vies_expiry(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app, settings
):
    # given - money authorized/charged for the net amount
    mocked_initialize.return_value = _session_result("CHARGE_SUCCESS", "126.00")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = "off"

    # when
    with freeze_time(timezone.now() + datetime.timedelta(hours=30)):
        Checkout.objects.filter(pk=exempt_checkout.pk).update(
            price_expiration=timezone.now()
        )
        content = get_graphql_content(
            api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(exempt_checkout)})
        )

    # then
    data = content["data"]["checkoutComplete"]
    assert data["errors"] == []
    order = Order.objects.get()
    assert order.total_gross_amount == Decimal("126.00")
    assert order.tax_exemption is True


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_net_payment_cannot_complete_sk_order_after_lock_expiry(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given - payment started for the net amount, no money known yet
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    assert _initialize(api_client, exempt_checkout)["errors"] == []

    with freeze_time(timezone.now() + datetime.timedelta(minutes=30)):
        # lock TTL elapsed without money -> address change allowed
        content = get_graphql_content(
            api_client.post_graphql(
                SHIPPING_UPDATE, {"id": _id(exempt_checkout), "address": SK_ADDRESS}
            )
        )
        assert content["data"]["checkoutShippingAddressUpdate"]["errors"] == []
        state = _state(api_client, exempt_checkout)
        assert state["b2bTaxState"]["effectiveTaxExemption"] is False
        assert state["totalPrice"]["gross"]["amount"] == 154.98

        # the old Stripe intent is confirmed late for the net amount
        transaction = TransactionItem.objects.get(checkout_id=exempt_checkout.pk)
        transaction.charged_value = Decimal("126.00")
        transaction.save(update_fields=["charged_value"])

        # when
        content = get_graphql_content(
            api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(exempt_checkout)})
        )

    # then
    data = content["data"]["checkoutComplete"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CheckoutErrorCode.CHECKOUT_NOT_FULLY_PAID.name
    assert Order.objects.exists() is False


def test_complete_without_payment_is_not_bypassed(api_client, exempt_checkout):
    # when - direct API completion, no transaction at all
    content = get_graphql_content(
        api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(exempt_checkout)})
    )

    # then
    data = content["data"]["checkoutComplete"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CheckoutErrorCode.CHECKOUT_NOT_FULLY_PAID.name
    assert Order.objects.exists() is False


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_taxed_company_order_stores_company_snapshot(
    mocked_initialize, api_client, maky_b2b_checkout, stripe_test_app
):
    # given - company without automatic exemption (never verified)
    mocked_initialize.return_value = _session_result("CHARGE_SUCCESS", "154.98")
    assert _initialize(api_client, maky_b2b_checkout)["errors"] == []

    # when
    content = get_graphql_content(
        api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(maky_b2b_checkout)})
    )

    # then
    assert content["data"]["checkoutComplete"]["errors"] == []
    order = Order.objects.get()
    assert order.tax_exemption is False
    assert order.total_gross_amount == Decimal("154.98")
    snapshot = order.private_metadata[maky_b2b.ORDER_SNAPSHOT_KEY]
    assert snapshot["taxReason"] == "DOMESTIC_VAT"
    assert snapshot["decision"] == maky_b2b.Decision.TAXED_UNVERIFIED
    assert snapshot["companyName"] == MAKY_B2B_COMPANY
    assert snapshot["managed"] is False
