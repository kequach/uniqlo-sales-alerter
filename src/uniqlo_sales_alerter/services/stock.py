"""Real-time stock verification for sale items.

Fetches per-variant stock from the Uniqlo API and keeps only sizes
that are actually in stock.  Countries with unreliable stock APIs
(``stock_api="none"``) still fetch L2 variant data for accurate URLs,
but never drop items for being out of stock.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from uniqlo_sales_alerter.models.products import (
    SaleItem,
    StockVariant,
    build_product_url,
)

if TYPE_CHECKING:
    from uniqlo_sales_alerter.clients.uniqlo import UniqloClient
    from uniqlo_sales_alerter.config import AppConfig, WatchedVariant

logger = logging.getLogger(__name__)

_IN_STOCK_STATUSES = frozenset({"IN_STOCK", "LOW_STOCK"})
_MAX_STOCK_CONCURRENCY = 10


class StockVerifier:
    """Verifies real-time stock for a batch of :class:`SaleItem` objects."""

    def __init__(
        self,
        client: UniqloClient,
        config: AppConfig,
        watched_by_product: dict[str, list[WatchedVariant]],
    ) -> None:
        self._client = client
        self._config = config
        self._watched_by_product = watched_by_product

    async def verify(self, items: list[SaleItem]) -> list[SaleItem]:
        """Fetch real-time stock and keep only in-stock sizes.

        Items where no sizes survive are dropped entirely.  Countries
        with ``stock_api="none"`` skip the stock call but still fetch L2
        variant data for accurate product URLs.
        """
        if not items:
            return items

        unreliable_stock = self._config.capabilities.stock_api == "none"
        concurrency = asyncio.Semaphore(_MAX_STOCK_CONCURRENCY)

        async def _limited(item: SaleItem) -> SaleItem | None:
            async with concurrency:
                if unreliable_stock:
                    return await self._enrich_from_l2(item)
                return await self._verify_one(item)

        tasks = [_limited(item) for item in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        verified: list[SaleItem] = []
        for item, result in zip(items, results):
            if isinstance(result, Exception):
                logger.warning(
                    "Stock check failed for %s, keeping listing data: %s",
                    item.product_id, result,
                )
                verified.append(item)
            elif result is not None:
                verified.append(result)
            else:
                logger.debug("Dropped %s — no in-stock sizes", item.product_id)
        return verified

    # ------------------------------------------------------------------
    # Single-item verification
    # ------------------------------------------------------------------

    async def _verify_one(self, item: SaleItem) -> SaleItem | None:
        """Verify stock for a single item; returns *None* to drop it."""
        l2s, stock_map = await asyncio.gather(
            self._client.fetch_product_l2s(item.product_id, item.price_group),
            self._client.fetch_variant_stock(item.product_id, item.price_group),
        )

        if not l2s or not stock_map:
            return item

        wanted = {s.upper() for s in item.available_sizes}
        base = self._config.product_page_base
        style = self._config.capabilities.url_style

        preferred_colors = self._preferred_color_map(item, l2s)

        verified_sizes: list[str] = []
        verified_urls: list[str] = []
        verified_color_names: list[str] = []
        verified_quantities: list[int] = []
        verified_statuses: list[str] = []

        for size_name in item.available_sizes:
            variant = pick_in_stock_variant(
                size_name, l2s, stock_map, wanted,
                preferred_color=preferred_colors.get(size_name.upper()),
            )
            if variant is not None:
                verified_sizes.append(size_name)
                verified_color_names.append(variant.color_name)
                verified_quantities.append(variant.quantity)
                verified_statuses.append(variant.status)
                if style == "code":
                    verified_urls.append(
                        build_product_url(
                            base, item.product_id, item.price_group,
                            variant.color_code, variant.size_code,
                            url_style="code",
                        ),
                    )
                else:
                    verified_urls.append(
                        build_product_url(
                            base, item.product_id, item.price_group,
                            variant.color_display_code,
                            variant.size_display_code,
                        ),
                    )

        if not verified_sizes:
            return None

        return item.model_copy(update={
            "available_sizes": verified_sizes,
            "product_urls": verified_urls,
            "color_names": verified_color_names,
            "stock_quantities": verified_quantities,
            "stock_statuses": verified_statuses,
        })

    def _preferred_color_map(
        self, item: SaleItem, l2s: list[dict],
    ) -> dict[str, str]:
        """Build a size-name -> preferred-color map from watched variants."""
        watched = self._watched_by_product.get(item.product_id.upper(), [])
        if not watched:
            return {}
        code_to_name: dict[str, str] = {}
        for l2 in l2s:
            size_data = l2.get("size", {})
            code_to_name[size_data.get("displayCode", "")] = size_data.get("name", "")
        preferred: dict[str, str] = {}
        for wv in watched:
            size_name_upper = code_to_name.get(wv.size, "").upper()
            if size_name_upper and wv.color:
                preferred[size_name_upper] = wv.color
        return preferred

    # ------------------------------------------------------------------
    # L2-only enrichment (unreliable stock API)
    # ------------------------------------------------------------------

    async def _enrich_from_l2(self, item: SaleItem) -> SaleItem:
        """Enrich URLs from L2 variant data without stock filtering.

        Used for countries with unreliable stock APIs (``stock_api="none"``).
        """
        l2s = await self._client.fetch_product_l2s(
            item.product_id, item.price_group,
        )
        if not l2s:
            return item
        return rebuild_from_l2(
            item, l2s, self._config.product_page_base,
            url_style=self._config.capabilities.url_style,
        )


# ---------------------------------------------------------------------------
# Pure functions (no state, easy to test)
# ---------------------------------------------------------------------------


def pick_in_stock_variant(
    size_name: str,
    l2s: list[dict],
    stock_map: dict[str, dict],
    wanted_sizes: set[str],
    preferred_color: str | None = None,
) -> StockVariant | None:
    """Find an in-stock colour for *size_name*.

    Returns a :class:`StockVariant` or *None*.  When *preferred_color*
    is given (from a watched URL) and that colour is in stock, it wins
    regardless of quantity.  Otherwise the highest-quantity variant is
    chosen.
    """
    candidates: list[StockVariant] = []
    for l2 in l2s:
        size_data = l2.get("size", {})
        if size_data.get("name", "").upper() != size_name.upper():
            continue
        l2_id = l2.get("l2Id", "")
        stock = stock_map.get(l2_id, {})
        status = stock.get("statusCode", "")
        if status in _IN_STOCK_STATUSES:
            color_data = l2.get("color", {})
            candidates.append(StockVariant(
                color_display_code=color_data.get("displayCode", ""),
                size_display_code=size_data.get("displayCode", ""),
                color_name=color_data.get("name", ""),
                quantity=stock.get("quantity", 0),
                status=status,
                color_code=color_data.get("code", ""),
                size_code=size_data.get("code", ""),
            ))

    if not candidates:
        return None

    if preferred_color:
        for candidate in candidates:
            if candidate.color_display_code == preferred_color:
                return candidate

    return max(candidates, key=lambda variant: variant.quantity)


def rebuild_from_l2(
    item: SaleItem,
    l2s: list[dict],
    base: str,
    *,
    url_style: str = "display_code",
) -> SaleItem:
    """Rebuild URLs and color names from L2 data without stock filtering.

    Picks the first colour variant per wanted size.  Stock data is
    genuinely unknown in this path.
    """
    l2_by_size: dict[str, dict] = {}
    for l2 in l2s:
        upper_name = l2.get("size", {}).get("name", "").upper()
        if upper_name and upper_name not in l2_by_size:
            l2_by_size[upper_name] = l2

    rebuilt_sizes: list[str] = []
    rebuilt_urls: list[str] = []
    rebuilt_colors: list[str] = []
    for size_name in item.available_sizes:
        l2 = l2_by_size.get(size_name.upper())
        if l2:
            color_data = l2.get("color", {})
            color_name = color_data.get("name", "")
            if url_style == "code":
                color_param = color_data.get("code", "")
                size_param = l2.get("size", {}).get("code", "")
            else:
                color_param = color_data.get("displayCode", "")
                size_param = l2.get("size", {}).get("displayCode", "")
            rebuilt_sizes.append(size_name)
            rebuilt_urls.append(
                build_product_url(
                    base, item.product_id, item.price_group,
                    color_param, size_param, url_style=url_style,
                ),
            )
            rebuilt_colors.append(color_name)
        else:
            rebuilt_sizes.append(size_name)
            rebuilt_urls.append("")
            rebuilt_colors.append("")

    return item.model_copy(update={
        "available_sizes": rebuilt_sizes,
        "product_urls": rebuilt_urls,
        "color_names": rebuilt_colors,
        "stock_quantities": [0] * len(rebuilt_sizes),
        "stock_statuses": [""] * len(rebuilt_sizes),
    })
