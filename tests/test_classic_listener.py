"""Focused tests for classic_listener.ClassicSocketManager.async_send_packet.

These tests exercise only the writer-registration / locking surface added in
2026-04-27 to support TLS-direct command sending. The rest of the listener
(reader loop, reconnect, broadcast parsers) is already covered indirectly
through coordinator tests.
"""
from __future__ import annotations

import asyncio
import unittest

from custom_components.aifa_smart.classic_listener import ClassicSocketManager


class _FakeWriter:
    """Stand-in for asyncio.StreamWriter that records writes + drains."""

    def __init__(self, *, closing: bool = False) -> None:
        self.writes: list[bytes] = []
        self.drain_calls = 0
        self._closing = closing

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        self.drain_calls += 1

    def is_closing(self) -> bool:
        return self._closing


def _build_manager() -> ClassicSocketManager:
    """Construct a manager with no-op callbacks for unit testing."""
    return ClassicSocketManager(
        hass=object(),
        client=object(),
        on_observed_status=lambda *args, **kwargs: False,
        on_sensor_state=lambda *args, **kwargs: False,
        on_data_changed=lambda *args, **kwargs: None,
        on_notify_full_refresh=lambda *args, **kwargs: None,
    )


class ClassicSocketSendPacketTests(unittest.TestCase):
    """Verify async_send_packet's writer-dispatch + lock semantics."""

    def test_send_returns_false_when_no_writer_registered(self) -> None:
        """Without an active TLS connection, send must return False (so the
        caller can fall back to HTTPS) and never throw."""
        manager = _build_manager()

        async def run() -> bool:
            return await manager.async_send_packet("8014", "ffa0a1f0")

        self.assertFalse(asyncio.run(run()))

    def test_send_returns_false_when_writer_is_closing(self) -> None:
        """A writer that's mid-shutdown must not be written to — return False."""
        manager = _build_manager()
        closing_writer = _FakeWriter(closing=True)
        manager._socket_writers["8014"] = closing_writer  # noqa: SLF001
        manager._socket_write_locks["8014"] = asyncio.Lock()  # noqa: SLF001

        async def run() -> bool:
            return await manager.async_send_packet("8014", "ffa0a1f0")

        self.assertFalse(asyncio.run(run()))
        self.assertEqual(closing_writer.writes, [])

    def test_send_writes_bytes_when_writer_registered(self) -> None:
        """With a live registered writer, packets get hex-decoded and drained."""
        manager = _build_manager()
        writer = _FakeWriter()
        manager._socket_writers["8014"] = writer  # noqa: SLF001
        manager._socket_write_locks["8014"] = asyncio.Lock()  # noqa: SLF001

        async def run() -> bool:
            return await manager.async_send_packet("8014", "ffa0a1f0")

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(writer.writes, [bytes.fromhex("ffa0a1f0")])
        self.assertEqual(writer.drain_calls, 1)

    def test_send_normalizes_hex_input(self) -> None:
        """Mixed-case + leading/trailing whitespace hex must be accepted."""
        manager = _build_manager()
        writer = _FakeWriter()
        manager._socket_writers["8014"] = writer  # noqa: SLF001
        manager._socket_write_locks["8014"] = asyncio.Lock()  # noqa: SLF001

        async def run() -> bool:
            return await manager.async_send_packet("8014", "  FFA0A1F0  ")

        self.assertTrue(asyncio.run(run()))
        self.assertEqual(writer.writes[0], bytes.fromhex("ffa0a1f0"))

    def test_send_returns_false_for_invalid_hex(self) -> None:
        """Non-hex input fails fast with False (caller should fall back)."""
        manager = _build_manager()
        writer = _FakeWriter()
        manager._socket_writers["8014"] = writer  # noqa: SLF001
        manager._socket_write_locks["8014"] = asyncio.Lock()  # noqa: SLF001

        async def run() -> bool:
            return await manager.async_send_packet("8014", "not-hex-zz")

        self.assertFalse(asyncio.run(run()))
        self.assertEqual(writer.writes, [])

    def test_send_returns_false_for_empty_hex(self) -> None:
        """An empty hex string is rejected; otherwise we'd write zero bytes."""
        manager = _build_manager()
        writer = _FakeWriter()
        manager._socket_writers["8014"] = writer  # noqa: SLF001
        manager._socket_write_locks["8014"] = asyncio.Lock()  # noqa: SLF001

        async def run() -> bool:
            return await manager.async_send_packet("8014", "")

        self.assertFalse(asyncio.run(run()))

    def test_has_active_writer_reflects_registration(self) -> None:
        """has_active_writer follows the per-device writer registration."""
        manager = _build_manager()

        self.assertFalse(manager.has_active_writer("8014"))

        live_writer = _FakeWriter()
        manager._socket_writers["8014"] = live_writer  # noqa: SLF001
        self.assertTrue(manager.has_active_writer("8014"))

        live_writer._closing = True  # noqa: SLF001
        self.assertFalse(manager.has_active_writer("8014"))


if __name__ == "__main__":
    unittest.main()
