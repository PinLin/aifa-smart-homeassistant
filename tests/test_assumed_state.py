"""AC entities always report assumed_state: the hub only proves it fired the IR code."""
from __future__ import annotations

import unittest

from custom_components.aifa_smart.climate import AifaAirConditionerClimate
from tests.hub_reports import status
from tests.test_command_confirmation import _build_coordinator


class AssumedStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = _build_coordinator("lan")
        self.coordinator.last_update_success = True
        self.state = self.coordinator.get_ac_runtime_state("7001", "ac-1", device_code="136")

    def test_climate_is_assumed_even_with_a_hub_report(self) -> None:
        entity = AifaAirConditionerClimate(self.coordinator, "7001", "ac-1")
        self.assertTrue(entity.assumed_state)
        self.state.apply_observed_status(status("cool"))
        self.assertTrue(entity.assumed_state)
        # The extra attribute tells whether the shown state is the hub's report.
        self.assertFalse(entity.extra_state_attributes["aifa_state_assumed"])
        self.assertNotIn("assumed_state", entity.extra_state_attributes)


if __name__ == "__main__":
    unittest.main()
