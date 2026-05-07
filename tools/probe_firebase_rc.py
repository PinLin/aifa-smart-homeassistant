"""Phase 0 POC: verify Firebase Remote Config fetch works from a non-Android
Python client.

Hits two endpoints:
  1. https://firebaseinstallations.googleapis.com/v1/projects/{projectId}/installations
     (returns FIS auth token)
  2. https://firebaseremoteconfig.googleapis.com/v1/projects/{projectNumber}/namespaces/firebase:fetch
     (returns RC payload)

Prints both responses. Compares the RC payload to reverse/firebase_activate.json
and confirms the same six AC keys are present.

If FIS register returns 403 with API_KEY_ANDROID_APP_BLOCKED or similar,
AIFA's API key has SHA fingerprint restrictions; the spec's runtime-fetch
approach is dead. Fall back to spec Approach A (maintainer-controlled remote
catalog).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid

# Extracted from AIFA Smart 1.2.5 APK resources.arsc (2026-05-03):
PROJECT_ID = "i-ctrl-pro"
PROJECT_NUMBER = "240868916940"
APP_ID = "1:240868916940:android:a1bbaac859617483224c69"
API_KEY = "AIzaSyC5uitwFWZC0V8wsDG-UaQdXJ7OYoSE1tY"


def _post_json(url: str, payload: dict, headers: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", errors="replace")


def register_installation() -> str:
    """Get an FIS auth token; mirrors what Firebase SDK does at app start."""
    url = (
        f"https://firebaseinstallations.googleapis.com/v1/"
        f"projects/{PROJECT_ID}/installations"
    )
    fid = uuid.uuid4().hex[:22]
    payload = {
        "fid": fid,
        "appId": APP_ID,
        "authVersion": "FIS_v2",
        "sdkVersion": "a:17.2.0",
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": API_KEY,
        "x-firebase-client": "fire-installations/17.2.0",
    }
    status, body = _post_json(url, payload, headers)
    print(f"FIS register: {status}")
    print(body[:500])
    if status != 200:
        raise SystemExit(f"FIS register failed: {status}")
    return json.loads(body)["authToken"]["token"]


def fetch_remote_config(fis_token: str) -> dict:
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
    status, body = _post_json(url, payload, headers)
    print(f"RC fetch: {status}")
    if status != 200:
        print(body[:500])
        raise SystemExit(f"RC fetch failed: {status}")
    return json.loads(body)


def main() -> int:
    token = register_installation()
    rc = fetch_remote_config(token)
    keys = sorted((rc.get("entries") or {}).keys())
    print(f"\nGot {len(keys)} RC keys:")
    for k in keys:
        print(f"  - {k}")
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
        print(f"\nMISSING expected keys: {missing}", file=sys.stderr)
        return 2
    print(f"\nTemplate version: {rc.get('templateVersion')}")
    print("OK — Firebase RC reachable from non-Android client.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
