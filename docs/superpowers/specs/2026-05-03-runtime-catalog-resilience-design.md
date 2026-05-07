# Runtime catalog resilience — design

**Status:** spec drafted, awaiting maintainer review
**Date:** 2026-05-03
**Predecessor decision:** 2026-05-01 brainstorm rejected runtime Firebase fetch ([memory: aifa_smart 整合不做 runtime Firebase fetch](../../../memory)). This spec **reverses** that decision; rationale below.

---

## 1. Problem

The integration ships `custom_components/aifa_smart/ac_catalog.py` — a static snapshot of AIFA's per-device-code AC capability matrix (modes, fan speeds, dehumidifier flag, classic temp control, etc.). The matrix lives in AIFA's Firebase Remote Config and changes when AIFA adds new AC models.

Today the only way to refresh is:

1. RE the Firebase snapshot from a rooted Android (`docs/regen-ac-catalog.md`)
2. Run `tools/regen_ac_catalog.py`
3. Commit + cut an integration release
4. HACS users update

Maintainer (PinLin) plans to release the integration **infrequently**. Without a fresh catalog, new AIFA AC models accumulate as "unknown device_codes" → integration shows the permissive default (`auto, cool, heat, fan, dry`) regardless of whether the AC actually has heat / fan / dry. Existing user installs slowly drift from reality.

**Goal:** decouple catalog freshness from integration releases. Catalog updates should reach existing user installs without requiring a new integration version.

## 2. Non-goals

- Runtime resilience against AIFA cloud API changes (those break control commands, not just metadata, and require code changes anyway).
- Mirroring AIFA app's full Firebase usage — only Remote Config matters. FCM / Analytics / Crashlytics / In-App Messaging stay out of scope.
- Replacing the static `ac_catalog.py` — it remains as a permanent floor.

## 3. Decision: revisit 2026-05-01 rejection

The previous brainstorm rejected runtime Firebase fetch on these grounds; this spec addresses each:

| 2026-05-01 concern | This spec's response |
|---|---|
| AIFA's Firebase API key in repo is risky (TOS, key rotation) | Android Firebase API keys are not secrets — they're shipped in every APK. Key rotation is a real risk but historically AIFA's project-id `1:240868916940:android:a1bbaac859617483224c69` has been stable across 1.2.5. We accept the risk; on rotation, integration falls through to bundled floor and surfaces a HA repair issue. |
| HA users see "integration calls Google" | This is true but mitigated by: (1) call cadence mirrors AIFA app (cold-start + 1h gate + 24h periodic — looser than AIFA), (2) public Firebase Remote Config endpoint, no PII, (3) documented in README. |
| Integration breaks when key rotates | Bundled `ac_catalog.py` floor means control commands keep working. Capability metadata stales until either next integration release or AIFA un-rotates. Repair issue surfaces this to user. |
| Maintenance complexity | Accepted in exchange for "catalog updates without integration release". |

Two alternatives were also brainstormed and rejected for this spec:

- **Approach A (maintainer-controlled remote catalog via GitHub raw):** still requires maintainer to commit + push catalog updates. Lower friction than integration release but doesn't satisfy the "very infrequent maintenance" goal as cleanly as direct Firebase fetch.
- **Approach B (Approach A + scheduled CI agent regen):** still requires the maintainer's local rooted-AVD infrastructure to extract the Firebase snapshot. Not fully automatable from a CI runner.

## 4. Cadence

Mirror AIFA app's pattern with one HA-specific extension:

| Trigger | Behavior |
|---|---|
| HA boot / integration reload | One-shot fetch 30s after `async_setup_entry` (avoid blocking startup), respecting 1h cooldown vs `_active.fetched_at` |
| Every 24h thereafter | Periodic refresh, same 1h cooldown gate (effectively a no-op if a manual reload just happened) |
| HA foreground / lifecycle events | None (HA has no equivalent; AIFA app also doesn't refetch here) |
| Capability lookup | Hot path — never triggers fetch, always reads in-memory `_active` |

AIFA app fetches only on cold-start + 1h gate; HA boxes typically run uptime weeks-to-months, so adding a 24h background tick keeps catalog fresh without HA reboots. Worst-case cadence (HA running, no reload) is one fetch per 24h — looser than AIFA app, which a heavy mobile user triggers more often.

## 5. Architecture

```
HA cold start
  └─ ac.py / catalog.py imports → _active = _BUNDLED  (floor, never fails)

async_setup_entry
  └─ catalog.async_initialize(hass)
       ├─ Store.async_load("aifa_smart_catalog")
       │    ├─ valid cache → build Catalog(source="cache"), swap _active
       │    └─ missing/corrupt → stay on bundled
       ├─ async_call_later(30s, async_refresh)        ← first fetch
       └─ async_track_time_interval(async_refresh, 24h)  ← periodic

async_refresh(hass)
  ├─ if _active.fetched_at and now - _active.fetched_at < 1h: return EARLY  (mirror AIFA cooldown; bundled has fetched_at=None so first run always proceeds)
  ├─ FirebaseRcClient.async_register_installation()  → FIS token
  ├─ FirebaseRcClient.async_fetch_remote_config()    → raw dict
  ├─ parse + merge_with_bundled(observed_* preserved) → Catalog(source="live")
  ├─ Store.async_save(disk cache)
  └─ swap _active

Capability lookup (ac.py hot path)
  └─ catalog.get_active() → frozen Catalog → dict.get / set membership (O(1))
```

Three layers, three lifetimes:

- **Bundled (`ac_catalog.py`, ship-time):** floor, never fails, regen workflow unchanged
- **Disk cache (HA install lifetime):** survives HA restarts
- **In-memory live (process lifetime):** freshest

Failure cascades down: live → cache → bundled. Control commands always work.

## 6. Components

### 6.1 `firebase_rc_client.py` (new)

Pure transport layer for Firebase RC + Installations Service.

```python
class FirebaseRcClient:
    def __init__(self, session: aiohttp.ClientSession,
                 project_id: str, app_id: str, api_key: str): ...

    async def async_register_installation(self) -> InstallationToken: ...
    async def async_fetch_remote_config(self,
                                         *, namespace: str = "firebase") -> dict: ...
```

- Constants (`PROJECT_ID`, `APP_ID`, `API_KEY`) hardcoded in `const.py`, sourced from APK / blutter output. Documented as "public Android Firebase config, not secrets."
- FIS token is fetched fresh per refresh (no caching). Trade-off: 2 HTTPS round-trips per refresh, which is fine at the 24h cadence — caching would add complexity without operational benefit.
- Uses `aiohttp_client.async_get_clientsession(hass)` for shared session.
- Raises `FirebaseAuthError` on 4xx, `FirebaseTransientError` on 5xx / network. Caller decides retry / fallback.

### 6.2 `catalog.py` (new)

Façade + orchestration.

```python
@dataclass(frozen=True)
class Catalog:
    available_modes_by_device_code: dict[str, tuple[str, ...]]
    classic_temp_control_codes: frozenset[str]
    extra_windspeed_codes: frozenset[str]
    show_display_mold_codes: frozenset[str]
    single_mode_codes: frozenset[str]
    dehumidifier_codes: frozenset[str]
    observed_extra_windspeed_codes: frozenset[str]  # ALWAYS from bundled
    observed_turbo_codes: frozenset[str]            # ALWAYS from bundled
    source: Literal["bundled", "cache", "live"]
    fetched_at: datetime | None
    template_version: int | None

_BUNDLED: Catalog = ...      # built from ac_catalog.py at import
_active: Catalog = _BUNDLED  # process-wide

def get_active() -> Catalog: ...

async def async_initialize(hass) -> None: ...
async def async_refresh(hass) -> RefreshResult: ...
```

**Merge rule:** Firebase response provides the six `AC_AVAILABLE_MODES` / `AC_*_CODES` keys; `AC_OBSERVED_EXTRA_WINDSPEED_CODES` / `AC_OBSERVED_TURBO_CODES` are never in Firebase and always taken from bundled. Identical to current `regen_ac_catalog.py` behavior.

### 6.3 Disk cache (HA `Store` helper)

`<config>/.storage/aifa_smart_catalog.json`

```json
{
  "version": 1,
  "fetched_at": "2026-05-03T12:34:56+00:00",
  "template_version": 47,
  "configs": {
    "ac_available_modes": { "1": ["auto", "cool", ...], ... },
    "ac_classic_temp_control": { "codes": [...] },
    "ac_extra_windspeed": { "codes": [...] },
    "ac_show_display_mold": { "codes": [...] },
    "ac_single_mode_codes": { "codes": [...] },
    "ac_dehumidifier_codes": { "codes": [...] }
  }
}
```

`version: 1` reserved for future schema migration. `template_version` carried for diagnostics. Schema mismatch → ignore cache, fall back to bundled, refetch on next interval (don't pollute cache with malformed data).

### 6.4 `ac.py` refactor

Replace module-level imports of catalog frozensets with lookup-time `catalog.get_active()` reads:

```python
# Before:
from .ac_catalog import AC_AVAILABLE_MODES_BY_DEVICE_CODE, ...

def device_code_capabilities(code):
    configured_modes = AC_AVAILABLE_MODES_BY_DEVICE_CODE.get(normalized_code)
    extra_windspeed = normalized_code in (
        AC_EXTRA_WINDSPEED_CODES | AC_OBSERVED_EXTRA_WINDSPEED_CODES
    )

# After:
from . import catalog

def device_code_capabilities(code):
    cat = catalog.get_active()
    configured_modes = cat.available_modes_by_device_code.get(normalized_code)
    extra_windspeed = normalized_code in (
        cat.extra_windspeed_codes | cat.observed_extra_windspeed_codes
    )
```

Per-lookup cost: one module-attr read + one frozen-dataclass attr access. Negligible.

Live swap mid-run: safe because `Catalog` is frozen — no torn reads possible; next lookup transparently picks up the new active.

### 6.5 Integration `__init__.py`

Add to `async_setup_entry`, after coordinator construction, before platform forward:

```python
await catalog.async_initialize(hass)
```

Unsub for the time-interval listener stored on the runtime data so `async_unload_entry` cancels it via HA's normal listener-tracking machinery.

## 7. Error handling + observability

Principle: **never break control commands**; degrade silently to floor.

| Failure | Handler | User-visible |
|---|---|---|
| Boot, no internet | `aiohttp.ClientConnectorError` → log warning | Use cache or bundled; retry next 24h tick |
| Firebase 4xx (key invalid / restricted) | log error, bump `consecutive_failures` | Same as above. After ≥ 3 consecutive failures: HA repair issue |
| Firebase 429 (quota) | log warning, no special backoff (24h is already loose) | Same |
| Firebase 5xx / `FirebaseTransientError` | log warning, no in-process retry — next 24h tick is the retry | Same |
| Schema parse failure (AIFA changed RC structure) | log error with `template_version`, do **not** write disk cache | Same |
| Disk cache corrupt | catch + delete cache file | Fallback to bundled; cache rebuilt on next successful fetch |
| AIFA rotates API key (401/403) | Same as Firebase 4xx | Repair issue: "AIFA catalog refresh failing — integration still works on bundled metadata" |

Diagnostics (`diagnostics.py` extension) include a `catalog` block:

```json
{
  "source": "live",
  "fetched_at": "2026-05-03T12:34:56+00:00",
  "template_version": 47,
  "last_refresh_outcome": "success",
  "consecutive_failures": 0
}
```

HA repair issue threshold: `consecutive_failures >= 3` to avoid noise on transient outages.

## 8. Testing

### 8.1 Phase 0 POC (must pass before implementation)

`tools/probe_firebase_rc.py` (new) — hits AIFA Firebase RC with hardcoded API key + project from non-Android Python environment. Confirms:

1. FIS register from a non-Android client returns 200 + a token (not 403 due to SHA fingerprint restrictions).
2. RC fetch returns the same six AC keys we get from a rooted-Android extraction.

If this fails, the spec is dead and we fall back to Approach A (maintainer-controlled remote catalog). Phase 0 is **the first step** of the implementation plan, gated before any production code lands.

### 8.2 Unit tests

`test_catalog.py` (new):

- `parse_firebase_payload()` happy path against synthetic payload modeled on `reverse/firebase_activate.json`
- `merge_with_bundled()` preserves `AC_OBSERVED_*`, overlays the six Firebase-sourced fields
- Cache load — happy path swaps `_active`
- Cache load — schema mismatch → ignored, `_active` stays on bundled, no exception
- Cache load — corrupt JSON → catch, delete file, fall back to bundled
- 1h cooldown gate — `async_refresh` early-returns when `_active.fetched_at < 1h ago`

`test_firebase_rc_client.py` (new):

- Mocked aiohttp: FIS register 200 → returns token
- FIS register 4xx → raises `FirebaseAuthError`
- RC fetch 200 → returns dict
- RC fetch 401 → raises `FirebaseAuthError`
- RC fetch 5xx → raises `FirebaseTransientError`
- URL + auth header construction matches Firebase RC API contract

### 8.3 Existing tests

`test_ac.py` and friends: no changes required. They exercise `ac.py` logic against the default catalog state (bundled) — refactor leaves behavior identical for the bundled case.

### 8.4 No end-to-end HA test

HA fixtures are heavyweight; coordinator-style integration tests have a high mock-theater risk (just trimmed in the same session). `async_initialize` schedule wire-up validated via manual smoke + diagnostics output instead.

## 9. Out-of-scope / future work

- Distinguishing AIFA Plus codes (5-digit) — current scope is iCtrl-AC only ([memory: aifa_smart scope is iCtrl-AC only](../../../memory)). Plus-specific Remote Config keys, if AIFA adds them, will be ignored at parse time.
- A scheduled CI agent that auto-regenerates bundled `ac_catalog.py` from a fresh Firebase snapshot — orthogonal to runtime fetch and still useful for keeping the floor current. Not in this spec.
- Distributed cache invalidation (e.g. push notification when AIFA bumps template version) — over-engineering for the metadata change rate.

## 10. Risks ledger

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Phase 0 POC fails (Android SHA restriction) | Medium | High — kills the approach | Fall back to Approach A; brainstorm doc preserved |
| AIFA rotates API key | Low | Medium — refresh stops, integration still works on bundled | Repair issue; ship a bundled refresh in next integration release |
| AIFA changes RC schema | Low | Medium — refresh fails until parser updated | Schema parse error caught; bundled remains active; maintainer notified via repair issue |
| Google deprecates Firebase RC v1 endpoint | Very low | High | Long-term concern; not actionable now |
| User reports "integration calls Google" privacy concern | Low | Low | Documented in README; one-line opt-out env var (defer to plan if requested) |
