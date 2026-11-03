import datetime
import threading
from decimal import Decimal
from unittest import mock

import graphene
import pytest
from django.conf import settings
from django.db import connections
from django.utils import timezone
from freezegun import freeze_time
from prices import Money

from ....checkout import maky_b2b, maky_b2b_payment, maky_b2b_rollback
from ....checkout.calculations import fetch_checkout_data
from ....checkout.error_codes import CheckoutErrorCode
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import Checkout, CheckoutMetadata
from ....checkout.payment_utils import update_checkout_payment_statuses
from ....checkout.tests.fixtures.maky_b2b import (
    MAKY_B2B_COMPANY,
    MAKY_B2B_VAT_ID,
    maky_b2b_context_version,
    maky_b2b_verification,
)
from ....checkout.utils import change_billing_address_in_checkout
from ....core.models import EventDelivery
from ....order.models import Order
from ....payment import TransactionEventType
from ....payment.interface import TransactionSessionResult
from ....payment.models import TransactionEvent, TransactionItem
from ....plugins.manager import get_plugins_manager
from ....product.models import ProductVariantChannelListing
from ....webhook.event_types import WebhookEventSyncType
from ....webhook.models import Webhook
from ...core.enums import TransactionInitializeErrorCode
from ...tests.fixtures import ApiClient
from ...tests.utils import assert_no_permission, get_graphql_content

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
def test_time_alone_does_not_release_started_payment(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given - payment started for the net amount, no money known yet
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    shipping_before = exempt_checkout.shipping_address.as_data()

    with freeze_time(timezone.now() + datetime.timedelta(days=2)):
        # when - the customer switches delivery to SK long after the start
        content = get_graphql_content(
            api_client.post_graphql(
                SHIPPING_UPDATE, {"id": _id(exempt_checkout), "address": SK_ADDRESS}
            )
        )
        state = _state(api_client, exempt_checkout)

        # the old intent is confirmed late for the net amount
        transaction = TransactionItem.objects.get(checkout_id=exempt_checkout.pk)
        transaction.charged_value = Decimal("126.00")
        transaction.save(update_fields=["charged_value"])
        exempt_checkout.refresh_from_db()
        update_checkout_payment_statuses(
            exempt_checkout,
            Money(Decimal("126.00"), exempt_checkout.currency),
            checkout_has_lines=True,
        )
        complete = get_graphql_content(
            api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(exempt_checkout)})
        )

    # then - change rejected, the late payment completes the confirmed offer
    errors = content["data"]["checkoutShippingAddressUpdate"]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == CheckoutErrorCode.B2B_CHECKOUT_LOCKED.name
    assert state["b2bTaxState"]["paymentLocked"] is True
    assert state["b2bTaxState"]["effectiveTaxExemption"] is True
    assert complete["data"]["checkoutComplete"]["errors"] == []
    order = Order.objects.get()
    assert order.total_gross_amount == Decimal("126.00")
    assert order.tax_exemption is True
    assert order.shipping_address.as_data() == shipping_before


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_failed_charge_does_not_release_payment_lock(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given - the provider reports a failure; the intent can still be confirmed
    mocked_initialize.return_value = _session_result("CHARGE_FAILURE")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    assert TransactionEvent.objects.filter(
        transaction__checkout_id=exempt_checkout.pk,
        type=TransactionEventType.CHARGE_FAILURE,
    ).exists()

    # when
    content = get_graphql_content(
        api_client.post_graphql(
            SHIPPING_UPDATE, {"id": _id(exempt_checkout), "address": SK_ADDRESS}
        )
    )

    # then
    errors = content["data"]["checkoutShippingAddressUpdate"]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == CheckoutErrorCode.B2B_CHECKOUT_LOCKED.name
    assert _state(api_client, exempt_checkout)["b2bTaxState"]["paymentLocked"] is True


LINES_ADD = """
mutation($id: ID!, $variantId: ID!) {
  checkoutLinesAdd(id: $id, lines: [{variantId: $variantId, quantity: 1}]) {
    errors { field code message }
  }
}
"""

LINES_UPDATE = """
mutation($id: ID!, $variantId: ID!) {
  checkoutLinesUpdate(id: $id, lines: [{variantId: $variantId, quantity: 3}]) {
    errors { field code message }
  }
}
"""

LINES_DELETE = """
mutation($id: ID!, $lineId: ID!) {
  checkoutLinesDelete(id: $id, linesIds: [$lineId]) {
    errors { field code message }
  }
}
"""

ADD_PROMO_CODE = """
mutation($id: ID!, $code: String!) {
  checkoutAddPromoCode(id: $id, promoCode: $code) {
    errors { field code message }
  }
}
"""

REMOVE_PROMO_CODE = """
mutation($id: ID!, $code: String!) {
  checkoutRemovePromoCode(id: $id, promoCode: $code) {
    errors { field code message }
  }
}
"""


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        (LINES_ADD, "checkoutLinesAdd"),
        (LINES_UPDATE, "checkoutLinesUpdate"),
        (LINES_DELETE, "checkoutLinesDelete"),
    ],
)
def test_lines_change_during_payment_is_locked(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app, mutation, field
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    line = exempt_checkout.lines.get()
    quantity_before = line.quantity
    variables = {
        "id": _id(exempt_checkout),
        "variantId": graphene.Node.to_global_id("ProductVariant", line.variant_id),
        "lineId": graphene.Node.to_global_id("CheckoutLine", line.pk),
    }

    # when
    content = get_graphql_content(api_client.post_graphql(mutation, variables))

    # then
    errors = content["data"][field]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == CheckoutErrorCode.B2B_CHECKOUT_LOCKED.name
    assert errors[0]["message"] == maky_b2b.B2bLockedError.message
    line.refresh_from_db()
    assert line.quantity == quantity_before


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
@pytest.mark.parametrize(
    ("mutation", "field"),
    [
        (ADD_PROMO_CODE, "checkoutAddPromoCode"),
        (REMOVE_PROMO_CODE, "checkoutRemovePromoCode"),
    ],
)
def test_promo_code_change_during_payment_is_locked(
    mocked_initialize,
    api_client,
    exempt_checkout,
    stripe_test_app,
    voucher,
    mutation,
    field,
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    voucher_code_before = exempt_checkout.voucher_code

    # when
    content = get_graphql_content(
        api_client.post_graphql(
            mutation, {"id": _id(exempt_checkout), "code": voucher.code}
        )
    )

    # then
    errors = content["data"][field]["errors"]
    assert len(errors) == 1
    assert errors[0]["field"] == "promoCode"
    assert errors[0]["code"] == CheckoutErrorCode.B2B_CHECKOUT_LOCKED.name
    exempt_checkout.refresh_from_db()
    assert exempt_checkout.voucher_code == voucher_code_before


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_complete_rejects_lower_total_than_confirmed_payment(
    mocked_initialize, api_client, exempt_checkout, stripe_test_app
):
    # given - 126.00 charged for the confirmed offer
    mocked_initialize.return_value = _session_result("CHARGE_SUCCESS", "126.00")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    # the catalogue price drops afterwards (staff change, no checkout mutation):
    # 133.49 with VAT -> 108.53 net + 4.87 shipping = 113.40
    line = exempt_checkout.lines.get()
    ProductVariantChannelListing.objects.filter(
        variant_id=line.variant_id, channel=exempt_checkout.channel
    ).update(price_amount=Decimal("133.49"), discounted_price_amount=Decimal("133.49"))
    Checkout.objects.filter(pk=exempt_checkout.pk).update(
        price_expiration=timezone.now(), discount_expiration=timezone.now()
    )

    # when
    content = get_graphql_content(
        api_client.post_graphql(CHECKOUT_COMPLETE, {"id": _id(exempt_checkout)})
    )

    # then - native "fully paid" would accept the overpayment; the snapshot does not
    data = content["data"]["checkoutComplete"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == CheckoutErrorCode.B2B_TAX_CONTEXT_CHANGED.name
    assert data["errors"][0]["message"] == (
        "Checkout differs from the offer confirmed by the started payment (total)."
    )
    exempt_checkout.refresh_from_db()
    assert exempt_checkout.total_gross_amount == Decimal("113.40")
    assert Order.objects.exists() is False


@pytest.fixture
def stripe_test_app_with_cancel_webhook(stripe_test_app, permission_manage_payments):
    stripe_test_app.permissions.add(permission_manage_payments)
    webhook = Webhook.objects.create(
        app=stripe_test_app,
        name="cancel",
        target_url="https://stripe-app.example.com/webhooks/cancel",
    )
    webhook.events.create(
        event_type=WebhookEventSyncType.TRANSACTION_CANCELATION_REQUESTED
    )
    return stripe_test_app


RELEASE = """
mutation($id: ID!) {
  checkoutB2bPaymentLockRelease(id: $id) {
    status
    checkout { b2bTaxState { paymentLocked } }
    errors { field code message }
  }
}
"""


def _release(app_api_client, checkout):
    content = get_graphql_content(
        app_api_client.post_graphql(RELEASE, {"id": _id(checkout)})
    )
    return content["data"]["checkoutB2bPaymentLockRelease"]


@pytest.fixture
def b2b_app_api_client(
    app_api_client, permission_manage_taxes, permission_handle_taxes
):
    app_api_client.app.permissions.add(permission_manage_taxes, permission_handle_taxes)
    return app_api_client


@mock.patch(
    "saleor.webhook.transport.synchronous.transport.handle_transaction_request_task.delay"
)
@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_release_cancels_unpaid_attempt_and_unlocks_after_confirmation(
    mocked_initialize,
    mocked_request_task,
    api_client,
    b2b_app_api_client,
    exempt_checkout,
    stripe_test_app_with_cancel_webhook,
    settings,
):
    # given
    settings.PLUGINS = ["saleor.plugins.webhook.plugin.WebhookPlugin"]
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED")
    assert _initialize(api_client, exempt_checkout)["errors"] == []
    transaction = TransactionItem.objects.get(checkout_id=exempt_checkout.pk)

    # when
    data = _release(b2b_app_api_client, exempt_checkout)

    # then - cancel requested from the payment app, still locked
    assert data["errors"] == []
    assert data["status"] == maky_b2b_payment.ReleaseStatus.CANCELATION_REQUESTED
    assert data["checkout"]["b2bTaxState"]["paymentLocked"] is True
    request_event = TransactionEvent.objects.get(
        transaction=transaction, type=TransactionEventType.CANCEL_REQUEST
    )
    delivery = EventDelivery.objects.get(
        event_type=WebhookEventSyncType.TRANSACTION_CANCELATION_REQUESTED
    )
    mocked_request_task.assert_called_once_with(delivery.id, request_event.id)

    # when - the app confirms the cancelation
    TransactionEvent.objects.create(
        transaction=transaction,
        type=TransactionEventType.CANCEL_SUCCESS,
        amount_value=Decimal(0),
        currency=transaction.currency,
        psp_reference="pi_test_123",
    )
    content = get_graphql_content(
        api_client.post_graphql(
            SHIPPING_UPDATE, {"id": _id(exempt_checkout), "address": SK_ADDRESS}
        )
    )

    # then - the change is allowed and taxed again
    assert content["data"]["checkoutShippingAddressUpdate"]["errors"] == []
    state = _state(api_client, exempt_checkout)
    assert state["b2bTaxState"]["paymentLocked"] is False
    assert state["b2bTaxState"]["effectiveTaxExemption"] is False
    assert state["totalPrice"]["gross"]["amount"] == 154.98


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_release_never_touches_attempt_with_money(
    mocked_initialize,
    api_client,
    b2b_app_api_client,
    exempt_checkout,
    stripe_test_app_with_cancel_webhook,
):
    # given
    mocked_initialize.return_value = _session_result("CHARGE_SUCCESS", "126.00")
    assert _initialize(api_client, exempt_checkout)["errors"] == []

    # when
    data = _release(b2b_app_api_client, exempt_checkout)

    # then
    assert data["errors"] == []
    assert data["status"] == maky_b2b_payment.ReleaseStatus.PAYMENT_HAS_FUNDS
    assert data["checkout"]["b2bTaxState"]["paymentLocked"] is True
    assert (
        TransactionEvent.objects.filter(
            transaction__checkout_id=exempt_checkout.pk,
            type=TransactionEventType.CANCEL_REQUEST,
        ).exists()
        is False
    )
    assert EventDelivery.objects.exists() is False


def test_release_without_attempts_is_released(b2b_app_api_client, exempt_checkout):
    # when
    data = _release(b2b_app_api_client, exempt_checkout)

    # then
    assert data["errors"] == []
    assert data["status"] == maky_b2b_payment.ReleaseStatus.RELEASED
    assert data["checkout"]["b2bTaxState"]["paymentLocked"] is False


def test_release_requires_b2b_app(
    app_api_client, permission_manage_taxes, exempt_checkout
):
    # given - only one of the two permissions
    app_api_client.app.permissions.add(permission_manage_taxes)

    # when
    response = app_api_client.post_graphql(RELEASE, {"id": _id(exempt_checkout)})

    # then
    assert_no_permission(response)


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_attestation_blocked_by_unresolved_unmanaged_attempt(
    mocked_initialize, api_client, maky_b2b_checkout, stripe_test_app, app
):
    # given - a regular (not yet B2B managed) payment was started and failed
    mocked_initialize.return_value = _session_result("CHARGE_FAILURE", "154.98")
    assert _initialize(api_client, maky_b2b_checkout)["errors"] == []
    assert _state(api_client, maky_b2b_checkout)["b2bTaxState"]["paymentLocked"] is True

    # when
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=maky_b2b_verification(),
        app=app,
    )

    # then - the old intent for 154.98 could still be paid after a net quote
    assert result.error_code == maky_b2b.B2bErrorCode.CHECKOUT_LOCKED
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False


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


# ---------------------------------------------------------------------------
# Concurrency: an address change and transactionInitialize in two real
# requests (threads with their own DB connections), in a controlled order.
# ---------------------------------------------------------------------------

DE_BILLING_NEW_STREET = {
    "firstName": "Max",
    "lastName": "Muster",
    "companyName": MAKY_B2B_COMPANY,
    "streetAddress1": "Neue Strasse 2",
    "city": "BERLIN",
    "postalCode": "10115",
    "country": "DE",
}
THREAD_TIMEOUT = 30


@pytest.fixture
def managed_taxed_checkout(maky_b2b_checkout, app):
    """Return a managed checkout taxed after a VIES INVALID answer (154.98)."""
    result = maky_b2b.apply_attestation(
        checkout_pk=maky_b2b_checkout.pk,
        expected_context_version=maky_b2b_context_version(maky_b2b_checkout),
        verification=maky_b2b_verification(status=maky_b2b.VerificationStatus.INVALID),
        app=app,
    )
    assert result.error_code is None
    maky_b2b_checkout.refresh_from_db()
    return maky_b2b_checkout


def _paused(original, inside: threading.Event, go: threading.Event):
    """Wrap `original` so that its first call signals `inside` and waits for `go`."""
    calls = []

    def wrapper(*args, **kwargs):
        if not calls:
            calls.append(1)
            inside.set()
            assert go.wait(THREAD_TIMEOUT)
        return original(*args, **kwargs)

    return wrapper


def _in_thread(target, results: dict, key: str) -> threading.Thread:
    # Test mirrors (the replica alias) are redirected per thread by Django; a new
    # thread must point every alias at the test database again.
    test_db_settings = connections[
        settings.DATABASE_CONNECTION_DEFAULT_NAME
    ].settings_dict

    def run():
        for alias in connections:
            connections[alias].settings_dict = test_db_settings
        try:
            results[key] = target()
        finally:
            connections.close_all()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _current_context_hash(checkout) -> str:
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(
        checkout, lines, get_plugins_manager(allow_replica=False)
    )
    return maky_b2b.evaluate_checkout_info(checkout_info).context.hash


@pytest.mark.django_db(transaction=True)
@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_concurrent_change_first_payment_snapshots_new_data(
    mocked_initialize, managed_taxed_checkout, stripe_test_app
):
    # given - the address change holds the checkout lock and is paused
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED", "154.98")
    checkout = managed_taxed_checkout
    inside, go, results = threading.Event(), threading.Event(), {}
    with mock.patch(
        "saleor.graphql.checkout.mutations.checkout_billing_address_update."
        "change_billing_address_in_checkout",
        _paused(change_billing_address_in_checkout, inside, go),
    ):
        change = _in_thread(
            lambda: ApiClient(user=None).post_graphql(
                BILLING_UPDATE, {"id": _id(checkout), "address": DE_BILLING_NEW_STREET}
            ),
            results,
            "change",
        )
        assert inside.wait(THREAD_TIMEOUT)

        # when - payment starts while the change is still open
        payment = _in_thread(
            lambda: ApiClient(user=None).post_graphql(
                TRANSACTION_INITIALIZE,
                {
                    "id": _id(checkout),
                    "amount": None,
                    "paymentGateway": {"id": APP_IDENTIFIER, "data": None},
                },
            ),
            results,
            "payment",
        )
        payment.join(timeout=1)
        payment_waited_for_change = payment.is_alive()
        go.set()
        change.join(THREAD_TIMEOUT)
        payment.join(THREAD_TIMEOUT)

    # then - the payment waited and its snapshot is bound to the new address
    assert payment_waited_for_change is True
    change_data = get_graphql_content(results["change"])["data"]
    assert change_data["checkoutBillingAddressUpdate"]["errors"] == []
    payment_data = get_graphql_content(results["payment"])["data"]
    assert payment_data["transactionInitialize"]["errors"] == []
    checkout.refresh_from_db()
    assert (
        checkout.billing_address.street_address_1
        == (DE_BILLING_NEW_STREET["streetAddress1"])
    )
    lock = CheckoutMetadata.objects.get(checkout=checkout).private_metadata[
        maky_b2b.STATE_KEY
    ]["paymentLock"]
    assert lock["contextHash"] == _current_context_hash(checkout)


@pytest.mark.django_db(transaction=True)
@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_concurrent_payment_first_change_is_rejected(
    mocked_initialize, managed_taxed_checkout, stripe_test_app
):
    # given - transactionInitialize holds the checkout lock and is paused
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED", "154.98")
    checkout = managed_taxed_checkout
    street_before = checkout.billing_address.street_address_1
    context_hash_before = _current_context_hash(checkout)
    inside, go, results = threading.Event(), threading.Event(), {}
    with mock.patch(
        "saleor.checkout.maky_b2b_payment.fetch_checkout_data",
        _paused(fetch_checkout_data, inside, go),
    ):
        payment = _in_thread(
            lambda: ApiClient(user=None).post_graphql(
                TRANSACTION_INITIALIZE,
                {
                    "id": _id(checkout),
                    "amount": None,
                    "paymentGateway": {"id": APP_IDENTIFIER, "data": None},
                },
            ),
            results,
            "payment",
        )
        assert inside.wait(THREAD_TIMEOUT)

        # when - the address change arrives while the snapshot is being written
        change = _in_thread(
            lambda: ApiClient(user=None).post_graphql(
                BILLING_UPDATE, {"id": _id(checkout), "address": DE_BILLING_NEW_STREET}
            ),
            results,
            "change",
        )
        change.join(timeout=1)
        change_waited_for_payment = change.is_alive()
        go.set()
        payment.join(THREAD_TIMEOUT)
        change.join(THREAD_TIMEOUT)

    # then - the change waited for the lock and was rejected
    assert change_waited_for_payment is True
    payment_data = get_graphql_content(results["payment"])["data"]
    assert payment_data["transactionInitialize"]["errors"] == []
    errors = get_graphql_content(results["change"])["data"][
        "checkoutBillingAddressUpdate"
    ]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == CheckoutErrorCode.B2B_CHECKOUT_LOCKED.name
    checkout.refresh_from_db()
    assert checkout.billing_address.street_address_1 == street_before
    lock = CheckoutMetadata.objects.get(checkout=checkout).private_metadata[
        maky_b2b.STATE_KEY
    ]["paymentLock"]
    assert lock["contextHash"] == context_hash_before


@pytest.mark.django_db(transaction=True)
def test_guard_requires_the_transaction_of_the_change(maky_b2b_checkout):
    # when / then
    with pytest.raises(RuntimeError):
        maky_b2b.guard_context_change(maky_b2b_checkout.pk)


@pytest.mark.django_db(transaction=True)
@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_concurrent_payment_first_rollback_unmanage_is_blocked(
    mocked_initialize, managed_taxed_checkout, stripe_test_app, settings
):
    # given - rollback preparation; a payment holds the checkout lock and is paused
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED", "154.98")
    checkout = managed_taxed_checkout
    inside, go, results = threading.Event(), threading.Event(), {}
    with mock.patch(
        "saleor.checkout.maky_b2b_payment.fetch_checkout_data",
        _paused(fetch_checkout_data, inside, go),
    ):
        payment = _in_thread(
            lambda: ApiClient(user=None).post_graphql(
                TRANSACTION_INITIALIZE,
                {
                    "id": _id(checkout),
                    "amount": None,
                    "paymentGateway": {"id": APP_IDENTIFIER, "data": None},
                },
            ),
            results,
            "payment",
        )
        assert inside.wait(THREAD_TIMEOUT)

        # when - unmanaging the checkout arrives while the snapshot is written
        unmanage = _in_thread(
            lambda: maky_b2b_rollback.unmanage_checkout(checkout.pk),
            results,
            "unmanage",
        )
        unmanage.join(timeout=1)
        unmanage_waited_for_payment = unmanage.is_alive()
        go.set()
        payment.join(THREAD_TIMEOUT)
        unmanage.join(THREAD_TIMEOUT)

    # then - the checkout stays managed until its payment attempt is resolved
    assert unmanage_waited_for_payment is True
    payment_data = get_graphql_content(results["payment"])["data"]
    assert payment_data["transactionInitialize"]["errors"] == []
    assert results["unmanage"].status == maky_b2b_rollback.UnmanageStatus.BLOCKED
    assert results["unmanage"].attempts.status == maky_b2b.PaymentAttempts.UNRESOLVED
    metadata = CheckoutMetadata.objects.get(checkout=checkout)
    assert maky_b2b.STATE_KEY in metadata.private_metadata
    assert [item.checkout_pk for item in maky_b2b_rollback.inventory()] == [checkout.pk]


@mock.patch("saleor.plugins.manager.PluginsManager.transaction_initialize_session")
def test_drain_mode_after_unmanage_nothing_becomes_managed_again(
    mocked_initialize, exempt_checkout, stripe_test_app, app, api_client, settings
):
    # given - rollback preparation unmanaged the only B2B checkout
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = maky_b2b.MODE_DRAIN
    result = maky_b2b_rollback.unmanage_checkout(exempt_checkout.pk)
    assert result.status == maky_b2b_rollback.UnmanageStatus.UNMANAGED
    mocked_initialize.return_value = _session_result("CHARGE_ACTION_REQUIRED", "154.98")

    # when - the storefront sends a new attestation and the customer pays
    attestation = maky_b2b.apply_attestation(
        checkout_pk=exempt_checkout.pk,
        expected_context_version=maky_b2b_context_version(exempt_checkout),
        verification=maky_b2b_verification(),
        app=app,
    )
    payment = _initialize(api_client, exempt_checkout)

    # then - an ordinary taxed payment, the inventory stays empty
    assert payment["errors"] == []
    assert attestation.error_code is None
    exempt_checkout.refresh_from_db(fields=["tax_exemption"])
    assert exempt_checkout.tax_exemption is False
    assert TransactionItem.objects.get(
        checkout_id=exempt_checkout.pk
    ).psp_reference == ("pi_test_123")
    assert maky_b2b_rollback.inventory() == []
