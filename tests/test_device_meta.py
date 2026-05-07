"""Tests for user-facing device labels."""
from __future__ import annotations

import unittest

from custom_components.aifa_smart.api import AifaDevice, AifaSubDevice
from custom_components.aifa_smart.device_meta import (
    device_model_label,
    is_probably_tv_like,
    sub_device_category_label,
    sub_device_model_label,
)


class DeviceMetaTests(unittest.TestCase):
    """Coverage for human-readable device metadata."""

    def test_parent_device_type_four_gets_human_label(self) -> None:
        """The current hub type should not render as a bare number."""
        device = AifaDevice("8014", "i-Ctrl AC-5bb2", None, "4", True, None, None, None)
        self.assertEqual(device_model_label(device), "AIFA IR Hub")

    def test_sub_device_model_prefers_model_code_over_catalog_brand(self) -> None:
        """AC sub-device labels should avoid raw type noise and misleading catalog brands."""
        sub_device = AifaSubDevice(
            "31867",
            "8014",
            "冷氣",
            "0",
            "0",
            "136",
            device_code_brand_localized_name="開利",
            device_code_model_name="ARC-466A12",
        )

        self.assertEqual(sub_device_category_label(sub_device), "Air Conditioner")
        self.assertEqual(
            sub_device_model_label(sub_device),
            "Air Conditioner / ARC-466A12 / device code 136",
        )

    def test_sub_device_model_omits_catalog_brand_when_model_hint_is_missing(self) -> None:
        """AC labels should not fall back to potentially misleading catalog brands."""
        sub_device = AifaSubDevice(
            "31867",
            "8014",
            "冷氣",
            "0",
            "0",
            "136",
            device_code_brand_localized_name="開利",
        )

        self.assertEqual(
            sub_device_model_label(sub_device),
            "Air Conditioner / device code 136",
        )

    def test_name_hints_detect_tv_like_devices(self) -> None:
        """Television-like names should help downstream entity selection."""
        sub_device = AifaSubDevice("12", "1", "客廳電視", None, None, None)
        self.assertTrue(is_probably_tv_like(sub_device))


if __name__ == "__main__":
    unittest.main()
