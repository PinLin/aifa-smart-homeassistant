"""Tests for the AIFA LAN transport helpers and the shared stream parser."""
from __future__ import annotations

import asyncio
import unittest

from custom_components.aifa_smart.classic_listener import (
    AifaClassicSocketListenerSpec,
    ClassicSocketManager,
    ClassicStreamBuffers,
    process_classic_stream_chunk,
)
from custom_components.aifa_smart.lan import (
    build_connect_packet,
    build_search_packet,
    compute_broadcast_addresses,
    normalize_mac,
    parse_search_reply,
    wire_mac,
)

_STATUS_COOL_26 = bytes.fromhex("fea00136260024110000000000f0")


class LanPacketTests(unittest.TestCase):
    """Packet layout matches what the hub accepted in the field."""

    def test_search_packet_layout(self) -> None:
        packet = build_search_packet()
        self.assertEqual(len(packet), 54)
        self.assertEqual(packet[:3], b"\x54\x01\x00")
        self.assertEqual(packet[16], 0xC0)
        self.assertEqual(packet[3:16] + packet[17:], bytes(13 + 37))

    def test_connect_packet_layout(self) -> None:
        packet = build_connect_packet("A0b1C2d3E4f5")
        self.assertEqual(len(packet), 54)
        self.assertEqual(packet[:3], b"\x54\x01\x00")
        self.assertEqual(packet[3:15], b"A0b1C2d3E4f5")
        self.assertEqual(packet[15], 0)
        self.assertEqual(packet[16], 0xC2)
        self.assertEqual(packet[17:], bytes(37))

    def test_connect_packet_keeps_case_and_strips_separators(self) -> None:
        packet = build_connect_packet("a0:b1:c2:d3:e4:f5")
        self.assertEqual(packet[3:15], b"a0b1c2d3e4f5")

    def test_connect_packet_rejects_bad_mac(self) -> None:
        for mac in ("", "A0B1C2D3E4", "A0B1C2D3E4F5AA"):
            with self.subTest(mac=mac), self.assertRaises(ValueError):
                build_connect_packet(mac)

    def test_parse_search_reply(self) -> None:
        reply = bytearray(54)
        reply[:3] = b"\x55\x01\xa4"
        reply[3:15] = b"A0B1C2D3E4F5"
        reply[15] = 0x04
        reply[16] = 0xC1
        self.assertEqual(parse_search_reply(bytes(reply)), "A0B1C2D3E4F5")

    def test_parse_search_reply_rejects_other_frames(self) -> None:
        self.assertIsNone(parse_search_reply(build_search_packet()))
        self.assertIsNone(parse_search_reply(b"\x55\x01\xa4" + bytes(10)))

    def test_mac_helpers(self) -> None:
        self.assertEqual(wire_mac(" a0-b1-c2-d3-e4-f5 "), "a0b1c2d3e4f5")
        self.assertEqual(normalize_mac("a0:b1:c2:d3:e4:f5"), "A0B1C2D3E4F5")
        self.assertEqual(normalize_mac(None), "")


class BroadcastAddressTests(unittest.TestCase):
    """Broadcasts go to each IPv4 subnet, never to 255.255.255.255."""

    def test_dual_nic_includes_disabled_adapter(self) -> None:
        adapters = [
            {
                "name": "enp0s3",
                "enabled": True,
                "default": True,
                "ipv4": [{"address": "192.168.30.50", "network_prefix": 24}],
            },
            {
                "name": "enp0s8",
                "enabled": False,
                "default": False,
                "ipv4": [{"address": "192.168.20.20", "network_prefix": 24}],
            },
        ]
        self.assertEqual(
            compute_broadcast_addresses(adapters),
            ["192.168.20.255", "192.168.30.255"],
        )

    def test_excludes_loopback_link_local_and_point_to_point(self) -> None:
        adapters = [
            {"name": "lo", "ipv4": [{"address": "127.0.0.1", "network_prefix": 8}]},
            {"name": "ll", "ipv4": [{"address": "169.254.3.4", "network_prefix": 16}]},
            {"name": "p2p", "ipv4": [{"address": "10.0.0.1", "network_prefix": 31}]},
            {"name": "host", "ipv4": [{"address": "10.0.0.9", "network_prefix": 32}]},
            {"name": "v6only", "ipv4": [], "ipv6": [{"address": "fe80::1"}]},
            {"name": "broken", "ipv4": [{"address": "nope", "network_prefix": 24}]},
            {"name": "lan", "ipv4": [{"address": "10.1.2.3", "network_prefix": 16}]},
        ]
        self.assertEqual(compute_broadcast_addresses(adapters), ["10.1.255.255"])

    def test_deduplicates_and_never_adds_global_broadcast(self) -> None:
        adapters = [
            {"name": "a", "ipv4": [{"address": "192.168.1.10", "network_prefix": 24}]},
            {"name": "b", "ipv4": [{"address": "192.168.1.11", "network_prefix": 24}]},
        ]
        result = compute_broadcast_addresses(adapters)
        self.assertEqual(result, ["192.168.1.255"])
        self.assertNotIn("255.255.255.255", compute_broadcast_addresses([]))


class _Recorder:
    def __init__(self) -> None:
        self.wake_replies = 0
        self.statuses: list[tuple[int, object]] = []
        self.notifies = 0

    async def reply_wake(self) -> None:
        self.wake_replies += 1

    def on_status(self, sub_type: int, observed: object) -> bool:
        self.statuses.append((sub_type, observed))
        return True

    def on_notify(self) -> None:
        self.notifies += 1


def _feed(recorder: _Recorder, buffers: ClassicStreamBuffers, *chunks: bytes) -> list[bool]:
    async def run() -> list[bool]:
        results = []
        for chunk in chunks:
            results.append(
                await process_classic_stream_chunk(
                    chunk,
                    buffers,
                    device_type=None,
                    reply_wake=recorder.reply_wake,
                    on_status=recorder.on_status,
                    on_sensor=lambda _sensor: False,
                    on_notify=recorder.on_notify,
                )
            )
        return results

    return asyncio.run(run())


class SharedStreamParserTests(unittest.TestCase):
    """process_classic_stream_chunk is the parser for both cloud and LAN links."""

    def test_status_split_across_chunks(self) -> None:
        recorder = _Recorder()
        buffers = ClassicStreamBuffers()
        results = _feed(recorder, buffers, _STATUS_COOL_26[:5], _STATUS_COOL_26[5:])
        self.assertEqual(results, [False, True])
        self.assertEqual(len(recorder.statuses), 1)
        sub_type, observed = recorder.statuses[0]
        self.assertEqual(sub_type, 0)
        self.assertEqual(observed.mode, "cool")  # type: ignore[attr-defined]
        self.assertEqual(observed.target_temperature, 26)  # type: ignore[attr-defined]

    def test_wake_frame_split_across_chunks_gets_one_reply(self) -> None:
        recorder = _Recorder()
        _feed(recorder, ClassicStreamBuffers(), b"\xf1", b"\xee\xa0", b"\xf0")
        self.assertEqual(recorder.wake_replies, 1)

    def test_f1aa_frame_is_not_answered(self) -> None:
        recorder = _Recorder()
        results = _feed(recorder, ClassicStreamBuffers(), bytes.fromhex("f1aaa0f0"))
        self.assertEqual(recorder.wake_replies, 0)
        self.assertEqual(results, [False])

    def test_mixed_stream(self) -> None:
        recorder = _Recorder()
        _feed(
            recorder,
            ClassicStreamBuffers(),
            bytes.fromhex("f1aaa0f0f1eea0f0") + _STATUS_COOL_26 + bytes.fromhex("f1ee00f0"),
        )
        self.assertEqual(recorder.wake_replies, 1)
        self.assertEqual(len(recorder.statuses), 1)

    def test_buffers_are_capped(self) -> None:
        buffers = ClassicStreamBuffers()
        _feed(_Recorder(), buffers, b"SENSOR{" + b"x" * 70000)
        self.assertLessEqual(len(buffers.sensor), 65536)


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        return None


class ClassicManagerUsesSharedParserTests(unittest.TestCase):
    """The cloud listener keeps its wake reply and callback wiring."""

    def test_cloud_chunk_replies_and_forwards_spec(self) -> None:
        observed: list[tuple[object, int]] = []
        notified: list[str] = []
        manager = ClassicSocketManager(
            hass=object(),
            client=object(),
            on_observed_status=lambda spec, sub_type, status: observed.append(
                (spec, sub_type)
            )
            or True,
            on_sensor_state=lambda *args: False,
            on_data_changed=lambda: None,
            on_notify_full_refresh=lambda: None,
        )
        manager._schedule_notify_probe = notified.append  # type: ignore[method-assign]  # noqa: SLF001
        spec = AifaClassicSocketListenerSpec(device_id="7001", mac="M", targets=())
        writer = _FakeWriter()
        manager._socket_write_locks["7001"] = asyncio.Lock()  # noqa: SLF001
        buffers = ClassicStreamBuffers()

        async def run() -> bool:
            return await manager._async_process_chunk(  # noqa: SLF001
                spec,
                bytes.fromhex("f1eea0f0") + _STATUS_COOL_26 + bytes.fromhex("faf5"),
                writer,  # type: ignore[arg-type]
                buffers,
            )

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(writer.writes, [bytes.fromhex("ccdd")])
        self.assertEqual(observed, [(spec, 0)])
        self.assertEqual(buffers.notify, bytes.fromhex("faf5"))


if __name__ == "__main__":
    unittest.main()
