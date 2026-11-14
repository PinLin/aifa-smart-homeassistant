"""Shared fixtures for the AIFA Smart integration tests.

Most tests are unittest.TestCase classes on pure functions, packet helpers
and coordinators assembled without hass; the files that need a real hass
use the `hass` fixture of pytest-homeassistant-custom-component.
"""
from __future__ import annotations

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True, scope="session")
def _start_pycares_shutdown_thread() -> None:
    """Start the DNS resolver's process-wide thread before any test runs.

    HA's client session starts it on first use and it never exits, so
    verify_cleanup would blame whichever test happened to be first.
    """
    try:
        import pycares
    except ImportError:
        return
    manager = getattr(pycares, "_shutdown_manager", None)
    if manager is not None:
        manager.start()
