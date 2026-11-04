"""Deterministic in-process search over a declaration JSONL index."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from dvp.toolkits.retrieval.declarations import tokenize


class LeanRetriever:
    """In-process retriever over a declaration JSONL index."""

    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path.expanduser().resolve()
        self.records: list[dict[str, Any]] = []
        self.name_to_ids: dict[str, set[int]] = {}
        self.inverted: dict[str, set[int]] = {}
        self.doc_freq: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        if not self.index_path.exists():
            raise FileNotFoundError(f"Lean retrieval index not found: {self.index_path}")

        with self.index_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                record = json.loads(line)
                record["_tokens"] = set(self._record_tokens(record))
                item_id = len(self.records)
                self.records.append(record)
                for name in {record.get("name", ""), record.get("fullname", "")}:
                    if name:
                        lowered = str(name).lower()
                        self.name_to_ids.setdefault(lowered, set()).add(item_id)
                for token in record["_tokens"]:
                    self.inverted.setdefault(token, set()).add(item_id)

        total = max(1, len(self.records))
        self.doc_freq = {token: len(ids) for token, ids in self.inverted.items()}
        self._idf_cache = {
            token: math.log((total + 1) / (freq + 1)) + 1.0
            for token, freq in self.doc_freq.items()
        }

    def _record_tokens(self, record: dict[str, Any]) -> list[str]:
        keywords = record.get("keywords")
        if isinstance(keywords, list) and keywords:
            return [str(item).lower() for item in keywords if item]
        parts = [
            str(record.get("fullname", "")),
            str(record.get("signature", "")),
            str(record.get("doc", "")),
        ]
        return tokenize(" ".join(parts))

    def _candidate_ids(self, query_tokens: list[str]) -> set[int]:
        candidates: set[int] = set()
        for token in query_tokens:
            candidates.update(self.inverted.get(token, set()))
        return candidates

    def _format(self, item_id: int, score: float, reason: str) -> dict[str, Any]:
        record = self.records[item_id]
        return {
            "kind": record.get("kind", ""),
            "name": record.get("name", ""),
            "fullname": record.get("fullname", ""),
            "signature": record.get("signature", ""),
            "semantic": record.get("doc", ""),
            "keywords": record.get("keywords", []),
            "filename": record.get("file_path", ""),
            "line_start": record.get("line_start"),
            "line_end": record.get("line_end"),
            "score": round(float(score), 4),
            "reason": reason,
            "source": ["target"],
        }

    def _token_search(self, query: str, limit: int, mode: str) -> list[dict[str, Any]]:
        query_tokens = tokenize(query)
        if not query_tokens:
            return []
        candidates = self._candidate_ids(query_tokens)
        scored: list[tuple[float, int]] = []
        query_set = set(query_tokens)
        for item_id in candidates:
            record = self.records[item_id]
            token_set = record["_tokens"]
            overlap = query_set & token_set
            if not overlap:
                continue
            score = sum(self._idf_cache.get(token, 1.0) for token in overlap)
            fullname_tokens = set(tokenize(str(record.get("fullname", ""))))
            signature_tokens = set(tokenize(str(record.get("signature", ""))))
            score += 2.0 * len(overlap & fullname_tokens)
            score += 0.5 * len(overlap & signature_tokens)
            scored.append((score, item_id))

        scored.sort(key=lambda item: (item[0], self.records[item[1]].get("fullname", "")), reverse=True)
        return [
            self._format(item_id, score, f"{mode} token overlap")
            for score, item_id in scored[:limit]
        ]

    def _name_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        lowered = query.strip().lower()
        if not lowered:
            return []

        scored: dict[int, float] = {}
        if lowered in self.name_to_ids:
            for item_id in self.name_to_ids[lowered]:
                scored[item_id] = max(scored.get(item_id, 0.0), 100.0)

        for name, ids in self.name_to_ids.items():
            if name == lowered:
                continue
            if name.startswith(lowered):
                score = 80.0
            elif lowered in name:
                score = 60.0
            else:
                continue
            for item_id in ids:
                scored[item_id] = max(scored.get(item_id, 0.0), score)

        ranked = sorted(scored.items(), key=lambda item: (item[1], self.records[item[0]].get("fullname", "")), reverse=True)
        results = [
            self._format(item_id, score, "name match")
            for item_id, score in ranked[:limit]
        ]
        if len(results) < limit:
            seen = {item.get("fullname") for item in results}
            for item in self._token_search(query, limit, "name-token"):
                if item.get("fullname") in seen:
                    continue
                results.append(item)
                seen.add(item.get("fullname"))
                if len(results) >= limit:
                    break
        return results

    def search(
        self,
        *,
        natural_language_query: str | None = None,
        lean_name: str | None = None,
        keywords: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        combined: list[dict[str, Any]] = []
        if lean_name and lean_name.strip():
            combined.extend(self._name_search(lean_name, limit))
        if keywords and keywords.strip():
            combined.extend(self._token_search(keywords.replace(",", " "), limit, "keyword"))
        if natural_language_query and natural_language_query.strip():
            combined.extend(self._token_search(natural_language_query, limit, "natural language"))

        dedup: dict[str, dict[str, Any]] = {}
        for result in combined:
            key = result.get("fullname") or result.get("signature")
            if not key:
                continue
            previous = dedup.get(key)
            if previous is None or result.get("score", 0.0) > previous.get("score", 0.0):
                dedup[key] = result

        return sorted(dedup.values(), key=lambda item: item.get("score", 0.0), reverse=True)[:limit]
