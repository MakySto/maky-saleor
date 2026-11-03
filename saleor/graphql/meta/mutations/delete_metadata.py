from typing import cast

import graphene

from ....checkout import maky_b2b
from ....checkout.models import Checkout
from ....core import models
from ...core import ResolveInfo
from ...core.mutations import validate_maky_b2b_metadata_keys
from ...core.types import MetadataError, NonNullList
from ..permissions import PUBLIC_META_PERMISSION_MAP
from .base import BaseMetadataMutation
from .utils import (
    delete_metadata_keys,
    get_valid_metadata_instance,
    invalidate_checkout_prices_after_b2b_input_change,
    maky_b2b_guarded_metadata_change,
)


class DeleteMetadata(BaseMetadataMutation):
    class Meta:
        description = (
            "Delete metadata of an object. To use it, you need to have access to the "
            "modified object."
        )
        permission_map = PUBLIC_META_PERMISSION_MAP
        error_type_class = MetadataError
        error_type_field = "metadata_errors"

    class Arguments:
        id = graphene.ID(
            description="ID or token (for Order and Checkout) of an object to update.",
            required=True,
        )
        keys = NonNullList(
            graphene.String,
            description="Metadata keys to delete.",
            required=True,
        )

    @classmethod
    def perform_mutation(  # type: ignore[override]
        cls, _root, info: ResolveInfo, /, *, id: str, keys: list[str]
    ):
        validate_maky_b2b_metadata_keys(keys, private=False, field="keys")
        instance = cast(models.ModelWithMetadata, cls.get_instance(info, id=id))
        if instance:
            b2b_checkout = (
                instance
                if isinstance(instance, Checkout) and maky_b2b.INPUT_KEY in keys
                else None
            )
            with maky_b2b_guarded_metadata_change(b2b_checkout):
                meta_instance = get_valid_metadata_instance(instance)
                delete_metadata_keys(meta_instance, keys)
                if b2b_checkout is not None:
                    invalidate_checkout_prices_after_b2b_input_change(b2b_checkout)
        return cls.success_response(instance)
