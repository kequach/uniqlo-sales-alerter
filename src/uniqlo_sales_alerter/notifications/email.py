"""Email notification channel using async SMTP."""

from __future__ import annotations

import html as html_mod
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from uniqlo_sales_alerter import __version__
from uniqlo_sales_alerter.models.products import SaleItem
from uniqlo_sales_alerter.notifications.base import (
    PROJECT_URL,
    DealActions,
    format_price,
    format_rating,
    format_stock_suffix,
    resolve_color_image,
)

if TYPE_CHECKING:
    from uniqlo_sales_alerter.config import EmailChannelConfig

logger = logging.getLogger(__name__)

_SMTP_TIMEOUT = 30


def _expand_to_variants(deal: SaleItem) -> list[SaleItem]:
    """Expand a multi-size deal into one ``SaleItem`` per size+colour variant."""
    if not deal.product_urls or len(deal.available_sizes) <= 1:
        if deal.product_urls and deal.color_images:
            img = resolve_color_image(
                deal.product_urls[0], deal.color_images, deal.image_url,
            )
            if img != deal.image_url:
                return [deal.model_copy(update={"image_url": img})]
        return [deal]
    variants: list[SaleItem] = []
    for i, (sz, url) in enumerate(zip(deal.available_sizes, deal.product_urls)):
        v = deal.variant_at(i)
        img = resolve_color_image(url, deal.color_images, deal.image_url)
        variants.append(deal.model_copy(update={
            "available_sizes": [sz],
            "product_urls": [url],
            "color_names": [v.color_name],
            "stock_quantities": [v.quantity],
            "stock_statuses": [v.status],
            "image_url": img,
        }))
    return variants


def _size_link_html(
    size_label: str, url: str, qty: int, status: str, threshold: int,
) -> str:
    """Render a size link with an optional stock-suffix span."""
    safe_sz = html_mod.escape(size_label)
    anchor = f'<a href="{url}">{safe_sz}</a>'
    stock_text, is_low = format_stock_suffix(qty, status, threshold)
    if not stock_text:
        return anchor
    if is_low:
        return (
            f'{anchor} <span style="color:#fff;background:#c0392b;'
            f'font-weight:600;font-size:.85em;padding:1px 5px;'
            f'border-radius:2px;">({html_mod.escape(stock_text)})</span>'
        )
    return (
        f'{anchor} <span style="color:#999;font-size:.85em;">'
        f'({html_mod.escape(stock_text)})</span>'
    )


def _build_html(
    deals: list[SaleItem],
    server_url: str = "",
    low_stock_threshold: int = 0,
    ignored_keywords: list[str] | None = None,
) -> str:
    """Build the HTML email body, expanding each deal into per-variant rows."""
    variants: list[SaleItem] = []
    for deal in deals:
        variants.extend(_expand_to_variants(deal))

    rows: list[str] = []
    for variant in variants:
        safe_name = html_mod.escape(variant.name)
        watched_badge = ' <span style="color:gold;">⭐ Watched</span>' if variant.is_watched else ""
        variant_url = variant.product_urls[0] if variant.product_urls else ""
        img_html = (
            f'<img src="{variant.image_url}" alt="{safe_name}" '
            f'style="max-width:120px;max-height:160px;border-radius:4px;" />'
            if variant.image_url
            else ""
        )
        img_tag = (
            f'<a href="{variant_url}">{img_html}</a>'
            if img_html and variant_url else img_html
        )
        color_name = variant.color_names[0] if variant.color_names else ""
        color_html = (
            f'<small>Color: <strong>{html_mod.escape(color_name)}</strong></small><br/>'
            if color_name else ""
        )
        size_links = " &middot; ".join(
            _size_link_html(
                size_label, url,
                variant.variant_at(i).quantity,
                variant.variant_at(i).status,
                low_stock_threshold,
            )
            for i, (size_label, url) in enumerate(
                zip(variant.available_sizes, variant.product_urls),
            )
        ) or ", ".join(variant.available_sizes)
        rating_text = format_rating(variant)
        rating_html = (
            f'<small style="color:#888;">{html_mod.escape(rating_text)}</small><br/>'
            if rating_text else ""
        )
        price = format_price(variant)
        if price.show_strikethrough:
            price_html = (
                f'<span style="text-decoration:line-through;color:#999;">'
                f'{price.original_text}</span> &rarr; '
                f'<span style="color:#c0392b;font-weight:bold;">'
                f'{price.sale_text}</span> '
                f'<span style="color:#27ae60;">({price.discount_label})</span>'
            )
        elif price.show_sale_badge:
            price_html = (
                f'<span style="color:#c0392b;font-weight:bold;">'
                f'{price.sale_text}</span> '
                f'<span style="color:#27ae60;font-weight:bold;">'
                f'{price.discount_label}</span>'
            )
        else:
            price_html = (
                f'<span style="font-weight:bold;">{price.sale_text}</span>'
            )
        actions = DealActions(variant, server_url)
        action_html = ""
        if actions.ignore_url:
            if actions.unwatch_url:
                extra_link = (
                    f' &middot; <a href="{actions.unwatch_url}" '
                    f'style="color:#c0392b;">Unwatch</a>'
                )
            elif actions.watch_urls:
                _, wurl = actions.watch_urls[0]
                extra_link = (
                    f' &middot; <a href="{wurl}" style="color:#c0392b;">'
                    f'Watch</a>'
                )
            else:
                extra_link = ""
            action_html = (
                '<br/><small>'
                f'<a href="{actions.ignore_url}" style="color:#999;">'
                f'Ignore</a>'
                + extra_link
                + '</small>'
            )
        rows.append(
            f"""
            <tr style="border-bottom:1px solid #eee;">
                <td style="padding:12px;">{img_tag}</td>
                <td style="padding:12px;">
                    <strong>{safe_name}</strong>{watched_badge}<br/>
                    {color_html}
                    {rating_html}
                    {price_html}<br/>
                    <small>Size: {size_links}</small>
                    {action_html}
                </td>
            </tr>"""
        )

    settings_link = (
        f' · <a href="{server_url}/settings" style="color:#999;">Settings</a>'
        if server_url else ""
    )
    kw_line = ""
    if ignored_keywords:
        escaped = ", ".join(html_mod.escape(keyword) for keyword in ignored_keywords)
        kw_line = f"<br/>Ignored keywords: {escaped}"

    return f"""
    <html><body>
    <h2>Uniqlo Sale Alert — {len(deals)} deal(s) found</h2>
    <table style="border-collapse:collapse;width:100%;max-width:600px;">
        {"".join(rows)}
    </table>
    <p style="color:#999;font-size:12px;">
        Sent by <a href="{PROJECT_URL}"
        style="color:#999;">Uniqlo Sales Alerter</a> v{__version__}{settings_link}{kw_line}
    </p>
    </body></html>
    """


class EmailNotifier:
    """Sends deal notifications via SMTP email."""

    def __init__(
        self,
        config: EmailChannelConfig,
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
        return (
            self._config.enabled
            and bool(self._config.smtp_host)
            and bool(self._config.from_address)
            and bool(self._config.to_addresses)
        )

    async def send(self, deals: list[SaleItem]) -> None:
        if not deals:
            return

        try:
            import aiosmtplib
        except ImportError:
            msg = "aiosmtplib is not installed — run: pip install aiosmtplib"
            logger.error(msg)
            raise RuntimeError(msg)

        smtp_cfg = self._config

        implicit_tls = smtp_cfg.use_tls and smtp_cfg.smtp_port == 465
        starttls = smtp_cfg.use_tls and not implicit_tls
        tls_mode = (
            "implicit TLS" if implicit_tls
            else "STARTTLS" if starttls
            else "plaintext"
        )

        logger.debug(
            "Sending %d deal(s) via %s:%d (%s) to %s",
            len(deals), smtp_cfg.smtp_host, smtp_cfg.smtp_port, tls_mode,
            ", ".join(smtp_cfg.to_addresses),
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Uniqlo Sale Alert — {len(deals)} deal(s)"
        msg["From"] = smtp_cfg.from_address
        msg["To"] = ", ".join(smtp_cfg.to_addresses)
        msg.attach(MIMEText(
            _build_html(
                deals,
                self._server_url,
                self._low_stock_threshold,
                ignored_keywords=self._ignored_keywords,
            ),
            "html",
        ))

        try:
            await aiosmtplib.send(
                msg,
                hostname=smtp_cfg.smtp_host,
                port=smtp_cfg.smtp_port,
                use_tls=implicit_tls,
                start_tls=starttls,
                username=smtp_cfg.smtp_user or None,
                password=smtp_cfg.smtp_password or None,
                timeout=_SMTP_TIMEOUT,
            )
            logger.debug("Email sent to %s", smtp_cfg.to_addresses)
        except aiosmtplib.SMTPAuthenticationError as exc:
            logger.error(
                "SMTP authentication failed for %s@%s:%d — %s",
                smtp_cfg.smtp_user, smtp_cfg.smtp_host, smtp_cfg.smtp_port, exc,
            )
            raise
        except aiosmtplib.SMTPRecipientsRefused as exc:
            logger.error(
                "All recipients refused by %s:%d — %s",
                smtp_cfg.smtp_host, smtp_cfg.smtp_port, exc,
            )
            raise
        except aiosmtplib.SMTPResponseException as exc:
            logger.error(
                "SMTP server %s:%d returned error %d: %s",
                smtp_cfg.smtp_host, smtp_cfg.smtp_port, exc.code, exc.message,
            )
            raise
        except aiosmtplib.SMTPConnectError as exc:
            logger.error(
                "Cannot connect to SMTP server %s:%d — %s",
                smtp_cfg.smtp_host, smtp_cfg.smtp_port, exc,
            )
            raise
        except (TimeoutError, aiosmtplib.SMTPTimeoutError) as exc:
            logger.error(
                "SMTP connection to %s:%d timed out after %ds — %s",
                smtp_cfg.smtp_host, smtp_cfg.smtp_port, _SMTP_TIMEOUT, exc,
            )
            raise
        except Exception:
            logger.exception(
                "Unexpected error sending email via %s:%d",
                smtp_cfg.smtp_host, smtp_cfg.smtp_port,
            )
            raise
