"""Tests for the AIFA AC packet helper."""
from __future__ import annotations

import unittest

from custom_components.aifa_smart.ac import (
    AIFA_AC_ESTIMATED_CAPABILITIES,
    AIFA_AC_DEFAULT_TARGET_TEMP,
    SHARED_AIFA_AC_PROFILE,
    build_ac_query_packet,
    build_ac_query_single_type_packet,
    build_estimated_capabilities,
    build_power_off_command,
    build_power_on_command,
    build_query_anti_mold_packet,
    build_status_command,
    clamp_target_temperature,
    clamp_timer_ir_value,
    decode_observed_status_packet,
    decode_status_command,
    encode_timer_ir_bcd,
    encode_temperature_bcd,
    get_ac_capabilities,
    get_ac_profile,
    get_supported_fan_modes,
    get_supported_swing_modes,
    is_supported_ac_device_code,
    is_supported_ac_sub_device,
    normalize_ac_mode,
)


class AifaAcPacketTests(unittest.TestCase):
    """Coverage for the shared AIFA AC packet profile."""

    def test_support_detection_accepts_configured_and_unknown_ac_device_codes(self) -> None:
        """The shared packet family should accept known and optimistic unknown device-codes."""
        self.assertTrue(is_supported_ac_device_code("136"))
        self.assertTrue(is_supported_ac_device_code("999"))
        self.assertFalse(is_supported_ac_device_code(None))
        self.assertFalse(is_supported_ac_device_code("16"))

    def test_ac_sub_device_detection_checks_type_and_device_code(self) -> None:
        """Climate entities should only attach to AC-like sub-devices."""
        self.assertTrue(is_supported_ac_sub_device("0", "136"))
        self.assertTrue(is_supported_ac_sub_device("0", "999"))
        self.assertFalse(is_supported_ac_sub_device("1", "136"))
        self.assertFalse(is_supported_ac_sub_device("0", None))
        self.assertFalse(is_supported_ac_sub_device("0", "16"))

    def test_remote_config_capabilities_for_a_known_ac_code(self) -> None:
        """A known AC catalog entry should expose the full mode/fan/swing set.

        Uses '136' as a representative AC code in the catalog. Per the
        2026-04-28 BCD-device-aware encoder refactor, capability flags
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
        self.assertEqual(clamp_target_temperature(1), 17)
        self.assertEqual(clamp_target_temperature(99), 30)

    def test_classic_query_packets_match_reverse_engineered_bytes(self) -> None:
        """The private classic socket queries should keep the known byte layout."""
        self.assertEqual(build_ac_query_packet(), "fff3f2f0")
        self.assertEqual(build_ac_query_single_type_packet(0), "fff3f2a0f0")
        self.assertEqual(build_ac_query_single_type_packet(1), "fff3f2a1f0")
        self.assertEqual(build_query_anti_mold_packet(0), "a1faaca0f0")
        self.assertEqual(build_query_anti_mold_packet(1), "a1faaca1f0")

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

    def test_decode_observed_status_packet_power_saving_mask_is_0x02(self) -> None:
        """Observed-packet decoder must use power_saving mask 0x02, not stale 0x01."""
        from custom_components.aifa_smart.ac import decode_observed_status_packet

        # 14-byte observed packet: feature byte=0x02 (power_saving on)
        # Byte layout: fe a0 01 36 26 00 24 01 00 02 00 00 00 f0
        pkt = bytes.fromhex("fea00136260024010002000000f0")
        result = decode_observed_status_packet(pkt)
        self.assertIsNotNone(result)
        assert result is not None
        _, status = result
        self.assertFalse(status.sleep)
        self.assertTrue(status.power_saving)
        self.assertFalse(status.turbo)

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

    def test_build_inferred_mode_speed_and_swing_samples(self) -> None:
        """Inferred mode / fan / swing values produce expected hex per current map."""
        self.assertEqual(
            build_status_command("dry", 24, device_code="136"),
            "ffa0080136240040802000000000f0",
        )
        self.assertEqual(
            build_status_command("fan", 24, device_code="136"),
            "ffa0080136240080800100000000f0",
        )
        self.assertEqual(
            build_status_command("auto", 24, device_code="136"),
            "ffa0080136240010802000000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", fan_mode="medium"),
            "ffa0060136240020402000000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", fan_mode="low"),
            "ffa0060136240020202000000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", fan_mode="auto"),
            "ffa0060136240020012000000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", swing_mode="off"),
            "ffa0060136240020800000000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", swing_mode="fixed_1"),
            "ffa0060136240020800400000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", swing_mode="auto"),
            "ffa0060136240020800100000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 26, device_code="136", timer_ir_value=1, swing_mode="auto"),
            "ffa0060136260120800100000000f0",
        )

    def test_build_helper_flag_samples(self) -> None:
        """Helper flags packed into byte 10.

        sleep=0x08 verified 2026-04-27 from AVD AIFA app capture.
        power_saving=0x02 and turbo=0x04 are still inferred (no AVD path
        could trigger them) but locked here to preserve integration behavior.
        """
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", sleep=True),
            "ffa0060136240020802008000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", power_saving=True),
            "ffa0060136240020802002000000f0",
        )
        self.assertEqual(
            build_status_command("cool", 24, device_code="136", turbo=True),
            "ffa0060136240020802004000000f0",
        )

    def test_decode_status_helper_flags_are_exclusive(self) -> None:
        """Helper-byte round-trip: sleep=0x08 verified, PS=0x02 + turbo=0x04 inferred."""
        decoded = decode_status_command("ffa0060136240020012002000000f0", device_code="136")

        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertTrue(decoded.power_saving)
        self.assertFalse(decoded.turbo)
        self.assertFalse(decoded.sleep)

        turbo = decode_status_command("ffa0060136240020012004000000f0", device_code="136")
        self.assertIsNotNone(turbo)
        assert turbo is not None
        self.assertTrue(turbo.turbo)
        self.assertFalse(turbo.power_saving)

        sleep = decode_status_command("ffa0060136240020012008000000f0", device_code="136")
        self.assertIsNotNone(sleep)
        assert sleep is not None
        self.assertTrue(sleep.sleep)
        self.assertFalse(sleep.power_saving)

    def test_decode_status_rejects_unsupported_packets(self) -> None:
        """Short power packets should not decode as full status packets."""
        self.assertIsNone(decode_status_command("ffa0a1f0"))
        self.assertIsNone(decode_status_command("not-hex"))


class AifaAcEncoderRegressionTests(unittest.TestCase):
    """Encoder regression tests using device_code='136' as a fixed test vector.

    The bit positions exercised here are universal across device codes per
    Dart-side AirConditionerStatusOn.toBytes reverse engineering; the choice
    of '136' is purely historical (early captures used that code) and does
    not imply 136 is special.
    """

    def test_power_on_off_short_packets(self) -> None:
        """Short on / off packets are 4-byte literals."""
        self.assertEqual(build_power_on_command(), "ffa0a1f0")
        self.assertEqual(build_power_off_command(), "ffa0a2f0")

    def test_cool_24_app_default_byte_match(self) -> None:
        """Verified: function 'by by' [step 2] cool 24 default →
        ffa0060136240020010100000000f0.

        Reached by encoder when caller passes fan=auto, swing=auto.
        """
        encoded = build_status_command(
            "cool", 24, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0060136240020010100000000f0")

        decoded = decode_status_command(encoded, device_code="136")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.mode, "cool")
        self.assertEqual(decoded.target_temperature, 24)

    def test_cool_19_cold_side_prefix(self) -> None:
        """Verified: 'by by' [step 3] cool 19 → byte 2 = 0x07 (cool<24 prefix)."""
        encoded = build_status_command(
            "cool", 19, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0070136190020010100000000f0")

    def test_cool_20_cold_side_prefix(self) -> None:
        """Verified: 'by by' [step 4] cool 20 → byte 2 = 0x07."""
        encoded = build_status_command(
            "cool", 20, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0070136200020010100000000f0")

    def test_heat_24_app_default_byte_match(self) -> None:
        """Verified: 'by by' [step 5] heat 24 default → byte 2 = 0x08."""
        encoded = build_status_command(
            "heat", 24, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0080136240008010100000000f0")

        decoded = decode_status_command(encoded, device_code="136")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.mode, "heat")
        self.assertEqual(decoded.target_temperature, 24)

    def test_fan_only_24_app_default_byte_match(self) -> None:
        """Verified: function 'fanonlysample' → fan_only 24 with byte 2 = 0x08."""
        encoded = build_status_command(
            "fan", 24, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0080136240080010100000000f0")

        decoded = decode_status_command(encoded, device_code="136")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.mode, "fan_only")
        self.assertEqual(decoded.target_temperature, 24)

    def test_cool_28_high_fan_swing_4_byte_match(self) -> None:
        """Verified: function '自動29' bytes (despite the user-given name).

        byte 7 = 0x20 = cool, byte 8 = 0x80 = high, byte 9 = 0x40 = 4th cycle
        position. The user named this 自動 but the bytes are NOT auto fan.
        Function names are user labels and do not reflect byte semantics.
        """
        encoded = build_status_command(
            "cool", 28, device_code="136", fan_mode="high", swing_mode="fixed_4"
        )
        self.assertEqual(encoded, "ffa0060136280020804000000000f0")

    def test_dry_24_app_default_byte_match(self) -> None:
        """Verified: probe1 cmd 644474 — Dry mode tap → byte 2=0x08, byte 7=0x40."""
        encoded = build_status_command(
            "dry", 24, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0080136240040010100000000f0")

    def test_auto_24_app_default_byte_match(self) -> None:
        """Verified: probe1 cmd 644455 RESPONSE — Auto mode → byte 2=0x08, byte 7=0x10."""
        encoded = build_status_command(
            "auto", 24, device_code="136", fan_mode="auto", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0080136240010010100000000f0")

    def test_cool_24_sleep_on_byte_match(self) -> None:
        """Verified: probe1 cmd 644452 — Sleep button tapped → byte 10 = 0x08.

        Sleep also forces fan to auto (AVD AIFA app silently drops speed:0
        when Sleep is on). build_status_command callers are expected to pass
        fan_mode="auto" when sleep=True.
        """
        encoded = build_status_command(
            "cool", 24, device_code="136", fan_mode="auto", swing_mode="auto", sleep=True
        )
        self.assertEqual(encoded, "ffa0060136240020010108000000f0")

    def test_fan_only_24_medium_byte_match(self) -> None:
        """Verified: probe1 cmd 644454 RESPONSE — fan_only + speed:2 medium → byte 8=0x40."""
        encoded = build_status_command(
            "fan", 24, device_code="136", fan_mode="medium", swing_mode="auto"
        )
        self.assertEqual(encoded, "ffa0080136240080400100000000f0")

    def test_swing_cycle_byte_matches(self) -> None:
        """Verified: probe1 cmds 644485-644489 — Direction tap cycle.

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
                encoded = build_status_command(
                    "cool", 24, device_code="136", fan_mode="auto", swing_mode=swing
                )
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
        """A None device_code is now a hard error instead of silently
        defaulting to device 136."""
        from custom_components.aifa_smart.ac import build_status_command

        with self.assertRaisesRegex(ValueError, "device_code is required"):
            build_status_command("cool", 24, device_code=None)

    def test_build_status_command_raises_on_unparseable_device_code(self) -> None:
        from custom_components.aifa_smart.ac import build_status_command

        with self.assertRaisesRegex(ValueError, "cannot be BCD-encoded"):
            build_status_command("cool", 24, device_code="not-a-number")

    def test_build_status_command_uses_cloud_code_19844_bcd(self) -> None:
        """5-digit cloud code support: bytes 3-4 mirror Dart's substring
        truncation. The hub disambiguates by set-code-loaded value, so this
        only produces correct AC behavior when PATCH+set-code has aligned
        the hub with the cloud's deviceCode first."""
        from custom_components.aifa_smart.ac import build_status_command

        cmd = build_status_command("cool", 24, device_code="19844")
        self.assertEqual(cmd[6:10], "1984")

    def test_build_status_command_raises_on_overlong_device_code(self) -> None:
        from custom_components.aifa_smart.ac import build_status_command

        with self.assertRaisesRegex(ValueError, "cannot be BCD-encoded"):
            build_status_command("cool", 24, device_code="100000")

    def test_decode_status_command_accepts_device_11_bytes(self) -> None:
        """Round-trip: a device-11 packet from build_status_command must
        decode cleanly via decode_status_command."""
        from custom_components.aifa_smart.ac import (
            build_status_command,
            decode_status_command,
        )

        cmd = build_status_command(
            "cool", 24, device_code="11", fan_mode="auto", swing_mode="auto"
        )
        decoded = decode_status_command(cmd, device_code="11")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.mode, "cool")
        self.assertEqual(decoded.target_temperature, 24)
        self.assertEqual(decoded.fan_mode, "auto")
        self.assertEqual(decoded.swing_mode, "auto")

    def test_decode_status_command_accepts_device_25_bytes(self) -> None:
        from custom_components.aifa_smart.ac import (
            build_status_command,
            decode_status_command,
        )

        cmd = build_status_command(
            "cool", 24, device_code="25", fan_mode="medium", swing_mode="fixed_5"
        )
        decoded = decode_status_command(cmd, device_code="25")
        self.assertIsNotNone(decoded)
        assert decoded is not None
        self.assertEqual(decoded.fan_mode, "medium")
        self.assertEqual(decoded.swing_mode, "fixed_5")

    def test_decode_status_command_rejects_non_bcd_byte_4(self) -> None:
        """Bytes 3-4 must look like BCD (each nibble 0..9) to be a valid
        device-code-bearing packet. Garbage byte 4 like 0xff means it's not
        an AC status packet at all."""
        from custom_components.aifa_smart.ac import decode_status_command

        # Same shape as a real cool 24 packet but byte 4 = 0xff
        bogus = "ffa00601ff240020010100000000f0"
        self.assertIsNone(decode_status_command(bogus))

    def test_round_trip_sweep_for_common_device_codes(self) -> None:
        """For each common device code the user might encounter, build a
        canonical cool-24 packet and decode it back. mode/temp/fan/swing
        must round-trip; bytes 3-4 must match the BCD of the device code."""
        from custom_components.aifa_smart.ac import (
            _bcd_encode_device_code,
            build_status_command,
            decode_status_command,
        )

        # Codes drawn from the AIFA cloud catalog: popular brands + known
        # outliers from the cross-device matrix experiments 2026-04-28.
        for code in (1, 2, 7, 11, 25, 100, 134, 136, 170, 173, 999):
            with self.subTest(device_code=code):
                cmd = build_status_command(
                    "cool", 24, device_code=str(code),
                    fan_mode="auto", swing_mode="auto",
                )
                bcd_high, bcd_low = _bcd_encode_device_code(code)
                self.assertEqual(cmd[6:8], f"{bcd_high:02x}")
                self.assertEqual(cmd[8:10], f"{bcd_low:02x}")

                decoded = decode_status_command(cmd, device_code=str(code))
                self.assertIsNotNone(decoded, f"decode failed for code {code}")
                assert decoded is not None
                self.assertEqual(decoded.mode, "cool")
                self.assertEqual(decoded.target_temperature, 24)
                self.assertEqual(decoded.fan_mode, "auto")
                self.assertEqual(decoded.swing_mode, "auto")


if __name__ == "__main__":
    unittest.main()
