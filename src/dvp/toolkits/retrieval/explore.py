"""LeanExplore API/local search and normalized declaration results."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from dvp.toolkits.retrieval.explore_retry import retry_request
from dvp.toolkits.retrieval.explore_source import declaration_context, declaration_evidence
from dvp.toolkits.retrieval.explore_local import LocalSearchRuntime, guarded_engine_type


_MAX_SOURCE_BYTES = 2 * 1024 * 1024


def _source_location(source_link: str) -> dict[str, str]:
    """Accept only versioned GitHub source locations, never arbitrary fetch URLs."""
    try:
        url = urlsplit(source_link)
    except ValueError:
        return {}
    parts = unquote(url.path).strip("/").split("/")
    if (
        url.scheme != "https" or url.netloc != "github.com"
        or url.query or len(parts) < 5 or parts[2] != "blob"
        or any(part in ("", ".", "..") for part in parts)
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts[:2])
        or not re.fullmatch(r"[0-9a-fA-F]{40}|v\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", parts[3])
        or not parts[-1].endswith(".lean")
    ):
        return {}
    repository = "/".join(parts[:2])
    return {
        "source_repository": f"https://github.com/{repository}",
        "source_revision": parts[3],
        "source_path": "/".join(parts[4:]),
    }


def _dependencies(value: Any) -> list[str]:
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            value = json.loads(value)
        except ValueError:
            return [value]
    if isinstance(value, list):
        return list(dict.fromkeys(str(item) for item in value if item is not None))
    return []


def _lean_explore_queries(
    *,
    natural_language_query: str | None,
    lean_name: str | None,
    keywords: str | None,
) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    if lean_name and lean_name.strip():
        queries.append((lean_name.strip(), "LeanExplore name query"))
    if keywords and keywords.strip():
        queries.append((keywords.replace(",", " ").strip(), "LeanExplore keyword query"))
    if natural_language_query and natural_language_query.strip():
        queries.append((natural_language_query.strip(), "LeanExplore natural-language query"))
    return queries


def _lean_explore_module_to_path(module: str) -> str:
    if not module:
        return ""
    return module.replace(".", "/") + ".lean"


def _lean_explore_line_span(source_link: str) -> tuple[int | None, int | None]:
    match = re.search(r"#L(?P<start>\d+)(?:-L(?P<end>\d+))?", source_link or "")
    if not match:
        return None, None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    return start, end


class LeanExploreRetriever:
    """LeanExplore-backed retriever.

    The public ``api`` mode needs no API key. The ``local`` mode uses downloaded
    data. API results are enriched with context from their versioned GitHub
    source; local mode reads only explicitly configured, versioned local source.
    """

    def __init__(
        self,
        *,
        backend: str = "local",
        api_key: str | None = None,
        timeout: float = 20.0,
        packages: list[str] | None = None,
        rerank_top: int | None = 50,
        use_local_data: bool = False,
        bm25_only: bool = False,
        bm25_k: int = 1000,
        include_context: bool | None = None,
        retries: int = 2,
        embedding_model: str | None = None,
        reranker_model: str | None = None,
        source_dir: str | Path | None = None,
        local_concurrency: int = 4,
        local_max_pending: int = 128,
    ) -> None:
        self.backend = backend.strip().lower().replace("-", "_")
        self.packages = packages
        self.rerank_top = rerank_top
        self.bm25_only = bm25_only
        self.bm25_k = bm25_k
        self.timeout = timeout
        self.retries = int(retries)
        if not 0 <= self.retries <= 2:
            raise ValueError("DAVINCI_LEANEXPLORE_RETRIES must be between 0 and 2")
        self.include_context = self.backend == "api" if include_context is None else include_context
        self.source_dir = Path(source_dir).expanduser().resolve() if source_dir else None
        self.local_concurrency = local_concurrency
        self.local_max_pending = local_max_pending
        self._local_runtime: LocalSearchRuntime | None = None
        self.engine = None
        if self.backend == "api":
            from lean_explore.api import ApiClient

            self.client = ApiClient(api_key=api_key, timeout=timeout)
        elif self.backend == "local":
            from lean_explore.search import SearchEngine, Service

            engine_options: dict[str, Any] = {"use_local_data": use_local_data}
            for key, value in (
                ("embedding_model_name", embedding_model),
                ("reranker_model_name", reranker_model),
            ):
                if value:
                    path = Path(value).expanduser().resolve()
                    if not path.is_dir():
                        raise FileNotFoundError(f"LeanExplore local model directory does not exist: {path}")
                    engine_options[key] = str(path)
            self._local_runtime = LocalSearchRuntime(local_concurrency, local_max_pending)
            runtime = self._local_runtime
            self.engine = runtime.initialize(
                lambda: guarded_engine_type(SearchEngine, runtime)(**engine_options)
            )
            self.client = Service(self.engine)
        else:
            raise ValueError("DAVINCI_LEANEXPLORE_BACKEND must be api or local")

    async def search(
        self,
        *,
        natural_language_query: str | None = None,
        lean_name: str | None = None,
        keywords: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        arguments = dict(
            natural_language_query=natural_language_query,
            lean_name=lean_name,
            keywords=keywords,
            limit=limit,
        )
        if self.backend == "local":
            assert self._local_runtime is not None
            return await self._local_runtime.run(lambda: self._search(**arguments))
        return await self._search(**arguments)

    async def aclose(self) -> None:
        """Drain local searches and dispose their database on its owner loop."""
        if self._local_runtime is not None:
            await self._local_runtime.aclose()

    async def _search(
        self,
        *,
        natural_language_query: str | None = None,
        lean_name: str | None = None,
        keywords: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        combined: list[dict[str, Any]] = []
        deadline = asyncio.get_running_loop().time() + self.timeout

        for query, reason in _lean_explore_queries(
            natural_language_query=natural_language_query,
            lean_name=lean_name,
            keywords=keywords,
        ):
            if self.backend == "local" and self.bm25_only:
                query_results = await self._search_bm25_only(query=query, limit=limit)
            else:
                async def request():
                    return await self.client.search(
                        query=query,
                        limit=limit,
                        rerank_top=self.rerank_top,
                        packages=self.packages,
                    )

                response = (
                    await retry_request(request, deadline=deadline, retries=self.retries)
                    if self.backend == "api" else await request()
                )
                query_results = response.results
            for rank, result in enumerate(query_results, start=1):
                combined.append(self._format(result, rank=rank, reason=reason))

        dedup: dict[str, dict[str, Any]] = {}
        for result in combined:
            key = result.get("fullname") or result.get("signature")
            if not key:
                continue
            previous = dedup.get(key)
            if previous is None or result.get("score", 0.0) > previous.get("score", 0.0):
                dedup[key] = result

        results = sorted(dedup.values(), key=lambda item: item.get("score", 0.0), reverse=True)[:limit]
        if self.include_context:
            await self._add_context(results)
        else:
            for result in results:
                result["context_status"] = "disabled"
        return results

    async def _add_context(self, results: list[dict[str, Any]]) -> None:
        """Read versioned local files, or fetch API source with a bounded wait."""
        files: dict[str, list[dict[str, Any]]] = {}
        for result in results:
            result["context_status"] = "unavailable"
            if result.get("signature_status") == "name_mismatch":
                result["context_error"] = "Source belongs to a different declaration; check the requested name with Lean."
                continue
            result["context_error"] = "No supported versioned source location and line span."
            location = _source_location(str(result.get("source_link") or ""))
            if not location or not result.get("line_start") or not result.get("line_end"):
                continue
            repository = location["source_repository"].removeprefix("https://github.com/")
            url = f"https://raw.githubusercontent.com/{repository}/{location['source_revision']}/{location['source_path']}"
            files.setdefault(url, []).append(result)
        if not files:
            return
        if self.backend == "local":
            started = time.perf_counter()
            await asyncio.to_thread(self._add_local_context, files)
            LocalSearchRuntime.record_context(time.perf_counter() - started)
            return
        timeout = min(max(self.timeout, 0.1), 10.0)
        deadline = asyncio.get_running_loop().time() + timeout
        gate = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async def fetch(url: str) -> str:
                async with gate:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > _MAX_SOURCE_BYTES:
                                raise ValueError("Source file exceeds the 2 MiB context limit.")
                return chunks.decode("utf-8")

            async def enrich(url: str, items: list[dict[str, Any]]) -> None:
                try:
                    text = await retry_request(lambda: fetch(url), deadline=deadline, retries=self.retries)
                    self._apply_context(text, items)
                except asyncio.CancelledError:
                    for item in items:
                        if item["context_status"] != "available":
                            item["context_error"] = "Versioned source context exceeded the time limit."
                    raise
                except asyncio.TimeoutError:
                    for item in items:
                        if item["context_status"] != "available":
                            item["context_error"] = "Versioned source context exceeded the time limit."
                except Exception as exc:
                    for item in items:
                        if item["context_status"] != "available":
                            item["context_error"] = f"{type(exc).__name__}: {exc}"

            tasks = [asyncio.create_task(enrich(url, items)) for url, items in files.items()]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
            except asyncio.TimeoutError:
                pass  # Search hits remain useful when optional context is unavailable.

    def _add_local_context(self, files: dict[str, list[dict[str, Any]]]) -> None:
        """Read/scan versioned sources outside the SDK owner and model gates."""
        for items in files.values():
            try:
                text = self._read_local_source(items[0])
                self._apply_context(text, items)
            except (OSError, ValueError, UnicodeError) as exc:
                for item in items:
                    item["context_error"] = f"Local source unavailable: {exc}"

    def _read_local_source(self, result: dict[str, Any]) -> str:
        if self.source_dir is None:
            raise ValueError("DAVINCI_LEANEXPLORE_SOURCE_DIR is not configured; no network fallback is used.")
        location = _source_location(str(result.get("source_link") or ""))
        repository = location["source_repository"]
        revision = location["source_revision"]
        version_dir = (self.source_dir / repository.removeprefix("https://github.com/") / revision).resolve()
        if not version_dir.is_relative_to(self.source_dir):
            raise ValueError("Versioned source directory is outside DAVINCI_LEANEXPLORE_SOURCE_DIR.")
        manifest_path = (version_dir / ".dvp-source.json").resolve()
        path = (version_dir / location["source_path"]).resolve()
        if not manifest_path.is_relative_to(version_dir) or not path.is_relative_to(version_dir):
            raise ValueError("Source file or manifest is outside its versioned directory.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or (
            manifest.get("repository") != repository or manifest.get("revision") != revision
        ):
            raise ValueError("Source manifest repository/revision does not match the indexed declaration.")
        with path.open("rb") as handle:
            content = handle.read(_MAX_SOURCE_BYTES + 1)
        if len(content) > _MAX_SOURCE_BYTES:
            raise ValueError("Source file exceeds the 2 MiB context limit.")
        return content.decode("utf-8")

    @staticmethod
    def _apply_context(text: str, items: list[dict[str, Any]]) -> None:
        """Use the same source-span and identity checks for both backends."""
        lines = text.splitlines()
        for item in items:
            start, end = int(item["line_start"]), int(item["line_end"])
            if not 1 <= start <= end <= len(lines):
                item["context_error"] = "Indexed source span is outside the versioned file."
                continue
            expected = re.sub(r"\s+", " ", str(item.get("_source_text") or "")).strip()
            actual = re.sub(r"\s+", " ", "\n".join(lines[start - 1:end])).strip()
            if not expected or expected not in actual:
                item["context_error"] = "Indexed declaration does not match its versioned source span."
                continue
            context = declaration_context(text, start)
            if item["signature_status"] == "source_header":
                context_evidence = declaration_evidence(
                    item.get("_source_text"), item["fullname"],
                    namespace=context["namespace"],
                )
                if context_evidence["signature_status"] != "source_header":
                    # A lexical scan can miss scope commands. Reject conflicting
                    # context, not the matching header.
                    item["context_error"] = (
                        "Lexical namespace conflicts with the reported declaration name; "
                        "context extraction may be incomplete. Use lean_verify with "
                        f"`#check {item['fullname']}` to confirm the type."
                    )
                    continue
            item["context"] = context
            item["context_status"] = "available"
            item.pop("context_error", None)

    async def _search_bm25_only(self, *, query: str, limit: int) -> list[Any]:
        if self.engine is None:
            return []

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession

        from lean_explore.models import Declaration
        from lean_explore.search.tokenization import is_autogenerated

        bm25_map = self.engine._retrieve_bm25_candidates(query, max(self.bm25_k, limit))
        ranked = sorted(bm25_map.items(), key=lambda item: item[1], reverse=True)
        candidate_ids = [decl_id for decl_id, _ in ranked[: max(self.bm25_k, limit)]]
        if not candidate_ids:
            return []

        async with AsyncSession(self.engine.engine) as session:
            stmt = select(Declaration).where(Declaration.id.in_(candidate_ids))
            result = await session.execute(stmt)
            declarations = {decl.id: decl for decl in result.scalars().all()}

        if self.packages:
            declarations = self.engine._filter_by_packages(declarations, self.packages)

        results: list[Any] = []
        for decl_id in candidate_ids:
            decl = declarations.get(decl_id)
            if decl is None or is_autogenerated(decl.name):
                continue
            results.append(self.engine._to_search_result(decl))
            if len(results) >= limit:
                break
        return results

    def _format(self, result: Any, *, rank: int, reason: str) -> dict[str, Any]:
        source_text = str(getattr(result, "source_text", "") or "")
        source_link = str(getattr(result, "source_link", "") or "")
        line_start, line_end = _lean_explore_line_span(source_link)
        name = str(getattr(result, "name", "") or "")
        evidence = declaration_evidence(source_text, name)
        short_name = name.rsplit(".", 1)[-1] if name else ""
        semantic = str(getattr(result, "informalization", "") or getattr(result, "docstring", "") or "")

        return {
            "id": getattr(result, "id", None),
            **evidence,
            "name": short_name,
            "fullname": name,
            "semantic": semantic,
            "docstring": str(getattr(result, "docstring", "") or ""),
            "dependencies": _dependencies(getattr(result, "dependencies", None)),
            "module": str(getattr(result, "module", "") or ""),
            "keywords": [],
            "filename": _lean_explore_module_to_path(str(getattr(result, "module", "") or "")),
            "line_start": line_start,
            "line_end": line_end,
            "score": round(1.0 / max(rank, 1), 4),
            "score_kind": "reciprocal_rank",
            "reason": reason,
            "source": ["lean_explore"],
            "source_link": source_link,
            "source_origin": "lean_explore",
            **({"source_code_role": "generator"} if evidence["signature_status"] == "name_mismatch" else {}),
            **_source_location(source_link),
            "_source_text": source_text,
        }
