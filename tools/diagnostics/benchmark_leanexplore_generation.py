#!/usr/bin/env python3
"""Benchmark actual LeanExplore informalization prompts without changing a DB.

Read credentials from INF_API_KEY or a hidden terminal prompt. Save per-request
outputs and timing, but never credentials. No retries are used during measurement
so overload remains visible. A successful response is not a correctness proof.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import time

import httpx


def emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * q) - 1)]


def aggregate(rows: list[dict], elapsed: float, concurrency: int, name: str) -> dict:
    valid = [r for r in rows if r["valid"]]
    latency = [r["seconds"] for r in rows]
    result = {
        "stage": name,
        "concurrency": concurrency,
        "requests": len(rows),
        "successful": len(valid),
        "elapsed_seconds": elapsed,
        "successful_per_second": len(valid) / elapsed if elapsed else 0,
        "latency_mean_seconds": statistics.mean(latency) if latency else None,
        "latency_p50_seconds": percentile(latency, 0.5),
        "latency_p95_seconds": percentile(latency, 0.95),
        "http_statuses": dict(Counter(str(r.get("http_status")) for r in rows)),
        "finish_reasons": dict(Counter(str(r.get("finish_reason")) for r in rows)),
        "errors": dict(Counter(e for r in rows for e in r["errors"])),
        "mean_prompt_chars": statistics.mean(r["prompt_chars"] for r in rows),
        "mean_output_chars": statistics.mean(r.get("output_chars", 0) for r in rows),
        "responses_with_reasoning": sum(bool(r.get("reasoning_chars")) for r in rows),
    }
    for field in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        observed = [r[field] for r in valid if isinstance(r.get(field), (int, float))]
        result[f"mean_{field}"] = statistics.mean(observed) if observed else None
        result[f"sum_{field}"] = sum(observed) if observed else None
    result["completion_tokens_per_second"] = (
        result["sum_completion_tokens"] / elapsed
        if result["sum_completion_tokens"] is not None
        else None
    )
    # Large jobs keep the queue full for much longer than these small batches.
    # Also report the window after all workers started and before the queue ran
    # out. Keep the complete-batch metric: either window alone can be noisy.
    if len(rows) > concurrency and all("started_at" in r and "finished_at" in r for r in rows):
        starts = sorted(datetime.fromisoformat(r["started_at"]) for r in rows)
        window_start, window_end = starts[concurrency - 1], starts[-1]
        window_seconds = (window_end - window_start).total_seconds()
        during = [
            r for r in rows
            if window_start < datetime.fromisoformat(r["finished_at"]) <= window_end
        ]
        result["full_queue_window_seconds"] = window_seconds
        result["full_queue_completed"] = len(during)
        result["full_queue_successful"] = sum(r["valid"] for r in during)
        result["full_queue_successful_per_second"] = (
            result["full_queue_successful"] / window_seconds if window_seconds > 0 else None
        )
    return result


async def main(args: argparse.Namespace, key: str) -> None:
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "requests.jsonl").exists():
        raise SystemExit("Output already contains requests.jsonl; use a fresh output directory.")
    sample_path = Path(args.samples)
    samples = [json.loads(line) for line in sample_path.read_text().splitlines() if line.strip()]
    stages = [tuple(map(int, spec.split(":"))) for spec in args.stages.split(",")]
    if any(c < 1 or n < c for c, n in stages):
        raise SystemExit("Each stage needs 1 <= concurrency <= requests.")
    if sum(n for _, n in stages) + args.compare_thinking > len(samples):
        raise SystemExit("Insufficient distinct samples for requested stages.")
    base_url = args.base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    metadata = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "requested_model": args.model,
        "samples": str(sample_path.resolve()),
        "samples_sha256": hashlib.sha256(sample_path.read_bytes()).hexdigest(),
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
        "thinking": args.thinking,
        "timeout_seconds": args.timeout,
        "stages_requested": stages,
        "dataset_updates": False,
        "retries": 0,
        "success_definition": "HTTP 200, nonempty content, basic bold-title format, stop finish, and no raw thinking tag; not semantic validation",
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    max_concurrency = max(c for c, _ in stages)
    timeout = httpx.Timeout(args.timeout, connect=30, pool=30)
    limits = httpx.Limits(max_connections=max_concurrency + 2, max_keepalive_connections=max_concurrency + 2)
    async with httpx.AsyncClient(
        headers=headers, timeout=timeout, limits=limits, trust_env=False, follow_redirects=False
    ) as client:
        models_response = await client.get(f"{base_url}/models")
        model_info = {"http_status": models_response.status_code}
        if models_response.status_code == 200:
            try:
                model_info["ids"] = [entry["id"] for entry in models_response.json().get("data", [])]
            except (ValueError, KeyError, TypeError):
                model_info["error"] = "invalid_models_response"
        else:
            model_info["body_excerpt"] = models_response.text[:500].replace(key, "[REDACTED]")
        emit({"models": model_info})
        (output / "models.json").write_text(json.dumps(model_info, indent=2) + "\n")
        if models_response.status_code in (401, 403):
            raise SystemExit("Endpoint rejected authentication; stopping before generation.")
        model = args.model
        ids = model_info.get("ids", [])
        if ids and model not in ids:
            matches = [item for item in ids if item.lower() == model.lower()]
            if len(matches) == 1:
                model = matches[0]
            elif len(ids) == 1:
                model = ids[0]
            else:
                raise SystemExit("Requested model not found and available model selection is ambiguous.")
        metadata["resolved_model"] = model
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

        async def request(sample: dict, stage: str, thinking: str) -> dict:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": sample["prompt"]}],
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
                "stream": False,
                "thinking": {"type": thinking},
                "chat_template_kwargs": {
                    "thinking": thinking == "enabled",
                    "enable_thinking": thinking == "enabled",
                },
            }
            row = {
                "stage": stage,
                "id": sample["id"],
                "name": sample["name"],
                "module": sample["module"],
                "prompt_chars": len(sample["prompt"]),
                "thinking_requested": thinking,
                "errors": [],
                "valid": False,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            started = time.perf_counter()
            try:
                response = await client.post(f"{base_url}/chat/completions", json=payload)
                row["http_status"] = response.status_code
                if response.status_code != 200:
                    row["errors"].append(f"http_{response.status_code}")
                    row["error_excerpt"] = response.text[:1000].replace(key, "[REDACTED]")
                else:
                    data = response.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                    content = message.get("content") or ""
                    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                    row["finish_reason"] = choice.get("finish_reason")
                    row["response_model"] = data.get("model")
                    row["content"] = content
                    row["output_chars"] = len(content)
                    row["reasoning_chars"] = len(reasoning)
                    usage = data.get("usage") or {}
                    row["usage"] = usage
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        row[field] = usage.get(field)
                    row["reasoning_tokens"] = (usage.get("completion_tokens_details") or {}).get(
                        "reasoning_tokens", usage.get("reasoning_tokens")
                    )
                    if not isinstance(content, str) or not content.strip():
                        row["errors"].append("empty_content")
                    elif not re.match(r"^\s*\*\*.+?\*\*\s*\S", content, re.DOTALL):
                        row["errors"].append("invalid_format")
                    if choice.get("finish_reason") != "stop":
                        row["errors"].append("non_stop_finish")
                    if "<think>" in content or "</think>" in content:
                        row["errors"].append("reasoning_in_content")
                    row["valid"] = not row["errors"]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                row["errors"].append(type(exc).__name__)
                row["error_excerpt"] = str(exc)[:500].replace(key, "[REDACTED]")
            finally:
                row["seconds"] = time.perf_counter() - started
                row["finished_at"] = datetime.now(timezone.utc).isoformat()
            with (output / "requests.jsonl").open("a") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            return row

        summaries = []
        offset = 0
        stop_reason = None
        for concurrency, count in stages:
            stage_name = f"c{concurrency}_n{count}_{offset}"
            selected = samples[offset: offset + count]
            offset += count
            sem = asyncio.Semaphore(concurrency)
            completed = 0

            async def worker(sample: dict) -> dict:
                nonlocal completed
                async with sem:
                    row = await request(sample, stage_name, args.thinking)
                    completed += 1
                    if completed == count or completed % max(concurrency, 8) == 0:
                        emit({"progress": stage_name, "completed": completed, "total": count})
                    return row

            emit({"starting_stage": stage_name, "concurrency": concurrency, "requests": count})
            start = time.perf_counter()
            rows = await asyncio.gather(*(worker(sample) for sample in selected))
            summary = aggregate(rows, time.perf_counter() - start, concurrency, stage_name)
            summaries.append(summary)
            emit(summary)
            (output / "stages.json").write_text(json.dumps(summaries, indent=2) + "\n")
            failure_rate = 1 - summary["successful"] / count
            if failure_rate > args.stop_error_rate:
                stop_reason = f"stage_failed_error_rate_{failure_rate:.3f}"
                break
            if concurrency >= 16 and len(summaries) > 1:
                previous_rate = summaries[-2]["successful_per_second"]
                if summary["successful_per_second"] < previous_rate * args.minimum_gain:
                    stop_reason = "throughput_plateau"
                    break

        if args.compare_thinking:
            other = "enabled" if args.thinking == "disabled" else "disabled"
            selected = samples[offset: offset + args.compare_thinking]
            start = time.perf_counter()
            rows = await asyncio.gather(*(request(sample, "thinking_comparison", other) for sample in selected))
            comparison = aggregate(rows, time.perf_counter() - start, len(selected), "thinking_comparison")
            emit(comparison)
            (output / "thinking-comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")

        healthy = [s for s in summaries if s["successful"] / s["requests"] >= 1 - args.stop_error_rate]
        recommendation = None
        if healthy:
            highest_rate = max(s["successful_per_second"] for s in healthy)
            close = [s for s in healthy if s["successful_per_second"] >= 0.9 * highest_rate]
            selected = min(close, key=lambda s: s["concurrency"])
            raw_hours = args.population / selected["successful_per_second"] / 3600
            recommendation = {
                "concurrency": selected["concurrency"],
                "stage": selected["stage"],
                "measured_successful_per_second": selected["successful_per_second"],
                "population": args.population,
                "raw_hours": raw_hours,
                "hours_with_20_percent_margin": raw_hours * 1.2,
                "hours_with_35_percent_margin": raw_hours * 1.35,
                "caveat": "Small sample; not a guarantee of sustained throughput or mathematical correctness. Excludes embeddings and indexing.",
            }
        result = {"stop_reason": stop_reason, "recommendation": recommendation, "stages": summaries}
        (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        emit({"finished": result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stages", default="1:4,4:16,8:32,16:64,32:128")
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default="disabled")
    parser.add_argument("--compare-thinking", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--population", type=int, required=True,
                        help="Number of descriptions to generate, used to estimate total run time.")
    parser.add_argument("--stop-error-rate", type=float, default=0.05)
    parser.add_argument("--minimum-gain", type=float, default=1.15)
    arguments = parser.parse_args()
    if arguments.population < 1:
        parser.error("--population must be positive")
    api_key = os.environ.get("INF_API_KEY") or getpass.getpass("INF_API_KEY (hidden): ")
    if not api_key:
        raise SystemExit("Missing INF_API_KEY")
    asyncio.run(main(arguments, api_key))
