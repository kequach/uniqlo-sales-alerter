"""Pydantic models for Uniqlo API responses and application-level sale items."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field, field_validator

_DEFAULT_CURRENCY = "€"
_LOW_STOCK_STATUS = "LOW_STOCK"
_ALPHA_PREFIX_RE = re.compile(r"^[A-Z]+")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def is_low_stock(qty: int, status: str, threshold: int) -> bool:
    """Return *True* when a variant should be treated as low stock.

    The user-specified *threshold* is authoritative when positive: a
    variant is "low" iff its remaining quantity is positive and at or
    below *threshold*, irrespective of the API's own ``LOW_STOCK`` flag.
    This prevents the Uniqlo backend (which uses an undocumented internal
    threshold) from overriding an explicit user preference such as
    "alert me only when fewer than 5 are left".

    When *threshold* is ``0`` (i.e. the user has explicitly disabled the
    numeric comparison), the API's ``statusCode == "LOW_STOCK"`` flag is
    used as the sole signal.  Unknown stock (``qty <= 0``) is never
    treated as low so missing data can't spuriously fire alerts.
    """
    if threshold > 0:
        return 0 < qty <= threshold
    return status == _LOW_STOCK_STATUS


# ---------------------------------------------------------------------------
# Variant types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StockVariant:
    """A single colour+size variant with verified stock data.

    Replaces the raw tuple formerly returned by stock-verification helpers,
    giving each field a readable name.
    """

    color_display_code: str
    size_display_code: str
    color_name: str
    quantity: int
    status: str
    color_code: str
    size_code: str


def parse_variant_codes(url: str) -> tuple[str, str]:
    """Extract ``(color, size)`` display codes from a Uniqlo product URL.

    Handles both URL styles:

    * ``colorDisplayCode``/``sizeDisplayCode`` — returned as-is.
    * ``colorCode``/``sizeCode`` — alphabetic prefix stripped so the
      returned values are always short display-code form.
    """
    params = parse_qs(urlparse(url).query)

    color = params.get("colorDisplayCode", [""])[0]
    if not color:
        raw = params.get("colorCode", [""])[0]
        color = _ALPHA_PREFIX_RE.sub("", raw)

    size = params.get("sizeDisplayCode", [""])[0]
    if not size:
        raw = params.get("sizeCode", [""])[0]
        size = _ALPHA_PREFIX_RE.sub("", raw)

    return color, size


# ---------------------------------------------------------------------------
# Shared URL builder
# ---------------------------------------------------------------------------


def build_product_url(
    base: str,
    product_id: str,
    price_group: str,
    color: str = "",
    size: str = "",
    *,
    url_style: str = "display_code",
) -> str:
    """Reconstruct a Uniqlo product page URL from component fields.

    ``base`` is :pyattr:`AppConfig.product_page_base` (e.g.
    ``https://www.uniqlo.com/de/de/products``).

    When *url_style* is ``"code"``, *color* and *size* are full API code
    values (e.g. ``COL09``, ``SMA003``) and the price group is omitted
    from the path.  Otherwise they are display codes (``09``, ``003``)
    and ``/{price_group}`` is appended.
    """
    params: list[str] = []
    if url_style == "code":
        if color:
            params.append(f"colorCode={color}")
        if size:
            params.append(f"sizeCode={size}")
        qs = ("?" + "&".join(params)) if params else ""
        return f"{base}/{product_id}{qs}"

    if color:
        params.append(f"colorDisplayCode={color}")
    if size:
        params.append(f"sizeDisplayCode={size}")
    qs = ("?" + "&".join(params)) if params else ""
    return f"{base}/{product_id}/{price_group}{qs}"


# ---------------------------------------------------------------------------
# Uniqlo Commerce API response models (partial — only fields we need)
# ---------------------------------------------------------------------------


class UniqloPrice(BaseModel):
    """A price with an optional currency descriptor."""

    value: float
    currency: dict[str, str] | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: Any) -> float:
        return float(v)


class UniqloPriceInfo(BaseModel):
    """Base and optional promotional price for a product."""

    base: UniqloPrice
    promo: UniqloPrice | None = None
    is_dual_price: bool = Field(default=False, alias="isDualPrice")


class UniqloSize(BaseModel):
    """A single size option with its API codes.

    ``code`` is the full API code (e.g. ``SMA003``, ``INS027``, ``KAG140``)
    used in ``colorCode``/``sizeCode`` URL params for v3-storefront countries.
    ``display_code`` is the shorter numeric portion (e.g. ``003``, ``027``)
    used in ``colorDisplayCode``/``sizeDisplayCode`` URL params.
    """

    name: str
    code: str = ""
    display_code: str = Field(default="", alias="displayCode")


class UniqloImageDetail(BaseModel):
    image: str = ""


class UniqloProduct(BaseModel, populate_by_name=True):
    """Represents a single product from the Uniqlo API."""

    product_id: str = Field(alias="productId")
    name: str = ""
    gender_category: str = Field(default="", alias="genderCategory")
    prices: UniqloPriceInfo
    sizes: list[UniqloSize] = Field(default_factory=list)
    images: dict[str, Any] = Field(default_factory=dict)
    price_group: str = Field(default="", alias="priceGroup")
    rating: dict[str, Any] = Field(default_factory=dict)
    representative: dict[str, Any] = Field(default_factory=dict)
    representative_color_display_code: str = Field(
        default="", alias="representativeColorDisplayCode"
    )

    @property
    def is_on_sale(self) -> bool:
        return self.prices.promo is not None and self.prices.promo.value < self.prices.base.value

    @property
    def discount_percentage(self) -> float:
        if not self.is_on_sale or self.prices.base.value == 0:
            return 0.0
        promo = self.prices.promo
        if promo is None:
            return 0.0
        return round((self.prices.base.value - promo.value) / self.prices.base.value * 100, 1)

    @property
    def main_image_url(self) -> str | None:
        main_images: dict[str, Any] = self.images.get("main", {})
        for _color_code, detail in main_images.items():
            if isinstance(detail, dict) and "image" in detail:
                return detail["image"]
        return None

    @property
    def color_image_map(self) -> dict[str, str]:
        """Map of colour display code to image URL."""
        result: dict[str, str] = {}
        for color_code, detail in self.images.get("main", {}).items():
            if isinstance(detail, dict) and "image" in detail:
                result[color_code] = detail["image"]
        return result

    @property
    def size_names(self) -> list[str]:
        return [size.name for size in self.sizes]

    @property
    def currency_symbol(self) -> str:
        if self.prices.base.currency:
            return self.prices.base.currency.get("symbol", _DEFAULT_CURRENCY)
        return _DEFAULT_CURRENCY


class UniqloPagination(BaseModel):
    total: int = 0
    offset: int = 0
    count: int = 0


class UniqloApiResult(BaseModel):
    items: list[UniqloProduct] = Field(default_factory=list)
    pagination: UniqloPagination = Field(default_factory=UniqloPagination)


class UniqloApiResponse(BaseModel):
    status: str = ""
    result: UniqloApiResult = Field(default_factory=UniqloApiResult)


# ---------------------------------------------------------------------------
# Application-level models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantInfo:
    """Per-variant data extracted from the parallel lists in :class:`SaleItem`.

    Provides safe, named access to what is otherwise a set of positional
    lookups across ``available_sizes``, ``product_urls``, ``color_names``,
    ``stock_quantities``, and ``stock_statuses``.
    """

    size: str
    url: str
    color_name: str
    quantity: int
    status: str


class SaleItem(BaseModel):
    """A product that passed all configured filters."""

    product_id: str
    name: str
    original_price: float
    sale_price: float
    currency_symbol: str = _DEFAULT_CURRENCY
    discount_percentage: float
    gender: str
    available_sizes: list[str]
    image_url: str | None = None
    color_images: dict[str, str] = Field(default_factory=dict)
    product_urls: list[str] = Field(default_factory=list)
    color_names: list[str] = Field(default_factory=list)
    stock_quantities: list[int] = Field(default_factory=list)
    stock_statuses: list[str] = Field(default_factory=list)
    price_group: str = ""
    rating_average: float | None = None
    rating_count: int | None = None
    is_watched: bool = False
    has_known_discount: bool = True

    def variant_at(self, index: int) -> VariantInfo:
        """Return variant data at *index* with safe defaults for sparse lists."""
        return VariantInfo(
            size=self.available_sizes[index] if index < len(self.available_sizes) else "",
            url=self.product_urls[index] if index < len(self.product_urls) else "",
            color_name=self.color_names[index] if index < len(self.color_names) else "",
            quantity=self.stock_quantities[index] if index < len(self.stock_quantities) else 0,
            status=self.stock_statuses[index] if index < len(self.stock_statuses) else "",
        )


class SaleCheckResult(BaseModel):
    """Result of a single sale-check run."""

    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_products_scanned: int = 0
    total_on_sale: int = 0
    matching_deals: list[SaleItem] = Field(default_factory=list)
    new_deals: list[SaleItem] = Field(default_factory=list)
