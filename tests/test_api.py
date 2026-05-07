"""Tests for AIFA Smart API normalization."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from custom_components.aifa_smart.api import (
    AifaSmartApiClient,
    AifaFunctionCommand,
    AifaTokens,
    _extract_classic_sensor_packets,
    _extract_classic_notify_packets,
    _extract_classic_status_packets,
    _extract_classic_wake_packets,
    decode_classic_sensor_packet,
    _oauth_payload,
    _parse_device_code_catalog,
    _parse_function,
    _parse_macro,
    _parse_device,
    _parse_sub_device,
    _transfer_payload,
)
from custom_components.aifa_smart.const import (
    DEVICE_TRANSFER_PATH,
    MACROS_PATH,
    OAUTH_CLIENT_ID,
)


class ApiNormalizationTests(unittest.TestCase):
    """Coverage for local normalization helpers."""

    def test_parse_device_normalizes_common_fields(self) -> None:
        """Device rows should map into the normalized dataclass."""
        device = _parse_device(
            {
                "id": 42,
                "deviceName": "Living Room",
                "mac": "e8db84ee5bb2",
                "deviceType": "i-Ctrl Pro Plus",
                "online": 1,
                "temperature": "25.5",
                "humidity": "61",
                "firmware": "1.2.3",
                "subDevices": [{"id": 1}, {"id": 2}],
            }
        )

        self.assertIsNotNone(device)
        assert device is not None
        self.assertEqual(device.id, "42")
        self.assertEqual(device.name, "Living Room")
        self.assertEqual(device.mac, "e8db84ee5bb2")
        self.assertEqual(device.device_type, "i-Ctrl Pro Plus")
        self.assertTrue(device.online)
        self.assertEqual(device.temperature, 25.5)
        self.assertEqual(device.humidity, 61.0)
        self.assertEqual(device.firmware, "1.2.3")
        self.assertEqual(len(device.sub_devices), 2)

    def test_parse_device_reads_nested_sensors_block(self) -> None:
        """Device rows should read temperature and humidity from the plural sensors block."""
        device = _parse_device(
            {
                "id": 8014,
                "deviceName": "i-Ctrl AC-5bb2",
                "sensors": {
                    "temperature": "26.4",
                    "humidity": "58",
                },
            }
        )

        self.assertIsNotNone(device)
        assert device is not None
        self.assertEqual(device.temperature, 26.4)
        self.assertEqual(device.humidity, 58.0)

    def test_parse_sub_device_keeps_device_code_and_types(self) -> None:
        """Sub-device rows should normalize control metadata."""
        sub_device = _parse_sub_device(
            {
                "id": 31867,
                "name": "冷氣",
                "type": 0,
                "subType": 0,
                "deviceCode": 136,
            },
            8014,
        )

        self.assertIsNotNone(sub_device)
        assert sub_device is not None
        self.assertEqual(sub_device.id, "31867")
        self.assertEqual(sub_device.device_id, "8014")
        self.assertEqual(sub_device.name, "冷氣")
        self.assertEqual(sub_device.type, "0")
        self.assertEqual(sub_device.sub_type, "0")
        self.assertEqual(sub_device.device_code, "136")

    def test_parse_device_code_catalog_keeps_brand_and_selected_code(self) -> None:
        """Device-code catalogs should preserve both brands and concrete code values."""
        catalog = _parse_device_code_catalog(
            {
                "brands": [
                    {
                        "id": 32,
                        "name": "CHIMEI",
                        "localizations": [
                            {
                                "countryCode": "tw",
                                "languageCode": "zh",
                                "name": "奇美",
                            }
                        ],
                    }
                ],
                "deviceCodes": [
                    {
                        "id": 136,
                        "code": 15,
                        "brandId": 32,
                        "country": 1,
                        "version": 5,
                        "subversion": 210623,
                        "type": 0,
                        "remote": False,
                        "popular": False,
                    }
                ],
            }
        )

        self.assertEqual(len(catalog.brands), 1)
        self.assertEqual(len(catalog.device_codes), 1)
        selected = catalog.get_device_code("136")
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.code, "15")
        self.assertEqual(selected.brand_id, "32")
        self.assertEqual(selected.brand_name, "CHIMEI")
        self.assertEqual(selected.brand_localized_name, "奇美")
        self.assertEqual(selected.version, 5)
        self.assertFalse(selected.remote)

    def test_find_by_code_distinguishes_id_from_code(self) -> None:
        """`find_by_code` matches the catalog's `code` field, not `id`.

        Regression guard: cloud's `sub_device.deviceCode` stores the IR
        codec family number (e.g. 11 for Daikin Classic AC), NOT the
        catalog row id. Earlier coordinator code mistakenly fed that
        value into `get_device_code` (which keys by id) and ended up
        attaching the wrong brand/model to the sub-device. `find_by_code`
        is the correct lookup for cloud-derived `deviceCode` values.
        """
        catalog = _parse_device_code_catalog(
            {
                "brands": [
                    {"id": 1, "name": "DAIKIN", "localizations": []},
                ],
                "deviceCodes": [
                    {"id": 4, "code": 11, "brandId": 1, "country": 1,
                     "version": 5, "subversion": 0, "type": 0,
                     "remote": False, "popular": False},
                    {"id": 11, "code": 160, "brandId": 1, "country": 1,
                     "version": 5, "subversion": 0, "type": 0,
                     "remote": False, "popular": False},
                ],
            }
        )

        # Cloud says deviceCode=11 — the catalog's `code: 11` row, which
        # has internal id=4. The legacy `get_device_code("11")` finds the
        # row whose id is 11 (which is actually code 160) — exactly the
        # bug. `find_by_code("11")` must instead return the id=4 / code=11
        # row.
        wrong = catalog.get_device_code("11")
        self.assertIsNotNone(wrong)
        assert wrong is not None
        self.assertEqual(wrong.code, "160")  # confirms the legacy bug shape

        right = catalog.find_by_code("11")
        self.assertIsNotNone(right)
        assert right is not None
        self.assertEqual(right.id, "4")
        self.assertEqual(right.code, "11")

    def test_find_by_code_returns_none_for_missing(self) -> None:
        """`find_by_code` returns None when no catalog row matches."""
        catalog = _parse_device_code_catalog(
            {"brands": [], "deviceCodes": [
                {"id": 1, "code": 7, "brandId": 1, "country": 1,
                 "version": 5, "subversion": 0, "type": 0,
                 "remote": False, "popular": False},
            ]}
        )
        self.assertIsNone(catalog.find_by_code("999"))
        self.assertIsNone(catalog.find_by_code(None))

    def test_oauth_payload_includes_verified_client_id(self) -> None:
        """OAuth requests should always include the verified client_id."""
        payload = _oauth_payload("password", email="user@example.com", password="secret")

        self.assertEqual(
            payload,
            {
                "client_id": OAUTH_CLIENT_ID,
                "grant_type": "password",
                "email": "user@example.com",
                "password": "secret",
            },
        )

    def test_transfer_payload_uses_verified_mac_shape(self) -> None:
        """Classic i-Ctrl transfer calls should use the verified mac-based payload."""
        payload = _transfer_payload(
            "e8db84ee5bb2",
            command="ffa0a2f0",
            device_id="8014",
            sub_device_id="31867",
            extra={"type": "status"},
        )

        self.assertEqual(
            payload,
            {
                "mac": "e8db84ee5bb2",
                "command": "ffa0a2f0",
                "deviceId": 8014,
                "subDeviceId": 31867,
                "type": "status",
            },
        )

    def test_extract_classic_status_packets_keeps_tail(self) -> None:
        """Observed status extraction should keep complete frames and short tails."""
        packets, tail = _extract_classic_status_packets(bytes.fromhex("fea00136000000000000000000f0fe"))
        self.assertEqual([packet.hex() for packet in packets], ["fea00136000000000000000000f0"])
        self.assertEqual(tail, bytes.fromhex("fe"))

    def test_extract_classic_wake_packets_keeps_tail(self) -> None:
        """Wake-up extraction should keep complete frames and short tails."""
        packets, tail = _extract_classic_wake_packets(bytes.fromhex("00f1ee01f0f1ee"))
        self.assertEqual([packet.hex() for packet in packets], ["f1ee01f0"])
        self.assertEqual(tail, bytes.fromhex("f1ee"))

    def test_extract_classic_notify_packets_keeps_tail(self) -> None:
        """Notify extraction should keep complete frames and partial tails."""
        packets, tail = _extract_classic_notify_packets(bytes.fromhex("00faf5a00136f0faf5a1"))
        self.assertEqual([packet.hex() for packet in packets], ["faf5a00136f0"])
        self.assertEqual(tail, bytes.fromhex("faf5a1"))

    def test_extract_classic_sensor_packets_keeps_tail(self) -> None:
        """Live sensor extraction should keep complete JSON payloads and partial tails."""
        packets, tail = _extract_classic_sensor_packets(
            b"xxSENSOR{\"temp\":\"28915\"}SENSOR{\"hum\":\"37282\""
        )
        self.assertEqual(
            packets,
            [b'SENSOR{"temp":"28915"}'],
        )
        self.assertEqual(tail, b'SENSOR{"hum":"37282"')

    def test_decode_classic_sensor_packet_matches_app_formula(self) -> None:
        """Classic live sensor payloads should decode with the same formula as the app."""
        decoded = decode_classic_sensor_packet(
            bytes.fromhex(
                "53454e534f527b0a2274656d70223a223238393135222c0a2268756d223a223337323832220a7d"
            )
        )

        self.assertIsNotNone(decoded)
        assert decoded is not None
        expected_temperature = (-0.00322 * (((28915 / 65536) * 175 - 45) ** 2)) + (
            1.05 * ((28915 / 65536) * 175 - 45)
        ) + 0.235
        expected_humidity = (37282 / 65536) * 100
        self.assertAlmostEqual(decoded.temperature or 0.0, expected_temperature, places=4)
        self.assertAlmostEqual(decoded.humidity or 0.0, expected_humidity, places=4)
        self.assertEqual(decoded.raw, {"temp": "28915", "hum": "37282"})

    def test_parse_function_keeps_commands_and_schedule(self) -> None:
        """Function rows should normalize nested commands."""
        function = _parse_function(
            {
                "id": 42316,
                "deviceId": 8014,
                "name": "Auto 29",
                "days": 127,
                "notification": 2,
                "active": False,
                "local": False,
                "time": {"start": 60, "length": 30},
                "commands": [
                    {
                        "id": 585196,
                        "deviceId": 8014,
                        "subDeviceId": 31867,
                        "command": "ffa0060136280020804000000000f0",
                    }
                ],
            }
        )

        self.assertIsNotNone(function)
        assert function is not None
        self.assertEqual(function.id, "42316")
        self.assertEqual(function.device_id, "8014")
        self.assertEqual(function.name, "Auto 29")
        self.assertEqual(function.days, 127)
        self.assertEqual(function.notification, 2)
        self.assertEqual(function.start_time, 60)
        self.assertEqual(function.time_interval, 30)
        self.assertEqual(function.sub_device_ids, ["31867"])
        self.assertEqual(len(function.commands), 1)
        self.assertEqual(function.commands[0].command, "ffa0060136280020804000000000f0")

    def test_parse_function_keeps_zero_schedule_values(self) -> None:
        """Function schedule parsing should preserve explicit zero values."""
        function = _parse_function(
            {
                "id": 42316,
                "deviceId": 8014,
                "name": "Auto 29",
                "time": {"start": 0, "length": 0},
                "commands": [
                    {
                        "id": 585196,
                        "deviceId": 8014,
                        "subDeviceId": 31867,
                        "command": "ffa0060136280020804000000000f0",
                    }
                ],
            }
        )

        self.assertIsNotNone(function)
        assert function is not None
        self.assertEqual(function.start_time, 0)
        self.assertEqual(function.time_interval, 0)

    def test_parse_macro_keeps_nested_commands_and_delay(self) -> None:
        """Macro rows should normalize nested raw commands."""
        macro = _parse_macro(
            {
                "id": 9,
                "name": "Movie Time",
                "commands": [
                    {
                        "id": 101,
                        "deviceId": 8014,
                        "subDeviceId": 31867,
                        "command": "ffaa0011",
                        "delay": 500,
                        "orderNo": 2,
                    },
                    {
                        "id": 100,
                        "deviceId": 8014,
                        "command": "ffaa0000",
                        "delay": 0,
                        "orderNo": 1,
                    },
                ],
            }
        )

        self.assertIsNotNone(macro)
        assert macro is not None
        self.assertEqual(macro.id, "9")
        self.assertEqual(macro.name, "Movie Time")
        self.assertEqual([command.id for command in macro.commands], ["100", "101"])
        self.assertEqual(macro.commands[0].delay, 0)
        self.assertEqual(macro.commands[1].sub_device_id, "31867")


class ApiClientBehaviorTests(unittest.IsolatedAsyncioTestCase):
    """Coverage for client request shaping."""

    async def test_open_classic_socket_returns_reader_writer_and_initial_bytes(self) -> None:
        """Classic socket opening should preserve any post-handshake payload for the caller."""
        client = AifaSmartApiClient(session=object())

        async def fake_ensure_access_token(*, force_login: bool = False, force_refresh: bool = False):
            self.assertFalse(force_login)
            self.assertFalse(force_refresh)
            return AifaTokens(access_token="test-token")

        written: list[bytes] = []

        class FakeWriter:
            def write(self, data: bytes) -> None:
                written.append(data)

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                return None

            async def wait_closed(self) -> None:
                return None

        class FakeReader:
            async def read(self, size: int) -> bytes:
                self_size = size
                assert self_size == 32
                return b"\x00" + bytes.fromhex("fea00136000000000000000000f0")

        async def fake_open_connection(*args, **kwargs):
            self.assertEqual(args[0], "aifaremote.com")
            self.assertEqual(args[1], 8751)
            self.assertIn("ssl", kwargs)
            self.assertEqual(kwargs.get("server_hostname"), "aifaremote.com")
            return FakeReader(), FakeWriter()

        client._ensure_access_token = fake_ensure_access_token  # type: ignore[method-assign]

        with patch("custom_components.aifa_smart.api.asyncio.open_connection", fake_open_connection):
            session = await client.async_open_classic_socket("e8db84ee5bb2")

        self.assertEqual(session.initial_bytes, bytes.fromhex("fea00136000000000000000000f0"))
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][:4], bytes.fromhex("a1fad7c0"))
        self.assertIn(b'"mac":"e8db84ee5bb2"', written[0])
        self.assertIn(b'"token":"test-token"', written[0])

    async def test_transfer_packet_posts_verified_transfer_shape(self) -> None:
        """Classic i-Ctrl transfer should use the verified mac-based endpoint."""
        client = AifaSmartApiClient(session=object())
        requests: list[tuple[str, str, dict[str, object]]] = []

        async def fake_authorized_request(
            method: str, path: str, **kwargs: object
        ) -> None:
            requests.append((method, path, kwargs))
            return None

        client._authorized_request = fake_authorized_request  # type: ignore[method-assign]

        response = await client.async_transfer_packet(
            "e8db84ee5bb2",
            packet="ffa0a2f0",
            device_id="8014",
            sub_device_id="31867",
        )

        self.assertTrue(response)
        self.assertEqual(
            requests,
            [
                (
                    "POST",
                    DEVICE_TRANSFER_PATH,
                    {
                        "json": {
                            "mac": "e8db84ee5bb2",
                            "packet": "ffa0a2f0",
                            "deviceId": 8014,
                            "subDeviceId": 31867,
                        }
                    },
                )
            ],
        )

    async def test_valid_stored_tokens_skip_login_and_refresh(self) -> None:
        """A client restored from entry.data should reuse the stored access token."""
        from datetime import UTC, datetime, timedelta

        future = datetime.now(UTC) + timedelta(hours=1)
        stored = AifaTokens(access_token="persisted", refresh_token="r", expires_at=future)
        client = AifaSmartApiClient(
            session=object(),
            email="user@example.com",
            password="secret",
            tokens=stored,
        )

        async def fail(*args, **kwargs):
            raise AssertionError("network auth should not be invoked")

        client._async_login_password = fail  # type: ignore[method-assign]
        client._async_refresh_token = fail  # type: ignore[method-assign]

        tokens = await client._ensure_access_token()

        self.assertIs(tokens, stored)

    async def test_password_login_invokes_tokens_updated_callback(self) -> None:
        """Password grant should feed new tokens into the persistence callback."""
        observed: list[AifaTokens] = []
        client = AifaSmartApiClient(
            session=object(),
            email="user@example.com",
            password="hunter2",
            on_tokens_updated=observed.append,
        )

        async def fake_login(email: str, password: str) -> AifaTokens:
            self.assertEqual(email, "user@example.com")
            self.assertEqual(password, "hunter2")
            return AifaTokens(access_token="fresh", refresh_token="r1")

        client._async_login_password = fake_login  # type: ignore[method-assign]

        tokens = await client._ensure_access_token(force_login=True)

        self.assertEqual(tokens.access_token, "fresh")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].access_token, "fresh")
        self.assertEqual(observed[0].refresh_token, "r1")

    async def test_refresh_path_invokes_tokens_updated_callback(self) -> None:
        """Refreshing an expiring token should notify the persistence listener."""
        from datetime import UTC, datetime, timedelta

        # expires_at in the past → expiring_soon() returns True → refresh path
        expired = datetime.now(UTC) - timedelta(minutes=1)
        existing = AifaTokens(
            access_token="stale", refresh_token="old-refresh", expires_at=expired
        )
        observed: list[AifaTokens] = []
        client = AifaSmartApiClient(
            session=object(),
            tokens=existing,
            on_tokens_updated=observed.append,
        )

        async def fake_refresh(refresh_token: str) -> AifaTokens:
            self.assertEqual(refresh_token, "old-refresh")
            return AifaTokens(access_token="refreshed", refresh_token="new-refresh")

        client._async_refresh_token = fake_refresh  # type: ignore[method-assign]

        tokens = await client._ensure_access_token()

        self.assertEqual(tokens.access_token, "refreshed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].refresh_token, "new-refresh")

    async def test_refresh_failure_propagates_for_reauth(self) -> None:
        """A revoked refresh token should propagate so the coordinator triggers reauth."""
        from custom_components.aifa_smart.api import AifaSmartAuthError

        existing = AifaTokens(access_token="stale", refresh_token="bad-refresh")
        client = AifaSmartApiClient(session=object(), tokens=existing)

        async def fake_refresh(refresh_token: str) -> AifaTokens:
            raise AifaSmartAuthError("refresh revoked")

        client._async_refresh_token = fake_refresh  # type: ignore[method-assign]

        with self.assertRaises(AifaSmartAuthError):
            await client._ensure_access_token(force_refresh=True)

    async def test_expired_access_without_refresh_or_password_raises_for_reauth(self) -> None:
        """When stored access_token expired and no refresh_token and no password, raise AuthError so reauth flow runs."""
        from datetime import UTC, datetime, timedelta

        from custom_components.aifa_smart.api import AifaSmartAuthError

        # Stored access_token is past its expiry, refresh_token absent, no email/password.
        expired = datetime.now(UTC) - timedelta(hours=1)
        stored = AifaTokens(access_token="stale", refresh_token=None, expires_at=expired)
        client = AifaSmartApiClient(session=object(), tokens=stored)

        with self.assertRaises(AifaSmartAuthError):
            await client._ensure_access_token()

    async def test_get_macros_reads_verified_top_level_key(self) -> None:
        """Macro fetching should accept the observed response shape."""
        client = AifaSmartApiClient(session=object())

        async def fake_authorized_request(
            method: str, path: str, **kwargs: object
        ) -> dict[str, object]:
            self.assertEqual((method, path, kwargs), ("GET", MACROS_PATH, {}))
            return {
                "macros": [
                    {
                        "id": 7,
                        "name": "Movie Time",
                        "commands": [
                            {
                                "id": 70,
                                "deviceId": 8014,
                                "command": "ffaa0011",
                                "orderNo": 1,
                            }
                        ],
                    }
                ]
            }

        client._authorized_request = fake_authorized_request  # type: ignore[method-assign]

        macros = await client.async_get_macros()

        self.assertEqual(len(macros), 1)
        self.assertEqual(macros[0].name, "Movie Time")
        self.assertEqual(len(macros[0].commands), 1)
        self.assertEqual(macros[0].commands[0].command, "ffaa0011")


if __name__ == "__main__":
    unittest.main()
