"""Runtime AC capability catalog.

Three-layer fallback:
  1. Live (from Firebase Remote Config) — freshest, process lifetime
  2. Disk cache (HA Store) — survives restarts
  3. Bundled (ac_catalog.py) — ships with the integration, never fails

`get_active()` returns the current frozen Catalog. Capability lookups in
ac.py call this on every read; the cost is one module-attr read.

Live swap is safe: Catalog is frozen, so consumers never see a torn read.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from . import ac_catalog as _bundled
from .firebase_rc_client import (
    FirebaseAuthError,
    FirebaseRcClient,
    FirebaseTransientError,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Catalog:
    available_modes_by_device_code: dict[str, tuple[str, ...]]
    classic_temp_control_codes: frozenset[str]
    extra_windspeed_codes: frozenset[str]
    show_display_mold_codes: frozenset[str]
    single_mode_codes: frozenset[str]
    dehumidifier_codes: frozenset[str]
    # AC_OBSERVED_* are hand-maintained from hardware testing (not in Firebase).
    # Always sourced from the bundled module, even after a live refresh.
    observed_extra_windspeed_codes: frozenset[str]
    observed_turbo_codes: frozenset[str]
    source: Literal["bundled", "cache", "live"]
    # fetched_at: when set, always tz-aware UTC. T6's cooldown arithmetic
    # relies on this. None means "never fetched" (bundled state).
    fetched_at: datetime | None
    template_version: int | None


def _build_bundled() -> Catalog:
    return Catalog(
        available_modes_by_device_code=_bundled.AC_AVAILABLE_MODES_BY_DEVICE_CODE,
        classic_temp_control_codes=_bundled.AC_CLASSIC_TEMP_CONTROL_CODES,
        extra_windspeed_codes=_bundled.AC_EXTRA_WINDSPEED_CODES,
        show_display_mold_codes=_bundled.AC_SHOW_DISPLAY_MOLD_CODES,
        single_mode_codes=_bundled.AC_SINGLE_MODE_CODES,
        dehumidifier_codes=_bundled.AC_DEHUMIDIFIER_CODES,
        observed_extra_windspeed_codes=_bundled.AC_OBSERVED_EXTRA_WINDSPEED_CODES,
        observed_turbo_codes=_bundled.AC_OBSERVED_TURBO_CODES,
        source="bundled",
        fetched_at=None,
        template_version=None,
    )


_BUNDLED: Catalog = _build_bundled()
_active: Catalog = _BUNDLED


def get_active() -> Catalog:
    """Return the currently active Catalog. Cheap; safe to call on every lookup."""
    return _active


def get_bundled() -> Catalog:
    """Return the bundled-floor Catalog. Useful for diagnostics + tests."""
    return _BUNDLED


_REQUIRED_RC_KEYS = (
    "ac_available_modes",
    "ac_classic_temp_control",
    "ac_extra_windspeed",
    "ac_show_display_mold",
    "ac_single_mode_codes",
    "ac_dehumidifier_codes",
)


class CatalogParseError(ValueError):
    """Raised when a Firebase RC payload is missing keys or malformed."""


def _decode_codes_list(raw: str) -> frozenset[str]:
    parsed = json.loads(raw)
    codes = parsed.get("codes", [])
    return frozenset(str(int(c)) for c in codes)


def _decode_modes_map(raw: str) -> dict[str, tuple[str, ...]]:
    parsed = json.loads(raw)
    return {str(k): tuple(str(m) for m in v) for k, v in parsed.items()}


def parse_firebase_payload(
    payload: dict,
    *,
    fetched_at: datetime | None = None,
) -> Catalog:
    """Build a live Catalog from a Firebase RC fetch response.

    AC_OBSERVED_* fields are taken from the bundled module — they reflect
    hardware-testing notes that are never in Firebase.
    """
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        raise CatalogParseError("RC payload missing 'entries' dict")

    missing = [k for k in _REQUIRED_RC_KEYS if k not in entries]
    if missing:
        raise CatalogParseError(f"RC payload missing required keys: {missing}")

    try:
        modes = _decode_modes_map(entries["ac_available_modes"])
        classic = _decode_codes_list(entries["ac_classic_temp_control"])
        extra = _decode_codes_list(entries["ac_extra_windspeed"])
        mold = _decode_codes_list(entries["ac_show_display_mold"])
        single = _decode_codes_list(entries["ac_single_mode_codes"])
        dehum = _decode_codes_list(entries["ac_dehumidifier_codes"])
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError) as err:
        raise CatalogParseError(f"Malformed RC value: {err}") from err

    template_version = payload.get("templateVersion")
    if isinstance(template_version, str):
        try:
            template_version = int(template_version)
        except ValueError:
            template_version = None

    if fetched_at is None:
        fetched_at = datetime.now(tz=timezone.utc)

    return Catalog(
        available_modes_by_device_code=modes,
        classic_temp_control_codes=classic,
        extra_windspeed_codes=extra,
        show_display_mold_codes=mold,
        single_mode_codes=single,
        dehumidifier_codes=dehum,
        observed_extra_windspeed_codes=_BUNDLED.observed_extra_windspeed_codes,
        observed_turbo_codes=_BUNDLED.observed_turbo_codes,
        source="live",
        fetched_at=fetched_at,
        template_version=template_version,
    )


CACHE_SCHEMA_VERSION = 1


def dump_for_cache(cat: Catalog) -> dict:
    """Serialize Catalog to a JSON-safe dict for HA Store."""
    return {
        "version": CACHE_SCHEMA_VERSION,
        "fetched_at": cat.fetched_at.isoformat() if cat.fetched_at else None,
        "template_version": cat.template_version,
        "configs": {
            "ac_available_modes": {
                code: list(modes)
                for code, modes in cat.available_modes_by_device_code.items()
            },
            "ac_classic_temp_control": {"codes": sorted(cat.classic_temp_control_codes)},
            "ac_extra_windspeed": {"codes": sorted(cat.extra_windspeed_codes)},
            "ac_show_display_mold": {"codes": sorted(cat.show_display_mold_codes)},
            "ac_single_mode_codes": {"codes": sorted(cat.single_mode_codes)},
            "ac_dehumidifier_codes": {"codes": sorted(cat.dehumidifier_codes)},
        },
    }


def load_from_cache(raw: dict | None) -> Catalog | None:
    """Rebuild a Catalog from a dump_for_cache() output. Returns None on any
    malformed input — callers should fall back to bundled."""
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != CACHE_SCHEMA_VERSION:
        return None
    configs = raw.get("configs")
    if not isinstance(configs, dict):
        return None
    if any(k not in configs for k in _REQUIRED_RC_KEYS):
        return None

    try:
        modes_raw = configs["ac_available_modes"]
        modes = {str(k): tuple(str(m) for m in v) for k, v in modes_raw.items()}
        classic = frozenset(str(c) for c in configs["ac_classic_temp_control"]["codes"])
        extra = frozenset(str(c) for c in configs["ac_extra_windspeed"]["codes"])
        mold = frozenset(str(c) for c in configs["ac_show_display_mold"]["codes"])
        single = frozenset(str(c) for c in configs["ac_single_mode_codes"]["codes"])
        dehum = frozenset(str(c) for c in configs["ac_dehumidifier_codes"]["codes"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None

    fetched_at_raw = raw.get("fetched_at")
    fetched_at: datetime | None = None
    if isinstance(fetched_at_raw, str):
        try:
            parsed = datetime.fromisoformat(fetched_at_raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            # Pin invariant: fetched_at is always tz-aware UTC (T6 cooldown
            # arithmetic relies on it). A hand-edited cache file with a naive
            # timestamp is treated as UTC rather than crashing.
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            fetched_at = parsed

    template_version = raw.get("template_version")
    if not isinstance(template_version, int) or isinstance(template_version, bool):
        template_version = None

    return Catalog(
        available_modes_by_device_code=modes,
        classic_temp_control_codes=classic,
        extra_windspeed_codes=extra,
        show_display_mold_codes=mold,
        single_mode_codes=single,
        dehumidifier_codes=dehum,
        observed_extra_windspeed_codes=_BUNDLED.observed_extra_windspeed_codes,
        observed_turbo_codes=_BUNDLED.observed_turbo_codes,
        source="cache",
        fetched_at=fetched_at,
        template_version=template_version,
    )


REFRESH_COOLDOWN = timedelta(hours=1)
REFRESH_INTERVAL = timedelta(hours=24)
INITIAL_FETCH_DELAY = timedelta(seconds=30)
FAILURE_REPAIR_THRESHOLD = 3

_STORE_VERSION = 1
_REPAIR_ISSUE_ID = "catalog_refresh_failing"


def _store_key() -> str:
    """Return the HA Store key. Lazy to avoid module-load HA imports.

    `const.py` is plain Python (no HA deps), so a top-level import would also
    work, but keeping this lazy matches the existing pattern used by
    async_refresh / async_initialize and gives one source of truth for any
    future diagnostics path (T8).
    """
    from .const import DOMAIN

    return f"{DOMAIN}_catalog"


_consecutive_failures: int = 0
_last_outcome: str = "never_attempted"
_refresh_lock: asyncio.Lock = asyncio.Lock()


def _bump_failures_and_maybe_open_issue(hass, kind: str, detail: str) -> None:
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= FAILURE_REPAIR_THRESHOLD:
        from homeassistant.helpers import issue_registry as ir
        from .const import DOMAIN

        ir.async_create_issue(
            hass,
            DOMAIN,
            _REPAIR_ISSUE_ID,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="catalog_refresh_failing",
            translation_placeholders={"kind": kind, "detail": detail[:200]},
        )


def _clear_repair_issue(hass) -> None:
    from homeassistant.helpers import issue_registry as ir
    from .const import DOMAIN

    ir.async_delete_issue(hass, DOMAIN, _REPAIR_ISSUE_ID)


@dataclass(frozen=True)
class RefreshResult:
    status: Literal[
        "success",
        "skipped_cooldown",
        "auth_error",
        "transient_error",
        "parse_error",
    ]
    detail: str | None = None


async def _refresh_with_client(client: FirebaseRcClient) -> RefreshResult:
    """Inner refresh entry point — testable without HA setup.

    Public callers should use async_refresh(hass) which builds the client.
    Concurrent calls serialize through _refresh_lock so we don't double-fetch.

    Counter ownership note: this function does NOT mutate
    `_consecutive_failures`. async_refresh() owns that bookkeeping (paired
    with HA repair-issue create/clear), based on the returned status.
    """
    global _active, _last_outcome

    async with _refresh_lock:
        if _active.fetched_at is not None:
            age = datetime.now(tz=timezone.utc) - _active.fetched_at
            if age < REFRESH_COOLDOWN:
                _last_outcome = "skipped_cooldown"
                return RefreshResult("skipped_cooldown", f"age={age.total_seconds():.0f}s")

        try:
            token = await client.async_register_installation()
            payload = await client.async_fetch_remote_config(fis_token=token)
        except FirebaseAuthError as err:
            _last_outcome = "auth_error"
            _LOGGER.error("Firebase RC auth error: %s", err)
            return RefreshResult("auth_error", str(err))
        except FirebaseTransientError as err:
            _last_outcome = "transient_error"
            _LOGGER.warning("Firebase RC transient error: %s", err)
            return RefreshResult("transient_error", str(err))

        try:
            new_active = parse_firebase_payload(payload)
        except CatalogParseError as err:
            _last_outcome = "parse_error"
            _LOGGER.error("Firebase RC parse error: %s", err)
            return RefreshResult("parse_error", str(err))

        _active = new_active
        _last_outcome = "success"
        return RefreshResult("success", f"template_version={new_active.template_version}")


def _build_client(hass) -> FirebaseRcClient:
    # Lazy HA imports — keep top-level catalog.py importable in unit tests
    # that stub `homeassistant` minimally.
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    from .const import (
        FIREBASE_API_KEY,
        FIREBASE_APP_ID,
        FIREBASE_PROJECT_ID,
        FIREBASE_PROJECT_NUMBER,
    )

    return FirebaseRcClient(
        async_get_clientsession(hass),
        project_id=FIREBASE_PROJECT_ID,
        project_number=FIREBASE_PROJECT_NUMBER,
        app_id=FIREBASE_APP_ID,
        api_key=FIREBASE_API_KEY,
    )


async def async_refresh(hass) -> RefreshResult:
    """Fetch latest RC, swap _active, write disk cache. Safe to call concurrently.

    Owns the `_consecutive_failures` counter bookkeeping: bumps it on
    auth/transient/parse errors (opening a HA repair issue at
    FAILURE_REPAIR_THRESHOLD) and resets it on success (clearing any open
    repair issue). `skipped_cooldown` is intentionally a no-op for the
    counter — we didn't actually attempt a fetch, so neither bumping nor
    resetting is appropriate.
    """
    from homeassistant.helpers.storage import Store

    global _consecutive_failures

    client = _build_client(hass)
    result = await _refresh_with_client(client)
    if result.status == "success":
        store: Store = Store(hass, _STORE_VERSION, _store_key())
        await store.async_save(dump_for_cache(_active))
        _consecutive_failures = 0
        _clear_repair_issue(hass)
    elif result.status in ("auth_error", "transient_error", "parse_error"):
        _bump_failures_and_maybe_open_issue(hass, result.status, result.detail or "")
    return result


async def async_initialize(hass) -> Callable[[], None]:
    """Load disk cache (if any), schedule first fetch + periodic refresh.

    Returns a single composite cancel callable that tears down BOTH the
    one-shot initial fetch AND the periodic interval, so callers (T7) can wire
    it into `entry.async_on_unload`. First fetch runs after INITIAL_FETCH_DELAY
    in the background; if the entry unloads inside that window, cancelling the
    one-shot prevents the refresh firing against a torn-down hass. Cache load
    IS awaited so capability lookups during platform setup get the cached
    data, not bundled.
    """
    from homeassistant.helpers.event import (
        async_call_later,
        async_track_time_interval,
    )
    from homeassistant.helpers.storage import Store

    global _active

    store: Store = Store(hass, _STORE_VERSION, _store_key())
    raw = await store.async_load()
    cached = load_from_cache(raw)
    if cached is not None:
        _active = cached
        _LOGGER.debug(
            "Catalog loaded from cache (template_version=%s)", cached.template_version
        )
    elif raw is not None:
        # Schema mismatch / corruption: drop the file so next save rebuilds.
        _LOGGER.warning("Catalog cache unreadable; falling back to bundled")
        await store.async_remove()

    async def _scheduled_refresh(_now=None) -> None:
        try:
            await async_refresh(hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected refresh error: %s", err)

    unsub_initial = async_call_later(
        hass, INITIAL_FETCH_DELAY.total_seconds(), _scheduled_refresh
    )
    unsub_periodic = async_track_time_interval(
        hass, _scheduled_refresh, REFRESH_INTERVAL
    )

    def _cancel() -> None:
        unsub_initial()
        unsub_periodic()

    return _cancel


def get_status() -> dict:
    """Status snapshot for diagnostics."""
    return {
        "source": _active.source,
        "fetched_at": _active.fetched_at.isoformat() if _active.fetched_at else None,
        "template_version": _active.template_version,
        "last_refresh_outcome": _last_outcome,
        "consecutive_failures": _consecutive_failures,
    }
