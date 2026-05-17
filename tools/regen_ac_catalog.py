"""Regenerate `custom_components/aifa_smart/ac_catalog.py` from Firebase RC.

The catalog inside the integration is derived from AIFA Smart's Firebase
Remote Config payload. AIFA app fetches this at runtime via the Firebase
SDK; the integration ships a static snapshot to avoid bringing the
Firebase SDK + AIFA's API key into the HA runtime.

Run this script after refreshing `reverse/firebase_activate.json` (see
the wiki's "Regen AC Catalog" page for the full extraction procedure:
https://github.com/PinLin/aifa-smart-homeassistant/wiki/Regen-AC-Catalog).

Usage:
    python3 tools/regen_ac_catalog.py
    python3 tools/regen_ac_catalog.py --firebase /path/to/firebase_activate.json

The script:
  1. Reads `firebase_activate.json` (default: `../reverse/firebase_activate.json`)
  2. Parses the seven AC-capability config strings
  3. Preserves the manually-maintained `AC_OBSERVED_*` frozensets from
     the existing `ac_catalog.py` (those reflect hardware testing, not
     Firebase data — they survive regenerations)
  4. Writes the new `ac_catalog.py` with sorted, deterministic output
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

DEFAULT_FIREBASE_JSON = Path(__file__).resolve().parent.parent.parent / "reverse" / "firebase_activate.json"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "aifa_smart" / "ac_catalog.py"
)


def _load_configs(firebase_path: Path) -> dict[str, str]:
    """Return Firebase configs_key as a {name: raw-string} dict.

    Firebase RC payload structure:
      {"configs_key": {"<name>": "<JSON-encoded string>", ...}, ...}
    Each value is itself a JSON string that needs a second decode to use.
    """
    payload = json.loads(firebase_path.read_text(encoding="utf-8"))
    configs = payload.get("configs_key")
    if not isinstance(configs, dict):
        raise ValueError(
            f"Unexpected payload shape in {firebase_path}: missing configs_key dict"
        )
    return configs


def _decode_codes_list(raw: str) -> list[int]:
    """Parse a {"codes": [<int>, ...]} string into a sorted unique int list."""
    parsed = json.loads(raw)
    codes = parsed.get("codes", [])
    return sorted({int(c) for c in codes})


def _decode_modes_map(raw: str) -> dict[str, list[str]]:
    """Parse a {"<code>": ["<mode>", ...]} string."""
    parsed = json.loads(raw)
    return {str(k): [str(m) for m in v] for k, v in parsed.items()}


def _extract_existing_observed_sets(catalog_path: Path) -> dict[str, list[str]]:
    """Read the existing ac_catalog.py and return AC_OBSERVED_* frozenset
    contents so a regenerate run preserves manual hardware-testing notes.

    Returns ``{name: [code, ...]}`` (sorted as we read them); when the file
    doesn't exist or has no observed sets, returns empty lists for the
    canonical names so the regenerated file still defines them.
    """
    canonical = {
        "AC_OBSERVED_EXTRA_WINDSPEED_CODES": [],
        "AC_OBSERVED_TURBO_CODES": [],
    }
    if not catalog_path.exists():
        return canonical

    tree = ast.parse(catalog_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if name not in canonical:
            continue
        # We expect a `frozenset([...])` call value; pull the list literal.
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and value.args
            and isinstance(value.args[0], (ast.List, ast.Tuple))
        ):
            elements = value.args[0].elts
            canonical[name] = sorted(
                str(elt.value) for elt in elements
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            )
    return canonical


def _emit_modes_map(modes: dict[str, list[str]]) -> str:
    """Format the AC_AVAILABLE_MODES_BY_DEVICE_CODE dict literal.

    Tuple syntax rules: empty tuple is `()`, single-element needs trailing
    comma `('x',)`, multi-element doesn't `('x', 'y')`. Match the style of
    the hand-written original.
    """
    lines = []
    for code in sorted(modes.keys(), key=lambda x: int(x)):
        items = modes[code]
        if not items:
            literal = "()"
        elif len(items) == 1:
            literal = f"({items[0]!r},)"
        else:
            literal = "(" + ", ".join(repr(m) for m in items) + ")"
        lines.append(f"    {code!r}: {literal},")
    return "\n".join(lines)


def _emit_codes_set(codes: list[int]) -> str:
    """Format codes as `frozenset(['1', '2', ...])` with strings."""
    items = ", ".join(repr(str(c)) for c in codes)
    return f"frozenset([{items}])"


def _emit_observed_set(codes: list[str]) -> str:
    """Like `_emit_codes_set` but takes pre-stringified codes and emits
    `frozenset()` when empty (to match the hand-written style)."""
    if not codes:
        return "frozenset()"
    items = ", ".join(repr(c) for c in codes)
    return f"frozenset([{items}])"


def regenerate(firebase_path: Path, catalog_path: Path) -> str:
    """Build the new ac_catalog.py content as a string. Writes when called via CLI."""
    configs = _load_configs(firebase_path)

    available_modes = _decode_modes_map(configs["ac_available_modes"])
    classic_temp_codes = _decode_codes_list(configs["ac_classic_temp_control"])
    extra_windspeed_codes = _decode_codes_list(configs["ac_extra_windspeed"])
    show_display_mold_codes = _decode_codes_list(configs["ac_show_display_mold"])
    single_mode_codes = _decode_codes_list(configs["ac_single_mode_codes"])
    dehumidifier_codes = _decode_codes_list(configs["ac_dehumidifier_codes"])

    observed = _extract_existing_observed_sets(catalog_path)

    body = f'''"""Generated AIFA AC capability metadata from Firebase Remote Config."""
from __future__ import annotations

# Generated from reverse/firebase_activate.json by tools/regen_ac_catalog.py.
# Do NOT edit by hand — re-run the tool after refreshing the Firebase
# snapshot. See https://github.com/PinLin/aifa-smart-homeassistant/wiki/Regen-AC-Catalog.

AC_AVAILABLE_MODES_BY_DEVICE_CODE: dict[str, tuple[str, ...]] = {{
{_emit_modes_map(available_modes)}
}}

AC_CLASSIC_TEMP_CONTROL_CODES: frozenset[str] = {_emit_codes_set(classic_temp_codes)}

AC_EXTRA_WINDSPEED_CODES: frozenset[str] = {_emit_codes_set(extra_windspeed_codes)}

AC_SHOW_DISPLAY_MOLD_CODES: frozenset[str] = {_emit_codes_set(show_display_mold_codes)}

AC_SINGLE_MODE_CODES: frozenset[str] = {_emit_codes_set(single_mode_codes)}

AC_DEHUMIDIFIER_CODES: frozenset[str] = {_emit_codes_set(dehumidifier_codes)}

# The two AC_OBSERVED_* sets below are NOT from Firebase Remote Config.
# They track codes for which hardware testing has empirically confirmed
# behavior beyond what the Firebase capability matrix declares (e.g.,
# extra windspeed support that's not in the catalog, or turbo-bit
# responsiveness on specific AC families). The regen tool preserves
# whatever values are in this file across runs.

AC_OBSERVED_EXTRA_WINDSPEED_CODES: frozenset[str] = {_emit_observed_set(observed["AC_OBSERVED_EXTRA_WINDSPEED_CODES"])}

AC_OBSERVED_TURBO_CODES: frozenset[str] = {_emit_observed_set(observed["AC_OBSERVED_TURBO_CODES"])}
'''
    return body


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate ac_catalog.py from a Firebase RC snapshot."
    )
    parser.add_argument(
        "--firebase",
        type=Path,
        default=DEFAULT_FIREBASE_JSON,
        help=f"path to firebase_activate.json (default: {DEFAULT_FIREBASE_JSON})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"path to write ac_catalog.py (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the generated content to stdout instead of writing",
    )
    args = parser.parse_args()

    if not args.firebase.exists():
        print(f"firebase_activate.json not found: {args.firebase}")
        print("See https://github.com/PinLin/aifa-smart-homeassistant/wiki/Regen-AC-Catalog for how to extract a fresh snapshot.")
        return 1

    content = regenerate(args.firebase, args.output)
    if args.dry_run:
        print(content)
        return 0

    args.output.write_text(content, encoding="utf-8")
    print(f"Wrote {args.output} ({len(content)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
