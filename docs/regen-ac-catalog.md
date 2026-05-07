# Regenerating `ac_catalog.py`

> **Heads up:** the integration now also fetches Remote Config at runtime
> (30s after setup, then every 24h with a 1h cooldown), so the bundled
> `ac_catalog.py` is now a *floor*, not the only source. Regenerating still
> has value: it pins a known-good snapshot against future Firebase outages
> or API-key rotation, and it's what fresh installs see before the first
> live fetch lands. Diagnostics report `catalog.source` as `live` /
> `cache` / `bundled` so you can tell which tier is in play.

The integration ships a static snapshot of AIFA Smart's per-device
capability matrix (which AC modes / fan speeds / quirks each
`device_code` supports). The matrix lives in the AIFA app's Firebase
Remote Config payload, which the app fetches at startup. The integration
fetches the same payload directly over HTTPS at runtime (no Firebase SDK
in the HA runtime — just the same REST endpoint the app uses), and falls
back to the bundled snapshot if the runtime fetch fails.

`custom_components/aifa_smart/ac_catalog.py` is the snapshot. When it
ages out (AIFA adds new `device_code`s, changes capabilities for
existing codes, etc.), follow this procedure to refresh.

> The integration falls back to a permissive default mode list (all 5
> modes) for any `device_code` not in `ac_catalog.py`. Stale catalog
> means UI may show extra mode buttons — it does NOT block control
> commands. Refresh is mostly a UX-polish exercise.

---

## 1. Refresh `reverse/firebase_activate.json`

Boot the rooted Android emulator (or a real rooted Android with the
AIFA app installed):

```bash
emulator -avd AIFA_Root -no-snapshot-load -no-audio &
until adb shell getprop sys.boot_completed | grep -q 1; do sleep 3; done
adb root
```

Make sure the AIFA app has been opened recently and has logged in. The
Firebase SDK fetches the latest config at app startup and writes it to
the app's data directory.

Pull the cached config:

```bash
adb shell run-as tw.com.aifa.ictrl_pro \
    cat /data/data/tw.com.aifa.ictrl_pro/files/firebase_remote_config/firebase_activate.json \
    > $WORKSPACE/reverse/firebase_activate.json
```

(Path may vary by Firebase SDK version; if the file isn't there, search:
`adb shell run-as tw.com.aifa.ictrl_pro find . -name 'firebase_activate*'`.)

Verify the file is valid JSON and has the expected top-level shape:

```bash
python3 -c "
import json, sys
data = json.load(open('$WORKSPACE/reverse/firebase_activate.json'))
configs = data['configs_key']
expected = ['ac_available_modes', 'ac_classic_temp_control', 'ac_dehumidifier_codes',
            'ac_extra_windspeed', 'ac_show_display_mold', 'ac_single_mode_codes']
missing = [k for k in expected if k not in configs]
print('missing keys:', missing or 'none')
print('fetch_time:', data.get('fetch_time_key'))
"
```

---

## 2. Regenerate `ac_catalog.py`

From the integration repo root:

```bash
python3 tools/regen_ac_catalog.py
```

The script:

- Reads `../reverse/firebase_activate.json` by default
- Parses the six AC-capability config strings
- Preserves `AC_OBSERVED_EXTRA_WINDSPEED_CODES` and
  `AC_OBSERVED_TURBO_CODES` from the existing file (those are
  hand-maintained from hardware testing, not Firebase data)
- Writes `custom_components/aifa_smart/ac_catalog.py`

For a dry run (print to stdout instead of writing):

```bash
python3 tools/regen_ac_catalog.py --dry-run
```

To use a non-default Firebase snapshot path:

```bash
python3 tools/regen_ac_catalog.py --firebase /tmp/some-other-firebase.json
```

---

## 3. Verify

Compile-check + unit tests:

```bash
python3 -m py_compile custom_components/aifa_smart/ac_catalog.py
python3 -m unittest discover tests
```

Eyeball the diff for sanity:

```bash
git diff custom_components/aifa_smart/ac_catalog.py
```

Common patterns to expect:

- New `device_code` keys added (AIFA introduced new AC models)
- Existing codes' mode tuples may shrink/grow (AIFA refined a model's
  capability list — uncommon)
- `AC_*_CODES` frozensets gain/lose entries occasionally

If the diff looks suspicious (e.g., entire catalog wiped, frozensets
empty when they shouldn't be), the Firebase snapshot is probably
malformed — re-extract.

---

## 4. Commit

```bash
git add custom_components/aifa_smart/ac_catalog.py reverse/firebase_activate.json
git commit -m "$(cat <<'EOF'
Refresh ac_catalog.py from Firebase RC snapshot

Source: AIFA app v<X.Y.Z> firebase_activate.json fetched <YYYY-MM-DD>.

[Briefly note any noteworthy changes — new codes added, capability
shifts, etc.]
EOF
)"
```

The `reverse/firebase_activate.json` file lives in the workspace, NOT
the integration repo (the integration repo only carries the generated
`ac_catalog.py`). Adjust the commit accordingly.

---

## When to refresh

- A user reports their AC's HA UI is missing a mode button it should
  have (or showing one it shouldn't)
- Before a major release, run a refresh to catch silent drift
- AIFA app gets a major version bump that mentions new AC models

There's no automated schedule. Drift is mostly cosmetic (catalog only
controls which mode buttons HA renders; encoded packets are catalog-
agnostic), so opportunistic refresh is enough.

---

## Why no automated CI/CD

We considered a scheduled GitHub Action that would refresh
automatically. Rejected because:

- AIFA app's Firebase API key in repo secrets is risky (TOS, key
  rotation breaks integration on next AIFA release)
- Volume from a single CI run is fine, but adds maintenance complexity
  for low-impact drift
- Manual refresh is fast (~5 min once the AVD is running)

If a future maintainer wants to automate, the regen tool already does
the data-shaping work — only the "extract fresh `firebase_activate.json`"
step would need automation (which means handling Firebase Installations
auth + key management).
