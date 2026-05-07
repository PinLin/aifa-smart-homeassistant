"""Tests for the runtime catalog façade."""
from __future__ import annotations

import unittest

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

    def test_parse_raises_on_top_level_list_payload(self) -> None:
        """If a Firebase value parses to a top-level list instead of dict/object,
        the resulting AttributeError must be wrapped as CatalogParseError so
        callers (T6) can distinguish 'schema drift' from real bugs."""
        broken = self._sample_rc_response()
        broken["entries"]["ac_available_modes"] = "[1, 2]"
        with self.assertRaises(catalog.CatalogParseError):
            catalog.parse_firebase_payload(broken)

    def test_parse_ignores_observed_keys_in_firebase_payload(self) -> None:
        """The §6.2 merge rule says observed_* fields ALWAYS come from bundled,
        never from Firebase. Even if AIFA ever publishes an observed key in RC
        (they don't today), the parser must ignore it."""
        payload = self._sample_rc_response()
        # Inject a synthetic observed key with a value that would be visible
        # if the parser ever wired it through.
        payload["entries"]["ac_observed_extra_windspeed"] = '{"codes": [9999]}'
        parsed = catalog.parse_firebase_payload(payload)
        self.assertNotIn("9999", parsed.observed_extra_windspeed_codes)
        # And confirm the bundled value is what's actually in there:
        self.assertEqual(
            parsed.observed_extra_windspeed_codes,
            catalog.get_bundled().observed_extra_windspeed_codes,
        )


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

    def test_load_from_cache_rejects_bool_version(self) -> None:
        """Bool is an int subclass; version gate must reject it explicitly so
        a future schema bump doesn't silently accept the wrong shape."""
        dumped = catalog.dump_for_cache(self._live_catalog())
        dumped["version"] = True  # True == 1 == CACHE_SCHEMA_VERSION
        self.assertIsNone(catalog.load_from_cache(dumped))

    def test_load_from_cache_coerces_naive_fetched_at_to_utc(self) -> None:
        """A hand-edited cache with a naive timestamp must coerce to UTC, not
        leave T6's cooldown arithmetic crashing on naive vs aware subtraction."""
        dumped = catalog.dump_for_cache(self._live_catalog())
        dumped["fetched_at"] = "2026-05-03T12:00:00"  # naive — no offset
        restored = catalog.load_from_cache(dumped)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertIsNotNone(restored.fetched_at)
        self.assertIsNotNone(restored.fetched_at.tzinfo)

    def test_load_from_cache_ignores_observed_keys_in_payload(self) -> None:
        """Even if a corrupt or future cache file contained observed_* keys,
        they must be ignored in favor of the bundled values (§6.2 invariant)."""
        dumped = catalog.dump_for_cache(self._live_catalog())
        dumped["configs"]["ac_observed_extra_windspeed"] = {"codes": ["9999"]}
        restored = catalog.load_from_cache(dumped)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertNotIn("9999", restored.observed_extra_windspeed_codes)
        self.assertEqual(
            restored.observed_extra_windspeed_codes,
            catalog.get_bundled().observed_extra_windspeed_codes,
        )


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

    async def test_async_refresh_opens_repair_after_threshold(self) -> None:
        """Three consecutive auth failures must open the HA repair issue;
        earlier failures must NOT yet open it."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from custom_components.aifa_smart.firebase_rc_client import FirebaseAuthError

        fake_hass = MagicMock()
        issue_calls: list = []
        delete_calls: list = []

        fake_ir = MagicMock()
        fake_ir.async_create_issue = lambda *a, **k: issue_calls.append((a, k))
        fake_ir.async_delete_issue = lambda *a, **k: delete_calls.append((a, k))
        fake_ir.IssueSeverity = MagicMock()
        fake_ir.IssueSeverity.WARNING = "warning"

        fake_storage = MagicMock()
        fake_storage.Store = MagicMock(return_value=MagicMock(
            async_save=AsyncMock(return_value=None),
        ))

        client = AsyncMock()
        client.async_register_installation.side_effect = FirebaseAuthError("403")

        with patch.object(catalog, "_build_client", return_value=client), \
             patch.dict("sys.modules", {
                 "homeassistant.helpers": MagicMock(issue_registry=fake_ir),
                 "homeassistant.helpers.issue_registry": fake_ir,
                 "homeassistant.helpers.storage": fake_storage,
             }):
            # 1st failure: no issue
            await catalog.async_refresh(fake_hass)
            self.assertEqual(issue_calls, [])
            # 2nd failure: still no issue
            await catalog.async_refresh(fake_hass)
            self.assertEqual(issue_calls, [])
            # 3rd failure: issue opens
            await catalog.async_refresh(fake_hass)
            self.assertEqual(len(issue_calls), 1)

    async def test_concurrent_refresh_calls_serialize_and_only_first_fetches(self) -> None:
        """Two overlapping refresh calls must serialize via _refresh_lock; the
        second should hit the cooldown gate inside the lock and skip.
        """
        from unittest.mock import AsyncMock
        import asyncio

        # Use a real coroutine side_effect so the await actually yields control
        # to the event loop. Without a true suspension point, AsyncMock can
        # complete synchronously and mask the race condition.
        async def slow_register() -> str:
            await asyncio.sleep(0)
            return "tok"

        client = AsyncMock()
        client.async_register_installation.side_effect = slow_register
        client.async_fetch_remote_config.return_value = {
            "entries": {
                "ac_available_modes": '{"1": ["cool"]}',
                "ac_classic_temp_control": '{"codes": []}',
                "ac_extra_windspeed": '{"codes": []}',
                "ac_show_display_mold": '{"codes": []}',
                "ac_single_mode_codes": '{"codes": []}',
                "ac_dehumidifier_codes": '{"codes": []}',
            },
            "templateVersion": 51,
        }

        results = await asyncio.gather(
            catalog._refresh_with_client(client),
            catalog._refresh_with_client(client),
        )
        statuses = sorted(r.status for r in results)
        self.assertEqual(statuses, ["skipped_cooldown", "success"])
        # Only the first call actually hit Firebase
        self.assertEqual(client.async_register_installation.await_count, 1)
        self.assertEqual(client.async_fetch_remote_config.await_count, 1)


if __name__ == "__main__":
    unittest.main()
