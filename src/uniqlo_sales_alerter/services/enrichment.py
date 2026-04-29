"""Metadata enrichment for watched variants and ignored products.

Resolves product names, human-readable colour/size names, and
reconstructs missing URLs by fetching data from the Uniqlo API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uniqlo_sales_alerter.models.products import UniqloProduct, build_product_url

if TYPE_CHECKING:
    from uniqlo_sales_alerter.clients.uniqlo import UniqloClient
    from uniqlo_sales_alerter.config import AppConfig

logger = logging.getLogger(__name__)


def _find_color_name(l2s: list[dict], color_code: str) -> str:
    """Look up the human-readable colour name from L2 variant data."""
    for l2 in l2s:
        color = l2.get("color", {})
        if color.get("displayCode") == color_code:
            return color.get("name", "")
    return ""


def _find_size_name(product: UniqloProduct, size_code: str) -> str:
    """Look up the human-readable size name from a product's size list."""
    for size_entry in product.sizes:
        if size_entry.display_code == size_code:
            return size_entry.name
    return ""


async def enrich_config(config: AppConfig, client: UniqloClient) -> bool:
    """Fill in missing metadata for watched variants and ignored products.

    Returns ``True`` when at least one entry was updated (caller should
    persist the config).
    """
    base = config.product_page_base

    incomplete_variants = [
        wv for wv in config.filters.watched_variants
        if wv.id and (
            not wv.name or not wv.color_name
            or not wv.size_name or not wv.url
        )
    ]
    incomplete_ignored = [
        ignored for ignored in config.filters.ignored_products
        if ignored.id and (not ignored.name or not ignored.url)
    ]
    if not incomplete_variants and not incomplete_ignored:
        return False

    all_ids = list(
        {wv.id for wv in incomplete_variants}
        | {ignored.id for ignored in incomplete_ignored}
    )
    products = await client.fetch_products_by_ids(all_ids)
    product_by_id = {p.product_id.upper(): p for p in products}

    l2_keys = {
        (wv.id, wv.price_group)
        for wv in incomplete_variants
        if not wv.color_name or not wv.size_name
    }
    l2_by_product: dict[str, list[dict]] = {}
    for pid, pg in l2_keys:
        l2_by_product[pid.upper()] = await client.fetch_product_l2s(pid, pg)

    changed = False

    for ignored in incomplete_ignored:
        product = product_by_id.get(ignored.id.upper())
        if product and not ignored.name:
            ignored.name = product.name
            changed = True
        if not ignored.url:
            pg = product.price_group if product else "00"
            ignored.url = build_product_url(base, ignored.id, pg)
            changed = True

    for wv in incomplete_variants:
        product = product_by_id.get(wv.id.upper())

        if product and not wv.name:
            wv.name = product.name
            changed = True

        if not wv.url:
            wv.url = build_product_url(
                base, wv.id, wv.price_group, wv.color, wv.size,
            )
            changed = True

        if not wv.size_name and product:
            wv.size_name = _find_size_name(product, wv.size)
            changed = changed or bool(wv.size_name)

        if not wv.color_name:
            l2s = l2_by_product.get(wv.id.upper(), [])
            wv.color_name = _find_color_name(l2s, wv.color)
            changed = changed or bool(wv.color_name)

    if changed:
        logger.debug(
            "Enriched metadata for %d watched variant(s) "
            "and %d ignored product(s)",
            len(incomplete_variants), len(incomplete_ignored),
        )
    return changed
