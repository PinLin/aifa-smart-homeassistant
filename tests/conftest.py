"""Shared fixtures for the AIFA Smart integration tests.

The current suite is unit-test heavy (unittest.TestCase classes that
exercise pure functions, packet helpers, mocked sessions, and stubbed
coordinator scenarios) — none of them actually boot a HomeAssistant
instance. We therefore load pytest-homeassistant-custom-component for
the real `homeassistant` / `aiohttp` packages it brings in, but skip
the usual autouse `enable_custom_integrations` fixture: that fixture
depends on the async `hass` fixture, which pytest-asyncio cannot
resolve from inside `unittest.TestCase` setUp. Tests that need a real
hass should opt in explicitly via `pytest.mark.usefixtures("hass")`
once they are converted to pytest-style functions.
"""
from __future__ import annotations

pytest_plugins = ["pytest_homeassistant_custom_component"]
