"""MAKY-B2B-V1: recovery from a started payment (fork-only mutation)."""

from typing import Final

import graphene
from django.core.exceptions import ValidationError

from ....checkout import maky_b2b_payment
from ....checkout.error_codes import CheckoutB2bPaymentLockReleaseErrorCode
from ...app.dataloaders import get_app_promise
from ...core import ResolveInfo
from ...core.context import SyncWebhookControlContext
from ...core.doc_category import DOC_CATEGORY_CHECKOUT
from ...core.mutations import BaseMutation
from ...core.types import Error
from ...plugins.dataloaders import get_plugin_manager_promise
from ..maky_b2b_types import B2bPaymentLockReleaseStatusEnum
from ..types import Checkout
from .checkout_b2b_tax_attestation_update import (
    MAKY_B2B_NOTE,
    MakyB2bAppMutationMixin,
)
from .utils import get_checkout

CheckoutB2bPaymentLockReleaseErrorCodeEnum: Final[graphene.Enum] = (
    graphene.Enum.from_enum(CheckoutB2bPaymentLockReleaseErrorCode)
)
CheckoutB2bPaymentLockReleaseErrorCodeEnum.doc_category = DOC_CATEGORY_CHECKOUT


class CheckoutB2bPaymentLockReleaseError(Error):
    code = CheckoutB2bPaymentLockReleaseErrorCodeEnum(
        description="The error code.", required=True
    )

    class Meta:
        doc_category = DOC_CATEGORY_CHECKOUT


class CheckoutB2bPaymentLockRelease(MakyB2bAppMutationMixin, BaseMutation):
    checkout = graphene.Field(Checkout, description="The checkout.")
    status = B2bPaymentLockReleaseStatusEnum(
        description="Result of the release request."
    )

    class Arguments:
        id = graphene.ID(required=True, description="The checkout's ID.")

    class Meta:
        description = (
            "Recovery when the customer abandons a started payment to change "
            "checkout data. Payment attempts holding money are never touched. "
            "Attempts without money that may still succeed are asked to cancel "
            "through their payment app; `b2bTaxState.paymentLocked` turns false "
            "once the app confirms the cancelation. Idempotent; requires an "
            "active app with both MANAGE_TAXES and HANDLE_TAXES." + MAKY_B2B_NOTE
        )
        doc_category = DOC_CATEGORY_CHECKOUT
        error_type_class = CheckoutB2bPaymentLockReleaseError

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, id
    ):
        app = get_app_promise(info.context).get()
        try:
            checkout = get_checkout(cls, info, id=id)
        except ValidationError as e:
            raise ValidationError(
                {
                    "id": ValidationError(
                        "Checkout not found.",
                        code=CheckoutB2bPaymentLockReleaseErrorCode.NOT_FOUND.value,
                    )
                }
            ) from e
        manager = get_plugin_manager_promise(info.context).get()
        result = maky_b2b_payment.release_payment_attempts(
            checkout_pk=checkout.pk, manager=manager, app=app
        )
        if result.checkout is None:
            raise ValidationError(
                {
                    "id": ValidationError(
                        "Checkout not found.",
                        code=CheckoutB2bPaymentLockReleaseErrorCode.NOT_FOUND.value,
                    )
                }
            )
        checkout.refresh_from_db()
        return CheckoutB2bPaymentLockRelease(
            checkout=SyncWebhookControlContext(node=checkout), status=result.status
        )
