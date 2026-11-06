"""Tests for the AIFA AC packet helper."""
from __future__ import annotations

import unittest

from custom_components.aifa_smart.ac import (
    AIFA_AC_ESTIMATED_CAPABILITIES,
    AIFA_AC_DEFAULT_TARGET_TEMP,
    CLASSIC_AC_QUERY_PACKET,
    SHARED_AIFA_AC_PROFILE,
    build_classic_temperature_key_command,
    build_estimated_capabilities,
    build_mode_key_command,
    build_power_off_command,
    build_power_on_command,
    build_app_status_command,
    clamp_target_temperature,
    clamp_timer_ir_value,
    decode_observed_status_packet,
    encode_timer_ir_bcd,
    encode_temperature_bcd,
    get_ac_capabilities,
    get_ac_profile,
    get_supported_fan_modes,
    get_supported_swing_modes,
    hub_requires_mode_keys,
    is_supported_ac_device_code,
    is_supported_ac_sub_device,
    normalize_ac_mode,
    AifaAcStatus,
)


def _build(
    mode: str,
    temperature: int | None,
    *,
    key_press: int,
    code: str = "136",
    fan: str = "auto",
    swing: str = "auto",
    timer: int = 0,
    sleep: bool = False,
    turbo: bool = False,
    power_saving: bool = False,
    sub_type: int = 0,
) -> str:
    return build_app_status_command(
        AifaAcStatus(
            device_code=code,
            mode=mode,
            target_temperature=temperature,
            timer_ir_value=timer,
            fan_mode=fan,
            swing_mode=swing,
            turbo=turbo,
            sleep=sleep,
            power_saving=power_saving,
            packet_byte_12=0,
            packet_byte_13=0,
        ),
        key_press=key_press,
        sub_type=sub_type,
        device_code=code,
    )


def _as_report(command: str):
    """i-Ctrl Pro's fromCommandBytes: drop the keyPress byte, then fromBytes."""
    packet = bytes.fromhex(command)
    decoded = decode_observed_status_packet(b"\xfe" + packet[1:2] + packet[3:])
    assert decoded is not None
    return decoded[1]


class AifaAcPacketTests(unittest.TestCase):
    """Coverage for the shared AIFA AC packet profile."""

    def test_support_detection_accepts_configured_and_unknown_ac_device_codes(self) -> None:
        """The shared packet family should accept known and optimistic unknown device-codes."""
        self.assertTrue(is_supported_ac_device_code("136"))
        self.assertTrue(is_supported_ac_device_code("999"))
        self.assertFalse(is_supported_ac_device_code(None))
        # Empty ac_available_modes only means "no anti-mold"; the AC is still controllable.
        self.assertTrue(is_supported_ac_device_code("115"))

    def test_ac_sub_device_detection_checks_type_and_device_code(self) -> None:
        """Climate entities should only attach to AC-like sub-devices."""
        self.assertTrue(is_supported_ac_sub_device("0", "136"))
        self.assertTrue(is_supported_ac_sub_device("0", "999"))
        self.assertFalse(is_supported_ac_sub_device("1", "136"))
        self.assertFalse(is_supported_ac_sub_device("0", None))
        self.assertTrue(is_supported_ac_sub_device("0", "115"))

    def test_empty_anti_mold_list_still_offers_every_mode(self) -> None:
        """Code 115 (classic temp control) has no anti-mold modes but all five AC modes."""
        capabilities = get_ac_capabilities("115")
        self.assertEqual(capabilities.available_modes, ("auto", "cool", "heat", "fan", "dry"))
        self.assertEqual(capabilities.anti_mold_modes, ())
        self.assertTrue(capabilities.classic_temp_control)

    def test_remote_config_capabilities_for_a_known_ac_code(self) -> None:
        """A known AC catalog entry should expose the full mode/fan/swing set.

        Uses '136' as a representative AC code in the catalog. Capability flags
        (extra_windspeed, supports_turbo, etc.) are catalog-driven plus
        a small set of universal flags — they do NOT vary by device code
        in a 136-specific way.
        """
        capabilities = get_ac_capabilities("136")
        self.assertEqual(
            capabilities.available_modes,
            ("auto", "cool", "heat", "fan", "dry"),
        )
        self.assertEqual(
            get_supported_fan_modes("136"),
            ("auto", "low", "medium", "high"),
        )
        self.assertEqual(
            get_supported_swing_modes("136"),
            ("off", "auto", "fixed_1", "fixed_2", "fixed_3", "fixed_4", "fixed_5"),
        )
        self.assertFalse(capabilities.extra_windspeed)
        self.assertTrue(capabilities.supports_turbo)
        self.assertTrue(capabilities.supports_sleep)
        self.assertTrue(capabilities.supports_power_saving)

    def test_temperature_encoding_uses_bcd(self) -> None:
        """AIFA temperatures are encoded as decimal BCD bytes."""
        self.assertEqual(encode_temperature_bcd(19), 0x19)
        self.assertEqual(encode_temperature_bcd(24), 0x24)
        self.assertEqual(encode_temperature_bcd(30), 0x30)

    def test_timer_ir_encoding_uses_bcd(self) -> None:
        """AIFA timer IR values use the same decimal BCD byte family."""
        self.assertEqual(clamp_timer_ir_value(None), 0)
        self.assertEqual(clamp_timer_ir_value(123), 99)
        self.assertEqual(encode_timer_ir_bcd(0), 0x00)
        self.assertEqual(encode_timer_ir_bcd(1), 0x01)
        self.assertEqual(encode_timer_ir_bcd(12), 0x12)

    def test_temperature_clamping_respects_supported_range(self) -> None:
        """Out-of-range target temperatures should clamp safely."""
        self.assertEqual(clamp_target_temperature(None), AIFA_AC_DEFAULT_TARGET_TEMP)
        # The app's setpoint slider runs 16..30.
        self.assertEqual(clamp_target_temperature(1), 16)
        self.assertEqual(clamp_target_temperature(99), 30)

    def test_classic_query_packet_matches_reverse_engineered_bytes(self) -> None:
        """The only query the integration sends is the app's DevicePacketAcQuery."""
        self.assertEqual(CLASSIC_AC_QUERY_PACKET, "fff3f2f0")

    def test_decode_observed_status_packet_recovers_off_state(self) -> None:
        """Observed controller packets should decode into an off state."""
        decoded = decode_observed_status_packet("fea00136000000000000000000f0")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        sub_type, status = decoded
        self.assertEqual(sub_type, 0)
        self.assertEqual(status.device_code, "136")
        self.assertEqual(status.mode, "off")
        self.assertIsNone(status.target_temperature)
        self.assertIsNone(status.fan_mode)
        self.assertIsNone(status.swing_mode)

    def test_decode_observed_status_packet_recovers_on_state(self) -> None:
        """Observed controller packets should decode the live semantic fields."""
        decoded = decode_observed_status_packet("fea00136260024110000000000f0")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        sub_type, status = decoded
        self.assertEqual(sub_type, 0)
        self.assertEqual(status.device_code, "136")
        self.assertEqual(status.mode, "cool")
        self.assertEqual(status.target_temperature, 26)
        self.assertEqual(status.fan_mode, "auto")
        self.assertEqual(status.swing_mode, "off")
        self.assertFalse(status.turbo)
        self.assertFalse(status.sleep)
        self.assertFalse(status.power_saving)

    def test_decode_observed_status_packet_recovers_other_modes(self) -> None:
        """Observed controller packets should map the app's private mode byte layout."""
        self.assertEqual(
            decode_observed_status_packet("fea00136000010110000000000f0")[1].mode,  # type: ignore[index]
            "auto",
        )
        self.assertEqual(
            decode_observed_status_packet("fea00136240040110000000000f0")[1].mode,  # type: ignore[index]
            "dry",
        )
        self.assertEqual(
            decode_observed_status_packet("fea0013624000c110000000000f0")[1].mode,  # type: ignore[index]
            "heat",
        )
        auto_status = decode_observed_status_packet("fea00136000010110000000000f0")
        self.assertIsNotNone(auto_status)
        assert auto_status is not None
        self.assertEqual(auto_status[1].mode, "auto")
        self.assertIsNone(auto_status[1].target_temperature)

        fan_status = decode_observed_status_packet("fea00136000080110000000000f0")
        self.assertIsNotNone(fan_status)
        assert fan_status is not None
        self.assertEqual(fan_status[1].mode, "fan_only")
        self.assertIsNone(fan_status[1].target_temperature)

        dry_status = decode_observed_status_packet("fea00136000040110000000000f0")
        self.assertIsNotNone(dry_status)
        assert dry_status is not None
        self.assertEqual(dry_status[1].mode, "dry")
        self.assertIsNone(dry_status[1].target_temperature)

    def test_decode_observed_status_packet_drops_zero_setpoint_in_cool(self) -> None:
        """A 0x00 setpoint byte means "no setpoint", never 0 °C."""
        decoded = decode_observed_status_packet("fea00136000020110000000000f0")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded[1].mode, "cool")
        self.assertIsNone(decoded[1].target_temperature)

    def test_decode_observed_status_packet_sleep_mask_is_0x08(self) -> None:
        """Observed-packet decoder must use sleep mask 0x08, not stale 0x10.

        Hub broadcasts mirror the command encoder's bit layout: sleep is the
        0x08 bit. Pre-fix, sleep state from the hub was silently dropped.
        """
        from custom_components.aifa_smart.ac import decode_observed_status_packet

        # 14-byte observed packet: cool/26°C/fan-auto/swing-off, feature byte=0x08 (sleep on)
        # Byte layout: fe a0 01 36 26 00 24 01 00 08 00 00 00 f0
        pkt = bytes.fromhex("fea00136260024010008000000f0")
        result = decode_observed_status_packet(pkt)

        self.assertIsNotNone(result)
        assert result is not None
        _, status = result
        self.assertTrue(status.sleep, "sleep should be True when feature byte has 0x08 set")
        self.assertFalse(status.power_saving)
        self.assertFalse(status.turbo)

    def test_decode_observed_status_packet_power_saving_mask_is_0x01(self) -> None:
        """i-Ctrl Pro's fromBytes reads power saving from bit 0 (0x654450)."""
        pkt = bytes.fromhex("fea00136260024010001000000f0")
        _, status = decode_observed_status_packet(pkt)  # type: ignore[misc]
        self.assertFalse(status.sleep)
        self.assertTrue(status.power_saving)
        self.assertFalse(status.turbo)
        # 0x02 is not a flag the app reads.
        _, status = decode_observed_status_packet(  # type: ignore[misc]
            bytes.fromhex("fea00136260024010002000000f0")
        )
        self.assertFalse(status.power_saving)

    def test_decode_observed_status_packet_turbo_mask_unchanged(self) -> None:
        """Turbo at 0x04 was already correct — regression guard."""
        from custom_components.aifa_smart.ac import decode_observed_status_packet

        # 14-byte observed packet: feature byte=0x04 (turbo on)
        # Byte layout: fe a0 01 36 26 00 24 01 00 04 00 00 00 f0
        pkt = bytes.fromhex("fea00136260024010004000000f0")
        result = decode_observed_status_packet(pkt)
        self.assertIsNotNone(result)
        assert result is not None
        _, status = result
        self.assertFalse(status.sleep)
        self.assertFalse(status.power_saving)
        self.assertTrue(status.turbo)

    def test_build_mode_speed_and_swing_samples(self) -> None:
        """Every field of the 15-byte packet is copied from the status given."""
        self.assertEqual(
            _build("dry", 24, key_press=8, fan="high", swing="fixed_5"),
            "ffa0080136240040802000000000f0",
        )
        self.assertEqual(
            _build("cool", 24, key_press=6, fan="low", swing="fixed_5"),
            "ffa0060136240020202000000000f0",
        )
        self.assertEqual(
            _build("cool", 24, key_press=6, fan="high", swing="off"),
            "ffa0060136240020800000000000f0",
        )
        self.assertEqual(
            _build("cool", 26, key_press=6, fan="high", timer=1),
            "ffa0060136260120800100000000f0",
        )

    def test_build_helper_flags_are_exclusive_like_the_app(self) -> None:
        """toBytes writes one flag: power saving 0x01, else turbo 0x04, else sleep 0x08."""
        self.assertEqual(
            _build("cool", 24, key_press=6, fan="high", swing="fixed_5", sleep=True),
            "ffa0060136240020802008000000f0",
        )
        self.assertEqual(
            _build("cool", 24, key_press=6, fan="high", swing="fixed_5", power_saving=True),
            "ffa0060136240020802001000000f0",
        )
        self.assertEqual(
            _build("cool", 24, key_press=6, fan="high", swing="fixed_5", turbo=True),
            "ffa0060136240020802004000000f0",
        )
        self.assertEqual(
            _build(
                "cool", 24, key_press=6, fan="high", swing="fixed_5",
                power_saving=True, turbo=True, sleep=True,
            )[20:22],
            "01",
        )

    def test_build_repeats_a_missing_setpoint_and_uses_the_slot(self) -> None:
        """A report without a setpoint (b4 = 0) goes back out as 0x00."""
        self.assertEqual(
            _build("cool", None, key_press=8, code="115", sub_type=1),
            "ffa1080115000020010100000000f0",
        )

    def test_build_refuses_an_off_status(self) -> None:
        with self.assertRaises(ValueError):
            _build("off", None, key_press=8)


class AifaAcEncoderRegressionTests(unittest.TestCase):
    """Packets captured from the AIFA app (1.2.5) against the toBytes layout.

    Byte 2 is the app's keyPress, so each case passes the captured value.
    Device code 136 is only the code those captures used.
    """

    def test_power_on_off_short_packets(self) -> None:
        """Short on / off packets are 4-byte literals."""
        self.assertEqual(build_power_on_command(), "ffa0a1f0")
        self.assertEqual(build_power_off_command(), "ffa0a2f0")

    def test_cool_24_app_default_byte_match(self) -> None:
        """App packet: cool 24 default →
        ffa0060136240020010100000000f0.

        Reached by encoder when caller passes fan=auto, swing=auto.
        """
        encoded = _build("cool", 24, key_press=0x06)
        self.assertEqual(encoded, "ffa0060136240020010100000000f0")

        report = _as_report(encoded)
        self.assertEqual((report.mode, report.target_temperature), ("cool", 24))

    def test_cool_19_cold_side_prefix(self) -> None:
        """App packet: cool 19 → byte 2 = 0x07 (cool<24 prefix)."""
        encoded = _build("cool", 19, key_press=0x07)
        self.assertEqual(encoded, "ffa0070136190020010100000000f0")

    def test_cool_20_cold_side_prefix(self) -> None:
        """App packet: cool 20 → byte 2 = 0x07."""
        encoded = _build("cool", 20, key_press=0x07)
        self.assertEqual(encoded, "ffa0070136200020010100000000f0")

    def test_heat_24_app_default_byte_match(self) -> None:
        """App packet: heat 24 default → byte 2 = 0x08."""
        encoded = _build("heat", 24, key_press=0x08)
        self.assertEqual(encoded, "ffa0080136240008010100000000f0")

        report = _as_report(encoded)
        self.assertEqual((report.mode, report.target_temperature), ("heat", 24))

    def test_fan_only_24_app_default_byte_match(self) -> None:
        """App packet: fan_only 24 with byte 2 = 0x08."""
        encoded = _build("fan_only", 24, key_press=0x08)
        self.assertEqual(encoded, "ffa0080136240080010100000000f0")

        report = _as_report(encoded)
        self.assertEqual((report.mode, report.target_temperature), ("fan_only", 24))

    def test_cool_28_high_fan_swing_4_byte_match(self) -> None:
        """App packet: cool 28, high fan, 4th swing position.

        byte 7 = 0x20 = cool, byte 8 = 0x80 = high, byte 9 = 0x40 = 4th cycle
        position. Function names are user labels and do not reflect byte semantics.
        """
        encoded = _build("cool", 28, key_press=0x06, fan="high", swing="fixed_4")
        self.assertEqual(encoded, "ffa0060136280020804000000000f0")

    def test_dry_24_app_default_byte_match(self) -> None:
        """App packet: Dry mode tap → byte 2=0x08, byte 7=0x40."""
        encoded = _build("dry", 24, key_press=0x08)
        self.assertEqual(encoded, "ffa0080136240040010100000000f0")

    def test_auto_24_app_default_byte_match(self) -> None:
        """App packet: Auto mode → byte 2=0x08, byte 7=0x10."""
        encoded = _build("auto", 24, key_press=0x08)
        self.assertEqual(encoded, "ffa0080136240010010100000000f0")

    def test_cool_24_sleep_on_byte_match(self) -> None:
        """App packet: Sleep button tapped → byte 10 = 0x08.

        Sleep also forces fan to auto (AVD AIFA app silently drops speed:0
        when Sleep is on).
        """
        encoded = _build("cool", 24, key_press=0x06, sleep=True)
        self.assertEqual(encoded, "ffa0060136240020010108000000f0")

    def test_fan_only_24_medium_byte_match(self) -> None:
        """App packet: fan_only + speed:2 medium → byte 8=0x40."""
        encoded = _build("fan_only", 24, key_press=0x08, fan="medium")
        self.assertEqual(encoded, "ffa0080136240080400100000000f0")

    def test_swing_cycle_byte_matches(self) -> None:
        """App packet: Direction tap cycle.

        Tapping AVD AIFA app's Direction button cycles through:
          default 0x01 → 0x04 → 0x08 → 0x80 → 0x40 → 0x20
        Integration mapping fixed_1..fixed_5 follows this cycle order.
        """
        cases = {
            "auto":    "ffa0060136240020010100000000f0",  # default 0x01
            "fixed_1": "ffa0060136240020010400000000f0",  # 0x04
            "fixed_2": "ffa0060136240020010800000000f0",  # 0x08
            "fixed_3": "ffa0060136240020018000000000f0",  # 0x80
            "fixed_4": "ffa0060136240020014000000000f0",  # 0x40
            "fixed_5": "ffa0060136240020012000000000f0",  # 0x20
        }
        for swing, expected in cases.items():
            with self.subTest(swing=swing):
                encoded = _build("cool", 24, key_press=0x06, swing=swing)
                self.assertEqual(encoded, expected)


class AifaAcDeviceCodeBcdTests(unittest.TestCase):
    """The Dart-side encoder always BCD-encodes device_code into bytes 3-4.

    Static evidence: AirConditionerStatusOn.toBytes calls deviceCodeToBytes
    which does .toString().padLeft(4, "0") then int.parse(substring(0,2),
    radix:16) and int.parse(substring(2,4), radix:16) — i.e., interpret each
    pair of decimal digits as a hex byte. So device 136 → "0136" → bytes
    [0x01, 0x36].
    """

    def test_device_136_encodes_as_01_36(self) -> None:
        from custom_components.aifa_smart.ac import _bcd_encode_device_code

        self.assertEqual(_bcd_encode_device_code(136), (0x01, 0x36))

    def test_cloud_code_19844_encodes_per_dart_truncation(self) -> None:
        """5-digit cloud codes (remote=true catalog rows 19844..19999): Dart
        does substring(2, 4) which silently drops the 5th char. Resulting
        bytes alias across siblings — the hub disambiguates using its
        set-code-loaded value, treating bytes 3-4 as a family hint."""
        from custom_components.aifa_smart.ac import _bcd_encode_device_code

        self.assertEqual(_bcd_encode_device_code(19844), (0x19, 0x84))
        self.assertEqual(_bcd_encode_device_code(19999), (0x19, 0x99))

    def test_cloud_code_5th_digit_dropped(self) -> None:
        """19844..19848 all collapse to (0x19, 0x84) because Dart's
        substring(2, 4) discards index 4. This aliasing is by design on
        the AIFA hub side."""
        from custom_components.aifa_smart.ac import _bcd_encode_device_code

        for code in (19844, 19845, 19846, 19847, 19848):
            self.assertEqual(_bcd_encode_device_code(code), (0x19, 0x84))

    def test_device_above_99999_raises(self) -> None:
        """6+ digit codes don't exist in any AIFA catalog (max real is 19999)
        and would force a 3-byte family hint, breaking the 15-byte layout."""
        from custom_components.aifa_smart.ac import _bcd_encode_device_code

        with self.assertRaises(ValueError):
            _bcd_encode_device_code(100000)

    def test_negative_device_code_raises(self) -> None:
        from custom_components.aifa_smart.ac import _bcd_encode_device_code

        with self.assertRaises(ValueError):
            _bcd_encode_device_code(-1)

    def test_build_status_command_raises_on_none_device_code(self) -> None:
        """A None device_code is a hard error instead of a guessed family."""
        with self.assertRaisesRegex(ValueError, "device_code is required"):
            _build("cool", 24, key_press=8, code=None)  # type: ignore[arg-type]

    def test_build_status_command_raises_on_unparseable_device_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be BCD-encoded"):
            _build("cool", 24, key_press=8, code="not-a-number")

    def test_build_status_command_uses_cloud_code_19844_bcd(self) -> None:
        """Bytes 3-4 mirror Dart's substring truncation for 5-digit codes."""
        cmd = _build("cool", 24, key_press=8, code="19844")
        self.assertEqual(cmd[6:10], "1984")

    def test_build_status_command_raises_on_overlong_device_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be BCD-encoded"):
            _build("cool", 24, key_press=8, code="100000")

    def test_round_trip_sweep_for_common_device_codes(self) -> None:
        """Build a cool-24 packet and read it back the way fromCommandBytes does."""
        from custom_components.aifa_smart.ac import _bcd_encode_device_code

        for code in (1, 2, 7, 11, 25, 100, 134, 136, 170, 173, 999):
            with self.subTest(device_code=code):
                cmd = _build("cool", 24, key_press=6, code=str(code), fan="medium", swing="fixed_5")
                bcd_high, bcd_low = _bcd_encode_device_code(code)
                self.assertEqual(cmd[6:8], f"{bcd_high:02x}")
                self.assertEqual(cmd[8:10], f"{bcd_low:02x}")
                report = _as_report(cmd)
                self.assertEqual(report.device_code, str(code))
                self.assertEqual(
                    (report.mode, report.target_temperature, report.fan_mode, report.swing_mode),
                    ("cool", 24, "medium", "fixed_5"),
                )


class ClassicTemperatureKeyCommandTests(unittest.TestCase):
    def test_up_and_down_keys_for_sub_type_0(self) -> None:
        self.assertEqual(build_classic_temperature_key_command(0, up=True), "ffa006f0")
        self.assertEqual(build_classic_temperature_key_command(0, up=False), "ffa007f0")

    def test_sub_type_1_uses_a1(self) -> None:
        self.assertEqual(build_classic_temperature_key_command(1, up=True), "ffa106f0")


class ModeKeyCommandTests(unittest.TestCase):
    def test_mode_keys_for_sub_type_0(self) -> None:
        expected = {
            "auto": "ffa015f0",
            "cool": "ffa016f0",
            "heat": "ffa017f0",
            "dry": "ffa018f0",
            "fan_only": "ffa019f0",
        }
        for mode, command in expected.items():
            with self.subTest(mode=mode):
                self.assertEqual(build_mode_key_command(mode, 0), command)

    def test_fan_alias_and_case_normalize(self) -> None:
        self.assertEqual(build_mode_key_command("fan", 0), "ffa019f0")
        self.assertEqual(build_mode_key_command("Cool", 0), "ffa016f0")

    def test_sub_type_1_uses_a1(self) -> None:
        self.assertEqual(build_mode_key_command("cool", 1), "ffa116f0")
        self.assertEqual(build_mode_key_command("heat", 1), "ffa117f0")

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_mode_key_command("off", 0)

    def test_hub_requires_mode_keys_only_for_exact_version_pair(self) -> None:
        self.assertTrue(hub_requires_mode_keys(5, 210623))
        self.assertFalse(hub_requires_mode_keys(5, 210624))
        self.assertFalse(hub_requires_mode_keys(4, 210623))
        self.assertFalse(hub_requires_mode_keys(5, None))
        self.assertFalse(hub_requires_mode_keys(None, None))


if __name__ == "__main__":
    unittest.main()
