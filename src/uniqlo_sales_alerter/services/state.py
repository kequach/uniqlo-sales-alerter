"""Seen-variant state management for new-deal detection.

Tracks which product variants have been seen so that only genuinely new
deals trigger notifications.  State is persisted to a JSON file on disk.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from uniqlo_sales_alerter.models.products import SaleItem, is_low_stock, parse_variant_codes

logger = logging.getLogger(__name__)


class SeenVariantStore:
    """Manages the set of previously seen variant keys on disk.

    A variant key has the form ``product_id:color:size:discount`` and
    uniquely identifies a purchasable variant at a specific price point.
    """

    def __init__(
        self,
        path: Path,
        *,
        suppress_low_stock: bool = False,
        low_stock_threshold: int = 0,
    ) -> None:
        self._path = path
        self._suppress_low_stock = suppress_low_stock
        self._low_stock_threshold = low_stock_threshold

    def load(self) -> set[str]:
        """Load previously seen variant keys from disk."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            variants = data.get("variants", [])
            logger.debug("Loaded %d seen variants from %s", len(variants), self._path)
            return set(variants)
        except FileNotFoundError:
            logger.debug("No state file at %s — starting fresh", self._path)
            return set()
        except (json.JSONDecodeError, KeyError):
            logger.warning("Corrupt state file %s — starting fresh", self._path)
            return set()

    def save(self, variants: set[str]) -> None:
        """Persist current variant keys to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "variants": sorted(variants),
        }
        self._path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.debug("Saved %d variant keys to %s", len(variants), self._path)

    def variant_keys(self, item: SaleItem) -> set[str]:
        """Extract ``product_id:color:size:discount`` keys from a SaleItem.

        The discount percentage is appended so that a price change is
        detected as a new deal.  For items without a known discount the
        literal ``"sale"`` is used instead.

        When :attr:`_suppress_low_stock` is True, low-stock variants are
        omitted from the returned set so they stay "unseen" — the user
        only gets alerted when stock climbs back above the threshold.
        """
        suffix = f"{item.discount_percentage:g}" if item.has_known_discount else "sale"

        keys: set[str] = set()
        saw_variant_url = False
        for idx, url in enumerate(item.product_urls):
            color, size = parse_variant_codes(url)
            if not (color and size):
                continue
            saw_variant_url = True
            if self._suppress_low_stock and self._variant_is_low(item, idx):
                continue
            keys.add(f"{item.product_id}:{color}:{size}:{suffix}")
        if not saw_variant_url:
            keys.add(f"{item.product_id}:{suffix}")
        return keys

    def find_new_deals(
        self, items: list[SaleItem], seen: set[str],
    ) -> list[SaleItem]:
        """Return items that have at least one variant not in *seen*."""
        return [
            item for item in items
            if self.variant_keys(item) - seen
        ]

    def _variant_is_low(self, item: SaleItem, idx: int) -> bool:
        """True when the variant at *idx* is currently in low-stock state."""
        variant = item.variant_at(idx)
        return is_low_stock(variant.quantity, variant.status, self._low_stock_threshold)
