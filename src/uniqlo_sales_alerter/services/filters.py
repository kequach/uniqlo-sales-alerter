"""Product filtering pipeline.

Each filter is an individual, named function with a clear docstring.
Adding a new filter means writing one function and inserting a single
``and`` clause in :func:`apply_filters`.  Watched products always
bypass all filters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from uniqlo_sales_alerter.models.products import (
    SaleItem,
    UniqloProduct,
    UniqloSize,
    build_product_url,
)

if TYPE_CHECKING:
    from uniqlo_sales_alerter.config import AppConfig, FilterConfig, WatchedVariant


# ---------------------------------------------------------------------------
# Individual filter predicates
# ---------------------------------------------------------------------------


def _is_watched(product_id: str, watched_ids: set[str]) -> bool:
    """True when *product_id* is on the watch list (prefix match)."""
    pid = product_id.upper()
    return any(pid.startswith(prefix) for prefix in watched_ids)


def _is_excluded(
    product: UniqloProduct,
    ignored_ids: set[str],
    ignored_keywords: list[str],
) -> bool:
    """True when the product should be excluded from results.

    A product is excluded when its ID matches an ignored-product entry
    (prefix match) **or** its name contains any ignored keyword
    (case-insensitive substring).
    """
    pid = product.product_id.upper()
    if any(pid.startswith(prefix) for prefix in ignored_ids):
        return True
    if ignored_keywords:
        name_lower = product.name.lower()
        if any(keyword in name_lower for keyword in ignored_keywords):
            return True
    return False


def _matches_gender(product: UniqloProduct, allowed: set[str]) -> bool:
    """True when the product's gender matches the filter.

    UNISEX always passes.  An empty *allowed* set means no filter.
    """
    if not allowed:
        return True
    gender = product.gender_category.upper()
    return gender == "UNISEX" or gender in allowed


def _matches_size(product: UniqloProduct, allowed: set[str]) -> bool:
    """True when the product has at least one size in the filter set.

    An empty *allowed* set means no filter (everything passes).
    """
    if not allowed:
        return True
    return any(size.name.upper() in allowed for size in product.sizes)


def _meets_discount_threshold(
    product: UniqloProduct,
    min_pct: float,
    has_known_discount: bool,
) -> bool:
    """True when the product's discount meets the minimum threshold.

    Items without a known discount (``promo >= base``, common in limited
    countries) bypass this check entirely — they still appear, but the
    ``has_known_discount`` flag tells notification channels to show a
    generic "Sale" badge.
    """
    if not has_known_discount:
        return True
    return product.discount_percentage >= min_pct


# ---------------------------------------------------------------------------
# Size helpers
# ---------------------------------------------------------------------------


def build_size_filter(config: FilterConfig) -> set[str]:
    """Build a single set of all configured size names, normalised to upper case."""
    sizes = config.sizes
    combined = [*sizes.clothing, *sizes.pants, *sizes.shoes]
    all_names = {name.upper() for name in combined}
    if sizes.one_size:
        all_names.add("ONE SIZE")
    return all_names


def matching_sizes(product: UniqloProduct, size_filter: set[str]) -> list[UniqloSize]:
    """Return the product's sizes that match the configured filter.

    When no filter is configured, all sizes pass.
    """
    if not size_filter:
        return list(product.sizes)
    return [size for size in product.sizes if size.name.upper() in size_filter]


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def build_variant_urls(
    product: UniqloProduct,
    sizes: list[UniqloSize],
    config: AppConfig,
) -> list[str]:
    """Build a direct product URL for each matching size.

    Uses the representative color as a preliminary default; the real
    in-stock colour is resolved later by stock verification.
    """
    base = config.product_page_base
    pid = product.product_id
    pg = product.price_group
    style = config.capabilities.url_style
    color = product.representative_color_display_code

    if style == "code":
        colors = product.representative.get("color", {})
        color_code = colors.get("code", "")
        return [
            build_product_url(base, pid, pg, color_code, size.code, url_style="code")
            for size in sizes
        ]

    return [
        build_product_url(base, pid, pg, color, size.display_code)
        for size in sizes
    ]


# ---------------------------------------------------------------------------
# Conversion helper
# ---------------------------------------------------------------------------


def to_sale_item(
    product: UniqloProduct,
    *,
    is_watched: bool,
    size_filter: set[str],
    watched_variants: list[WatchedVariant],
    in_sale_feed: bool,
    config: AppConfig,
) -> SaleItem:
    """Convert a :class:`UniqloProduct` to a :class:`SaleItem`.

    Combines filter-matched sizes with any extra sizes from watched
    variants, builds variant URLs, and derives the ``has_known_discount``
    flag from sale-feed membership.
    """
    matched = matching_sizes(product, size_filter)
    urls = build_variant_urls(product, matched, config)

    final_sizes = [size.name for size in matched]
    final_urls = list(urls)
    final_color_names = [""] * len(matched)

    if watched_variants:
        base = config.product_page_base
        pid = product.product_id
        pg = product.price_group
        code_to_name = {size.display_code: size.name for size in product.sizes}
        for wv in watched_variants:
            size_name = code_to_name.get(wv.size)
            if size_name is None:
                continue
            wv_url = build_product_url(base, pid, pg, wv.color, wv.size)
            if size_name in final_sizes:
                idx = final_sizes.index(size_name)
                final_urls[idx] = wv_url
                final_color_names[idx] = wv.color_name
            else:
                final_sizes.append(size_name)
                final_urls.append(wv_url)
                final_color_names.append(wv.color_name)

    rating = product.rating
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
        color_images=product.color_image_map,
        product_urls=final_urls,
        color_names=final_color_names,
        price_group=product.price_group,
        rating_average=rating.get("average"),
        rating_count=rating.get("count"),
        is_watched=is_watched,
        has_known_discount=product.is_on_sale or not in_sale_feed,
    )


# ---------------------------------------------------------------------------
# Main filter pipeline
# ---------------------------------------------------------------------------


def apply_filters(
    products: list[UniqloProduct],
    *,
    config: AppConfig,
    watched_ids: set[str],
    watched_by_product: dict[str, list[WatchedVariant]],
    ignored_ids: set[str],
    ignored_keywords: list[str],
    sale_product_ids: set[str] | None = None,
) -> list[SaleItem]:
    """Apply all configured filters and return matching :class:`SaleItem` objects.

    **Filter pipeline** (in evaluation order):

    1. **Watched bypass** — watched products skip exclusion checks.
    2. **Exclusion** — ignored product IDs and keyword matches.
    3. **Discount threshold** — ``min_sale_percentage`` (skipped for
       items without a known discount).
    4. **Gender** — must be in the configured set (UNISEX always passes).
    5. **Size** — at least one available size must match.

    Adding a new filter: write a predicate function above, then add it
    to the ``if is_watched or (...)`` condition below.
    """
    filter_cfg = config.filters
    gender_filter = {gender.upper() for gender in filter_cfg.gender}
    size_filter = build_size_filter(filter_cfg)
    sale_pids = (
        sale_product_ids
        if sale_product_ids is not None
        else {p.product_id.upper() for p in products}
    )
    results: list[SaleItem] = []

    for product in products:
        watched = _is_watched(product.product_id, watched_ids)

        if not watched and _is_excluded(product, ignored_ids, ignored_keywords):
            continue

        has_known_discount = product.is_on_sale
        passes_discount = _meets_discount_threshold(
            product, filter_cfg.min_sale_percentage, has_known_discount,
        )
        passes_gender = _matches_gender(product, gender_filter)
        passes_size = _matches_size(product, size_filter)

        if watched or (passes_discount and passes_gender and passes_size):
            wv_list = watched_by_product.get(product.product_id.upper(), [])
            in_sale_feed = product.product_id.upper() in sale_pids
            results.append(
                to_sale_item(
                    product,
                    is_watched=watched,
                    size_filter=size_filter,
                    watched_variants=wv_list,
                    in_sale_feed=in_sale_feed,
                    config=config,
                ),
            )

    results.sort(key=lambda item: item.discount_percentage, reverse=True)
    return results
