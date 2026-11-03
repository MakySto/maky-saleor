"""MAKY-B2B-V1 GraphQL types (fork only)."""

from typing import Final

import graphene

from ...checkout import maky_b2b, maky_b2b_payment
from ..core.doc_category import DOC_CATEGORY_CHECKOUT
from ..core.scalars import DateTime
from ..core.types import BaseObjectType


class _VerificationStatus(graphene.Enum):
    VALID = maky_b2b.VerificationStatus.VALID
    INVALID = maky_b2b.VerificationStatus.INVALID
    UNAVAILABLE = maky_b2b.VerificationStatus.UNAVAILABLE

    class Meta:
        name = "B2bVatVerificationStatus"
        description = "Result of a VIES VAT verification."
        doc_category = DOC_CATEGORY_CHECKOUT


B2bVatVerificationStatusEnum: Final = _VerificationStatus


B2bTaxDecisionEnum: Final[graphene.Enum] = graphene.Enum(
    "B2bTaxDecision",
    [(choice, choice) for choice in maky_b2b.Decision.CHOICES],
    description=(
        "Decision of the MAKY automatic B2B § 43 workflow. `TAXED_*` means the "
        "automatic exemption is not used and the regular Saleor calculation "
        "applies."
    ),
)
B2bTaxDecisionEnum.doc_category = DOC_CATEGORY_CHECKOUT


B2bPaymentLockReleaseStatusEnum: Final[graphene.Enum] = graphene.Enum(
    "B2bPaymentLockReleaseStatus",
    [(choice, choice) for choice in maky_b2b_payment.ReleaseStatus.CHOICES],
    description=(
        "Result of `checkoutB2bPaymentLockRelease`. `RELEASED`: no attempt can "
        "take money; changes are allowed. `CANCELATION_REQUESTED`: wait until "
        "`b2bTaxState.paymentLocked` is false. `ATTEMPT_BEING_CREATED`: a "
        "`transactionInitialize` is still running; retry shortly. "
        "`PAYMENT_HAS_FUNDS`: money is authorized or charged; finish the "
        "started payment."
    ),
)
B2bPaymentLockReleaseStatusEnum.doc_category = DOC_CATEGORY_CHECKOUT


class B2bTaxState(BaseObjectType):
    context_version = graphene.String(
        required=True,
        description="Opaque version of the current B2B context and revision. "
        "Pass it unchanged as `expectedContextVersion`.",
    )
    decision = B2bTaxDecisionEnum(required=True, description="Current decision.")
    effective_tax_exemption = graphene.Boolean(
        required=True,
        description="Whether the MAKY B2B § 43 exemption is effective for the "
        "prices of this checkout.",
    )
    checked_at = DateTime(description="Original time of the stored VIES check.")
    expires_at = DateTime(description="Time when a VALID check stops being usable.")
    payment_locked = graphene.Boolean(
        required=True,
        description="True while a payment attempt of the checkout holds money "
        "or may still take money (time alone never ends it). A new attestation "
        "is rejected; on a checkout with a B2B payment snapshot, changes of "
        "company/VAT input, addresses, delivery, lines and promo codes are "
        "rejected too. Use `checkoutB2bPaymentLockRelease` to abandon attempts "
        "without money.",
    )

    class Meta:
        description = "Summary of the B2B tax state. MAKY fork only (MAKY-B2B-V1)."
        doc_category = DOC_CATEGORY_CHECKOUT


def evaluation_to_b2b_tax_state(evaluation: "maky_b2b.Evaluation") -> dict:
    attestation = evaluation.state.attestation or {}
    return {
        "context_version": evaluation.context_version,
        "decision": evaluation.decision,
        "effective_tax_exemption": evaluation.effective_exemption
        if evaluation.managed
        else False,
        "checked_at": maky_b2b.parse_dt(attestation.get("checkedAt")),
        "expires_at": maky_b2b.parse_dt(attestation.get("expiresAt")),
        "payment_locked": evaluation.payment_locked,
    }
