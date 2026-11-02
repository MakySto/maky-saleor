from typing import cast

import graphene
from django.core.exceptions import ValidationError

from ....checkout import maky_b2b
from ....checkout.models import Checkout
from ....core import models
from ....core.error_codes import MetadataErrorCode
from ...core import ResolveInfo
from ...core.types import MetadataError, NonNullList
from ..inputs import MetadataInput, MetadataInputDescription
from ..permissions import PUBLIC_META_PERMISSION_MAP
from .base import BaseMetadataMutation
from .utils import (
    get_valid_metadata_instance,
    guard_maky_b2b_metadata_change,
    invalidate_checkout_prices_after_b2b_input_change,
    update_metadata,
)


class UpdateMetadata(BaseMetadataMutation):
    class Meta:
        description = (
            "Updates metadata of an object."
            f"{MetadataInputDescription.PRIVATE_METADATA_INPUT}"
        )
        permission_map = PUBLIC_META_PERMISSION_MAP
        error_type_class = MetadataError
        error_type_field = "metadata_errors"

    class Arguments:
        id = graphene.ID(
            description="ID or token (for Order and Checkout) of an object to update.",
            required=True,
        )
        input = NonNullList(
            MetadataInput,
            description="Fields required to update the object's metadata.",
            required=True,
        )

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, id: str, input: list
    ):
        instance = cast(models.ModelWithMetadata, cls.get_instance(info, id=id))
        if instance:
            cls.create_metadata_from_graphql_input(
                input, error_field_name="input", private=False
            )
            items = {data.key: data.value for data in input}
            if isinstance(instance, Checkout) and maky_b2b.INPUT_KEY in items:
                if not maky_b2b.validate_input_payload(items[maky_b2b.INPUT_KEY]):
                    raise ValidationError(
                        {
                            "input": ValidationError(
                                f"Invalid `{maky_b2b.INPUT_KEY}` payload.",
                                code=MetadataErrorCode.INVALID.value,
                            )
                        }
                    )
                guard_maky_b2b_metadata_change(instance)
            meta_instance = get_valid_metadata_instance(instance)
            update_metadata(meta_instance, items)
            if isinstance(instance, Checkout) and maky_b2b.INPUT_KEY in items:
                invalidate_checkout_prices_after_b2b_input_change(instance)

        return cls.success_response(instance)
