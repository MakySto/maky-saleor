import json
from decimal import Decimal

import pytest
from django.utils import timezone

from ....account.models import Address
from ....plugins.manager import get_plugins_manager
from ....product.models import ProductVariantChannelListing
from ....shipping.models import ShippingMethodChannelListing
from ....tax import TaxCalculationStrategy
from ....tax.models import TaxClassCountryRate
from ... import maky_b2b
from ...fetch import fetch_checkout_info, fetch_checkout_lines
from ...models import CheckoutMetadata
from ..utils import add_variant_to_checkout

MAKY_B2B_VAT_ID = "DE123456789"
MAKY_B2B_COMPANY = "Muster Handel GmbH"


def maky_b2b_input(**overrides) -> str:
    data = {
        "schema": 1,
        "isBusiness": True,
        "registrationId": "HRB 12345",
        "taxId": "",
        "vatId": MAKY_B2B_VAT_ID,
    }
    data.update(overrides)
    return json.dumps(data)


def maky_b2b_address(country="DE", company_name=MAKY_B2B_COMPANY, **overrides):
    data = {
        "first_name": "Max",
        "last_name": "Muster",
        "company_name": company_name,
        "street_address_1": "Hauptstrasse 1",
        "city": "BERLIN",
        "postal_code": "10115",
        "country": country,
    }
    if country == "SK":
        data.update({"city": "BRATISLAVA", "postal_code": "811 01"})
    if country == "AT":
        data.update({"city": "WIEN", "postal_code": "1010"})
    if country == "US":
        data.update(
            {
                "city": "NEW YORK",
                "postal_code": "10001",
                "country_area": "NY",
            }
        )
    data.update(overrides)
    return Address.objects.create(**data)


def maky_b2b_context_version(checkout) -> str:
    manager = get_plugins_manager(allow_replica=False)
    lines, _ = fetch_checkout_lines(checkout)
    checkout_info = fetch_checkout_info(checkout, lines, manager)
    return maky_b2b.evaluate_checkout_info(checkout_info).context_version


def maky_b2b_verification(**overrides) -> dict:
    data = {
        "vat_id": MAKY_B2B_VAT_ID,
        "status": maky_b2b.VerificationStatus.VALID,
        "checked_at": timezone.now(),
        "request_identifier": "WAPIAAAAZ1a2b3c4",
        "name": "---",
        "address": "---",
    }
    data.update(overrides)
    return data


@pytest.fixture
def maky_b2b_settings(settings):
    settings.MAKY_B2B_VAT_EXEMPTION_MODE = "on"
    return settings


@pytest.fixture
def maky_b2b_tax_configuration(channel_USD, default_tax_class):
    """MAKY production-like: FLAT_RATES, prices with VAT, 23 % everywhere."""
    tax_configuration = channel_USD.tax_configuration
    tax_configuration.country_exceptions.all().delete()
    tax_configuration.tax_calculation_strategy = TaxCalculationStrategy.FLAT_RATES
    tax_configuration.prices_entered_with_tax = True
    tax_configuration.charge_taxes = True
    tax_configuration.tax_app_id = None
    tax_configuration.save()
    TaxClassCountryRate.objects.filter(tax_class=default_tax_class).delete()
    TaxClassCountryRate.objects.bulk_create(
        [
            TaxClassCountryRate(country=country, rate=23, tax_class=default_tax_class)
            for country in ("SK", "DE", "AT")
        ]
    )
    return tax_configuration


@pytest.fixture
def maky_b2b_checkout(
    maky_b2b_settings,
    maky_b2b_tax_configuration,
    checkout,
    product,
    checkout_delivery,
    shipping_zone,
):
    """DE company checkout: 148.99 goods + 5.99 shipping, prices with 23 % VAT."""
    variant = product.variants.first()
    ProductVariantChannelListing.objects.filter(
        variant=variant, channel=checkout.channel
    ).update(
        price_amount=Decimal("148.99"),
        discounted_price_amount=Decimal("148.99"),
        prior_price_amount=None,
    )
    shipping_method = shipping_zone.shipping_methods.first()
    ShippingMethodChannelListing.objects.filter(
        shipping_method=shipping_method, channel=checkout.channel
    ).update(price_amount=Decimal("5.99"))

    manager = get_plugins_manager(allow_replica=False)
    checkout_info = fetch_checkout_info(checkout, [], manager)
    add_variant_to_checkout(checkout_info, variant, 1)

    checkout.billing_address = maky_b2b_address()
    checkout.shipping_address = maky_b2b_address()
    checkout.assigned_delivery = checkout_delivery(checkout, shipping_method)
    checkout.price_expiration = timezone.now()
    checkout.save()
    metadata, _ = CheckoutMetadata.objects.get_or_create(checkout=checkout)
    metadata.metadata = {maky_b2b.INPUT_KEY: maky_b2b_input()}
    metadata.save(update_fields=["metadata"])
    checkout.refresh_from_db()
    return checkout
