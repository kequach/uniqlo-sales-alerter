"""Telegram notification channel using the Bot API."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uniqlo_sales_alerter.models.products import SaleItem
from uniqlo_sales_alerter.notifications.base import (
    PROJECT_URL,
    DealActions,
    format_price,
    format_rating,
    format_stock_suffix,
    resolve_color_image,
    unique_colors,
)

if TYPE_CHECKING:
    from uniqlo_sales_alerter.config import TelegramChannelConfig

logger = logging.getLogger(__name__)


def _escape_md(text: str) -> str:
    """Escape characters reserved by Telegram MarkdownV2."""
    for char in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(char, f"\\{char}")
    return text


def _size_link(
    size_label: str, url: str, qty: int, status: str, threshold: int,
) -> str:
    """Render a single size as a MarkdownV2 link with optional stock suffix."""
    stock_text, is_low = format_stock_suffix(qty, status, threshold)
    if not stock_text:
        label = _escape_md(size_label)
    elif is_low:
        label = _escape_md(f"{size_label} · {stock_text} ⚠")
    else:
        label = _escape_md(f"{size_label} · {stock_text}")
    return f"[{label}]({url})"


def _build_caption(
    deal: SaleItem,
    server_url: str = "",
    low_stock_threshold: int = 0,
    ignored_keywords: list[str] | None = None,
) -> str:
    """Build a MarkdownV2 caption for a single deal."""
    name = _escape_md(deal.name)
    price = format_price(deal)

    if price.show_strikethrough:
        original_md = _escape_md(price.original_text)
        sale_price_md = _escape_md(price.sale_text)
        discount_md = _escape_md(price.discount_label)
        price_line = f"~{original_md}~ ➜ {sale_price_md} \\({discount_md}\\)"
    elif price.show_sale_badge:
        price_line = f"{_escape_md(price.sale_text)} ✦ {_escape_md(price.discount_label)}"
    else:
        price_line = _escape_md(price.sale_text)

    colors = unique_colors(deal)
    color_line = (
        f"Color: {_escape_md(' · '.join(colors))}"
        if colors else ""
    )

    rating_text = format_rating(deal)
    rating_line = _escape_md(rating_text) if rating_text else ""

    size_links = " \\| ".join(
        _size_link(
            size_label, url,
            deal.variant_at(i).quantity,
            deal.variant_at(i).status,
            low_stock_threshold,
        )
        for i, (size_label, url) in enumerate(
            zip(deal.available_sizes, deal.product_urls),
        )
    )

    footer = f"[Uniqlo Sales Alerter]({PROJECT_URL})"
    if server_url:
        footer += f" · [Settings]({server_url}/settings)"
    if ignored_keywords:
        keywords_text = _escape_md(", ".join(ignored_keywords))
        footer += f"\nIgnored keywords: {keywords_text}"

    lines = [
        f"*{name}*",
        price_line,
        size_links or _escape_md(", ".join(deal.available_sizes)),
        f"\n{footer}",
    ]
    if rating_line:
        lines.insert(2, rating_line)
    if color_line:
        lines.insert(1, color_line)
    if deal.is_watched:
        lines.insert(0, "⭐ *Watched item*")
    return "\n".join(lines)


class TelegramNotifier:
    """Sends deal notifications via Telegram Bot API."""

    def __init__(
        self,
        config: TelegramChannelConfig,
        *,
        server_url: str = "",
        low_stock_threshold: int = 0,
        ignored_keywords: list[str] | None = None,
    ) -> None:
        self._config = config
        self._server_url = server_url
        self._low_stock_threshold = low_stock_threshold
        self._ignored_keywords = ignored_keywords or []

    def is_enabled(self) -> bool:
        return self._config.enabled and bool(self._config.bot_token) and bool(self._config.chat_id)

    async def send(self, deals: list[SaleItem]) -> None:
        if not deals:
            return

        try:
            from telegram import Bot
        except ImportError:
            logger.error("python-telegram-bot is not installed; skipping Telegram notifications")
            return

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from telegram.error import TelegramError
        bot = Bot(token=self._config.bot_token)
        chat_id = self._config.chat_id

        for deal in deals:
            caption = _build_caption(
                deal,
                server_url=self._server_url,
                low_stock_threshold=self._low_stock_threshold,
                ignored_keywords=self._ignored_keywords,
            )
            actions = DealActions(deal, self._server_url)
            markup = None
            if actions.ignore_url:
                if actions.unwatch_url:
                    rows = [[InlineKeyboardButton(
                        "Unwatch", url=actions.unwatch_url,
                    )]]
                else:
                    rows = [
                        [InlineKeyboardButton(
                            f"Watch {size_label}", url=watch_url,
                        )]
                        for size_label, watch_url in actions.watch_urls
                    ]
                rows.append([InlineKeyboardButton(
                    "Ignore", url=actions.ignore_url,
                )])
                markup = InlineKeyboardMarkup(rows)
            photo_url = resolve_color_image(
                deal.product_urls[0] if deal.product_urls else "",
                deal.color_images,
                deal.image_url,
            )

            try:
                if photo_url:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_url,
                        caption=caption,
                        parse_mode="MarkdownV2",
                        reply_markup=markup,
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=caption,
                        parse_mode="MarkdownV2",
                        reply_markup=markup,
                    )
            except TelegramError:
                logger.exception("Failed to send Telegram message for %s", deal.product_id)
