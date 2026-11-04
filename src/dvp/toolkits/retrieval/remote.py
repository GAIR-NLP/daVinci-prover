"""HTTP transport for a version-pinned DVP LeanExplore retrieval service."""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from dvp.toolkits.retrieval.explore_retry import retry_request


PROTOCOL = "dvp.leanexplore.v1"


class RemoteRetrievalError(RuntimeError):
    """The configured server failed or returned incompatible retrieval data."""


class RemoteLeanExploreRetriever:
    """Keep the same normalized evidence as local retrieval, without loading models.

    The server owns retrieval settings and source enrichment. The client checks
    the dataset identity on every response and never falls back to another URL.
    """

    backend = "remote"

    def __init__(
        self, *, url: str, expected_lean_version: str,
        expected_mathlib_commit: str, expected_snapshot: str | None = None,
        token: str | None = None, timeout: float = 300, retries: int = 2,
    ) -> None:
        self.url = url.strip().rstrip("/")
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("DAVINCI_LEANEXPLORE_URL must be an HTTP(S) service URL without credentials or query")
        if not re.fullmatch(r"\d+\.\d+\.\d+", expected_lean_version or ""):
            raise ValueError("Set DAVINCI_LEANEXPLORE_EXPECTED_LEAN_VERSION, for example 4.35.0")
        if not re.fullmatch(r"[0-9a-f]{40}", expected_mathlib_commit or ""):
            raise ValueError("Set DAVINCI_LEANEXPLORE_EXPECTED_MATHLIB_COMMIT to the full Mathlib commit")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("DAVINCI_LEANEXPLORE_TIMEOUT must be positive and finite")
        if not 0 <= retries <= 2:
            raise ValueError("DAVINCI_LEANEXPLORE_RETRIES must be between 0 and 2")
        self.expected_lean_version = expected_lean_version
        self.expected_mathlib_commit = expected_mathlib_commit
        self.expected_snapshot = expected_snapshot or None
        self.timeout, self.retries = timeout, retries
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        # Do not inherit proxy credentials or follow redirects with a bearer token.
        async with httpx.AsyncClient(
            timeout=self.timeout, trust_env=False, follow_redirects=False,
        ) as client:
            async def request():
                response = await client.request(
                    method, self.url + path, headers=self._headers, **kwargs,
                )
                response.raise_for_status()
                return response.json()

            try:
                return await retry_request(request, timeout=self.timeout, retries=self.retries)
            except httpx.HTTPStatusError as exc:
                detail = ""
                try:
                    detail = str(exc.response.json().get("detail", ""))[:500]
                except (ValueError, AttributeError):
                    pass
                raise RemoteRetrievalError(
                    f"LeanExplore server returned HTTP {exc.response.status_code}"
                    + (f": {detail}" if detail else "")
                ) from exc
            except (httpx.RequestError, TimeoutError, ValueError) as exc:
                raise RemoteRetrievalError(
                    f"LeanExplore remote request failed ({type(exc).__name__}): {exc}"
                ) from exc

    def _validate_metadata(self, info: Any) -> dict[str, Any]:
        if not isinstance(info, dict) or info.get("protocol") != PROTOCOL:
            raise RemoteRetrievalError("Unsupported LeanExplore server protocol")
        for field, expected in (
            ("lean_version", self.expected_lean_version),
            ("mathlib_commit", self.expected_mathlib_commit),
            ("snapshot", self.expected_snapshot),
        ):
            if expected is not None and info.get(field) != expected:
                raise RemoteRetrievalError(
                    f"LeanExplore {field} mismatch: expected {expected}, got {info.get(field)!r}"
                )
        return info

    async def info(self) -> dict[str, Any]:
        return self._validate_metadata(await self._request("GET", "/v1/info"))

    async def search(
        self, *, natural_language_query: str | None = None,
        lean_name: str | None = None, keywords: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        body = {
            "natural_language_query": natural_language_query,
            "lean_name": lean_name, "keywords": keywords,
            "limit": max(1, min(int(limit), 50)),
            "expected_lean_version": self.expected_lean_version,
            "expected_mathlib_commit": self.expected_mathlib_commit,
            "expected_snapshot": self.expected_snapshot,
        }
        response = await self._request("POST", "/v1/search", json=body)
        if not isinstance(response, dict) or response.get("protocol") != PROTOCOL:
            raise RemoteRetrievalError("Unsupported LeanExplore search response")
        self._validate_metadata(response.get("metadata"))
        results = response.get("results")
        if (not isinstance(results, list) or len(results) > body["limit"]
                or response.get("count") != len(results)):
            raise RemoteRetrievalError("Malformed LeanExplore search results")
        for result in results:
            if (not isinstance(result, dict) or not isinstance(result.get("fullname"), str)
                    or not result["fullname"] or result.get("source_origin") != "lean_explore"):
                raise RemoteRetrievalError("Malformed LeanExplore declaration evidence")
            if (str(result.get("module", "")).split(".")[0] == "Mathlib"
                    and result.get("source_revision") != self.expected_mathlib_commit):
                raise RemoteRetrievalError("LeanExplore declaration source differs from the pinned Mathlib commit")
        return results
