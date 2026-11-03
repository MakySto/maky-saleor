from typing import Final

import graphene
from django.core.exceptions import ValidationError

from ....checkout import maky_b2b
from ....checkout.fetch import fetch_checkout_info, fetch_checkout_lines
from ....checkout.models import Checkout
from ....checkout.utils import invalidate_checkout
from ....core.tracing import traced_atomic_transaction
from ....graphql.core.mutations import BaseMutation
from ....order import ORDER_EDITABLE_STATUS
from ....order.models import Order
from ....permission.enums import CheckoutPermissions
from ....tax import error_codes
from ...app.dataloaders import get_app_promise
from ...core import ResolveInfo
from ...core.context import SyncWebhookControlContext
from ...core.doc_category import DOC_CATEGORY_TAXES
from ...core.types import Error
from ...core.types.taxes import TaxSourceObject
from ...plugins.dataloaders import get_plugin_manager_promise

TaxExemptionManageErrorCode: Final[graphene.Enum] = graphene.Enum.from_enum(
    error_codes.TaxExemptionManageErrorCode
)
TaxExemptionManageErrorCode.doc_category = DOC_CATEGORY_TAXES


class TaxExemptionManageError(Error):
    code = TaxExemptionManageErrorCode(description="The error code.", required=True)

    class Meta:
        doc_category = DOC_CATEGORY_TAXES


class TaxExemptionManage(BaseMutation):
    taxable_object = graphene.Field(TaxSourceObject)

    class Arguments:
        id = graphene.ID(
            description="ID of the Checkout or Order object.", required=True
        )
        tax_exemption = graphene.Boolean(
            description="Determines if a taxes should be exempt.", required=True
        )

    class Meta:
        description = (
            "Exempt checkout or order from charging the taxes. When tax exemption is "
            "enabled, taxes won't be charged for the checkout or order. Taxes may "
            "still be calculated in cases when product prices are entered with the "
            "tax included and the net price needs to be known."
        )
        doc_category = DOC_CATEGORY_TAXES
        error_type_class = TaxExemptionManageError
        permissions = (CheckoutPermissions.MANAGE_TAXES,)

    @classmethod
    def validate_input(cls, info: ResolveInfo, data):
        obj = cls.get_node_or_error(info, data["id"])
        if not isinstance(obj, Order | Checkout):
            code = error_codes.TaxExemptionManageErrorCode.NOT_FOUND.value
            message = "Invalid object ID. Only Checkout and Order ID's are accepted."
            raise ValidationError({"id": ValidationError(code=code, message=message)})

    @classmethod
    def validate_order_status(cls, order):
        if order.status not in ORDER_EDITABLE_STATUS:
            code = error_codes.TaxExemptionManageErrorCode.NOT_EDITABLE_ORDER.value
            message = (
                "Tax exemption can be manage only on orders in "
                f"{ORDER_EDITABLE_STATUS} statuses."
            )
            raise ValidationError(code=code, message=message)

    @classmethod
    def get_object(cls, info: ResolveInfo, object_global_id):
        obj = graphene.Node.get_node_from_global_id(info, object_global_id)
        return obj

    @classmethod
    def _invalidate_checkout(cls, info: ResolveInfo, checkout):
        manager = get_plugin_manager_promise(info.context).get()

        checkout_info = fetch_checkout_info(checkout, [], manager)
        lines_info, _ = fetch_checkout_lines(checkout)
        invalidate_checkout(
            checkout_info,
            lines_info,
            manager,
            save=False,
        )

    @classmethod
    def _handle_maky_b2b_checkout(cls, info: ResolveInfo, checkout, tax_exemption):
        """MAKY-B2B-V1: native flag on checkouts managed by the B2B workflow.

        Returns True when handled here. `true` cannot bypass context, attestation
        and backend mode; `false` also revokes the attestation, so the flag is
        not re-enabled by the workflow on the next read.
        """
        app = get_app_promise(info.context).get()
        result = maky_b2b.revoke_for_native_flag(
            checkout=checkout, tax_exemption=tax_exemption, app=app
        )
        if result == "unmanaged":
            return False
        if result == maky_b2b.B2bErrorCode.B2B_CHECKOUT_LOCKED:
            raise ValidationError(
                {
                    "id": ValidationError(
                        maky_b2b.B2bLockedError.message,
                        code=error_codes.TaxExemptionManageErrorCode.B2B_CHECKOUT_LOCKED.value,
                    )
                }
            )
        if result == maky_b2b.B2bErrorCode.B2B_MANAGED_CHECKOUT:
            raise ValidationError(
                {
                    "tax_exemption": ValidationError(
                        "Checkout is managed by the MAKY B2B workflow; use "
                        "checkoutB2bTaxAttestationUpdate.",
                        code=error_codes.TaxExemptionManageErrorCode.B2B_MANAGED_CHECKOUT.value,
                    )
                }
            )
        if result == maky_b2b.B2bErrorCode.NOT_FOUND:
            raise ValidationError(
                {
                    "id": ValidationError(
                        "Checkout not found.",
                        code=error_codes.TaxExemptionManageErrorCode.NOT_FOUND.value,
                    )
                }
            )
        checkout.refresh_from_db()
        return True

    @classmethod
    def perform_mutation(cls, _root, info: ResolveInfo, /, **data):
        cls.validate_input(info, data)
        obj = cls.get_object(info, data["id"])

        if isinstance(obj, Checkout):
            # MAKY-B2B-V1: the B2B check and the native write run under the same
            # checkout row lock, so an attestation cannot interleave.
            with traced_atomic_transaction():
                if not cls._handle_maky_b2b_checkout(info, obj, data["tax_exemption"]):
                    obj.tax_exemption = data["tax_exemption"]
                    cls._invalidate_checkout(info, obj)
                    obj.save(
                        update_fields=[
                            "tax_exemption",
                            "price_expiration",
                            "discount_expiration",
                            "last_change",
                        ]
                    )
            return TaxExemptionManage(
                taxable_object=SyncWebhookControlContext(node=obj)
            )

        obj.tax_exemption = data["tax_exemption"]

        if isinstance(obj, Order):
            cls.validate_order_status(obj)
            obj.should_refresh_prices = True
            obj.save(
                update_fields=["tax_exemption", "should_refresh_prices", "updated_at"]
            )

        return TaxExemptionManage(taxable_object=SyncWebhookControlContext(node=obj))
