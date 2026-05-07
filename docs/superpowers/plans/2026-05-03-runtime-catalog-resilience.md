# Runtime Catalog Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decouple AC capability metadata freshness from integration releases by adding runtime Firebase Remote Config fetch with three-layer fallback (live → disk cache → bundled floor).

**Architecture:** A new `catalog.py` façade exposes a frozen `Catalog` dataclass and a process-wide `_active` reference. At HA boot it loads from disk cache (HA `Store`); 30s after setup and every 24h thereafter, it fetches from AIFA's Firebase Remote Config (mirroring AIFA app's 1h cooldown). `ac.py` is refactored from module-level catalog imports to lookup-time `catalog.get_active()` reads. All failure modes degrade silently to the bundled `ac_catalog.py` floor; control commands never break.

**Tech Stack:** Python 3.11, Home Assistant `Store` helper, `homeassistant.helpers.aiohttp_client`, `aiohttp`, Firebase Installations Service + Remote Config REST APIs.

**Spec:** `docs/superpowers/specs/2026-05-03-runtime-catalog-resilience-design.md`

**Working tree:** Plan executes directly on `main` (no worktree). All deps for Phase 0 already present in `/Users/pinlin/Desktop/aifa-smart-workspace/reverse/`.

---

## Phase 0: POC (gates everything else)

If Phase 0 fails (Firebase rejects non-Android client), STOP. Don't write production code. Reopen the brainstorm and pivot to Approach A (maintainer-controlled remote catalog).

### Task 0: Probe Firebase RC from a non-Android Python client

**Files:**
- Create: `tools/probe_firebase_rc.py`

**Background:** AIFA Smart Android APK is at `/Users/pinlin/Desktop/aifa-smart-workspace/apk/` (extracted form at `reverse/apk_extract/`). Firebase Android API keys live in `res/values/strings.xml` under names like `google_api_key`, `google_app_id`, and project ID derives from the app ID (`1:240868916940:android:a1bbaac859617483224c69`).

- [ ] **Step 1: Extract Firebase config from APK**

```bash
grep -E 'google_api_key|google_app_id|project_id|gcm_defaultSenderId' \
    /Users/pinlin/Desktop/aifa-smart-workspace/reverse/apk_extract/res/values/strings.xml
```

Expected output: four `<string name="..."`> lines. Record the values; you'll hardcode them in the probe script and later in `const.py`.

If `apk_extract/res/values/strings.xml` doesn't exist:

```bash
ls /Users/pinlin/Desktop/aifa-smart-workspace/reverse/apk_extract/
# Locate the strings.xml; common alternative path:
find /Users/pinlin/Desktop/aifa-smart-workspace/reverse -name strings.xml -path '*values/*' 2>/dev/null
```

- [ ] **Step 2: Write the probe script**

```python
"""Phase 0 POC: verify Firebase Remote Config fetch works from a non-Android
Python client.

Hits two endpoints:
  1. https://firebaseinstallations.googleapis.com/v1/projects/{projectId}/installations
     (returns FIS auth token)
  2. https://firebaseremoteconfig.googleapis.com/v1/projects/{projectNumber}/namespaces/firebase:fetch
     (returns RC payload)

Prints both responses. Compare the RC payload to reverse/firebase_activate.json
and confirm the same six AC keys are present.

If FIS register returns 403 with "API_KEY_HTTP_REFERRER_BLOCKED" /
"API_KEY_ANDROID_APP_BLOCKED", AIFA's API key has SHA fingerprint or referrer
restrictions; the spec's approach is dead. Fall back to Approach A.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

import aiohttp

# Filled in from Step 1's grep output:
PROJECT_ID = "tw-com-aifa-ictrl-pro"  # placeholder — verify in strings.xml
PROJECT_NUMBER = "240868916940"
APP_ID = "1:240868916940:android:a1bbaac859617483224c69"
API_KEY = "REPLACE_FROM_STRINGS_XML"  # google_api_key value


async def register_installation(session: aiohttp.ClientSession) -> str:
    """Get an FIS auth token; mirrors what Firebase SDK does at app start."""
    url = (
        f"https://firebaseinstallations.googleapis.com/v1/"
        f"projects/{PROJECT_ID}/installations"
    )
    fid = uuid.uuid4().hex[:22]  # FID format: 22 base64url chars
    payload = {
        "fid": fid,
        "appId": APP_ID,
        "authVersion": "FIS_v2",
        "sdkVersion": "a:17.2.0",  # arbitrary; SDK string not enforced
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
        "x-firebase-client": "fire-installations/17.2.0",
    }
    async with session.post(url, json=payload, headers=headers) as resp:
        body = await resp.text()
        print(f"FIS register: {resp.status}")
        print(body)
        resp.raise_for_status()
        data = json.loads(body)
        return data["authToken"]["token"]


async def fetch_remote_config(session: aiohttp.ClientSession, fis_token: str) -> dict:
    """Fetch RC. Mirrors RemoteConfigRepository::initialize -> fetchAndActivate."""
    url = (
        f"https://firebaseremoteconfig.googleapis.com/v1/"
        f"projects/{PROJECT_NUMBER}/namespaces/firebase:fetch"
    )
    payload = {
        "appInstanceId": uuid.uuid4().hex,
        "appInstanceIdToken": fis_token,
        "appId": APP_ID,
        "countryCode": "TW",
        "languageCode": "zh-TW",
        "platformVersion": "31",
        "timeZone": "Asia/Taipei",
        "appBuild": "1.2.5",
        "packageName": "tw.com.aifa.ictrl_pro",
        "sdkVersion": "21.4.0",
        "analyticsUserProperties": {},
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
        "x-goog-firebase-installations-auth": fis_token,
    }
    async with session.post(url, json=payload, headers=headers) as resp:
        body = await resp.text()
        print(f"RC fetch: {resp.status}")
        resp.raise_for_status()
        return json.loads(body)


async def main() -> int:
    if API_KEY == "REPLACE_FROM_STRINGS_XML":
        print("Edit API_KEY constant first (see Step 1).", file=sys.stderr)
        return 1
    async with aiohttp.ClientSession() as session:
        token = await register_installation(session)
        rc = await fetch_remote_config(session, token)
    keys = sorted((rc.get("entries") or {}).keys())
    print(f"Got {len(keys)} RC keys")
    expected = {
        "ac_available_modes",
        "ac_classic_temp_control",
        "ac_dehumidifier_codes",
        "ac_extra_windspeed",
        "ac_show_display_mold",
        "ac_single_mode_codes",
    }
    missing = expected - set(keys)
    if missing:
        print(f"MISSING expected keys: {missing}", file=sys.stderr)
        return 2
    print(f"Template version: {rc.get('templateVersion')}")
    print("OK — Firebase RC reachable from non-Android client.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 3: Run the probe**

Run: `python3 tools/probe_firebase_rc.py`

Expected (POC pass):
```
FIS register: 200
{...token JSON...}
RC fetch: 200
Got 7+ RC keys
Template version: 47
OK — Firebase RC reachable from non-Android client.
```

Expected (POC fail — abort plan):
```
FIS register: 403
{
  "error": {
    "code": 403,
    "message": "Requests from Android client applications are blocked..." OR
               "API_KEY_ANDROID_APP_BLOCKED" OR similar
  }
}
```

If POC fails: do **not** commit the probe script. Stop. Report findings to maintainer; pivot to spec Approach A.

- [ ] **Step 4: Commit (only if POC passed)**

```bash
git add tools/probe_firebase_rc.py
git commit -m "Add Phase 0 POC for Firebase Remote Config from Python"
```

---

## Phase 1: Foundation (no fetch wiring yet)

### Task 1: Firebase RC transport client

**Files:**
- Modify: `custom_components/aifa_smart/const.py`
- Create: `custom_components/aifa_smart/firebase_rc_client.py`
- Create: `tests/test_firebase_rc_client.py`

- [ ] **Step 1: Add Firebase constants to `const.py`**

Append after line `MACROS_PATH = "/macros"` (use values verified in Task 0):

```python
# Firebase Remote Config — used to refresh AC capability metadata at runtime.
# These values are EXTRACTED from AIFA Smart's APK (res/values/strings.xml +
# the App ID found at runtime). Android Firebase API keys are not secrets —
# they ship in every APK. AIFA's GCP project has been stable across 1.2.5.
# If AIFA rotates the key, refresh fails and the integration falls back to
# the bundled ac_catalog.py — no breakage of control commands.
FIREBASE_PROJECT_ID = "tw-com-aifa-ictrl-pro"
FIREBASE_PROJECT_NUMBER = "240868916940"
FIREBASE_APP_ID = "1:240868916940:android:a1bbaac859617483224c69"
FIREBASE_API_KEY = "<value-from-strings.xml-google_api_key>"
FIREBASE_RC_NAMESPACE = "firebase"
FIREBASE_INSTALLATIONS_URL = (
    "https://firebaseinstallations.googleapis.com/v1/projects/"
    f"{FIREBASE_PROJECT_ID}/installations"
)
FIREBASE_RC_FETCH_URL = (
    "https://firebaseremoteconfig.googleapis.com/v1/projects/"
    f"{FIREBASE_PROJECT_NUMBER}/namespaces/{FIREBASE_RC_NAMESPACE}:fetch"
)
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_firebase_rc_client.py`:

```python
"""Tests for the Firebase Remote Config transport client."""
from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Stub homeassistant modules so const.py imports cleanly.
sys.modules.setdefault("homeassistant", types.SimpleNamespace())

from custom_components.aifa_smart.firebase_rc_client import (
    FirebaseAuthError,
    FirebaseRcClient,
    FirebaseTransientError,
)


class _FakeResponse:
    def __init__(self, status: int, body: dict | str) -> None:
        self.status = status
        self._body = body

    async def json(self) -> dict:
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("body is text")

    async def text(self) -> str:
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_session(response: _FakeResponse) -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    return session


class FirebaseRcClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_installation_returns_token_on_200(self) -> None:
        response = _FakeResponse(200, {"authToken": {"token": "fis-token-xyz"}})
        client = FirebaseRcClient(_fake_session(response), "p", "1:2:3:4", "key")

        token = await client.async_register_installation()

        self.assertEqual(token, "fis-token-xyz")

    async def test_register_installation_raises_auth_on_4xx(self) -> None:
        response = _FakeResponse(403, {"error": {"code": 403, "message": "blocked"}})
        client = FirebaseRcClient(_fake_session(response), "p", "1:2:3:4", "key")

        with self.assertRaises(FirebaseAuthError):
            await client.async_register_installation()

    async def test_register_installation_raises_transient_on_5xx(self) -> None:
        response = _FakeResponse(503, "service unavailable")
        client = FirebaseRcClient(_fake_session(response), "p", "1:2:3:4", "key")

        with self.assertRaises(FirebaseTransientError):
            await client.async_register_installation()

    async def test_fetch_remote_config_returns_payload_on_200(self) -> None:
        rc_payload = {
            "entries": {
                "ac_available_modes": '{"1": ["cool", "heat"]}',
                "ac_classic_temp_control": '{"codes": [16, 19]}',
            },
            "templateVersion": 47,
        }
        response = _FakeResponse(200, rc_payload)
        client = FirebaseRcClient(_fake_session(response), "p", "1:2:3:4", "key")

        result = await client.async_fetch_remote_config(fis_token="t")

        self.assertEqual(result, rc_payload)

    async def test_fetch_remote_config_raises_auth_on_401(self) -> None:
        response = _FakeResponse(401, {"error": "unauthorized"})
        client = FirebaseRcClient(_fake_session(response), "p", "1:2:3:4", "key")

        with self.assertRaises(FirebaseAuthError):
            await client.async_fetch_remote_config(fis_token="t")

    async def test_fetch_remote_config_raises_transient_on_500(self) -> None:
        response = _FakeResponse(500, "boom")
        client = FirebaseRcClient(_fake_session(response), "p", "1:2:3:4", "key")

        with self.assertRaises(FirebaseTransientError):
            await client.async_fetch_remote_config(fis_token="t")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to confirm they fail**

Run: `cd /Users/pinlin/Desktop/aifa-smart-workspace/aifa-smart-homeassistant && python3 -m unittest tests.test_firebase_rc_client -v`

Expected: ImportError or ModuleNotFoundError on `firebase_rc_client`.

- [ ] **Step 4: Implement `firebase_rc_client.py`**

```python
"""Firebase Remote Config transport for AIFA Smart catalog refresh.

Two-step flow that mirrors the Firebase Android SDK:
  1. Register an installation (FIS) with the API key — get an auth token.
  2. Hit the RC fetch endpoint with the auth token — get the config payload.

This module is pure transport. Caller (catalog.py) handles parsing, merging,
caching, and retry policy.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp

from .const import (
    FIREBASE_INSTALLATIONS_URL,
    FIREBASE_RC_FETCH_URL,
)

_LOGGER = logging.getLogger(__name__)


class FirebaseAuthError(RuntimeError):
    """Raised when Firebase rejects the API key / FIS token (4xx, non-quota)."""


class FirebaseTransientError(RuntimeError):
    """Raised on 5xx, network errors, or 429 — caller should retry later."""


class FirebaseRcClient:
    """Thin client for Firebase Installations + Remote Config REST endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        project_id: str,
        app_id: str,
        api_key: str,
    ) -> None:
        self._session = session
        self._project_id = project_id
        self._app_id = app_id
        self._api_key = api_key

    async def async_register_installation(self) -> str:
        """Return an FIS auth token. Raise FirebaseAuthError on 4xx."""
        payload = {
            "fid": uuid.uuid4().hex[:22],
            "appId": self._app_id,
            "authVersion": "FIS_v2",
            "sdkVersion": "a:17.2.0",
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        try:
            async with self._session.post(
                FIREBASE_INSTALLATIONS_URL, json=payload, headers=headers
            ) as resp:
                if 400 <= resp.status < 500 and resp.status != 429:
                    body = await resp.text()
                    raise FirebaseAuthError(
                        f"FIS register failed {resp.status}: {body[:300]}"
                    )
                if resp.status >= 500 or resp.status == 429:
                    body = await resp.text()
                    raise FirebaseTransientError(
                        f"FIS register transient {resp.status}: {body[:300]}"
                    )
                data = await resp.json()
                token = data["authToken"]["token"]
                return token
        except aiohttp.ClientError as err:
            raise FirebaseTransientError(f"FIS network error: {err}") from err

    async def async_fetch_remote_config(self, *, fis_token: str) -> dict[str, Any]:
        """Return the full RC fetch response. Raise FirebaseAuthError on 401/403."""
        payload = {
            "appInstanceId": uuid.uuid4().hex,
            "appInstanceIdToken": fis_token,
            "appId": self._app_id,
            "packageName": "tw.com.aifa.ictrl_pro",
            "sdkVersion": "21.4.0",
            "analyticsUserProperties": {},
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
            "x-goog-firebase-installations-auth": fis_token,
        }
        try:
            async with self._session.post(
                FIREBASE_RC_FETCH_URL, json=payload, headers=headers
            ) as resp:
                if 400 <= resp.status < 500 and resp.status != 429:
                    body = await resp.text()
                    raise FirebaseAuthError(
                        f"RC fetch failed {resp.status}: {body[:300]}"
                    )
                if resp.status >= 500 or resp.status == 429:
                    body = await resp.text()
                    raise FirebaseTransientError(
                        f"RC fetch transient {resp.status}: {body[:300]}"
                    )
                return await resp.json()
        except aiohttp.ClientError as err:
            raise FirebaseTransientError(f"RC network error: {err}") from err
```

- [ ] **Step 5: Run tests to confirm they pass**

Run: `python3 -m unittest tests.test_firebase_rc_client -v`

Expected: 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/aifa_smart/const.py \
        custom_components/aifa_smart/firebase_rc_client.py \
        tests/test_firebase_rc_client.py
git commit -m "Add Firebase Remote Config transport client"
```

---

### Task 2: Catalog dataclass + bundled fallback (no fetch yet)

**Files:**
- Create: `custom_components/aifa_smart/catalog.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Write failing tests for the bundled-only behavior**

Create `tests/test_catalog.py`:

```python
"""Tests for the runtime catalog façade."""
from __future__ import annotations

import sys
import types
import unittest

sys.modules.setdefault("homeassistant", types.SimpleNamespace())

from custom_components.aifa_smart import catalog
from custom_components.aifa_smart import ac_catalog as bundled


class CatalogBundledFallbackTests(unittest.TestCase):
    def test_get_active_returns_bundled_at_import_time(self) -> None:
        active = catalog.get_active()
        self.assertEqual(active.source, "bundled")
        self.assertIsNone(active.fetched_at)
        self.assertIsNone(active.template_version)

    def test_bundled_catalog_carries_modes_from_ac_catalog(self) -> None:
        active = catalog.get_active()
        # Pick a known device code from the static catalog.
        self.assertEqual(
            active.available_modes_by_device_code["1"],
            bundled.AC_AVAILABLE_MODES_BY_DEVICE_CODE["1"],
        )

    def test_bundled_catalog_carries_observed_sets(self) -> None:
        active = catalog.get_active()
        self.assertEqual(
            active.observed_extra_windspeed_codes,
            bundled.AC_OBSERVED_EXTRA_WINDSPEED_CODES,
        )
        self.assertEqual(
            active.observed_turbo_codes,
            bundled.AC_OBSERVED_TURBO_CODES,
        )

    def test_catalog_is_frozen_dataclass(self) -> None:
        active = catalog.get_active()
        with self.assertRaises(Exception):
            active.source = "live"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `python3 -m unittest tests.test_catalog -v`

Expected: ImportError on `from ... import catalog`.

- [ ] **Step 3: Implement `catalog.py`**

```python
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

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from . import ac_catalog as _bundled

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
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `python3 -m unittest tests.test_catalog -v`

Expected: 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/aifa_smart/catalog.py tests/test_catalog.py
git commit -m "Add catalog facade with bundled fallback"
```

---

### Task 3: Refactor `ac.py` to read catalog at lookup time

**Files:**
- Modify: `custom_components/aifa_smart/ac.py:14-22, 232-268`

- [ ] **Step 1: Confirm full test suite is green before changes**

Run: `python3 -m unittest discover tests -v 2>&1 | tail -3`

Expected: all 116+ tests pass.

- [ ] **Step 2: Replace module-level catalog imports with module-level catalog import**

In `custom_components/aifa_smart/ac.py`, replace lines 14-22:

```python
from .ac_catalog import (
    AC_AVAILABLE_MODES_BY_DEVICE_CODE,
    AC_CLASSIC_TEMP_CONTROL_CODES,
    AC_DEHUMIDIFIER_CODES,
    AC_EXTRA_WINDSPEED_CODES,
    AC_OBSERVED_EXTRA_WINDSPEED_CODES,
    AC_SHOW_DISPLAY_MOLD_CODES,
    AC_SINGLE_MODE_CODES,
)
```

with:

```python
from . import catalog
```

- [ ] **Step 3: Update `get_ac_capabilities` (around line 232) to read via catalog**

Replace the body that uses the imported frozensets with `catalog.get_active()`:

```python
def get_ac_capabilities(device_code: str | None) -> AifaAcCapabilities:
    """Return remote-config derived capabilities for a device code."""
    cat = catalog.get_active()
    normalized_code = _normalize_device_code(device_code)
    configured_modes = cat.available_modes_by_device_code.get(
        normalized_code or "", None
    )

    if configured_modes is None:
        available_modes = _DEFAULT_AVAILABLE_MODES
    else:
        available_modes = configured_modes

    supports_extra_windspeed = normalized_code in (
        cat.extra_windspeed_codes | cat.observed_extra_windspeed_codes
    )
    # Turbo is exposed for every AC sub-device. The per-device IR encoder
    # is now BCD-device-aware (byte 4 = device_code BCD low byte), so the
    # earlier "we only know 136 supports turbo" heuristic is obsolete —
    # the byte-10 0x04 feature bit is a uniform on-the-wire signal to the
    # hub, and per-device IR translation is the hub's job. The switch
    # entity remains entity_registry_enabled_default=False so users opt-in
    # after confirming their AC actually responds to turbo.
    supports_turbo = True

    return AifaAcCapabilities(
        device_code=normalized_code,
        available_modes=tuple(normalize_catalog_mode(mode) for mode in available_modes),
        fan_modes=_DEFAULT_FAN_MODES,
        swing_modes=_DEFAULT_SWING_MODES,
        classic_temp_control=normalized_code in cat.classic_temp_control_codes,
        extra_windspeed=supports_extra_windspeed,
        show_display_mold=normalized_code in cat.show_display_mold_codes,
        single_mode=normalized_code in cat.single_mode_codes,
        dehumidifier=normalized_code in cat.dehumidifier_codes,
        supports_sleep=True,
        supports_power_saving=True,
        supports_turbo=supports_turbo,
    )
```

Other call sites of the deleted imports: search and replace identically.

- [ ] **Step 4: Find and update remaining call sites**

```bash
grep -n "AC_AVAILABLE_MODES_BY_DEVICE_CODE\|AC_CLASSIC_TEMP_CONTROL_CODES\|AC_DEHUMIDIFIER_CODES\|AC_EXTRA_WINDSPEED_CODES\|AC_OBSERVED_EXTRA_WINDSPEED_CODES\|AC_SHOW_DISPLAY_MOLD_CODES\|AC_SINGLE_MODE_CODES" custom_components/aifa_smart/ac.py
```

Expected after change: zero matches in `ac.py`.

For any matches found, replace with `catalog.get_active().<field>` (e.g. `cat = catalog.get_active(); ... cat.classic_temp_control_codes ...`). Inside one function, hoist a single `cat = catalog.get_active()` and reuse.

- [ ] **Step 5: Run full suite, confirm still green**

Run: `python3 -m unittest discover tests -v 2>&1 | tail -3`

Expected: same test count, all pass.

- [ ] **Step 6: Commit**

```bash
git add custom_components/aifa_smart/ac.py
git commit -m "Refactor ac.py to read catalog at lookup time"
```

---

## Phase 2: Wire fetch + cache

### Task 4: Parse Firebase payload + merge with bundled

**Files:**
- Modify: `custom_components/aifa_smart/catalog.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: Add failing tests for parse + merge**

Append to `tests/test_catalog.py` before the `if __name__` block:

```python
class CatalogParseAndMergeTests(unittest.TestCase):
    def _sample_rc_response(self) -> dict:
        """Shape-accurate Firebase RC fetch response."""
        return {
            "entries": {
                "ac_available_modes": '{"1": ["cool", "heat"], "2": ["cool"]}',
                "ac_classic_temp_control": '{"codes": [16, 19]}',
                "ac_extra_windspeed": '{"codes": [99]}',
                "ac_show_display_mold": '{"codes": [134]}',
                "ac_single_mode_codes": '{"codes": [281]}',
                "ac_dehumidifier_codes": '{"codes": [19966]}',
                # Other RC keys we don't consume — must be ignored:
                "sensor_charts_enabled": "false",
                "ac_code_models": '{"1": "Brand X"}',
            },
            "templateVersion": 47,
        }

    def test_parse_extracts_six_ac_keys(self) -> None:
        from datetime import datetime, timezone
        parsed = catalog.parse_firebase_payload(
            self._sample_rc_response(),
            fetched_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(parsed.available_modes_by_device_code["1"], ("cool", "heat"))
        self.assertEqual(parsed.available_modes_by_device_code["2"], ("cool",))
        self.assertIn("16", parsed.classic_temp_control_codes)
        self.assertIn("99", parsed.extra_windspeed_codes)
        self.assertIn("134", parsed.show_display_mold_codes)
        self.assertIn("281", parsed.single_mode_codes)
        self.assertIn("19966", parsed.dehumidifier_codes)
        self.assertEqual(parsed.template_version, 47)
        self.assertEqual(parsed.source, "live")

    def test_parse_preserves_observed_sets_from_bundled(self) -> None:
        parsed = catalog.parse_firebase_payload(self._sample_rc_response())
        self.assertEqual(
            parsed.observed_extra_windspeed_codes,
            catalog.get_bundled().observed_extra_windspeed_codes,
        )
        self.assertEqual(
            parsed.observed_turbo_codes,
            catalog.get_bundled().observed_turbo_codes,
        )

    def test_parse_raises_on_missing_required_key(self) -> None:
        broken = {"entries": {"ac_available_modes": "{}"}, "templateVersion": 47}
        with self.assertRaises(catalog.CatalogParseError):
            catalog.parse_firebase_payload(broken)

    def test_parse_raises_on_malformed_modes_json(self) -> None:
        broken = self._sample_rc_response()
        broken["entries"]["ac_available_modes"] = "not-json"
        with self.assertRaises(catalog.CatalogParseError):
            catalog.parse_firebase_payload(broken)
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `python3 -m unittest tests.test_catalog.CatalogParseAndMergeTests -v`

Expected: AttributeError on `parse_firebase_payload` / `CatalogParseError`.

- [ ] **Step 3: Implement parse + helper**

Append to `custom_components/aifa_smart/catalog.py`:

```python
import json
from datetime import datetime, timezone

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
    except (json.JSONDecodeError, ValueError, TypeError) as err:
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
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `python3 -m unittest tests.test_catalog -v`

Expected: 4 + 4 = 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/aifa_smart/catalog.py tests/test_catalog.py
git commit -m "Parse Firebase RC payload into Catalog"
```

---

### Task 5: Disk cache load/save

**Files:**
- Modify: `custom_components/aifa_smart/catalog.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: Add failing tests for cache serialization**

Append to `tests/test_catalog.py`:

```python
class CatalogCacheSerializationTests(unittest.TestCase):
    def _live_catalog(self) -> "catalog.Catalog":
        from datetime import datetime, timezone
        return catalog.Catalog(
            available_modes_by_device_code={"1": ("cool", "heat")},
            classic_temp_control_codes=frozenset(["16"]),
            extra_windspeed_codes=frozenset(["99"]),
            show_display_mold_codes=frozenset(["134"]),
            single_mode_codes=frozenset(["281"]),
            dehumidifier_codes=frozenset(["19966"]),
            observed_extra_windspeed_codes=frozenset(["1"]),
            observed_turbo_codes=frozenset(["2"]),
            source="live",
            fetched_at=datetime(2026, 5, 3, 12, 0, 0, tzinfo=timezone.utc),
            template_version=47,
        )

    def test_dump_and_load_roundtrip(self) -> None:
        original = self._live_catalog()
        dumped = catalog.dump_for_cache(original)
        restored = catalog.load_from_cache(dumped)
        self.assertEqual(restored.source, "cache")
        self.assertEqual(
            restored.available_modes_by_device_code,
            original.available_modes_by_device_code,
        )
        self.assertEqual(restored.classic_temp_control_codes, original.classic_temp_control_codes)
        self.assertEqual(restored.fetched_at, original.fetched_at)
        self.assertEqual(restored.template_version, original.template_version)

    def test_load_from_cache_returns_none_on_unknown_version(self) -> None:
        dumped = {"version": 999, "fetched_at": None, "template_version": None, "configs": {}}
        self.assertIsNone(catalog.load_from_cache(dumped))

    def test_load_from_cache_returns_none_on_missing_required_key(self) -> None:
        dumped = catalog.dump_for_cache(self._live_catalog())
        del dumped["configs"]["ac_available_modes"]
        self.assertIsNone(catalog.load_from_cache(dumped))

    def test_load_from_cache_returns_none_on_corrupt_payload(self) -> None:
        self.assertIsNone(catalog.load_from_cache({"garbage": True}))
        self.assertIsNone(catalog.load_from_cache(None))
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `python3 -m unittest tests.test_catalog.CatalogCacheSerializationTests -v`

Expected: AttributeError on `dump_for_cache` / `load_from_cache`.

- [ ] **Step 3: Implement cache helpers**

Append to `catalog.py`:

```python
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
    if raw.get("version") != CACHE_SCHEMA_VERSION:
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
    except (KeyError, TypeError, ValueError):
        return None

    fetched_at_raw = raw.get("fetched_at")
    fetched_at: datetime | None = None
    if isinstance(fetched_at_raw, str):
        try:
            fetched_at = datetime.fromisoformat(fetched_at_raw)
        except ValueError:
            fetched_at = None

    template_version = raw.get("template_version")
    if not isinstance(template_version, int):
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
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `python3 -m unittest tests.test_catalog -v`

Expected: 12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add custom_components/aifa_smart/catalog.py tests/test_catalog.py
git commit -m "Add disk cache serialization for Catalog"
```

---

### Task 6: Async initialize + refresh with cooldown

**Files:**
- Modify: `custom_components/aifa_smart/catalog.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: Add failing tests for refresh cooldown**

Append to `tests/test_catalog.py`:

```python
class CatalogRefreshCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Reset module state between tests.
        catalog._active = catalog._BUNDLED  # type: ignore[attr-defined]
        catalog._consecutive_failures = 0   # type: ignore[attr-defined]

    async def asyncTearDown(self) -> None:
        catalog._active = catalog._BUNDLED  # type: ignore[attr-defined]
        catalog._consecutive_failures = 0   # type: ignore[attr-defined]

    async def test_refresh_proceeds_when_active_is_bundled(self) -> None:
        from unittest.mock import AsyncMock
        client = AsyncMock()
        client.async_register_installation.return_value = "tok"
        client.async_fetch_remote_config.return_value = {
            "entries": {
                "ac_available_modes": '{"1": ["cool"]}',
                "ac_classic_temp_control": '{"codes": []}',
                "ac_extra_windspeed": '{"codes": []}',
                "ac_show_display_mold": '{"codes": []}',
                "ac_single_mode_codes": '{"codes": []}',
                "ac_dehumidifier_codes": '{"codes": []}',
            },
            "templateVersion": 50,
        }

        outcome = await catalog._refresh_with_client(client)

        self.assertEqual(outcome.status, "success")
        self.assertEqual(catalog.get_active().source, "live")
        self.assertEqual(catalog.get_active().template_version, 50)

    async def test_refresh_early_returns_when_within_cooldown(self) -> None:
        from datetime import datetime, timezone, timedelta
        from unittest.mock import AsyncMock

        # Pretend we just fetched 30 minutes ago.
        recent = datetime.now(tz=timezone.utc) - timedelta(minutes=30)
        catalog._active = catalog.Catalog(  # type: ignore[attr-defined]
            **{**catalog._BUNDLED.__dict__,  # type: ignore[attr-defined]
               "source": "live", "fetched_at": recent, "template_version": 49}
        )
        client = AsyncMock()

        outcome = await catalog._refresh_with_client(client)

        self.assertEqual(outcome.status, "skipped_cooldown")
        client.async_register_installation.assert_not_called()

    async def test_refresh_failure_keeps_active_and_bumps_counter(self) -> None:
        from unittest.mock import AsyncMock
        from custom_components.aifa_smart.firebase_rc_client import FirebaseAuthError

        client = AsyncMock()
        client.async_register_installation.side_effect = FirebaseAuthError("403")
        before = catalog.get_active()

        outcome = await catalog._refresh_with_client(client)

        self.assertEqual(outcome.status, "auth_error")
        self.assertIs(catalog.get_active(), before)
        self.assertEqual(catalog._consecutive_failures, 1)  # type: ignore[attr-defined]
```

- [ ] **Step 2: Run tests, confirm they fail**

Run: `python3 -m unittest tests.test_catalog.CatalogRefreshCooldownTests -v`

Expected: AttributeError on `_refresh_with_client` / `_consecutive_failures`.

- [ ] **Step 3: Implement refresh logic**

Append to `catalog.py`:

```python
from dataclasses import replace
from datetime import timedelta

from .firebase_rc_client import (
    FirebaseAuthError,
    FirebaseRcClient,
    FirebaseTransientError,
)

REFRESH_COOLDOWN = timedelta(hours=1)
REFRESH_INTERVAL = timedelta(hours=24)
INITIAL_FETCH_DELAY = timedelta(seconds=30)
FAILURE_REPAIR_THRESHOLD = 3

_consecutive_failures: int = 0
_last_outcome: str = "never_attempted"


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
    """
    global _active, _consecutive_failures, _last_outcome

    if _active.fetched_at is not None:
        age = datetime.now(tz=timezone.utc) - _active.fetched_at
        if age < REFRESH_COOLDOWN:
            _last_outcome = "skipped_cooldown"
            return RefreshResult("skipped_cooldown", f"age={age.total_seconds():.0f}s")

    try:
        token = await client.async_register_installation()
        payload = await client.async_fetch_remote_config(fis_token=token)
    except FirebaseAuthError as err:
        _consecutive_failures += 1
        _last_outcome = "auth_error"
        _LOGGER.error("Firebase RC auth error: %s", err)
        return RefreshResult("auth_error", str(err))
    except FirebaseTransientError as err:
        _consecutive_failures += 1
        _last_outcome = "transient_error"
        _LOGGER.warning("Firebase RC transient error: %s", err)
        return RefreshResult("transient_error", str(err))

    try:
        new_active = parse_firebase_payload(payload)
    except CatalogParseError as err:
        _consecutive_failures += 1
        _last_outcome = "parse_error"
        _LOGGER.error("Firebase RC parse error: %s", err)
        return RefreshResult("parse_error", str(err))

    _active = new_active
    _consecutive_failures = 0
    _last_outcome = "success"
    return RefreshResult("success", f"template_version={new_active.template_version}")
```

- [ ] **Step 4: Run tests, confirm they pass**

Run: `python3 -m unittest tests.test_catalog -v`

Expected: 15 tests pass.

- [ ] **Step 5: Add HA-facing wrappers**

Append to `catalog.py`:

```python
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    FIREBASE_API_KEY,
    FIREBASE_APP_ID,
    FIREBASE_PROJECT_ID,
)

_STORE_KEY = f"{DOMAIN}_catalog"
_STORE_VERSION = 1


def _build_client(hass) -> FirebaseRcClient:
    return FirebaseRcClient(
        async_get_clientsession(hass),
        project_id=FIREBASE_PROJECT_ID,
        app_id=FIREBASE_APP_ID,
        api_key=FIREBASE_API_KEY,
    )


async def async_refresh(hass) -> RefreshResult:
    """Fetch latest RC, swap _active, write disk cache. Safe to call concurrently."""
    client = _build_client(hass)
    result = await _refresh_with_client(client)
    if result.status == "success":
        store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        await store.async_save(dump_for_cache(_active))
    return result


async def async_initialize(hass) -> None:
    """Load disk cache (if any), schedule first fetch + periodic refresh.

    Returns immediately; first fetch runs after INITIAL_FETCH_DELAY in the
    background. Cache load IS awaited so capability lookups during platform
    setup get the cached data, not bundled.
    """
    global _active

    store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
    raw = await store.async_load()
    cached = load_from_cache(raw)
    if cached is not None:
        _active = cached
        _LOGGER.debug("Catalog loaded from cache (template_version=%s)", cached.template_version)
    elif raw is not None:
        # Schema mismatch / corruption: drop the file so next save rebuilds.
        _LOGGER.warning("Catalog cache unreadable; falling back to bundled")
        await store.async_remove()

    async def _scheduled_refresh(_now=None) -> None:
        try:
            await async_refresh(hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected refresh error: %s", err)

    async_call_later(hass, INITIAL_FETCH_DELAY.total_seconds(), _scheduled_refresh)
    async_track_time_interval(hass, _scheduled_refresh, REFRESH_INTERVAL)
```

(`async_track_time_interval` returns an unsub callable; HA stores it on the entry via `entry.async_on_unload` if the caller wires it. Task 7 wires this.)

- [ ] **Step 6: Run full test suite, confirm still green**

Run: `python3 -m unittest discover tests -v 2>&1 | tail -3`

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add custom_components/aifa_smart/catalog.py tests/test_catalog.py
git commit -m "Add catalog refresh + initialize lifecycle"
```

---

### Task 7: Wire catalog into integration setup

**Files:**
- Modify: `custom_components/aifa_smart/__init__.py:65-99`

- [ ] **Step 1: Add `catalog.async_initialize` call after coordinator construction**

In `__init__.py`'s `async_setup_entry`, after `coordinator = AifaSmartCoordinator(hass, entry, client)` and before `await coordinator.async_config_entry_first_refresh()`, insert:

```python
    # Catalog must initialize before first capability lookup so climate
    # entities pick up cached/live data rather than bundled defaults.
    from . import catalog
    await catalog.async_initialize(hass)
```

- [ ] **Step 2: Wire the periodic-refresh unsub into `entry.async_on_unload`**

Refactor `catalog.async_initialize` so it returns the time-interval unsub:

In `catalog.py`:

```python
async def async_initialize(hass):
    """... (same docstring) ..."""
    # ... (cache load same as before) ...

    async def _scheduled_refresh(_now=None) -> None:
        try:
            await async_refresh(hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Unexpected refresh error: %s", err)

    async_call_later(hass, INITIAL_FETCH_DELAY.total_seconds(), _scheduled_refresh)
    return async_track_time_interval(hass, _scheduled_refresh, REFRESH_INTERVAL)
```

No new unit test for the unsub wiring — `async_track_time_interval` is HA stdlib; verifying the cancel path requires a full HA fixture. Manual smoke (Step 3) confirms wiring; integration unload during testing should not leave dangling timers.

In `__init__.py`:

```python
    from . import catalog
    catalog_unsub = await catalog.async_initialize(hass)
    if catalog_unsub:
        entry.async_on_unload(catalog_unsub)
```

- [ ] **Step 3: Manual smoke test in HA**

Use the `ha-deploy` skill to push to the test HA machine, then check logs:

```
grep -i 'catalog\|firebase' /var/log/home-assistant.log | tail -50
```

Expected log lines (within 30s of integration load):
```
... catalog loaded from cache ...   (on second+ run)
... Firebase RC auth: 200 ...        (debug level)
... template_version=47 ...
```

If logs show `auth_error` repeatedly, the API key is wrong — re-extract from APK strings.xml.

- [ ] **Step 4: Verify diagnostics dump (optional preview before Task 8)**

In HA UI: Settings → Devices & Services → AIFA Smart → ⋯ → Download diagnostics. The JSON should still exclude any catalog block (Task 8 will add it).

- [ ] **Step 5: Run full suite once more**

Run: `python3 -m unittest discover tests -v 2>&1 | tail -3`

Expected: green.

- [ ] **Step 6: Commit**

```bash
git add custom_components/aifa_smart/__init__.py custom_components/aifa_smart/catalog.py
git commit -m "Wire catalog refresh into config entry lifecycle"
```

---

## Phase 3: Observability

### Task 8: Diagnostics catalog block

**Files:**
- Modify: `custom_components/aifa_smart/diagnostics.py`
- Modify: `custom_components/aifa_smart/catalog.py`

- [ ] **Step 1: Add a public `get_status()` helper to `catalog.py`**

Append:

```python
def get_status() -> dict:
    """Status snapshot for diagnostics."""
    return {
        "source": _active.source,
        "fetched_at": _active.fetched_at.isoformat() if _active.fetched_at else None,
        "template_version": _active.template_version,
        "last_refresh_outcome": _last_outcome,
        "consecutive_failures": _consecutive_failures,
    }
```

- [ ] **Step 2: Plug into `diagnostics.py`'s snapshot**

In `diagnostics.py`, add to the `snapshot` dict (after `"devices": ...`):

```python
        "catalog": catalog.get_status(),
```

And add to the imports section at top:

```python
from . import catalog
```

- [ ] **Step 3: Verify diagnostics output**

Manual smoke (after deploy):
- Settings → AIFA Smart → Download diagnostics
- Open the JSON, find the `catalog` block
- Expected (after a successful refresh):
```json
"catalog": {
  "source": "live",
  "fetched_at": "2026-05-03T12:34:56+00:00",
  "template_version": 47,
  "last_refresh_outcome": "success",
  "consecutive_failures": 0
}
```

- [ ] **Step 4: Run test suite**

Run: `python3 -m unittest discover tests -v 2>&1 | tail -3`

Expected: green (no diagnostics tests, but nothing should regress).

- [ ] **Step 5: Commit**

```bash
git add custom_components/aifa_smart/diagnostics.py custom_components/aifa_smart/catalog.py
git commit -m "Surface catalog status in diagnostics"
```

---

### Task 9: HA repair issue on consecutive failures

**Files:**
- Modify: `custom_components/aifa_smart/catalog.py`

- [ ] **Step 1: Add repair-issue creation to refresh failure paths**

In `catalog.py`, replace the bare `_consecutive_failures += 1` increments with a helper, and call it from the `auth_error` / `transient_error` / `parse_error` branches. Add the helper:

```python
from homeassistant.helpers import issue_registry as ir

_REPAIR_ISSUE_ID = "catalog_refresh_failing"


def _bump_failures_and_maybe_open_issue(hass, kind: str, detail: str) -> None:
    global _consecutive_failures
    _consecutive_failures += 1
    if _consecutive_failures >= FAILURE_REPAIR_THRESHOLD:
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
    ir.async_delete_issue(hass, DOMAIN, _REPAIR_ISSUE_ID)
```

`_refresh_with_client` doesn't have `hass`, so split:

```python
async def async_refresh(hass) -> RefreshResult:
    client = _build_client(hass)
    result = await _refresh_with_client(client)
    if result.status == "success":
        store: Store = Store(hass, _STORE_VERSION, _STORE_KEY)
        await store.async_save(dump_for_cache(_active))
        _clear_repair_issue(hass)
    elif result.status in ("auth_error", "transient_error", "parse_error"):
        _bump_failures_and_maybe_open_issue(hass, result.status, result.detail or "")
    return result
```

Remove the `_consecutive_failures += 1` from inside `_refresh_with_client` (move counter ownership to `async_refresh`). Update the test in `CatalogRefreshCooldownTests::test_refresh_failure_keeps_active_and_bumps_counter` if it asserts on the inner counter — refactor it to call `async_refresh` with a mocked `hass`.

- [ ] **Step 2: Add translation strings**

In `custom_components/aifa_smart/strings.json`, add to the `issues` block (create the block if it doesn't exist):

```json
"issues": {
  "catalog_refresh_failing": {
    "title": "AIFA catalog refresh is failing",
    "description": "The integration could not refresh AC capability metadata from AIFA's Firebase Remote Config ({kind}). Control commands still work, but new AC models added by AIFA won't appear correctly until the integration is updated. Detail: {detail}"
  }
}
```

Mirror the same `issues.catalog_refresh_failing` block in `custom_components/aifa_smart/translations/zh-Hant.json` with translated strings:

```json
"issues": {
  "catalog_refresh_failing": {
    "title": "AIFA 冷氣型號資料更新失敗",
    "description": "整合無法從 AIFA Firebase Remote Config 抓最新冷氣 metadata（{kind}）。控制指令仍正常運作，但 AIFA 新增的冷氣型號要等整合更新後才會正確顯示。詳情：{detail}"
  }
}
```

- [ ] **Step 3: Update / add a test that exercises the issue path**

Replace the failure test in `tests/test_catalog.py` to use the public `async_refresh` with a stub hass + mocked client:

```python
async def test_async_refresh_opens_repair_after_threshold(self) -> None:
    from unittest.mock import AsyncMock, MagicMock, patch
    from custom_components.aifa_smart.firebase_rc_client import FirebaseAuthError

    fake_hass = MagicMock()
    issue_calls = []
    delete_calls = []

    with patch.object(catalog, "_build_client") as build_client, \
         patch("custom_components.aifa_smart.catalog.ir") as ir_mod:
        ir_mod.async_create_issue.side_effect = lambda *a, **k: issue_calls.append((a, k))
        ir_mod.async_delete_issue.side_effect = lambda *a, **k: delete_calls.append((a, k))
        client = AsyncMock()
        client.async_register_installation.side_effect = FirebaseAuthError("403")
        build_client.return_value = client

        # 2 failures: no issue yet
        await catalog.async_refresh(fake_hass)
        await catalog.async_refresh(fake_hass)
        self.assertEqual(issue_calls, [])

        # 3rd failure: issue opens
        await catalog.async_refresh(fake_hass)
        self.assertEqual(len(issue_calls), 1)
```

- [ ] **Step 4: Run tests, confirm pass**

Run: `python3 -m unittest tests.test_catalog -v`

Expected: green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/aifa_smart/catalog.py \
        custom_components/aifa_smart/strings.json \
        custom_components/aifa_smart/translations/zh-Hant.json \
        tests/test_catalog.py
git commit -m "Open HA repair issue after 3 catalog refresh failures"
```

---

### Task 10: README + final sweep

**Files:**
- Modify: `README.md`
- Modify: `docs/regen-ac-catalog.md`

- [ ] **Step 1: Document runtime catalog refresh in README**

Add a new section to `README.md` (near the existing "Cloud calls" / privacy disclosure if present, otherwise after the install instructions):

```markdown
## Catalog refresh

This integration ships with a snapshot of AIFA's per-AC-model capability matrix
(modes, fan speeds, dehumidifier flag, etc.). At runtime it tries to refresh
this snapshot from AIFA's Firebase Remote Config:

- 30 seconds after Home Assistant loads the integration
- Once every 24 hours thereafter
- Subject to a 1-hour cooldown to mirror the AIFA app's behavior

If the refresh fails (no internet, AIFA rotates their API key, etc.), the
integration falls back to the cached snapshot from the last successful
refresh, and ultimately to the snapshot bundled with the integration version
you have installed. Control commands always work — refresh failure only
affects which UI buttons are shown.

You can verify the current source via Settings → Devices & Services →
AIFA Smart → Download diagnostics; the `catalog` block reports
`source: "live"` (fresh from Firebase), `"cache"` (from previous fetch),
or `"bundled"` (no fetch ever succeeded on this install).
```

- [ ] **Step 2: Update `docs/regen-ac-catalog.md` to note the runtime path**

Add a paragraph at the top:

```markdown
> **As of v0.x.0 the integration also fetches Remote Config at runtime
> (every 24h), so the bundled `ac_catalog.py` is now a *floor*, not the
> only source. Regenerating still has value: it pins a known-good snapshot
> against future Firebase outages or API key rotation.**
```

- [ ] **Step 3: Confirm whole suite green**

Run: `python3 -m unittest discover tests -v 2>&1 | tail -3`

Expected: green.

- [ ] **Step 4: Commit**

```bash
git add README.md docs/regen-ac-catalog.md
git commit -m "Document runtime catalog refresh"
```

---

## Acceptance criteria

- `python3 -m unittest discover tests` passes
- `python3 tools/probe_firebase_rc.py` passes (Phase 0)
- HA log shows `catalog loaded from cache` on second-and-later boots
- HA diagnostics include a `catalog` block with `source` ∈ {`live`, `cache`, `bundled`}
- Disconnecting internet → integration still loads, control commands still work, log shows transient_error
- A user with an unknown `device_code` not in bundled but present in live RC sees the correct mode buttons after first refresh

## Out of scope (do not implement here)

- Scheduled CI agent that auto-regenerates bundled `ac_catalog.py`
- Plus device code support (5-digit codes are still scope: iCtrl-AC only)
- User-facing toggle to disable Firebase fetch (revisit if a user reports the privacy concern)
- ETags / `If-None-Match` against Firebase (RC fetch endpoint doesn't support them; template version comparison is informational only)
