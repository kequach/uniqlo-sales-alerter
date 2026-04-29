"""Configuration loading and validation.

Reads ``config.yaml``, resolves ``${ENV_VAR}`` placeholders from environment
variables, and exposes the result as typed Pydantic models.

When no YAML file is present the entire configuration can be supplied via
environment variables (see :func:`_config_from_env`).  Env vars are also
applied as overrides on top of a YAML file when both are present.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

logger = logging.getLogger(__name__)

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")
_DEFAULT_CONFIG_PATH = Path.cwd() / "config.yaml"


def _resolve_env_vars(value: object) -> object:
    """Recursively walk a data structure and replace ``${VAR}`` with ``os.environ[VAR]``."""
    if isinstance(value, str):
        def _replacer(match: re.Match[str]) -> str:
            var = match.group(1)
            resolved = os.environ.get(var, "")
            if not resolved:
                logger.warning("Environment variable %s is not set", var)
            return resolved

        return _ENV_VAR_RE.sub(_replacer, value)
    if isinstance(value, dict):
        return {key: _resolve_env_vars(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Environment variable → config mapping
# ---------------------------------------------------------------------------

_ENV_MAP: list[tuple[str, list[str], str]] = [
    # (ENV_VAR_NAME, config_path_segments, type)
    # -- uniqlo --
    ("UNIQLO_COUNTRY",              ["uniqlo", "country"],                       "str"),
    ("UNIQLO_CHECK_INTERVAL",       ["uniqlo", "check_interval_minutes"],        "int"),
    ("SCHEDULED_CHECKS",            ["uniqlo", "scheduled_checks"],              "list"),
    ("UNIQLO_SALE_PATHS",           ["uniqlo", "sale_paths"],                    "list"),
    # -- filters --
    ("FILTER_GENDER",               ["filters", "gender"],                       "list"),
    ("FILTER_MIN_SALE_PERCENTAGE",  ["filters", "min_sale_percentage"],          "float"),
    ("FILTER_SIZES_CLOTHING",       ["filters", "sizes", "clothing"],            "list"),
    ("FILTER_SIZES_PANTS",          ["filters", "sizes", "pants"],               "list"),
    ("FILTER_SIZES_SHOES",          ["filters", "sizes", "shoes"],               "list"),
    ("FILTER_SIZES_ONE_SIZE",       ["filters", "sizes", "one_size"],            "bool"),
    ("FILTER_WATCHED_URLS",         ["filters", "watched_urls"],                 "list"),
    ("FILTER_IGNORED_IDS",          ["filters", "ignored_products"],             "list"),
    ("FILTER_IGNORED_KEYWORDS",     ["filters", "ignored_keywords"],             "list"),
    # -- server --
    ("SERVER_URL",                  ["server_url"],                              "str"),
    ("PORT",                        ["port"],                                    "int"),
    # -- notifications --
    ("NOTIFY_ON",                   ["notifications", "notify_on"],              "str"),
    ("CHECK_ON_STARTUP",            ["notifications", "check_on_startup"],       "bool"),
    ("PREVIEW_CLI",                 ["notifications", "preview_cli"],            "bool"),
    ("PREVIEW_HTML",                ["notifications", "preview_html"],           "bool"),
    ("NOTIFY_LOW_STOCK_THRESHOLD",       ["notifications", "low_stock_threshold"],       "int"),
    ("NOTIFY_SUPPRESS_LOW_STOCK_ALERTS", ["notifications", "suppress_low_stock_alerts"], "bool"),
    # -- telegram --
    ("TELEGRAM_ENABLED",            ["notifications", "channels", "telegram", "enabled"],   "bool"),
    ("TELEGRAM_BOT_TOKEN",          ["notifications", "channels", "telegram", "bot_token"], "str"),
    ("TELEGRAM_CHAT_ID",            ["notifications", "channels", "telegram", "chat_id"],   "str"),
    # -- quiet hours --
    ("QUIET_HOURS_ENABLED",         ["quiet_hours", "enabled"],                             "bool"),
    ("QUIET_HOURS_START",           ["quiet_hours", "start"],                               "str"),
    ("QUIET_HOURS_END",             ["quiet_hours", "end"],                                 "str"),
    # -- email / SMTP --
    ("EMAIL_ENABLED",               ["notifications", "channels", "email", "enabled"],      "bool"),
    ("SMTP_HOST",                   ["notifications", "channels", "email", "smtp_host"],    "str"),
    ("SMTP_PORT",                   ["notifications", "channels", "email", "smtp_port"],    "int"),
    ("SMTP_USE_TLS",                ["notifications", "channels", "email", "use_tls"],      "bool"),
    ("SMTP_USER",                   ["notifications", "channels", "email", "smtp_user"],    "str"),
    ("SMTP_PASSWORD",               ["notifications", "channels", "email", "smtp_password"],"str"),
    ("SMTP_FROM",                   ["notifications", "channels", "email", "from_address"], "str"),
    ("SMTP_TO",                     ["notifications", "channels", "email", "to_addresses"], "list"),
]


def _coerce(value: str, type_hint: str) -> Any:
    """Convert a raw env var string to the appropriate Python type."""
    if type_hint == "int":
        return int(value)
    if type_hint == "float":
        return float(value)
    if type_hint == "bool":
        return value.strip().lower() in ("1", "true", "yes")
    if type_hint == "list":
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _set_nested(data: dict, keys: list[str], value: Any) -> None:
    """Set a value in a nested dict, creating intermediate dicts as needed."""
    for key in keys[:-1]:
        data = data.setdefault(key, {})
    data[keys[-1]] = value


def _config_from_env() -> dict:
    """Build a (partial) config dict from environment variables.

    Only env vars that are actually set are included; missing ones are
    left to Pydantic defaults.
    """
    result: dict[str, Any] = {}
    for env_name, path, type_hint in _ENV_MAP:
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        _set_nested(result, path, _coerce(raw, type_hint))
    return result


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class UniqloConfig(BaseModel):
    """Uniqlo API connection and scheduling settings."""

    country: str = "de/de"
    check_interval_minutes: int = Field(default=30, ge=0)
    scheduled_checks: list[str] = Field(default_factory=list)
    sale_paths: list[str] = Field(default_factory=list)

    @field_validator("scheduled_checks", mode="before")
    @classmethod
    def _validate_scheduled_checks(cls, v: Any) -> list[str]:
        """Validate and normalise scheduled check times to HH:MM format."""
        import time as _time

        if not isinstance(v, list):
            return v
        cleaned: list[str] = []
        for entry in v:
            if isinstance(entry, int):
                # PyYAML (YAML 1.1) parses "8:00" as sexagesimal → 480
                hours, minutes = divmod(entry, 60)
                if 0 <= hours <= 23 and 0 <= minutes <= 59:
                    entry = f"{hours:02d}:{minutes:02d}"
                else:
                    raise ValueError(
                        f"Invalid scheduled check time (numeric {entry}) — "
                        f"expected 24-hour HH:MM format (e.g. '12:00')"
                    ) from None
            else:
                entry = str(entry).strip()
            if not entry:
                continue
            try:
                _time.strptime(entry, "%H:%M")
            except ValueError:
                raise ValueError(
                    f"Invalid scheduled check time '{entry}' — "
                    f"expected 24-hour HH:MM format (e.g. '12:00')"
                ) from None
            cleaned.append(entry)
        return cleaned


class SizeFilters(BaseModel):
    clothing: list[str] = Field(default_factory=list)
    pants: list[str] = Field(default_factory=list)
    shoes: list[str] = Field(default_factory=list)
    one_size: bool = False


def parse_uniqlo_url(url: str) -> dict[str, str]:
    """Extract product fields from a Uniqlo product URL.

    Handles both URL styles:
    * ``display_code``: ``…/{id}/{priceGroup}?colorDisplayCode=XX&sizeDisplayCode=YYY``
    * ``code``:         ``…/{id}?colorCode=COLXX&sizeCode=SMAYYY``

    For the ``code`` style, the ``COL``/``SMA``/``INS``/… prefix is
    stripped so that the returned ``color``/``size`` values are always the
    short display-code form used internally.
    """
    import re
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    parts = [segment for segment in parsed.path.split("/") if segment]
    pid = pg = ""
    for i, seg in enumerate(parts):
        if seg == "products" and i + 1 < len(parts):
            pid = parts[i + 1]
            if i + 2 < len(parts):
                pg = parts[i + 2]
            break
    params = parse_qs(parsed.query)

    color = params.get("colorDisplayCode", [""])[0]
    size = params.get("sizeDisplayCode", [""])[0]

    if not color:
        raw = params.get("colorCode", [""])[0]
        color = re.sub(r"^[A-Z]+", "", raw)
    if not size:
        raw = params.get("sizeCode", [""])[0]
        size = re.sub(r"^[A-Z]+", "", raw)

    return {
        "id": pid,
        "price_group": pg or "00",
        "color": color,
        "size": size,
    }


class WatchedVariant(BaseModel):
    """A specific product colour+size combination to always track."""
    url: str = ""
    id: str = ""
    price_group: str = "00"
    name: str = ""
    color: str = ""
    color_name: str = ""
    size: str = ""
    size_name: str = ""

    @model_validator(mode="after")
    def _fill_from_url(self) -> "WatchedVariant":
        """Parse ``id``, ``color``, ``size``, ``price_group`` from the URL."""
        if self.url and not self.id:
            fields = parse_uniqlo_url(self.url)
            self.id = fields["id"]
            self.price_group = fields["price_group"]
            self.color = self.color or fields["color"]
            self.size = self.size or fields["size"]
        return self


class IgnoredProduct(BaseModel):
    """A product to suppress from all results (any colour/size)."""
    id: str = ""
    url: str = ""
    name: str = ""

    @model_validator(mode="after")
    def _fill_from_url(self) -> "IgnoredProduct":
        """Extract product ID from a URL if ``id`` is blank."""
        if self.url and not self.id:
            fields = parse_uniqlo_url(self.url)
            self.id = fields["id"]
        return self


class FilterConfig(BaseModel):
    gender: list[str] = Field(default_factory=lambda: ["men", "women"])
    min_sale_percentage: float = Field(default=50.0, ge=0, le=100)
    sizes: SizeFilters = Field(default_factory=SizeFilters)
    watched_variants: list[WatchedVariant] = Field(default_factory=list)
    ignored_products: list[IgnoredProduct] = Field(default_factory=list)
    ignored_keywords: list[str] = Field(default_factory=list)
    watched_urls: list[str] = Field(default_factory=list, exclude=True)

    @field_validator("ignored_products", mode="before")
    @classmethod
    def _coerce_ignored(cls, v: Any) -> Any:
        """Allow env-var shorthand: plain ID strings become objects."""
        if isinstance(v, list):
            return [{"id": entry} if isinstance(entry, str) else entry for entry in v]
        return v

    @field_validator("ignored_keywords", mode="before")
    @classmethod
    def _coerce_keywords(cls, v: Any) -> Any:
        """Allow env-var shorthand: a single comma-separated string becomes a list."""
        if isinstance(v, str):
            return [k.strip() for k in v.split(",") if k.strip()]
        return v

    @model_validator(mode="after")
    def _migrate_watched_urls(self) -> "FilterConfig":
        """Auto-migrate legacy ``watched_urls`` to ``watched_variants``."""
        if not self.watched_urls:
            return self
        for url in self.watched_urls:
            fields = parse_uniqlo_url(url)
            if not fields["id"]:
                continue
            self.watched_variants.append(WatchedVariant(
                url=url, **fields,
            ))
        self.watched_urls = []
        return self


class TelegramChannelConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class EmailChannelConfig(BaseModel):
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    use_tls: bool = True
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)


class ChannelsConfig(BaseModel):
    telegram: TelegramChannelConfig = Field(default_factory=TelegramChannelConfig)
    email: EmailChannelConfig = Field(default_factory=EmailChannelConfig)


class NotificationConfig(BaseModel):
    """Notification behaviour and channel configuration."""

    preview_cli: bool = False
    preview_html: bool = False
    notify_on: Literal["all_then_new", "new_deals", "every_check"] = "all_then_new"
    check_on_startup: bool = True
    low_stock_threshold: int = Field(default=3, ge=0)
    suppress_low_stock_alerts: bool = False
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)


class QuietHoursConfig(BaseModel):
    """Suppress API calls and notifications during a daily time window."""
    enabled: bool = False
    start: str = "01:00"
    end: str = "08:00"

    @model_validator(mode="after")
    def _validate_times(self) -> "QuietHoursConfig":
        import time as _time
        for label, value in [("start", self.start), ("end", self.end)]:
            try:
                _time.strptime(value, "%H:%M")
            except ValueError:
                raise ValueError(
                    f"quiet_hours.{label} must be HH:MM (got {value!r})"
                )
        return self


# ---------------------------------------------------------------------------
# Country capabilities registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CountryCapabilities:
    """Describes which API endpoints a country uses.

    ``listing_sources`` — which sale-product listing endpoints return data.
    ``stock_api``       — ``"v5"`` (reliable v5 stock), or ``"none"``
                          (v5 stock returns all-OOS; keep listing data).
    ``is_limited``      — ``True`` when the API does not expose original
                          prices (discount % unavailable).
    ``url_style``       — ``"display_code"`` (default) uses query params
                          ``colorDisplayCode``/``sizeDisplayCode`` with
                          ``/{priceGroup}`` in the path.
                          ``"code"`` uses ``colorCode``/``sizeCode`` (the
                          full API ``code`` values like ``COL09``,
                          ``SMA003``) without a price-group path segment.
    """
    listing_sources: tuple[str, ...] = ("v5_disc",)
    stock_api: str = "v5"
    is_limited: bool = False
    url_style: str = "display_code"


_COUNTRY_CAPABILITIES: dict[str, CountryCapabilities] = {
    # Europe — v5 discount only, full pricing
    "de": CountryCapabilities(),
    "uk": CountryCapabilities(),
    "fr": CountryCapabilities(),
    "es": CountryCapabilities(),
    "it": CountryCapabilities(),
    "be": CountryCapabilities(),
    "nl": CountryCapabilities(),
    "dk": CountryCapabilities(),
    "se": CountryCapabilities(),
    # Asia-Pacific — v5, full pricing
    "au": CountryCapabilities(),
    "in": CountryCapabilities(),
    "id": CountryCapabilities(listing_sources=("v5_disc", "v5_ltd")),
    "vn": CountryCapabilities(listing_sources=("v5_disc", "v5_ltd")),
    "my": CountryCapabilities(listing_sources=("v5_disc", "v5_ltd")),
    # SEA v3 stores — stock API unreliable, storefront uses code-style URLs
    "ph": CountryCapabilities(
        listing_sources=("v3_disc", "v3_ltd"), stock_api="none",
        url_style="code",
    ),
    "th": CountryCapabilities(
        listing_sources=("v5_ltd", "v3_disc", "v3_ltd"), stock_api="none",
        url_style="code",
    ),
    # Limited-support countries — no discount %
    "us": CountryCapabilities(
        listing_sources=("v5_disc", "v5_ltd"), is_limited=True,
    ),
    "ca": CountryCapabilities(
        listing_sources=("v5_disc", "v5_ltd"), is_limited=True,
    ),
    "jp": CountryCapabilities(
        listing_sources=("v5_disc", "v5_ltd"), is_limited=True,
    ),
    "kr": CountryCapabilities(is_limited=True),
    "sg": CountryCapabilities(
        listing_sources=("v5_disc", "sale_paths"), is_limited=True,
    ),
}

_DEFAULT_CAPABILITIES = CountryCapabilities()


class AppConfig(BaseModel):
    uniqlo: UniqloConfig = Field(default_factory=UniqloConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)
    quiet_hours: QuietHoursConfig = Field(default_factory=QuietHoursConfig)
    server_url: str = ""
    port: int = 8000

    @model_validator(mode="after")
    def _normalise_gender(self) -> "AppConfig":
        self.filters.gender = [gender.upper() for gender in self.filters.gender]
        return self

    @property
    def country_code(self) -> str:
        """First segment of the country path, e.g. ``'de'`` from ``'de/de'``."""
        return self.uniqlo.country.split("/")[0]

    @property
    def lang_code(self) -> str:
        """Second segment of the country path, e.g. ``'de'`` from ``'de/de'``."""
        parts = self.uniqlo.country.split("/")
        return parts[1] if len(parts) > 1 else parts[0]

    @property
    def base_url(self) -> str:
        return f"https://www.uniqlo.com/{self.country_code}/api/commerce/v5/{self.lang_code}/products"

    @property
    def base_url_v3(self) -> str:
        return f"https://www.uniqlo.com/{self.country_code}/api/commerce/v3/{self.lang_code}/products"

    _CLIENT_ID_COUNTRY_OVERRIDES: dict[str, str] = {"uk": "gb"}

    @property
    def client_id(self) -> str:
        country = self._CLIENT_ID_COUNTRY_OVERRIDES.get(self.country_code, self.country_code)
        return f"uq.{country}.web-spa"

    @property
    def product_page_base(self) -> str:
        return f"https://www.uniqlo.com/{self.uniqlo.country}/products"

    @property
    def capabilities(self) -> CountryCapabilities:
        """API capabilities for the configured country."""
        return _COUNTRY_CAPABILITIES.get(
            self.country_code, _DEFAULT_CAPABILITIES,
        )

    @property
    def full_server_url(self) -> str:
        """``server_url`` with the configured port appended."""
        if not self.server_url:
            return ""
        return f"{self.server_url.rstrip('/')}:{self.port}"


def _transplant_seq_comments(
    old_seq: CommentedSeq,
    new_seq: CommentedSeq,
) -> None:
    """Transfer trailing/between-key comments from *old_seq* to *new_seq*.

    ruamel.yaml can store such comments in two places depending on whether
    the sequence contains scalars or mappings:

    **Scalar sequences** — comments after the last item are stored in
    ``seq.ca.items[last_index]`` (a per-item comment tuple on the seq).

    **Mapping sequences** — comments after the last item are stored as a
    post-comment (slot 2) on the last ``CommentedMap`` item's last key.
    """
    if not old_seq or not new_seq:
        return

    last_old_idx = len(old_seq) - 1
    last_new_idx = len(new_seq) - 1

    if old_seq.ca.comment:
        new_seq.ca.comment = old_seq.ca.comment

    # Per-item comments on the seq itself (scalar sequences)
    if old_seq.ca.items:
        for idx, entry in old_seq.ca.items.items():
            target_idx = last_new_idx if idx == last_old_idx else idx
            if 0 <= target_idx < len(new_seq):
                new_seq.ca.items[target_idx] = entry

    # Post-comment on last CommentedMap item's last key (mapping sequences)
    last_old_item = old_seq[-1]
    if not isinstance(last_old_item, CommentedMap) or not last_old_item.ca.items:
        return

    for key in reversed(list(last_old_item.keys())):
        entry = last_old_item.ca.items.get(key)
        if not entry or len(entry) <= 2 or entry[2] is None:
            continue

        last_new_item = new_seq[-1]
        if isinstance(last_new_item, dict) and not isinstance(last_new_item, CommentedMap):
            new_seq[-1] = CommentedMap(last_new_item)
            last_new_item = new_seq[-1]

        if isinstance(last_new_item, CommentedMap) and last_new_item:
            tgt = key if key in last_new_item else list(last_new_item.keys())[-1]
            ca_entry = list(
                last_new_item.ca.items.get(tgt, [None, None, None, None]),
            )
            while len(ca_entry) < 3:
                ca_entry.append(None)
            ca_entry[2] = entry[2]
            last_new_item.ca.items[tgt] = ca_entry
        break


def _deep_update_yaml(target: dict, source: dict) -> None:
    """Recursively merge *source* into *target*, preserving YAML comments.

    ruamel.yaml attaches comments that appear between the end of a block
    sequence and the next mapping key to internal comment structures on
    the sequence or its last item.  When we replace a sequence value with
    a new ``CommentedSeq`` of plain dicts those comments would be lost, so
    we transplant them to the replacement sequence.
    """
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update_yaml(target[key], value)
        elif isinstance(value, list):
            old = target.get(key)
            new_seq = CommentedSeq(value)
            if isinstance(old, CommentedSeq):
                _transplant_seq_comments(old, new_seq)
            target[key] = new_seq
        else:
            target[key] = value


def _write_yaml(data: dict[str, Any], path: Path) -> None:
    """Write config to YAML, preserving existing comments when possible."""
    yaml_writer = YAML()
    yaml_writer.preserve_quotes = True

    if path.exists():
        existing = yaml_writer.load(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            _deep_update_yaml(existing, data)
            to_write = existing
        else:
            to_write = data
    else:
        to_write = data

    with path.open("w", encoding="utf-8") as output:
        yaml_writer.dump(to_write, output)
    logger.debug("Configuration written to %s", path)


def save_config(config: AppConfig, path: Path | str | None = None) -> None:
    """Persist a validated configuration to YAML."""
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    _write_yaml(config.model_dump(), config_path)


def load_config(
    path: Path | str | None = None,
    *,
    apply_env_overrides: bool = True,
) -> AppConfig:
    """Load and validate configuration.

    Resolution order (later wins):

    1. Pydantic defaults
    2. ``config.yaml`` (with ``${VAR}`` placeholder substitution)
    3. Environment variables (``UNIQLO_COUNTRY``, ``FILTER_GENDER``, …)
       — only when *apply_env_overrides* is ``True`` (the default).

    Set *apply_env_overrides* to ``False`` when reloading after a web-UI
    save so that the persisted YAML is the sole source of truth.
    """
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH

    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        resolved: dict = _resolve_env_vars(raw)  # type: ignore[assignment]
    else:
        logger.info("No config file at %s — building config from env vars", config_path)
        resolved = {}

    if apply_env_overrides:
        env_overrides = _config_from_env()
        if env_overrides:
            resolved = _deep_merge(resolved, env_overrides)

    return AppConfig.model_validate(resolved)
