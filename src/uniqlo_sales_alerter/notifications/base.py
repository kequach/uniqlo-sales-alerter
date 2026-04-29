"""Protocol and shared helpers for notification channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from uniqlo_sales_alerter.models.products import SaleItem, is_low_stock, parse_variant_codes

PROJECT_URL = "https://github.com/kequach/uniqlo-sales-alerter"

FAVICON_LINK = (
    '<link rel="icon" href="data:image/svg+xml,'
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<path d='M4 15.5 15.5 4H28a1 1 0 0 1 1 1v12.5"
    "L17.5 29a2 2 0 0 1-2.8 0L4 18.3a2 2 0 0 1 0-2.8z'"
    " fill='%23ED1D24'/>"
    "<circle cx='23' cy='9' r='2.5' fill='%23fff'/>"
    '</svg>"/>'
)


# ---------------------------------------------------------------------------
# Shared formatting helpers (used by all notification channels)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FormattedPrice:
    """Channel-agnostic price data pre-computed from a :class:`SaleItem`.

    Channels only need to decide *how* to render each field (ANSI, HTML,
    MarkdownV2, etc.) — the business logic lives here once.
    """

    sale_text: str
    original_text: str
    discount_label: str
    show_strikethrough: bool
    show_sale_badge: bool


def format_price(deal: SaleItem) -> FormattedPrice:
    """Derive display-ready price fields from *deal*."""
    symbol = deal.currency_symbol
    sale = f"{symbol}{deal.sale_price:.2f}"
    if deal.has_known_discount and deal.discount_percentage > 0:
        return FormattedPrice(
            sale_text=sale,
            original_text=f"{symbol}{deal.original_price:.2f}",
            discount_label=f"-{deal.discount_percentage:.0f}%",
            show_strikethrough=True,
            show_sale_badge=False,
        )
    if not deal.has_known_discount:
        return FormattedPrice(
            sale_text=sale,
            original_text="",
            discount_label="Sale",
            show_strikethrough=False,
            show_sale_badge=True,
        )
    return FormattedPrice(
        sale_text=sale,
        original_text="",
        discount_label="",
        show_strikethrough=False,
        show_sale_badge=False,
    )


def format_stock_suffix(
    qty: int, status: str, threshold: int,
) -> tuple[str, bool]:
    """Return ``(text, is_low)`` for a single variant's stock cell.

    ``text`` is empty when stock data is unavailable (``qty <= 0`` and
    status is not ``LOW_STOCK``); channels should render nothing in that
    case.  Callers decide how to style the low-stock text — this helper
    only provides the raw label and a boolean.
    """
    low = is_low_stock(qty, status, threshold)
    if low and qty > 0:
        return f"{qty}, low stock", True
    if low:
        return "low stock", True
    if qty > 0:
        return f"{qty}", False
    return "", False


def format_rating(deal: SaleItem) -> str | None:
    """Return a channel-agnostic rating string, or *None* when no ratings.

    Example: ``"★ 4.3 (127 reviews)"``.  Only renders when the product has
    at least one rating so brand-new items aren't polluted with empty stars.
    """
    count = deal.rating_count or 0
    avg = deal.rating_average
    if count <= 0 or avg is None:
        return None
    noun = "review" if count == 1 else "reviews"
    return f"★ {avg:.1f} ({count} {noun})"


# ---------------------------------------------------------------------------
# Colour-aware image resolution
#
# The listing API often returns images only for the representative colour,
# but stock verification may assign a different in-stock colour to a variant.
# These helpers ensure each notification channel displays the photo matching
# the variant's actual colour, using two strategies:
#
#   1. Exact lookup — ``color_images`` maps colour display codes to URLs
#      (populated from the listing API's ``images.main``).
#   2. CDN derivation — when the exact code isn't in the map, the colour
#      portion of a known CDN URL is substituted
#      (``…/eugoods_09_… → …/eugoods_01_…``).
# ---------------------------------------------------------------------------

_COLOR_IN_CDN_URL = re.compile(r"(goods_)\d{2}(_)")


def _derive_color_image(existing_url: str, target_color: str) -> str | None:
    """Derive an image URL for *target_color* from an existing Uniqlo CDN URL.

    The CDN encodes the colour display code in the filename, e.g.
    ``…/eugoods_09_485476_3x4.jpg``.  This substitutes the colour portion
    so the notification shows the correct variant photo.

    Returns *None* if *existing_url* doesn't match the expected pattern.
    """
    result, count = _COLOR_IN_CDN_URL.subn(
        rf"\g<1>{target_color}\2", existing_url, count=1,
    )
    return result if count else None


def resolve_color_image(
    url: str,
    color_images: dict[str, str],
    fallback: str | None,
) -> str | None:
    """Pick the product image matching the variant URL's colour code.

    Resolution order:

    1. Parse ``colorDisplayCode`` from *url* and look it up in *color_images*.
    2. If the code is absent from the map, derive the URL from an existing
       entry via :func:`_derive_color_image` (CDN colour substitution).
    3. Return *fallback* (typically the listing's first/representative image).
    """
    if color_images and url:
        color_code, _ = parse_variant_codes(url)
        if color_code:
            if color_code in color_images:
                return color_images[color_code]
            any_url = next(iter(color_images.values()), None)
            if any_url:
                derived = _derive_color_image(any_url, color_code)
                if derived:
                    return derived
    return fallback


def unique_colors(deal: SaleItem) -> list[str]:
    """Deduplicated, non-empty colour names preserving insertion order."""
    return list(dict.fromkeys(name for name in deal.color_names if name))


@runtime_checkable
class Notifier(Protocol):
    """Structural interface for notification channels.

    Any class with matching ``send`` and ``is_enabled`` signatures is
    automatically considered a ``Notifier`` — no inheritance required.
    """

    def is_enabled(self) -> bool: ...

    async def send(self, deals: list[SaleItem]) -> None: ...


class DealActions:
    """Pre-built action URLs for a single deal."""

    __slots__ = ("ignore_url", "watch_urls", "unwatch_url")

    def __init__(self, deal: SaleItem, server_url: str) -> None:
        if not server_url:
            self.ignore_url = ""
            self.watch_urls: list[tuple[str, str]] = []
            self.unwatch_url = ""
            return
        name_enc = quote(deal.name, safe="")
        self.ignore_url = (
            f"{server_url}/actions/ignore/{deal.product_id}?name={name_enc}"
        )
        self.unwatch_url = (
            f"{server_url}/actions/unwatch/{deal.product_id}?name={name_enc}"
            if deal.is_watched else ""
        )
        self.watch_urls = []
        for size_label, product_url in zip(deal.available_sizes, deal.product_urls):
            encoded_url = quote(product_url, safe="")
            watch_action = (
                f"{server_url}/actions/watch/{deal.product_id}"
                f"?name={name_enc}&url={encoded_url}"
            )
            self.watch_urls.append((size_label, watch_action))
