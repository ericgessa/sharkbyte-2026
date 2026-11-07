"""Turn a Supabase access token (JWT) into a user id, or None.

Verification is delegated to Supabase Auth (``auth.get_user(jwt)``), which also
catches revoked sessions and works with whichever signing keys the project uses.
It is a network call, so do it once per REST request / once per WebSocket
connection, never per frame.
"""

from __future__ import annotations

import logging

from supabase import Client

from app.services.database import get_client

logger = logging.getLogger(__name__)


class SupabaseAuth:
    def __init__(self, client: Client | None = None) -> None:
        self._client = client

    def verify(self, access_token: str) -> str | None:
        client = self._client or get_client()
        try:
            res = client.auth.get_user(access_token)
        except Exception as exc:  # invalid/expired token, network error...
            logger.info("token rejected: %s", type(exc).__name__)
            return None
        user = getattr(res, "user", None)
        return str(user.id) if user is not None else None
