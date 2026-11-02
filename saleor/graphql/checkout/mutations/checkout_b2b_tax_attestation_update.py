"""MAKY-B2B-V1: atomic B2B tax attestation write (fork-only mutation)."""

from typing import Final

import graphene
from django.core.exceptions import ValidationError

from ....checkout import maky_b2b
from ....checkout.error_codes import CheckoutB2bTaxAttestationErrorCode
from ....core.exceptions import PermissionDenied
from ....permission.enums import CheckoutPermissions
from ...app.dataloaders import get_app_promise
from ...core import ResolveInfo
from ...core.context import SyncWebhookControlContext
from ...core.doc_category import DOC_CATEGORY_CHECKOUT
from ...core.mutations import BaseMutation
from ...core.scalars import DateTime
from ...core.types import Error
from ..maky_b2b_types import B2bVatVerificationStatusEnum
from ..types import Checkout
from .utils import get_checkout

MAKY_B2B_NOTE = " MAKY fork only (contract MAKY-B2B-V1)."

CheckoutB2bTaxAttestationErrorCodeEnum: Final[graphene.Enum] = graphene.Enum.from_enum(
    CheckoutB2bTaxAttestationErrorCode
)
CheckoutB2bTaxAttestationErrorCodeEnum.doc_category = DOC_CATEGORY_CHECKOUT


class CheckoutB2bTaxAttestationError(Error):
    code = CheckoutB2bTaxAttestationErrorCodeEnum(
        description="The error code.", required=True
    )

    class Meta:
        doc_category = DOC_CATEGORY_CHECKOUT


class CheckoutB2bVatVerificationInput(graphene.InputObjectType):
    vat_id = graphene.String(
        required=True,
        description="Full normalized VAT ID including the country prefix.",
    )
    status = B2bVatVerificationStatusEnum(
        required=True, description="Result of the VIES verification."
    )
    checked_at = DateTime(
        required=True,
        description="Original time of the VIES check. Reusing a cached result "
        "must keep its original time.",
    )
    request_identifier = graphene.String(
        description="VIES request identifier, when the service returned one."
    )
    name = graphene.String(description="Trader name returned by VIES.")
    address = graphene.String(description="Trader address returned by VIES.")

    class Meta:
        doc_category = DOC_CATEGORY_CHECKOUT
        description = "Result of a VIES verification." + MAKY_B2B_NOTE


class CheckoutB2bTaxAttestationInput(graphene.InputObjectType):
    expected_context_version = graphene.String(
        required=True,
        description="`checkout.b2bTaxState.contextVersion` read before the VIES call.",
    )
    verification = graphene.Field(
        CheckoutB2bVatVerificationInput,
        description="Verification result. `null` revokes any previous approval.",
    )

    class Meta:
        doc_category = DOC_CATEGORY_CHECKOUT
        description = "B2B tax attestation for a checkout." + MAKY_B2B_NOTE


class CheckoutB2bTaxAttestationUpdate(BaseMutation):
    checkout = graphene.Field(Checkout, description="An updated checkout.")

    class Arguments:
        id = graphene.ID(required=True, description="The checkout's ID.")
        input = CheckoutB2bTaxAttestationInput(
            required=True, description="Attestation to store."
        )

    class Meta:
        description = (
            "Atomically stores a B2B VAT verification for the current checkout "
            "context, decides the § 43 exemption, sets the native tax exemption "
            "and invalidates prices. Requires an active app with both "
            "MANAGE_TAXES and HANDLE_TAXES." + MAKY_B2B_NOTE
        )
        doc_category = DOC_CATEGORY_CHECKOUT
        error_type_class = CheckoutB2bTaxAttestationError
        # Not declared in `permissions`: Saleor treats that list as OR and
        # documents it so. This mutation requires an app with BOTH permissions.

    REQUIRED_PERMISSIONS = (
        CheckoutPermissions.MANAGE_TAXES,
        CheckoutPermissions.HANDLE_TAXES,
    )

    @classmethod
    def check_permissions(
        cls, context, permissions=None, require_all_permissions=False, **data
    ):
        app = get_app_promise(context).get()
        if not app or not app.is_active:
            return False
        return super().check_permissions(
            context, cls.REQUIRED_PERMISSIONS, require_all_permissions=True, **data
        )

    @classmethod
    def mutate(cls, root, info: ResolveInfo, **data):
        if not cls.check_permissions(info.context):
            raise PermissionDenied(permissions=list(cls.REQUIRED_PERMISSIONS))
        return super().mutate(root, info, **data)

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, id, input
    ):
        app = get_app_promise(info.context).get()
        try:
            checkout = get_checkout(cls, info, id=id)
        except ValidationError as e:
            raise ValidationError(
                {
                    "id": ValidationError(
                        "Checkout not found.",
                        code=CheckoutB2bTaxAttestationErrorCode.NOT_FOUND.value,
                    )
                }
            ) from e
        verification = input.get("verification")
        result = maky_b2b.apply_attestation(
            checkout_pk=checkout.pk,
            expected_context_version=input["expected_context_version"],
            verification=dict(verification) if verification is not None else None,
            app=app,
        )
        if result.error_code:
            raise ValidationError(
                {
                    result.error_field or "id": ValidationError(
                        result.error_message or "", code=result.error_code
                    )
                }
            )
        checkout.refresh_from_db()
        return CheckoutB2bTaxAttestationUpdate(
            checkout=SyncWebhookControlContext(node=checkout)
        )
