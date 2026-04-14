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
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

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
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Environment variable → config mapping
# ---------------------------------------------------------------------------

_ENV_MAP: list[tuple[str, list[str], str]] = [
    # (ENV_VAR_NAME, config_path_segments, type)
    # -- uniqlo --
    ("UNIQLO_COUNTRY",              ["uniqlo", "country"],                       "str"),
    ("UNIQLO_CHECK_INTERVAL",       ["uniqlo", "check_interval_minutes"],        "int"),
    ("UNIQLO_SALE_PATHS",           ["uniqlo", "sale_paths"],                    "list"),
    # -- filters --
    ("FILTER_GENDER",               ["filters", "gender"],                       "list"),
    ("FILTER_MIN_SALE_PERCENTAGE",  ["filters", "min_sale_percentage"],          "float"),
    ("FILTER_SIZES_CLOTHING",       ["filters", "sizes", "clothing"],            "list"),
    ("FILTER_SIZES_PANTS",          ["filters", "sizes", "pants"],               "list"),
    ("FILTER_SIZES_SHOES",          ["filters", "sizes", "shoes"],               "list"),
    ("FILTER_SIZES_ONE_SIZE",       ["filters", "sizes", "one_size"],            "bool"),
    ("FILTER_WATCHED_URLS",         ["filters", "watched_urls"],                 "list"),
    # -- notifications --
    ("NOTIFY_ON",                   ["notifications", "notify_on"],              "str"),
    ("PREVIEW_CLI",                 ["notifications", "preview_cli"],            "bool"),
    ("PREVIEW_HTML",                ["notifications", "preview_html"],           "bool"),
    # -- telegram --
    ("TELEGRAM_ENABLED",            ["notifications", "channels", "telegram", "enabled"],   "bool"),
    ("TELEGRAM_BOT_TOKEN",          ["notifications", "channels", "telegram", "bot_token"], "str"),
    ("TELEGRAM_CHAT_ID",            ["notifications", "channels", "telegram", "chat_id"],   "str"),
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
    country: str = "de/de"
    check_interval_minutes: int = Field(default=30, ge=1)
    sale_paths: list[str] = Field(default_factory=list)


class SizeFilters(BaseModel):
    clothing: list[str] = Field(default_factory=list)
    pants: list[str] = Field(default_factory=list)
    shoes: list[str] = Field(default_factory=list)
    one_size: bool = False


class FilterConfig(BaseModel):
    gender: list[str] = Field(default_factory=lambda: ["men", "women"])
    min_sale_percentage: float = Field(default=50.0, ge=0, le=100)
    sizes: SizeFilters = Field(default_factory=SizeFilters)
    watched_urls: list[str] = Field(default_factory=list)


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
    preview_cli: bool = False
    preview_html: bool = False
    notify_on: Literal["all_then_new", "new_deals", "every_check"] = "all_then_new"
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)


class AppConfig(BaseModel):
    uniqlo: UniqloConfig = Field(default_factory=UniqloConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    notifications: NotificationConfig = Field(default_factory=NotificationConfig)

    @model_validator(mode="after")
    def _normalise_gender(self) -> "AppConfig":
        self.filters.gender = [g.upper() for g in self.filters.gender]
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
        cc = self._CLIENT_ID_COUNTRY_OVERRIDES.get(self.country_code, self.country_code)
        return f"uq.{cc}.web-spa"

    @property
    def product_page_base(self) -> str:
        return f"https://www.uniqlo.com/{self.uniqlo.country}/products"

    _SEA_COUNTRIES: frozenset[str] = frozenset({"ph", "th", "sg"})

    @property
    def uses_sea_url_format(self) -> bool:
        """Return True for SE-Asian stores that use colorCode/sizeCode URL params."""
        return self.country_code in self._SEA_COUNTRIES


def _deep_update_yaml(target: dict, source: dict) -> None:
    """Recursively merge *source* into *target*, preserving YAML comments.

    ruamel.yaml attaches comments that appear *between* the last item of a
    block sequence and the next mapping key to that last sequence item.
    A naive ``target[key] = plain_list`` would discard those comments, so
    we rebuild a ``CommentedSeq`` and transplant the trailing comment from
    the old sequence's last item to the new one.
    """
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update_yaml(target[key], value)
        elif isinstance(value, list):
            old = target.get(key)
            new_seq = CommentedSeq(value)
            if isinstance(old, CommentedSeq) and old.ca.items:
                last_old = max(old.ca.items)
                last_new = len(new_seq) - 1
                if last_new >= 0:
                    new_seq.ca.items[last_new] = old.ca.items[last_old]
            target[key] = new_seq
        else:
            target[key] = value


def _write_yaml(data: dict[str, Any], path: Path) -> None:
    """Write config to YAML, preserving existing comments when possible."""
    rt = YAML()
    rt.preserve_quotes = True

    if path.exists():
        existing = rt.load(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            _deep_update_yaml(existing, data)
            to_write = existing
        else:
            to_write = data
    else:
        to_write = data

    with path.open("w", encoding="utf-8") as fh:
        rt.dump(to_write, fh)
    logger.info("Configuration written to %s", path)


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
