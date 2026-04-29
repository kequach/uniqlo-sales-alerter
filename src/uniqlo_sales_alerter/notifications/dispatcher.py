"""Notification dispatcher — routes alerts to all enabled channels."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uniqlo_sales_alerter.models.products import SaleItem
from uniqlo_sales_alerter.notifications.base import Notifier
from uniqlo_sales_alerter.notifications.console import ConsoleNotifier
from uniqlo_sales_alerter.notifications.email import EmailNotifier
from uniqlo_sales_alerter.notifications.html_report import HtmlReportNotifier
from uniqlo_sales_alerter.notifications.telegram import TelegramNotifier

if TYPE_CHECKING:
    from uniqlo_sales_alerter.config import AppConfig, EmailChannelConfig

logger = logging.getLogger(__name__)


def _log_email_disabled(cfg: EmailChannelConfig) -> None:
    """Log why the email notifier is disabled."""
    checks = [
        (cfg.enabled, "enabled: false"),
        (cfg.smtp_host, "smtp_host is empty"),
        (cfg.from_address, "from_address is empty"),
        (cfg.to_addresses, "to_addresses is empty"),
    ]
    reasons = [msg for ok, msg in checks if not ok]
    logger.info("Email disabled because: %s", ", ".join(reasons))


class NotificationDispatcher:
    """Creates notifiers from config and dispatches deals to all enabled channels.

    Failures in one channel are logged but never prevent other channels from
    being tried.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._notifiers: list[Notifier] = self._build_notifiers(config)

    @staticmethod
    def _build_notifiers(config: AppConfig) -> list[Notifier]:
        notify_cfg = config.notifications
        channels = notify_cfg.channels
        notifiers: list[Notifier] = []

        server_url = config.full_server_url
        threshold = notify_cfg.low_stock_threshold
        keywords = config.filters.ignored_keywords

        telegram = TelegramNotifier(
            channels.telegram,
            server_url=server_url,
            low_stock_threshold=threshold,
            ignored_keywords=keywords,
        )
        notifiers.append(telegram)
        logger.debug("Registered TelegramNotifier (enabled=%s)", telegram.is_enabled())

        email = EmailNotifier(
            channels.email,
            server_url=server_url,
            low_stock_threshold=threshold,
            ignored_keywords=keywords,
        )
        notifiers.append(email)
        logger.debug("Registered EmailNotifier (enabled=%s)", email.is_enabled())
        if not email.is_enabled():
            _log_email_disabled(channels.email)

        if notify_cfg.preview_cli:
            notifiers.append(ConsoleNotifier(
                enabled=True,
                server_url=server_url,
                low_stock_threshold=threshold,
                ignored_keywords=keywords,
            ))
            logger.debug("Registered ConsoleNotifier (preview_cli)")
        if notify_cfg.preview_html:
            notifiers.append(HtmlReportNotifier(
                enabled=True,
                server_url=server_url,
                low_stock_threshold=threshold,
                ignored_keywords=keywords,
            ))
            logger.debug("Registered HtmlReportNotifier (preview_html)")

        return notifiers

    def register(self, notifier: Notifier) -> None:
        """Register an additional notification channel at runtime."""
        self._notifiers.append(notifier)

    async def dispatch(self, deals: list[SaleItem]) -> None:
        """Send *deals* to every enabled notification channel."""
        if not deals:
            logger.debug("No deals to dispatch — skipping")
            return

        logger.debug(
            "Dispatching %d deal(s) to %d registered channel(s)",
            len(deals), len(self._notifiers),
        )

        for notifier in self._notifiers:
            name = type(notifier).__name__
            if not notifier.is_enabled():
                logger.debug("%s — skipped (disabled)", name)
                continue
            try:
                await notifier.send(deals)
                logger.info("Sent %d deal(s) via %s", len(deals), name)
            except Exception:
                logger.exception("Notification channel %s failed", name)
