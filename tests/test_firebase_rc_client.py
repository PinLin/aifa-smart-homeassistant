"""Tests for the Firebase Remote Config transport client."""
from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.aifa_smart.firebase_rc_client import (
    FirebaseAuthError,
    FirebaseRcClient,
    FirebaseTransientError,
)


class _FakeResponse:
    def __init__(self, status: int, body: dict | str) -> None:
        self.status = status
        self._body = body

    async def json(self) -> dict:
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("body is text")

    async def text(self) -> str:
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _fake_session(response: _FakeResponse) -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    return session


class FirebaseRcClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_installation_returns_token_on_200(self) -> None:
        response = _FakeResponse(200, {"authToken": {"token": "fis-token-xyz"}})
        client = FirebaseRcClient(
            _fake_session(response),
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        token = await client.async_register_installation()

        self.assertEqual(token, "fis-token-xyz")

    async def test_register_installation_raises_auth_on_4xx(self) -> None:
        response = _FakeResponse(403, {"error": {"code": 403, "message": "blocked"}})
        client = FirebaseRcClient(
            _fake_session(response),
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        with self.assertRaises(FirebaseAuthError):
            await client.async_register_installation()

    async def test_register_installation_raises_transient_on_5xx(self) -> None:
        response = _FakeResponse(503, "service unavailable")
        client = FirebaseRcClient(
            _fake_session(response),
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        with self.assertRaises(FirebaseTransientError):
            await client.async_register_installation()

    async def test_fetch_remote_config_returns_payload_on_200(self) -> None:
        rc_payload = {
            "entries": {
                "ac_available_modes": '{"1": ["cool", "heat"]}',
                "ac_classic_temp_control": '{"codes": [16, 19]}',
            },
            "templateVersion": 47,
        }
        response = _FakeResponse(200, rc_payload)
        client = FirebaseRcClient(
            _fake_session(response),
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        result = await client.async_fetch_remote_config(fis_token="t")

        self.assertEqual(result, rc_payload)

    async def test_fetch_remote_config_raises_auth_on_401(self) -> None:
        response = _FakeResponse(401, {"error": "unauthorized"})
        client = FirebaseRcClient(
            _fake_session(response),
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        with self.assertRaises(FirebaseAuthError):
            await client.async_fetch_remote_config(fis_token="t")

    async def test_fetch_remote_config_raises_transient_on_500(self) -> None:
        response = _FakeResponse(500, "boom")
        client = FirebaseRcClient(
            _fake_session(response),
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        with self.assertRaises(FirebaseTransientError):
            await client.async_fetch_remote_config(fis_token="t")

    async def test_register_installation_url_uses_project_id(self) -> None:
        response = _FakeResponse(200, {"authToken": {"token": "tok"}})
        session = _fake_session(response)
        client = FirebaseRcClient(
            session,
            project_id="custom-project",
            project_number="99",
            app_id="1:2:3:4",
            api_key="key",
        )

        await client.async_register_installation()

        # First positional arg to session.post is the URL
        posted_url = session.post.call_args[0][0]
        self.assertIn("projects/custom-project/installations", posted_url)

    async def test_fetch_remote_config_url_uses_project_number_and_namespace(self) -> None:
        response = _FakeResponse(200, {"entries": {}})
        session = _fake_session(response)
        client = FirebaseRcClient(
            session,
            project_id="p",
            project_number="42",
            app_id="1:2:3:4",
            api_key="key",
        )

        await client.async_fetch_remote_config(fis_token="t", namespace="custom-ns")

        posted_url = session.post.call_args[0][0]
        self.assertIn("projects/42/namespaces/custom-ns:fetch", posted_url)


if __name__ == "__main__":
    unittest.main()
