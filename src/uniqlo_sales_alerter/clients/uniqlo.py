"""Async HTTP client for the Uniqlo Commerce API."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import httpx

from uniqlo_sales_alerter.models.products import UniqloApiResponse, UniqloProduct

if TYPE_CHECKING:
    from uniqlo_sales_alerter.config import AppConfig

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_RETRIES = 3
TIMEOUT_SECONDS = 20
MAX_CONNECTIONS = 10

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_RATE_LIMIT_WAIT = 5.0
_MAX_RATE_LIMIT_WAIT = 60.0
_HTTP_FAILURE_PARAMS = {"httpFailure": "true"}


def _backoff_seconds(attempt: int, *, jitter: bool = True) -> float:
    """Exponential backoff: 2, 4, 8 … seconds, with optional random jitter."""
    base = float(2**attempt)
    if jitter:
        base *= 0.5 + random.random()  # noqa: S311
    return min(base, _MAX_RATE_LIMIT_WAIT)


def _retry_after(response: httpx.Response) -> float | None:
    """Parse the ``Retry-After`` header (seconds or HTTP-date) into seconds."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(float(value), _MAX_RATE_LIMIT_WAIT)
    except ValueError:
        return None


def _normalize_v3_product(raw: dict[str, Any]) -> dict[str, Any]:
    """Transform a v3 API product dict into the v5 schema expected by our models.

    Key differences handled:
    * ``prices.*.value`` — string in v3, float in v5
    * ``isDualPrice``   — absent in v3
    * ``genderCategory``— absent in v3 (``genderName`` used instead)
    * ``images.main``   — list in v3, dict keyed by colorCode in v5
    * ``priceGroup``    — absent in v3 (derived from ``plds`` or defaults to ``"00"``)
    """
    product = dict(raw)

    # Gender: v3 has 'genderName' (title-case) instead of 'genderCategory' (UPPER)
    gender = product.pop("genderName", "")
    if product.get("unisexFlag") in ("1", 1, True):
        gender = "UNISEX"
    product.setdefault("genderCategory", gender.upper() if gender else "")

    # Price group: extract from plds or default
    plds = product.get("plds")
    if plds and isinstance(plds, list) and plds:
        display_code = plds[0].get("displayCode", "000")
        product.setdefault("priceGroup", display_code.lstrip("0") or "00")
    product.setdefault("priceGroup", "00")

    # Representative color: v3 has it nested under representative.color
    if "representativeColorDisplayCode" not in product:
        rep_color = (
            product.get("representative", {})
            .get("color", {})
            .get("displayCode", "")
        )
        if rep_color:
            product["representativeColorDisplayCode"] = rep_color

    # Images: v3 main is a list of {url, colorCode}; v5 is {colorCode: {image: url}}
    images = product.get("images", {})
    main_list = images.get("main")
    if isinstance(main_list, list):
        main_dict: dict[str, dict[str, Any]] = {}
        for entry in main_list:
            if isinstance(entry, dict) and "url" in entry:
                color_code = entry.get("colorCode", "00")
                main_dict[color_code] = {"image": entry["url"]}
        images["main"] = main_dict
        product["images"] = images

    return product


class UniqloClient:
    """Fetches product data from the Uniqlo Commerce API.

    Uses a single shared :class:`httpx.AsyncClient` so that TCP connections
    are pooled and reused across listing, L2, and stock requests.

    All requests go through :meth:`_request` which handles retries for
    transient errors (5xx) and rate limits (429) with ``Retry-After``
    support and jittered exponential backoff.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._base_url = config.base_url
        self._base_url_v3 = config.base_url_v3
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "x-fr-clientid": config.client_id,
        }
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers,
                timeout=TIMEOUT_SECONDS,
                limits=httpx.Limits(
                    max_connections=MAX_CONNECTIONS,
                    max_keepalive_connections=MAX_CONNECTIONS,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client (call at shutdown)."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Retry-aware request helper
    # ------------------------------------------------------------------

    async def _request(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        label: str = "",
    ) -> httpx.Response:
        """GET *url* with automatic retry on 429 / 5xx.

        Respects the ``Retry-After`` header when present; otherwise falls
        back to jittered exponential backoff.  Raises after all retries
        are exhausted.
        """
        client = await self._ensure_client()
        last_exc: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = await client.get(url, params=params)

                if resp.status_code not in _RETRYABLE_STATUS_CODES:
                    resp.raise_for_status()
                    return resp

                # Retryable HTTP status ----------------------------------
                wait: float
                if resp.status_code == 429:
                    wait = (
                        _retry_after(resp)
                        or _DEFAULT_RATE_LIMIT_WAIT
                    )
                    msg = (
                        f"  [Rate limit] 429 on {label or url} — "
                        f"attempt {attempt}/{MAX_RETRIES}, "
                        f"retrying in {wait:.0f}s"
                    )
                    print(msg)
                    logger.warning("%s", msg.strip())
                else:
                    wait = _backoff_seconds(attempt)
                    logger.warning(
                        "HTTP %d on %s — attempt %d/%d, "
                        "retrying in %.1fs",
                        resp.status_code,
                        label or url,
                        attempt,
                        MAX_RETRIES,
                        wait,
                    )

                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code}",
                    request=resp.request,
                    response=resp,
                )

            except httpx.RequestError as exc:
                wait = _backoff_seconds(attempt)
                logger.warning(
                    "Request error on %s — attempt %d/%d, "
                    "retrying in %.1fs: %s",
                    label or url, attempt, MAX_RETRIES, wait, exc,
                )
                last_exc = exc

            if attempt < MAX_RETRIES:
                await asyncio.sleep(wait)

        if (
            isinstance(last_exc, httpx.HTTPStatusError)
            and last_exc.response.status_code == 429
        ):
            print(
                f"  [Rate limit] Gave up on {label or url} "
                f"after {MAX_RETRIES} attempts — still throttled.",
            )
        logger.error(
            "All %d retries exhausted for %s", MAX_RETRIES, label or url,
        )
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    _SOURCE_PARAMS: dict[str, tuple[str, dict[str, str]]] = {
        "v5_disc": ("v5", {"flagCodes": "discount"}),
        "v5_ltd": ("v5", {"flagCodes": "limitedOffer"}),
        "v3_disc": ("v3", {"flagCodes": "discount"}),
        "v3_ltd": ("v3", {"flagCodes": "limitedOffer"}),
    }

    def _fetch_source(self, source: str) -> Awaitable[list[UniqloProduct]]:
        """Return a coroutine for the named listing source."""
        version, params = self._SOURCE_PARAMS[source]
        if version == "v3":
            return self._fetch_all_v3(extra_params=params)
        return self._fetch_all(extra_params=params)

    async def fetch_sale_products(self) -> list[UniqloProduct]:
        """Fetch products flagged as on sale.

        Only queries the listing endpoints that actually return data for
        the configured country, as determined by :pyattr:`CountryCapabilities.listing_sources`.
        Results from all sources are merged and deduplicated by product ID.
        """
        caps = self._config.capabilities
        sources = caps.listing_sources

        tasks: list[asyncio.Task] = []
        labels: list[str] = []
        for src in sources:
            if src == "sale_paths":
                for path_id in self._config.uniqlo.sale_paths:
                    tasks.append(self._fetch_all(
                        extra_params={"path": path_id},
                    ))
                    labels.append(f"path:{path_id}")
            elif src in self._SOURCE_PARAMS:
                tasks.append(self._fetch_source(src))
                labels.append(src)

        results = await asyncio.gather(*tasks)

        seen: set[str] = set()
        merged: list[UniqloProduct] = []
        counts: dict[str, int] = {}
        for label, batch in zip(labels, results):
            count = 0
            for product in batch:
                if product.product_id not in seen:
                    seen.add(product.product_id)
                    merged.append(product)
                    count += 1
            counts[label] = count

        parts = " + ".join(f"{count} {source}" for source, count in counts.items())
        logger.info(
            "Fetched %s = %d unique sale candidates", parts, len(merged),
        )
        return merged

    async def fetch_all_products(self) -> list[UniqloProduct]:
        """Fetch every product from the catalogue, handling pagination."""
        return await self._fetch_all()

    async def fetch_products_by_ids(
        self, product_ids: list[str],
    ) -> list[UniqloProduct]:
        """Fetch specific products by ID, regardless of sale status."""
        if not product_ids:
            return []
        return await self._fetch_all(
            extra_params={"productIds": ",".join(product_ids)},
        )

    async def fetch_product_l2s(
        self, product_id: str, price_group: str,
    ) -> list[dict]:
        """Fetch L2 variants (color × size) for a single product."""
        url = (
            f"{self._base_url}/{product_id}"
            f"/price-groups/{price_group}"
        )
        try:
            resp = await self._request(
                url,
                params=_HTTP_FAILURE_PARAMS,
                label=f"L2s({product_id})",
            )
            data = resp.json()
            return data.get("result", {}).get("l2s", [])
        except Exception:
            logger.exception("Failed to fetch L2s for %s", product_id)
            return []

    async def fetch_variant_stock(
        self, product_id: str, price_group: str,
    ) -> dict[str, dict]:
        """Fetch per-variant stock for a product.

        Returns a mapping of ``l2Id`` → stock info dict with at least
        ``statusCode`` (``IN_STOCK``, ``LOW_STOCK``, or ``STOCK_OUT``).
        """
        url = (
            f"{self._base_url}/{product_id}"
            f"/price-groups/{price_group}/stock"
        )
        try:
            resp = await self._request(
                url,
                params=_HTTP_FAILURE_PARAMS,
                label=f"stock({product_id})",
            )
            data = resp.json()
            return data.get("result", {})
        except Exception:
            logger.exception(
                "Failed to fetch stock for %s (pg=%s)",
                product_id, price_group,
            )
            return {}

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    async def _paginate(
        self,
        fetch_page: Callable[
            [int, dict[str, str] | None],
            Awaitable[UniqloApiResponse | None],
        ],
        extra_params: dict[str, str] | None = None,
        *,
        label: str = "",
    ) -> list[UniqloProduct]:
        """Generic paginator shared by v5 and v3 endpoints."""
        all_products: list[UniqloProduct] = []
        offset = 0

        while True:
            page = await fetch_page(offset, extra_params)
            if page is None:
                break

            all_products.extend(page.result.items)
            total = page.result.pagination.total
            offset += PAGE_SIZE

            logger.debug(
                "Fetched %d / %d products%s",
                len(all_products), total,
                f" ({label})" if label else "",
            )

            if offset >= total:
                break

        if all_products:
            logger.debug(
                "Fetched %d products in total%s",
                len(all_products),
                f" ({label})" if label else "",
            )
        return all_products

    async def _fetch_all(
        self, extra_params: dict[str, str] | None = None,
    ) -> list[UniqloProduct]:
        return await self._paginate(self._fetch_page, extra_params)

    async def _fetch_all_v3(
        self, extra_params: dict[str, str] | None = None,
    ) -> list[UniqloProduct]:
        """Paginate the v3 products endpoint and normalise each item to v5 format."""
        return await self._paginate(self._fetch_page_v3, extra_params, label="v3")

    async def _fetch_page(
        self,
        offset: int,
        extra_params: dict[str, str] | None = None,
    ) -> UniqloApiResponse | None:
        params: dict[str, Any] = {
            "offset": offset,
            "limit": PAGE_SIZE,
            **_HTTP_FAILURE_PARAMS,
        }
        if extra_params:
            params.update(extra_params)

        try:
            resp = await self._request(
                self._base_url,
                params=params,
                label=f"page(offset={offset})",
            )
            data = resp.json()

            if data.get("status") != "ok":
                logger.error("API returned non-ok status: %s", data)
                return None

            return UniqloApiResponse.model_validate(data)

        except Exception:
            logger.exception(
                "Failed to fetch page at offset %d", offset,
            )
            return None

    # ------------------------------------------------------------------
    # v3 API support (used by Thailand, Philippines, …)
    # ------------------------------------------------------------------

    async def _fetch_page_v3(
        self,
        offset: int,
        extra_params: dict[str, str] | None = None,
    ) -> UniqloApiResponse | None:
        params: dict[str, Any] = {
            "offset": offset,
            "limit": PAGE_SIZE,
            **_HTTP_FAILURE_PARAMS,
        }
        if extra_params:
            params.update(extra_params)

        try:
            resp = await self._request(
                self._base_url_v3,
                params=params,
                label=f"v3-page(offset={offset})",
            )
            data = resp.json()

            if data.get("status") != "ok":
                logger.error("v3 API returned non-ok status: %s", data)
                return None

            items = data.get("result", {}).get("items", [])
            data["result"]["items"] = [
                _normalize_v3_product(item) for item in items
            ]
            return UniqloApiResponse.model_validate(data)

        except Exception:
            logger.exception(
                "Failed to fetch v3 page at offset %d", offset,
            )
            return None
