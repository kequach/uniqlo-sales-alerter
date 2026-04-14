"""Core service that checks for sales, applies filters, and manages the result cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import parse_qs, urlparse

from uniqlo_sales_alerter.clients.uniqlo import UniqloClient
from uniqlo_sales_alerter.models.products import (
    SaleCheckResult,
    SaleItem,
    UniqloProduct,
    UniqloSize,
)

if TYPE_CHECKING:
    from uniqlo_sales_alerter.config import AppConfig

logger = logging.getLogger(__name__)

_IN_STOCK_STATUSES = frozenset({"IN_STOCK", "LOW_STOCK"})
_MAX_STOCK_CONCURRENCY = 10
_DEFAULT_STATE_PATH = Path(
    os.environ.get("STATE_FILE", Path.cwd() / ".seen_variants.json"),
)


class _WatchedVariant(NamedTuple):
    """A single colour+size variant parsed from a watched URL."""

    product_id: str
    url: str
    color: str
    size_code: str


class SaleChecker:
    """Fetches products, filters for matching deals, and caches results."""

    def __init__(
        self,
        config: AppConfig,
        *,
        state_file: Path | None = None,
    ) -> None:
        self._config = config
        self._client = UniqloClient(config)
        self.last_result: SaleCheckResult | None = None
        self._state_path = state_file or _DEFAULT_STATE_PATH
        self._watched_ids, self._watched_by_product = self._parse_watched_urls(
            config.filters.watched_urls,
        )
        self._seen_variants: set[str] = (
            self._load_state()
            if config.notifications.notify_on == "new_deals"
            else set()
        )

    async def close(self) -> None:
        """Release underlying HTTP resources."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Persistent variant-level state
    # ------------------------------------------------------------------

    def _load_state(self) -> set[str]:
        """Load previously seen variant keys from disk."""
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            variants = data.get("variants", [])
            logger.info(
                "Loaded %d seen variants from %s", len(variants), self._state_path,
            )
            return set(variants)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return set()

    def _save_state(self, variants: set[str]) -> None:
        """Persist current variant keys to disk."""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": self._utc_now_iso(),
            "variants": sorted(variants),
        }
        self._state_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.debug(
            "Saved %d variant keys to %s", len(variants), self._state_path,
        )

    @staticmethod
    def _utc_now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _variant_keys(item: SaleItem) -> set[str]:
        """Extract ``product_id:color:size:discount`` keys from a SaleItem's URLs.

        Each URL contains ``colorDisplayCode`` and ``sizeDisplayCode`` query
        parameters that uniquely identify a purchasable variant.  The discount
        percentage is appended so that a price change on an existing variant is
        detected as a new deal.  For items without a known discount (some
        countries don't expose the original price), the literal ``"sale"`` is
        used instead so size/color changes still trigger notifications.
        Falls back to ``product_id:suffix`` when no URL parameters are available.
        """
        suffix = f"{item.discount_percentage:g}" if item.has_known_discount else "sale"
        keys: set[str] = set()
        for url in item.product_urls:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            color = (
                params.get("colorDisplayCode", [""])[0]
                or params.get("colorCode", [""])[0]
            )
            size = (
                params.get("sizeDisplayCode", [""])[0]
                or params.get("sizeCode", [""])[0]
            )
            if color and size:
                keys.add(f"{item.product_id}:{color}:{size}:{suffix}")
        if not keys:
            keys.add(f"{item.product_id}:{suffix}")
        return keys

    async def check(self) -> SaleCheckResult:
        """Run a full sale check: fetch sale items, filter, diff, and cache."""
        sale_products = await self._client.fetch_sale_products()
        logger.info("Fetched %d sale products from Uniqlo API", len(sale_products))

        # Items come from flagCodes=discount / limitedOffer, plus any
        # configured sale_paths (category IDs).  Some countries return
        # promo == base or promo = None so is_on_sale is False, but the
        # items are still genuinely on sale — we keep them and mark them
        # as unknown-discount downstream.

        # Watched products are included whenever they're in stock, even when
        # not on sale.  Fetch any watched IDs missing from the sale results.
        all_products: list[UniqloProduct] = list(sale_products)
        if self._watched_ids:
            sale_pids = {p.product_id.upper() for p in sale_products}
            missing_upper = self._watched_ids - sale_pids
            if missing_upper:
                to_fetch: set[str] = set()
                for pid_upper in missing_upper:
                    for wv in self._watched_by_product.get(pid_upper, []):
                        to_fetch.add(wv.product_id)
                watched_extra = await self._client.fetch_products_by_ids(
                    sorted(to_fetch),
                )
                logger.info(
                    "Fetched %d watched product(s) not currently on sale",
                    len(watched_extra),
                )
                all_products.extend(watched_extra)

        matching = self._apply_filters(all_products)

        # Verify real-time stock and pick in-stock colors for each size.
        matching = await self._verify_stock(matching)

        # Build variant keys for all current deals.
        current_variants: set[str] = set()
        for item in matching:
            current_variants |= self._variant_keys(item)

        # A deal is "new" if it has at least one variant not seen before.
        new_deals = [
            item for item in matching
            if self._variant_keys(item) - self._seen_variants
        ]

        result = SaleCheckResult(
            total_products_scanned=len(sale_products),
            total_on_sale=len(sale_products),
            matching_deals=matching,
            new_deals=new_deals,
        )

        self._seen_variants = current_variants
        self._save_state(current_variants)
        self.last_result = result
        return result

    # ------------------------------------------------------------------
    # Watched-URL parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_watched_urls(
        urls: list[str],
    ) -> tuple[set[str], dict[str, list[_WatchedVariant]]]:
        """Parse watched URLs into a product-ID set and a per-product variant map.

        Returns ``(watched_ids_upper, {product_id_upper: [variant, …]})``.
        """
        by_product: dict[str, list[_WatchedVariant]] = {}
        for url in urls:
            parsed = urlparse(url)
            parts = [p for p in parsed.path.split("/") if p]
            pid = ""
            for i, segment in enumerate(parts):
                if segment == "products" and i + 1 < len(parts):
                    pid = parts[i + 1]
                    break
            if not pid:
                logger.warning("Could not extract product ID from watched URL: %s", url)
                continue
            params = parse_qs(parsed.query)
            variant = _WatchedVariant(
                product_id=pid,
                url=url,
                color=(
                    params.get("colorDisplayCode", [""])[0]
                    or params.get("colorCode", [""])[0]
                ),
                size_code=(
                    params.get("sizeDisplayCode", [""])[0]
                    or params.get("sizeCode", [""])[0]
                ),
            )
            by_product.setdefault(pid.upper(), []).append(variant)
        return set(by_product.keys()), by_product

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _apply_filters(self, products: list[UniqloProduct]) -> list[SaleItem]:
        """Apply gender, size, and discount filters; always include watched products.

        Items without a known discount (``promo >= base``, common in US/CA/JP/
        KR/SG) bypass the ``min_sale_percentage`` filter but still must pass
        gender and size checks.
        """
        cfg = self._config.filters
        watched = self._watched_ids
        gender_filter = {g.upper() for g in cfg.gender}
        all_size_names = self._normalised_size_set()
        results: list[SaleItem] = []

        for product in products:
            is_watched = self._is_watched(product.product_id, watched)
            has_known_discount = product.is_on_sale
            passes_discount = (
                not has_known_discount
                or product.discount_percentage >= cfg.min_sale_percentage
            )
            passes_gender = self._matches_gender(product, gender_filter)
            passes_size = self._matches_size(product, all_size_names)

            if is_watched or (passes_discount and passes_gender and passes_size):
                watched_variants = self._watched_by_product.get(
                    product.product_id.upper(), [],
                )
                results.append(
                    self._to_sale_item(
                        product, is_watched, all_size_names, watched_variants,
                    )
                )

        results.sort(key=lambda s: s.discount_percentage, reverse=True)
        return results

    def _to_sale_item(
        self,
        product: UniqloProduct,
        is_watched: bool,
        size_filter: set[str],
        watched_variants: list[_WatchedVariant] | None = None,
    ) -> SaleItem:
        rating = product.rating
        matched_sizes = self._matching_sizes(product, size_filter)
        urls = self._build_variant_urls(product, matched_sizes)

        final_sizes = [s.name for s in matched_sizes]
        final_urls = list(urls)

        if watched_variants:
            code_to_name = {s.display_code: s.name for s in product.sizes}
            for wv in watched_variants:
                size_name = code_to_name.get(wv.size_code)
                if size_name is None:
                    continue
                if size_name in final_sizes:
                    idx = final_sizes.index(size_name)
                    final_urls[idx] = wv.url
                else:
                    final_sizes.append(size_name)
                    final_urls.append(wv.url)

        return SaleItem(
            product_id=product.product_id,
            name=product.name,
            original_price=product.prices.base.value,
            sale_price=(
                product.prices.promo.value if product.prices.promo else product.prices.base.value
            ),
            currency_symbol=product.currency_symbol,
            discount_percentage=product.discount_percentage,
            gender=product.gender_category,
            available_sizes=final_sizes,
            image_url=product.main_image_url,
            product_urls=final_urls,
            price_group=product.price_group,
            rating_average=rating.get("average"),
            rating_count=rating.get("count"),
            is_watched=is_watched,
            has_known_discount=product.is_on_sale,
        )

    def _normalised_size_set(self) -> set[str]:
        """Build a single set of all configured size names, normalised to upper case."""
        sizes = self._config.filters.sizes
        combined = [*sizes.clothing, *sizes.pants, *sizes.shoes]
        all_names = {n.upper() for n in combined}
        if sizes.one_size:
            all_names.add("ONE SIZE")
        return all_names

    @staticmethod
    def _is_watched(product_id: str, watched: set[str]) -> bool:
        pid = product_id.upper()
        return any(pid.startswith(w) for w in watched)

    @staticmethod
    def _matches_gender(product: UniqloProduct, gender_filter: set[str]) -> bool:
        if not gender_filter:
            return True
        cat = product.gender_category.upper()
        if cat == "UNISEX":
            return True
        return cat in gender_filter

    @staticmethod
    def _matches_size(product: UniqloProduct, size_filter: set[str]) -> bool:
        if not size_filter:
            return True
        return any(s.name.upper() in size_filter for s in product.sizes)

    @staticmethod
    def _matching_sizes(
        product: UniqloProduct, size_filter: set[str]
    ) -> list[UniqloSize]:
        """Return the product's in-stock sizes that match the configured filter.

        The listing-level ``sizes`` array from the Uniqlo API already reflects
        real-time online stock; sizes that are out of stock are omitted.  When a
        size filter is configured we return only the intersection so the user
        sees exactly which of their desired sizes are purchasable.  When no
        filter is configured we return every available size.
        """
        if not size_filter:
            return list(product.sizes)
        return [s for s in product.sizes if s.name.upper() in size_filter]

    def _build_variant_urls(
        self,
        product: UniqloProduct,
        sizes: list[UniqloSize],
    ) -> list[str]:
        """Build a direct URL for each matching size variant.

        Standard format: ``…/products/{id}/{priceGroup}?colorDisplayCode=…&sizeDisplayCode=…``
        SEA format:      ``…/products/{id}?colorCode=…&sizeCode=…``

        Uses the representative color as a preliminary default; the real
        in-stock colour is resolved later by ``_verify_stock``.
        """
        base = self._config.product_page_base
        pid = product.product_id
        pg = product.price_group
        color = product.representative_color_display_code
        uses_sea = self._config.uses_sea_url_format
        urls: list[str] = []
        for s in sizes:
            if uses_sea:
                url = f"{base}/{pid}?colorCode={color}&sizeCode={s.display_code}"
            else:
                url = f"{base}/{pid}/{pg}?colorDisplayCode={color}&sizeDisplayCode={s.display_code}"
            urls.append(url)
        return urls

    # ------------------------------------------------------------------
    # Real-time stock verification
    # ------------------------------------------------------------------

    async def _verify_stock(self, items: list[SaleItem]) -> list[SaleItem]:
        """Fetch real-time stock for each candidate and keep only in-stock sizes.

        For every matching size, pick a colour that is actually in stock and
        rewrite the variant URL accordingly.  Items where no sizes survive
        are dropped entirely.
        """
        if not items:
            return items
        sem = asyncio.Semaphore(_MAX_STOCK_CONCURRENCY)

        async def _limited(item: SaleItem) -> SaleItem | None:
            async with sem:
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

    async def _verify_one(self, item: SaleItem) -> SaleItem | None:
        """Verify stock for a single SaleItem; returns *None* to drop it."""
        uses_sea = self._config.uses_sea_url_format

        if uses_sea:
            # SEA stores (PH/TH/SG/…): v3 product detail endpoint returns
            # each L2 with stock embedded directly in l2.stock.
            l2s = await self._client.fetch_product_detail_v3(item.product_id)
            if not l2s:
                return item  # keep listing data when API unavailable
            stock_map: dict[str, dict] = {
                l2.get("l2Id", ""): l2.get("stock", {}) for l2 in l2s
            }
            # Pre-build displayCode → full code maps for URL construction.
            color_dc_to_code = {
                l2.get("color", {}).get("displayCode", ""): l2.get("color", {}).get("code", "")
                for l2 in l2s
            }
            size_dc_to_code = {
                l2.get("size", {}).get("displayCode", ""): l2.get("size", {}).get("code", "")
                for l2 in l2s
            }
        else:
            l2s, stock_map = await asyncio.gather(
                self._client.fetch_product_l2s(item.product_id, item.price_group),
                self._client.fetch_variant_stock(item.product_id, item.price_group),
            )
            if not l2s or not stock_map:
                return item  # keep listing data when stock API is unavailable

        wanted = {s.upper() for s in item.available_sizes}
        base = self._config.product_page_base

        # Build a size-code → size-name map from the L2 data, keyed by both
        # displayCode and full code so watched-URL lookups work for both formats.
        watched = self._watched_by_product.get(item.product_id.upper(), [])
        code_to_name: dict[str, str] = {}
        for l2 in l2s:
            sz = l2.get("size", {})
            name = sz.get("name") or sz.get("sizeName", "")
            for key in filter(None, [sz.get("displayCode"), sz.get("code")]):
                code_to_name[key] = name
        preferred_colors: dict[str, str] = {}
        for wv in watched:
            sn = code_to_name.get(wv.size_code, "").upper()
            if sn and wv.color:
                preferred_colors[sn] = wv.color

        # For each wanted size, find the best in-stock colour variant.
        verified_sizes: list[str] = []
        verified_urls: list[str] = []

        for size_name in item.available_sizes:
            best = self._pick_in_stock_variant(
                size_name,
                l2s,
                stock_map,
                wanted,
                preferred_color=preferred_colors.get(size_name.upper()),
            )
            if best is not None:
                color_dc, size_dc = best
                verified_sizes.append(size_name)
                if uses_sea:
                    # PH/TH/SG URLs use the full variant code (e.g. COL09, SMA004),
                    # not the numeric displayCode.
                    color_code = color_dc_to_code.get(color_dc, color_dc)
                    size_code = size_dc_to_code.get(size_dc, size_dc)
                    url = f"{base}/{item.product_id}?colorCode={color_code}&sizeCode={size_code}"
                else:
                    url = (
                        f"{base}/{item.product_id}/{item.price_group}"
                        f"?colorDisplayCode={color_dc}&sizeDisplayCode={size_dc}"
                    )
                verified_urls.append(url)

        if not verified_sizes:
            return None

        return item.model_copy(update={
            "available_sizes": verified_sizes,
            "product_urls": verified_urls,
        })

    @staticmethod
    def _pick_in_stock_variant(
        size_name: str,
        l2s: list[dict],
        stock_map: dict[str, dict],
        wanted_sizes: set[str],
        preferred_color: str | None = None,
    ) -> tuple[str, str] | None:
        """Find an in-stock colour for *size_name*.

        Returns ``(colorDisplayCode, sizeDisplayCode)`` or *None*.
        When *preferred_color* is given (from a watched URL) and that colour is
        in stock, it wins regardless of quantity.  Otherwise the highest-quantity
        variant is chosen.
        """
        candidates: list[tuple[int, str, str]] = []
        for l2 in l2s:
            sz = l2.get("size", {})
            # SEA stores use "sizeName"; standard stores use "name"
            name = sz.get("name") or sz.get("sizeName", "")
            if name.upper() != size_name.upper():
                continue
            l2id = l2.get("l2Id", "")
            stock = stock_map.get(l2id, {})
            if stock.get("statusCode") in _IN_STOCK_STATUSES:
                qty = stock.get("quantity", 0)
                # SEA stores use "colorCode"; standard stores use "displayCode"
                color_dc = (
                    l2.get("color", {}).get("displayCode")
                    or l2.get("color", {}).get("colorCode", "")
                )
                size_dc = sz.get("displayCode") or sz.get("sizeCode", "")
                candidates.append((qty, color_dc, size_dc))

        if not candidates:
            return None

        if preferred_color:
            for _qty, color_dc, size_dc in candidates:
                if color_dc == preferred_color:
                    return color_dc, size_dc

        candidates.sort(reverse=True)  # highest quantity first
        return candidates[0][1], candidates[0][2]
