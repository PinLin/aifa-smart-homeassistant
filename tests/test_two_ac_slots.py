"""One hub with two air conditioners: subType 0 reports as fe a0, subType 1 as fe a1.

The code check compares each AC only with reports of its own slot; slot 0's
report carries slot 0's code, so it says nothing about the second AC.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from custom_components.aifa_smart import coordinator as coordinator_module
from custom_components.aifa_smart.ac import decode_observed_status_packet
from custom_components.aifa_smart.api import AifaSubDevice
from custom_components.aifa_smart.classic_listener import build_hub_specs
from tests.test_command_confirmation import _build_coordinator, _hub_device


def _report(slot: int, high: int, low: int) -> bytes:
    return bytes([0xFE, 0xA0 | slot, high, low, 0x00, 0x00, 0x20, 0x11]) + bytes(5) + b"\xf0"


class TwoAcCodeCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        device = _hub_device(version=5, subversion=210623, device_code="115")
        device.sub_devices[0].name = "AC slot0"
        device.sub_devices.append(
            AifaSubDevice(
                id="ac-2",
                device_id="7001",
                name="AC slot1",
                type="0",
                sub_type="1",
                device_code="179",
            )
        )
        self.coordinator = _build_coordinator("lan", device=device)
        self.coordinator._device_code_checked = set()  # noqa: SLF001
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        patches = (
            patch.object(
                coordinator_module.ir,
                "async_create_issue",
                lambda hass, domain, issue_id, **kw: self.created.append(
                    (issue_id, kw["translation_placeholders"])
                ),
            ),
            patch.object(
                coordinator_module.ir,
                "async_delete_issue",
                lambda hass, domain, issue_id: self.deleted.append(issue_id),
            ),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.spec = build_hub_specs(self.coordinator.data.devices, require_online=False)["7001"]

    def _push(self, raw: bytes) -> None:
        sub_type, observed = decode_observed_status_packet(raw)
        self.coordinator._apply_lan_observed_status(self.spec, sub_type, observed)  # noqa: SLF001

    def test_slot_0_report_raises_no_issue_for_the_slot_1_ac(self) -> None:
        self._push(_report(0, 0x01, 0x15))
        self.assertEqual(self.created, [])
        self.assertIn("device_code_mismatch_ac-1", self.deleted)
        self.assertNotIn("device_code_mismatch_ac-2", self.deleted)

    def test_slot_1_report_is_checked_against_the_slot_1_ac(self) -> None:
        self._push(_report(1, 0x01, 0x79))
        self.assertEqual(self.created, [])
        self.assertEqual(self.deleted, ["device_code_mismatch_ac-2"])

    def test_a_real_mismatch_on_slot_1_is_still_reported(self) -> None:
        self._push(_report(0, 0x01, 0x15))
        self._push(_report(1, 0x01, 0x15))
        self.assertEqual(
            self.created,
            [
                (
                    "device_code_mismatch_ac-2",
                    {"name": "AC slot1", "hub_code": "115", "cloud_code": "179"},
                )
            ],
        )
