import json
from decimal import Decimal

import graphene
import pytest
from django.utils import timezone

from .....checkout import maky_b2b
from .....checkout.error_codes import (
    CheckoutB2bTaxAttestationErrorCode,
    CheckoutErrorCode,
)
from .....checkout.models import CheckoutMetadata
from .....checkout.tests.fixtures.maky_b2b import (
    MAKY_B2B_VAT_ID,
    maky_b2b_input,
    maky_b2b_verification,
)
from .....core.error_codes import MetadataErrorCode
from .....tax.error_codes import TaxExemptionManageErrorCode
from ....tests.utils import assert_no_permission, get_graphql_content

B2B_STATE_QUERY = """
query Checkout($id: ID!) {
  checkout(id: $id) {
    taxExemption
    b2bTaxState {
      contextVersion
      decision
      effectiveTaxExemption
      checkedAt
      expiresAt
      paymentLocked
    }
    totalPrice { net { amount } tax { amount } gross { amount currency } }
  }
}
"""

APPLY_MUTATION = """
mutation ApplyB2bVerification($id: ID!, $input: CheckoutB2bTaxAttestationInput!) {
  checkoutB2bTaxAttestationUpdate(id: $id, input: $input) {
    errors { field code message }
    checkout {
      id
      b2bTaxState {
        contextVersion
        decision
        effectiveTaxExemption
        checkedAt
        expiresAt
      }
      totalPrice {
        net { amount currency }
        tax { amount currency }
        gross { amount currency }
      }
    }
  }
}
"""


def _checkout_id(checkout):
    return graphene.Node.to_global_id("Checkout", checkout.token)


def _state(api_client, checkout):
    content = get_graphql_content(
        api_client.post_graphql(B2B_STATE_QUERY, {"id": _checkout_id(checkout)})
    )
    return content["data"]["checkout"]


def _verification_input(**overrides):
    data = maky_b2b_verification(**overrides)
    return {
        "vatId": data["vat_id"],
        "status": data["status"],
        "checkedAt": data["checked_at"].isoformat(),
        "requestIdentifier": data["request_identifier"],
        "name": data["name"],
        "address": data["address"],
    }


def _apply(app_api_client, checkout, permissions, version, verification="default"):
    variables = {
        "id": _checkout_id(checkout),
        "input": {
            "expectedContextVersion": version,
            "verification": _verification_input()
            if verification == "default"
            else verification,
        },
    }
    return app_api_client.post_graphql(
        APPLY_MUTATION, variables, permissions=permissions
    )


@pytest.fixture
def b2b_permissions(permission_manage_taxes, permission_handle_taxes):
    return [permission_manage_taxes, permission_handle_taxes]


def test_b2b_tax_state_query_for_anonymous_customer(api_client, maky_b2b_checkout):
    # when
    data = _state(api_client, maky_b2b_checkout)

    # then
    assert data["b2bTaxState"]["decision"] == maky_b2b.Decision.TAXED_UNVERIFIED
    assert data["b2bTaxState"]["effectiveTaxExemption"] is False
    assert data["b2bTaxState"]["paymentLocked"] is False
    assert data["b2bTaxState"]["checkedAt"] is None
    assert data["totalPrice"]["gross"]["amount"] == 154.98
    assert data["totalPrice"]["tax"]["amount"] == 28.98


def test_apply_valid_attestation(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]

    # when
    response = _apply(app_api_client, maky_b2b_checkout, b2b_permissions, version)

    # then
    data = get_graphql_content(response)["data"]["checkoutB2bTaxAttestationUpdate"]
    assert data["errors"] == []
    state = data["checkout"]["b2bTaxState"]
    assert state["decision"] == maky_b2b.Decision.EXEMPT
    assert state["effectiveTaxExemption"] is True
    assert state["contextVersion"] != version
    assert state["expiresAt"] is not None
    total = data["checkout"]["totalPrice"]
    assert total["gross"]["amount"] == 126.0
    assert total["net"]["amount"] == 126.0
    assert total["tax"]["amount"] == 0.0
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is True


@pytest.mark.parametrize("which", ["manage", "handle"])
def test_apply_requires_both_permissions(
    api_client,
    app_api_client,
    maky_b2b_checkout,
    permission_manage_taxes,
    permission_handle_taxes,
    which,
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    permission = (
        permission_manage_taxes if which == "manage" else permission_handle_taxes
    )

    # when
    response = _apply(app_api_client, maky_b2b_checkout, [permission], version)

    # then
    assert_no_permission(response)
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    assert maky_b2b.STATE_KEY not in metadata.private_metadata


def test_apply_by_staff_user_is_denied(
    api_client, staff_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]

    # when
    response = _apply(staff_api_client, maky_b2b_checkout, b2b_permissions, version)

    # then
    assert_no_permission(response)
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False


def test_apply_with_stale_version(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    maky_b2b_checkout.shipping_address.street_address_1 = "Neue Strasse 9"
    maky_b2b_checkout.shipping_address.save()

    # when
    response = _apply(app_api_client, maky_b2b_checkout, b2b_permissions, version)

    # then
    data = get_graphql_content(response)["data"]["checkoutB2bTaxAttestationUpdate"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == (
        CheckoutB2bTaxAttestationErrorCode.STALE_CONTEXT.name
    )
    assert data["errors"][0]["field"] == "expectedContextVersion"
    assert data["errors"][0]["message"] == (
        "Checkout context changed. Reload b2bTaxState and retry."
    )
    assert data["checkout"] is None
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False


def test_apply_with_not_existing_checkout(app_api_client, b2b_permissions):
    # given
    variables = {
        "id": graphene.Node.to_global_id(
            "Checkout", "e8f5a1ac-1f4f-4f1b-9e8c-1b2a3c4d5e6f"
        ),
        "input": {"expectedContextVersion": "x", "verification": None},
    }

    # when
    response = app_api_client.post_graphql(
        APPLY_MUTATION, variables, permissions=b2b_permissions
    )

    # then
    data = get_graphql_content(response)["data"]["checkoutB2bTaxAttestationUpdate"]
    assert len(data["errors"]) == 1
    assert (
        data["errors"][0]["code"] == CheckoutB2bTaxAttestationErrorCode.NOT_FOUND.name
    )


def test_public_metadata_cannot_fake_valid_state(
    api_client, maky_b2b_checkout, permission_handle_taxes
):
    # given
    mutation = """
    mutation($id: ID!, $input: [MetadataInput!]!) {
      updateMetadata(id: $id, input: $input) { errors { field code message } }
    }
    """
    variables = {
        "id": _checkout_id(maky_b2b_checkout),
        "input": [
            {"key": maky_b2b.STATE_KEY, "value": json.dumps({"attestation": "VALID"})}
        ],
    }

    # when
    response = api_client.post_graphql(mutation, variables)

    # then
    errors = get_graphql_content(response)["data"]["updateMetadata"]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == MetadataErrorCode.INVALID.name
    assert errors[0]["field"] == "input"
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    assert maky_b2b.STATE_KEY not in metadata.metadata


@pytest.mark.parametrize(
    "mutation_name", ["updatePrivateMetadata", "deletePrivateMetadata"]
)
def test_generic_private_metadata_cannot_touch_reserved_keys(
    api_client,
    app_api_client,
    maky_b2b_checkout,
    b2b_permissions,
    mutation_name,
):
    # given - managed EXEMPT checkout
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    _apply(app_api_client, maky_b2b_checkout, b2b_permissions, version)
    if mutation_name == "updatePrivateMetadata":
        mutation = """
        mutation($id: ID!, $input: [MetadataInput!]!) {
          updatePrivateMetadata(id: $id, input: $input) {
            errors { field code message }
          }
        }
        """
        variables = {
            "id": _checkout_id(maky_b2b_checkout),
            "input": [{"key": maky_b2b.STATE_KEY, "value": "{}"}],
        }
        field = "input"
    else:
        mutation = """
        mutation($id: ID!, $keys: [String!]!) {
          deletePrivateMetadata(id: $id, keys: $keys) {
            errors { field code message }
          }
        }
        """
        variables = {
            "id": _checkout_id(maky_b2b_checkout),
            "keys": [maky_b2b.STATE_KEY],
        }
        field = "keys"
    state_before = CheckoutMetadata.objects.get(
        checkout=maky_b2b_checkout
    ).private_metadata[maky_b2b.STATE_KEY]

    # when
    # the app already holds HANDLE_TAXES, which grants checkout private metadata
    response = app_api_client.post_graphql(mutation, variables)

    # then
    errors = get_graphql_content(response)["data"][mutation_name]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == MetadataErrorCode.INVALID.name
    assert errors[0]["field"] == field
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    assert metadata.private_metadata[maky_b2b.STATE_KEY] == state_before


def test_public_input_change_invalidates_cached_exemption(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    _apply(app_api_client, maky_b2b_checkout, b2b_permissions, version)
    assert _state(api_client, maky_b2b_checkout)["totalPrice"]["gross"]["amount"] == 126
    mutation = """
    mutation($id: ID!, $input: [MetadataInput!]!) {
      updateMetadata(id: $id, input: $input) { errors { field code message } }
    }
    """
    variables = {
        "id": _checkout_id(maky_b2b_checkout),
        "input": [
            {"key": maky_b2b.INPUT_KEY, "value": maky_b2b_input(vatId="DE111111111")}
        ],
    }

    # when
    response = api_client.post_graphql(mutation, variables)

    # then
    assert get_graphql_content(response)["data"]["updateMetadata"]["errors"] == []
    state = _state(api_client, maky_b2b_checkout)
    assert state["b2bTaxState"]["decision"] == maky_b2b.Decision.TAXED_STALE
    assert state["b2bTaxState"]["effectiveTaxExemption"] is False
    assert state["totalPrice"]["gross"]["amount"] == 154.98


def test_public_input_with_invalid_payload_is_rejected(api_client, maky_b2b_checkout):
    # given
    mutation = """
    mutation($id: ID!, $input: [MetadataInput!]!) {
      updateMetadata(id: $id, input: $input) { errors { field code message } }
    }
    """
    variables = {
        "id": _checkout_id(maky_b2b_checkout),
        "input": [{"key": maky_b2b.INPUT_KEY, "value": '{"isBusiness": "true"}'}],
    }

    # when
    response = api_client.post_graphql(mutation, variables)

    # then
    errors = get_graphql_content(response)["data"]["updateMetadata"]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == MetadataErrorCode.INVALID.name
    assert errors[0]["message"] == f"Invalid `{maky_b2b.INPUT_KEY}` payload."


TAX_EXEMPTION_MUTATION = """
mutation($id: ID!, $taxExemption: Boolean!) {
  taxExemptionManage(id: $id, taxExemption: $taxExemption) {
    errors { field code message }
  }
}
"""


def test_native_true_cannot_bypass_attestation_on_managed_checkout(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given - managed, revoked (TAXED_UNVERIFIED)
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    _apply(app_api_client, maky_b2b_checkout, b2b_permissions, version, None)

    # when
    response = app_api_client.post_graphql(
        TAX_EXEMPTION_MUTATION,
        {"id": _checkout_id(maky_b2b_checkout), "taxExemption": True},
    )

    # then
    errors = get_graphql_content(response)["data"]["taxExemptionManage"]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == TaxExemptionManageErrorCode.B2B_MANAGED_CHECKOUT.name
    maky_b2b_checkout.refresh_from_db()
    assert maky_b2b_checkout.tax_exemption is False


def test_native_false_revokes_attestation(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    _apply(app_api_client, maky_b2b_checkout, b2b_permissions, version)

    # when
    response = app_api_client.post_graphql(
        TAX_EXEMPTION_MUTATION,
        {"id": _checkout_id(maky_b2b_checkout), "taxExemption": False},
    )

    # then
    assert get_graphql_content(response)["data"]["taxExemptionManage"]["errors"] == []
    state = _state(api_client, maky_b2b_checkout)
    assert state["taxExemption"] is False
    assert state["b2bTaxState"]["decision"] == maky_b2b.Decision.TAXED_UNVERIFIED
    assert state["totalPrice"]["gross"]["amount"] == 154.98
    metadata = CheckoutMetadata.objects.get(checkout=maky_b2b_checkout)
    assert metadata.private_metadata[maky_b2b.STATE_KEY]["attestation"] is None


def test_checkout_complete_rejects_b2b_input_in_metadata(api_client, maky_b2b_checkout):
    # given
    mutation = """
    mutation($id: ID!, $metadata: [MetadataInput!]) {
      checkoutComplete(id: $id, metadata: $metadata) {
        errors { field code message }
      }
    }
    """
    variables = {
        "id": _checkout_id(maky_b2b_checkout),
        "metadata": [{"key": maky_b2b.INPUT_KEY, "value": maky_b2b_input()}],
    }

    # when
    response = api_client.post_graphql(mutation, variables)

    # then
    errors = get_graphql_content(response)["data"]["checkoutComplete"]["errors"]
    assert len(errors) == 1
    assert errors[0]["code"] == CheckoutErrorCode.INVALID.name
    assert errors[0]["field"] == "metadata"


def test_vat_id_must_match_checkout_input(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    verification = _verification_input(vat_id="DE987654321")

    # when
    response = _apply(
        app_api_client, maky_b2b_checkout, b2b_permissions, version, verification
    )

    # then
    data = get_graphql_content(response)["data"]["checkoutB2bTaxAttestationUpdate"]
    assert len(data["errors"]) == 1
    assert data["errors"][0]["code"] == (
        CheckoutB2bTaxAttestationErrorCode.INVALID_ATTESTATION.name
    )
    assert data["errors"][0]["field"] == "verification.vatId"
    assert MAKY_B2B_VAT_ID != verification["vatId"]


def test_expired_check_is_not_reported_as_effective(
    api_client, app_api_client, maky_b2b_checkout, b2b_permissions
):
    # given
    version = _state(api_client, maky_b2b_checkout)["b2bTaxState"]["contextVersion"]
    verification = _verification_input(
        checked_at=timezone.now() - timezone.timedelta(hours=25)
    )

    # when
    response = _apply(
        app_api_client, maky_b2b_checkout, b2b_permissions, version, verification
    )

    # then
    data = get_graphql_content(response)["data"]["checkoutB2bTaxAttestationUpdate"]
    assert data["errors"] == []
    assert data["checkout"]["b2bTaxState"]["decision"] == maky_b2b.Decision.TAXED_STALE
    assert data["checkout"]["totalPrice"]["gross"]["amount"] == 154.98
    assert Decimal(str(data["checkout"]["totalPrice"]["tax"]["amount"])) == Decimal(
        "28.98"
    )
