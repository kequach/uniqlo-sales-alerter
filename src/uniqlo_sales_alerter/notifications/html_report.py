"""HTML report notification channel — generates a local file and opens it in the browser."""

from __future__ import annotations

import html as html_mod
import logging
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from uniqlo_sales_alerter.models.products import SaleItem
from uniqlo_sales_alerter.notifications.base import (
    FAVICON_LINK,
    PROJECT_URL,
    DealActions,
    format_price,
    format_rating,
    format_stock_suffix,
    resolve_color_image,
    unique_colors,
)

logger = logging.getLogger(__name__)

_REPORT_CSS = """\
  :root {
    --uq-red: #ED1D24;
    --uq-dark-red: #c41219;
    --bg: #f2f2f2;
    --card-bg: #ffffff;
    --text: #333333;
    --muted: #757575;
    --border: #e0e0e0;
    --sale-green: #1a8c3a;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #191919;
      --card-bg: #2a2a2a;
      --text: #ececec;
      --muted: #999999;
      --border: #3a3a3a;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Helvetica Neue", Helvetica, Arial,
      "Hiragino Sans", "Yu Gothic", sans-serif;
    background: var(--bg); color: var(--text);
    padding: 0; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  /* ── Header bar ─────────────────────────────────── */
  header {
    background: var(--uq-red); color: #fff;
    padding: 20px 24px; text-align: center;
  }
  header .logo {
    font-size: 1.6rem; font-weight: 800;
    letter-spacing: .12em; text-transform: uppercase;
  }
  header .subtitle {
    font-size: .82rem; font-weight: 400;
    opacity: .85; margin-top: 4px;
  }

  /* ── Stats strip ────────────────────────────────── */
  .stats {
    display: flex; justify-content: center; gap: 32px;
    padding: 14px 24px;
    background: var(--card-bg);
    border-bottom: 1px solid var(--border);
    font-size: .85rem; color: var(--muted);
  }
  .stats strong { color: var(--text); }

  /* ── Card grid ──────────────────────────────────── */
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
    gap: 16px; max-width: 1240px;
    margin: 24px auto; padding: 0 24px;
  }
  .card {
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    display: flex; overflow: hidden;
    transition: box-shadow .15s, transform .15s;
  }
  .card:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px rgba(0,0,0,.1);
  }

  /* ── Card image ─────────────────────────────────── */
  .card-img {
    flex: 0 0 140px;
    display: flex; align-items: center; justify-content: center;
    background: #fafafa;
  }
  @media (prefers-color-scheme: dark) {
    .card-img { background: #222; }
  }
  .card-img img { width: 140px; height: 180px; object-fit: cover; }
  .no-img {
    width: 140px; height: 180px;
    display: flex; align-items: center; justify-content: center;
    color: var(--muted); font-size: .8rem;
  }

  /* ── Card body ──────────────────────────────────── */
  .card-body {
    padding: 14px 16px; display: flex;
    flex-direction: column; gap: 8px; flex: 1;
  }
  .card-title { font-weight: 700; font-size: .92rem; }
  .index { color: var(--muted); font-weight: 400; }
  .badge-watched {
    background: var(--uq-red); color: #fff;
    font-size: .6rem; font-weight: 800;
    letter-spacing: .04em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 2px;
    vertical-align: middle; margin-left: 6px;
  }

  /* ── Colour label ──────────────────────────────── */
  .color-label {
    font-size: .78rem; color: var(--muted);
    font-weight: 600;
  }

  /* ── Prices ─────────────────────────────────────── */
  .price-row { font-size: .93rem; }
  .price-old {
    text-decoration: line-through; color: var(--muted);
  }
  .arrow { margin: 0 4px; color: var(--muted); }
  .price-sale { color: var(--uq-red); font-weight: 700; }
  .discount {
    color: var(--sale-green); font-weight: 700;
    margin-left: 6px;
  }

  /* ── Size chips ─────────────────────────────────── */
  .sizes {
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: auto;
    align-items: center;
  }
  .size-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 12px;
    border: 1.5px solid var(--uq-red);
    border-radius: 2px;
    color: var(--uq-red); background: transparent;
    font-size: .76rem; font-weight: 700;
    text-decoration: none; text-transform: uppercase;
    transition: background .12s, color .12s;
  }
  .size-chip:hover {
    background: var(--uq-red); color: #fff;
  }
  .size-chip .stock-qty {
    font-size: .65rem; font-weight: 600;
    opacity: .55;
  }
  .size-chip:hover .stock-qty { opacity: .85; }
  .size-chip.low-stock {
    border-color: var(--uq-red);
    background: var(--uq-red); color: #fff;
  }
  .size-chip.low-stock .stock-qty {
    opacity: .85;
  }

  /* ── Watch chip (per-size star) ───────────────── */
  .watch-chip {
    display: inline-block; margin-left: 2px;
    padding: 4px 5px; font-size: .72rem;
    text-decoration: none; color: var(--muted);
    border-radius: 2px; vertical-align: middle;
    transition: color .12s;
  }
  .watch-chip:hover { color: var(--uq-red); }

  /* ── Rating ─────────────────────────────────── */
  .rating {
    font-size: .78rem; color: var(--muted);
    font-weight: 600;
  }

  /* ── Action buttons ─────────────────────────────── */
  .actions-row {
    display: flex; gap: 8px; margin-top: 6px;
  }
  .action-btn {
    display: inline-block; padding: 3px 10px;
    border-radius: 2px; font-size: .68rem; font-weight: 700;
    text-decoration: none; text-transform: uppercase;
    letter-spacing: .03em; transition: opacity .12s;
  }
  .action-btn:hover { opacity: .8; }
  .action-ignore {
    background: var(--border); color: var(--text);
  }
  .action-unwatch {
    background: var(--uq-red); color: #fff;
  }

  /* ── Footer ─────────────────────────────────────── */
  footer {
    text-align: center; color: var(--muted);
    font-size: .72rem; padding: 24px 0 32px;
    border-top: 1px solid var(--border);
    margin: 32px 24px 0;
  }
  footer span { color: var(--uq-red); font-weight: 700; }"""


def _stock_inline_html(qty: int, status: str, threshold: int) -> tuple[str, bool]:
    """Return ``(inner_html, is_low)`` for a stock count inside a size chip.

    ``inner_html`` is empty when stock data is unavailable so chips stay clean.
    """
    stock_text, is_low = format_stock_suffix(qty, status, threshold)
    if not stock_text:
        return "", False
    return f'<span class="stock-qty">{html_mod.escape(stock_text)}</span>', is_low


def _render_card(
    deal: SaleItem,
    index: int,
    server_url: str = "",
    low_stock_threshold: int = 0,
) -> str:
    """Render a single deal as an HTML card fragment."""
    safe_name = html_mod.escape(deal.name)
    watched = (
        '<span class="badge-watched">WATCHED</span>'
        if deal.is_watched else ""
    )
    first_url = deal.product_urls[0] if deal.product_urls else ""
    best_image = resolve_color_image(
        first_url, deal.color_images, deal.image_url,
    )
    img_inner = (
        f'<img src="{best_image}" alt="{safe_name}" loading="lazy"/>'
        if best_image
        else '<div class="no-img">No image</div>'
    )
    img = (
        f'<a href="{first_url}" target="_blank">{img_inner}</a>'
        if best_image and first_url else img_inner
    )

    def _size_chip(size_label: str, url: str, i: int) -> str:
        variant = deal.variant_at(i)
        stock_span, is_low = _stock_inline_html(
            variant.quantity, variant.status, low_stock_threshold,
        )
        css_class = "size-chip low-stock" if is_low else "size-chip"
        return f'<a class="{css_class}" href="{url}" target="_blank">{size_label}{stock_span}</a>'

    actions = DealActions(deal, server_url)
    if actions.unwatch_url:
        size_parts = [
            _size_chip(size_label, url, i)
            for i, (size_label, url) in enumerate(
                zip(deal.available_sizes, deal.product_urls),
            )
        ]
    else:
        watch_map = dict(actions.watch_urls)
        size_parts = []
        for i, (size_label, url) in enumerate(
            zip(deal.available_sizes, deal.product_urls),
        ):
            chip = _size_chip(size_label, url, i)
            watch_url = watch_map.get(size_label)
            if watch_url:
                chip += (
                    f'<a class="watch-chip" href="{watch_url}" '
                    f'target="_blank" title="Watch {size_label}">&#9734;</a>'
                )
            size_parts.append(chip)
    size_links = " ".join(size_parts) or ", ".join(deal.available_sizes)

    price = format_price(deal)
    if price.show_strikethrough:
        price_row = (
            f'<span class="price-old">{price.original_text}</span>'
            f'<span class="arrow">&rarr;</span>'
            f'<span class="price-sale">{price.sale_text}</span>'
            f'<span class="discount">{price.discount_label}</span>'
        )
    elif price.show_sale_badge:
        price_row = (
            f'<span class="price-sale">{price.sale_text}</span>'
            f'<span class="discount">{price.discount_label}</span>'
        )
    else:
        price_row = f'<span class="price-sale">{price.sale_text}</span>'

    action_row = ""
    if actions.ignore_url:
        unwatch_btn = (
            f'<a class="action-btn action-unwatch" '
            f'href="{actions.unwatch_url}" '
            f'target="_blank">Unwatch</a>'
        ) if actions.unwatch_url else ""
        action_row = (
            '<div class="actions-row">'
            f'<a class="action-btn action-ignore" '
            f'href="{actions.ignore_url}" '
            f'target="_blank">Ignore</a>'
            + unwatch_btn
            + '</div>'
        )

    colors = unique_colors(deal)
    color_row = (
        f'<div class="color-label">{" &middot; ".join(colors)}</div>'
        if colors else ""
    )

    rating_text = format_rating(deal)
    rating_row = (
        f'<div class="rating">{html_mod.escape(rating_text)}</div>'
        if rating_text else ""
    )

    return f"""
        <div class="card">
            <div class="card-img">{img}</div>
            <div class="card-body">
                <div class="card-title">
                    <span class="index">{index}.</span> {safe_name} {watched}
                </div>
                {color_row}
                {rating_row}
                <div class="price-row">
                    {price_row}
                </div>
                <div class="sizes">{size_links}</div>
                {action_row}
            </div>
        </div>"""


def _build_report(
    deals: list[SaleItem],
    generated_at: datetime,
    server_url: str = "",
    low_stock_threshold: int = 0,
    ignored_keywords: list[str] | None = None,
) -> str:
    """Build a self-contained HTML page styled in Uniqlo corporate identity."""
    cards = "".join(
        _render_card(deal, i, server_url, low_stock_threshold)
        for i, deal in enumerate(deals, 1)
    )
    timestamp = generated_at.strftime("%Y-%m-%d %H:%M UTC")
    settings_link = (
        f' &middot; <a href="{server_url}/settings"'
        f' style="text-decoration:none;color:inherit;">Settings</a>'
        if server_url else ""
    )
    kw_line = ""
    if ignored_keywords:
        escaped = ", ".join(html_mod.escape(keyword) for keyword in ignored_keywords)
        kw_line = f"<br/>Ignored keywords: {escaped}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>UNIQLO Sale Alert — {len(deals)} deal(s)</title>
{FAVICON_LINK}
<style>
{_REPORT_CSS}
</style>
</head>
<body>
<header>
  <div class="logo">UNIQLO</div>
  <div class="subtitle">Sale Alert &mdash; {len(deals)} deal(s)</div>
</header>
<div class="stats">
  <span><strong>{len(deals)}</strong> matching deals</span>
  <span>Generated <strong>{timestamp}</strong></span>
</div>
<div class="grid">
{cards}
</div>
<footer>Powered by <a href="{PROJECT_URL}"
  style="text-decoration:none;color:inherit;"><span>UNIQLO</span> Sales Alerter</a>{
  settings_link}{kw_line}</footer>
</body>
</html>"""


class HtmlReportNotifier:
    """Generates an HTML report file and opens it in the default browser."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        output_dir: str | None = None,
        server_url: str = "",
        low_stock_threshold: int = 0,
        ignored_keywords: list[str] | None = None,
    ) -> None:
        self._enabled = enabled
        self._output_dir = output_dir
        self._server_url = server_url
        self._low_stock_threshold = low_stock_threshold
        self._ignored_keywords = ignored_keywords or []

    def is_enabled(self) -> bool:
        return self._enabled

    async def send(self, deals: list[SaleItem]) -> None:
        if not deals:
            print("\n  No deals to display.\n")
            return

        now = datetime.now(timezone.utc)
        html = _build_report(
            deals,
            now,
            server_url=self._server_url,
            low_stock_threshold=self._low_stock_threshold,
            ignored_keywords=self._ignored_keywords,
        )

        if self._output_dir:
            out = Path(self._output_dir)
        else:
            out = Path(__file__).resolve().parents[3] / "reports"
        out.mkdir(parents=True, exist_ok=True)

        stamp = now.strftime("%Y%m%d_%H%M%S")
        path = out / f"uniqlo_deals_{stamp}.html"
        path.write_text(html, encoding="utf-8")

        print(f"\n  HTML report saved to: {path}")
        webbrowser.open(path.as_uri())
        logger.info("HTML report written to %s", path)
