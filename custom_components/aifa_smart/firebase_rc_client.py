"""Firebase Remote Config transport for AIFA Smart catalog refresh.

Two-step flow that mirrors the Firebase Android SDK:
  1. Register an installation (FIS) with the API key — get an auth token.
  2. Hit the RC fetch endpoint with the auth token — get the config payload.

This module is pure transport. Caller (catalog.py) handles parsing, merging,
caching, and retry policy.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)


class FirebaseAuthError(RuntimeError):
    """Raised when Firebase rejects the API key / FIS token (4xx, non-quota)."""


class FirebaseTransientError(RuntimeError):
    """Raised on 5xx, network errors, or 429 — caller should retry later."""


class FirebaseRcClient:
    """Thin client for Firebase Installations + Remote Config REST endpoints."""

    INSTALLATIONS_URL_TEMPLATE = (
        "https://firebaseinstallations.googleapis.com/v1/projects/{project_id}/installations"
    )
    RC_FETCH_URL_TEMPLATE = (
        "https://firebaseremoteconfig.googleapis.com/v1/"
        "projects/{project_number}/namespaces/{namespace}:fetch"
    )

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        project_id: str,
        project_number: str,
        app_id: str,
        api_key: str,
    ) -> None:
        self._session = session
        self._project_id = project_id
        self._project_number = project_number
        self._app_id = app_id
        self._api_key = api_key

    async def async_register_installation(self) -> str:
        """Return an FIS auth token. Raise FirebaseAuthError on 4xx."""
        url = self.INSTALLATIONS_URL_TEMPLATE.format(project_id=self._project_id)
        payload = {
            "fid": uuid.uuid4().hex[:22],
            "appId": self._app_id,
            "authVersion": "FIS_v2",
            "sdkVersion": "a:17.2.0",
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
        }
        try:
            async with self._session.post(
                url, json=payload, headers=headers
            ) as resp:
                if 400 <= resp.status < 500 and resp.status != 429:
                    body = await resp.text()
                    raise FirebaseAuthError(
                        f"FIS register failed {resp.status}: {body[:300]}"
                    )
                if resp.status >= 500 or resp.status == 429:
                    body = await resp.text()
                    raise FirebaseTransientError(
                        f"FIS register transient {resp.status}: {body[:300]}"
                    )
                data = await resp.json()
                token = data["authToken"]["token"]
                return token
        except aiohttp.ClientError as err:
            raise FirebaseTransientError(f"FIS network error: {err}") from err

    async def async_fetch_remote_config(
        self, *, fis_token: str, namespace: str = "firebase"
    ) -> dict[str, Any]:
        """Return the full RC fetch response. Raise FirebaseAuthError on 401/403."""
        url = self.RC_FETCH_URL_TEMPLATE.format(
            project_number=self._project_number, namespace=namespace
        )
        payload = {
            "appInstanceId": uuid.uuid4().hex,
            "appInstanceIdToken": fis_token,
            "appId": self._app_id,
            "packageName": "tw.com.aifa.ictrl_pro",
            "sdkVersion": "21.4.0",
            "analyticsUserProperties": {},
        }
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self._api_key,
            "x-goog-firebase-installations-auth": fis_token,
        }
        try:
            async with self._session.post(
                url, json=payload, headers=headers
            ) as resp:
                if 400 <= resp.status < 500 and resp.status != 429:
                    body = await resp.text()
                    raise FirebaseAuthError(
                        f"RC fetch failed {resp.status}: {body[:300]}"
                    )
                if resp.status >= 500 or resp.status == 429:
                    body = await resp.text()
                    raise FirebaseTransientError(
                        f"RC fetch transient {resp.status}: {body[:300]}"
                    )
                return await resp.json()
        except aiohttp.ClientError as err:
            raise FirebaseTransientError(f"RC network error: {err}") from err
