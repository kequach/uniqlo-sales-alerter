"""Console notification channel — prints deals to stdout for preview/dry-run."""

from __future__ import annotations

import sys

from uniqlo_sales_alerter.models.products import SaleItem
from uniqlo_sales_alerter.notifications.base import (
    PROJECT_URL,
    DealActions,
    format_price,
    format_rating,
    format_stock_suffix,
    unique_colors,
)

_USE_COLOR = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _ansi(code: str, text: str) -> str:
    """Wrap *text* in ANSI escape codes (no-op when stdout is not a TTY)."""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def _format_deal(
    deal: SaleItem,
    index: int,
    server_url: str = "",
    low_stock_threshold: int = 0,
) -> str:
    watched = _ansi("33", " [WATCHED]") if deal.is_watched else ""
    header = _ansi("1", f"  {index}. {deal.name}") + watched
    price = format_price(deal)
    if price.show_strikethrough:
        price_line = (
            f"     {_ansi('9', price.original_text)}"
            f" -> {_ansi('32;1', price.sale_text)}"
            f"  {_ansi('32', f'({price.discount_label})')}"
        )
    elif price.show_sale_badge:
        price_line = (
            f"     {_ansi('32;1', price.sale_text)}"
            f"  {_ansi('32', f'({price.discount_label})')}"
        )
    else:
        price_line = f"     {price.sale_text}"
    lines = [header, price_line]

    rating_text = format_rating(deal)
    if rating_text:
        lines.append(f"     {_ansi('33', rating_text)}")

    colors = unique_colors(deal)
    if colors:
        lines.append(f"     Color: {_ansi('35', ' · '.join(colors))}")

    for i, (size, url) in enumerate(zip(deal.available_sizes, deal.product_urls)):
        variant = deal.variant_at(i)
        stock_text, is_low = format_stock_suffix(
            variant.quantity, variant.status, low_stock_threshold,
        )
        if stock_text:
            stock_colored = (
                _ansi("31;1", f"  ({stock_text})") if is_low
                else _ansi("2", f"  ({stock_text})")
            )
        else:
            stock_colored = ""
        lines.append(f"     {_ansi('36', size):>8s}  {url}{stock_colored}")
    actions = DealActions(deal, server_url)
    if actions.ignore_url:
        lines.append(f"     {_ansi('2', f'[Ignore] {actions.ignore_url}')}")
        if actions.unwatch_url:
            lines.append(f"     {_ansi('2', f'[Unwatch] {actions.unwatch_url}')}")
        else:
            for size_label, watch_url in actions.watch_urls:
                lines.append(f"     {_ansi('2', f'[Watch {size_label}] {watch_url}')}")
    return "\n".join(lines)


class ConsoleNotifier:
    """Prints deal summaries to stdout. Used in preview / dry-run mode."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        server_url: str = "",
        low_stock_threshold: int = 0,
        ignored_keywords: list[str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._server_url = server_url
        self._low_stock_threshold = low_stock_threshold
        self._ignored_keywords = ignored_keywords or []

    def is_enabled(self) -> bool:
        return self._enabled

    async def send(self, deals: list[SaleItem]) -> None:
        if not deals:
            print("\n  No deals to display.\n")
            return

        print(_ansi("1;36", f"\n{'=' * 60}"))
        print(_ansi("1;36", f"  Uniqlo Sale Alert — {len(deals)} deal(s)"))
        print(_ansi("1;36", f"{'=' * 60}"))

        for i, deal in enumerate(deals, 1):
            print()
            print(_format_deal(
                deal, i,
                server_url=self._server_url,
                low_stock_threshold=self._low_stock_threshold,
            ))

        print()
        print(_ansi("2", f"  {PROJECT_URL}"))
        if self._server_url:
            print(_ansi("2", f"  Settings: {self._server_url}/settings"))
        if self._ignored_keywords:
            keywords_text = ", ".join(self._ignored_keywords)
            print(_ansi("2", f"  Ignored keywords: {keywords_text}"))
        print()
