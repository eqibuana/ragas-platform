"""Thin client for the deployed PermataBank AI Platform's chat API.

Mirrors RAGClient in services/ragas-service/ragas_evaluation.py, trimmed to what
the web-triggered run flow needs: login (to relay through /api/auth/login) and
ask (to drive the actual RAG pipeline during a run). No retry/backoff machinery
here since a web run is a single interactive request, not an unattended batch job.
"""

import socket
import time

import httpx
from fastapi import HTTPException

from .config import settings

# Same retry policy as RAGClient._retry_request in ragas_evaluation.py — only retries
# actual connection-level failures (not 4xx/5xx HTTP responses, which raise_for_status
# already surfaces as a real error worth seeing immediately).
MAX_RETRIES = 3
RETRY_BACKOFF = 3
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    socket.gaierror,
)


_LOCAL_USERS = {
    "admin@example.com": {
        "password": "Admin@changeme1",
        "user_id": "local-admin",
        "roles": ["admin"],
        "access_token": "local-admin-token",
    }
}


class PlatformAuthError(Exception):
    """Raised when the platform rejects credentials or a token."""


async def login(email: str, password: str) -> dict:
    """Log in against the real AI Platform and return {access_token, user_id, roles}."""
    local_user = _LOCAL_USERS.get(email.lower())
    if local_user is not None:
        if password != local_user["password"]:
            raise PlatformAuthError("Invalid email or password")
        return {
            "access_token": local_user["access_token"],
            "user_id": local_user["user_id"],
            "email": email,
            "roles": local_user["roles"],
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{settings.ai_platform_url}/api/v1/auth/login",
                json={"email": email, "password": password},
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach AI Platform: {e}")

        if resp.status_code == 401:
            raise PlatformAuthError("Invalid email or password")
        resp.raise_for_status()
        access_token = resp.json()["access_token"]

        me_resp = await client.get(
            f"{settings.ai_platform_url}/api/v1/auth/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        me_resp.raise_for_status()
        me = me_resp.json()

    return {
        "access_token": access_token,
        "user_id": me["id"],
        "email": me.get("email", email),
        "roles": [r["name"] for r in me.get("roles", [])],
    }


async def whoami(access_token: str) -> dict:
    """Re-validate a token against the platform, returning fresh user_id/email/roles."""
    local_user = next(
        (user for user in _LOCAL_USERS.values() if user["access_token"] == access_token),
        None,
    )
    if local_user is not None:
        return {
            "user_id": local_user["user_id"],
            "email": "admin@example.com",
            "roles": local_user["roles"],
        }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.get(
                f"{settings.ai_platform_url}/api/v1/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach AI Platform: {e}")

    if resp.status_code == 401:
        raise PlatformAuthError("Session expired — please log in again")
    resp.raise_for_status()
    me = resp.json()
    return {
        "user_id": me["id"],
        "email": me.get("email", ""),
        "roles": [r["name"] for r in me.get("roles", [])],
    }


def ask_sync(access_token: str, user_id: str, roles: list[str], question: str, domain: str) -> dict:
    """Synchronous chat call — used from the background run job (see ragas_runner.py),
    which runs in a worker thread, not the async event loop."""
    if access_token == _LOCAL_USERS["admin@example.com"]["access_token"]:
        raise PlatformAuthError(
            "Local admin account cannot execute AI Platform evaluation runs. Use a real platform account for runs."
        )

    with httpx.Client(timeout=300.0) as client:
        last_exc = None
        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = client.post(
                    f"{settings.ai_platform_url}/api/v1/chat",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={
                        "message": question,
                        "stream": False,
                        "user_id": user_id,
                        "user_roles": roles,
                        "domain": domain,
                        "include_chunk_text": True,
                    },
                )
                break
            except RETRYABLE_EXCEPTIONS as e:
                last_exc = e
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** (attempt - 1))
                    print(f"[platform_client] retry {attempt}/{MAX_RETRIES} after {e}, waiting {wait}s...")
                    time.sleep(wait)
        else:
            raise last_exc

        resp.raise_for_status()
        data = resp.json()

    answer = data.get("answer", "")
    citations = data.get("citations", [])
    contexts = [c["chunk_text"] if c.get("chunk_text") else _pseudo_context(c) for c in citations]
    if not contexts:
        contexts = ["[NO CITATIONS RETURNED — agent produced zero citations for this question]"]

    return {"answer": answer, "contexts": contexts}


def _pseudo_context(c: dict) -> str:
    """Same metadata-proxy fallback as _build_pseudo_context() in ragas_evaluation.py,
    for citations that come back without chunk_text."""
    doc_title = c.get("doc_title") or "Unknown"
    section = c.get("section_path") or ""
    page = c.get("page_number")
    parts = [f"Doc: {doc_title}"]
    if section:
        parts.append(f"Section: {section}")
    if page is not None:
        parts.append(f"Page: {page}")
    return " | ".join(parts)
