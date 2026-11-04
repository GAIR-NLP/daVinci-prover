#!/usr/bin/env python3
"""Wait for a successful description run, build vectors/indexes, and accept it.

This supervisor needs no credentials, never reads another process's environment,
and never activates a LeanExplore profile. All expensive stages run in fresh
subprocesses. Failed generation or a failed downstream stage stops the chain.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import traceback

from build_leanexplore_embeddings import DEFAULT_MANIFEST, DEFAULT_MODEL

ROOT = Path(__file__).resolve().parents[2]


def now():
    return datetime.now(timezone.utc).isoformat()


def load(path):
    return json.loads(path.read_text())


def save(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def process_identity(pid):
    """Read only public process state and start ticks; never /proc/*/environ."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    # The parenthesized command may contain spaces/parentheses.
    fields = stat[stat.rfind(")") + 2:].split()
    if fields[0] in {"Z", "X"}:
        return None
    return int(fields[19])


def stage_environment(threads):
    # API credentials and proxy settings are unnecessary for offline stages.
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "LD_LIBRARY_PATH")
        if key in os.environ
    }
    environment.update(
        PYTHONPATH=str(ROOT / "src"),
        OMP_NUM_THREADS=str(threads), MKL_NUM_THREADS=str(threads),
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
        PYTHONUNBUFFERED="1",
    )
    return environment


def verify_generation_inputs(args):
    manifest = load(args.generation / "manifest.json")
    if manifest["pid"] != args.generation_pid:
        raise ValueError("Generation PID does not match generation/manifest.json.")
    if Path(manifest["database"]).resolve() != args.snapshot / "lean_explore.db":
        raise ValueError("Generation database does not match the requested snapshot.")
    return manifest


def wait_for_generation(args, status, publish):
    report_path = args.generation / "report.json"
    initial_identity = process_identity(args.generation_pid)
    status["generation_process"] = {
        "pid": args.generation_pid, "initial_start_ticks": initial_identity,
    }
    while True:
        report = load(report_path) if report_path.exists() else None
        identity = process_identity(args.generation_pid)
        process_alive = identity is not None and (
            initial_identity is None or identity == initial_identity
        )
        status["generation_process"]["alive"] = process_alive
        status["generation_report_state"] = report.get("state") if report else None
        if report and report.get("pid") != args.generation_pid:
            raise RuntimeError("Generation result belongs to a different PID.")
        if report and report.get("state") == "failed":
            raise RuntimeError(f"Description generation failed; see {report_path}.")
        if not process_alive:
            if not report or report.get("state") != "completed":
                raise RuntimeError("Generation process exited without a completed report.")
            counts = report["final_database"]["counts"]
            if counts.get("missing_descriptions", 0) or counts.get("empty_descriptions", 0):
                raise RuntimeError("Completed generation report still has missing/empty descriptions.")
            publish()
            return report
        publish()
        time.sleep(args.poll_seconds)


def run_stage(args, name, arguments, status, publish):
    log_path = args.run_directory / f"{name}.log"
    stage = {
        "name": name, "state": "running", "started_at": now(),
        "command": list(map(str, arguments)), "log": str(log_path),
    }
    status["stages"].append(stage)
    status["phase"] = name
    start = time.monotonic()
    with log_path.open("ab", buffering=0) as log:
        log.write(f"\n=== {now()} {name} ===\n".encode())
        child = subprocess.Popen(
            list(map(str, arguments)), cwd=ROOT,
            env=stage_environment(args.threads),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        stage["pid"] = child.pid
        publish()
        while True:
            try:
                returncode = child.wait(timeout=args.poll_seconds)
                break
            except subprocess.TimeoutExpired:
                stage["elapsed_seconds"] = time.monotonic() - start
                publish()
        stage.update(
            returncode=returncode, finished_at=now(),
            elapsed_seconds=time.monotonic() - start,
            state="completed" if returncode == 0 else "failed",
        )
        publish()
    if returncode:
        raise RuntimeError(f"{name} exited with code {returncode}; see {log_path}.")


def update_manifest(args, generation, generation_manifest):
    database = args.snapshot / "lean_explore.db"
    embedding = load(args.snapshot / "embedding-build-report.json")
    indexes = load(args.snapshot / "index-build-report.json")
    acceptance = load(args.run_directory / "final.json")
    initial = load(args.generation / "initial-database.json")["counts"]
    if embedding.get("status") != "complete":
        raise RuntimeError("Embedding report is incomplete.")
    if indexes.get("status") != "complete_semantic_index":
        raise RuntimeError("Index report is incomplete.")
    if acceptance.get("status") != "passed" or acceptance.get("full_semantic_ready") is not True:
        raise RuntimeError("Snapshot acceptance did not pass.")
    if acceptance.get("profile_activated") or acceptance.get("network_attempts"):
        raise RuntimeError("Acceptance changed a profile or attempted network access.")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        counts = dict(connection.execute("""
            SELECT COUNT(*) AS declarations,
                   COUNT(informalization) AS descriptions,
                   COUNT(informalization_embedding) AS vectors,
                   SUM(informalization IS NULL OR trim(informalization) = '') AS pending_llm,
                   SUM(informalization_embedding IS NULL) AS pending_vectors
            FROM declarations
        """).fetchone())
        by_module = [dict(row) for row in connection.execute("""
            SELECT CASE WHEN instr(module, '.') > 0
                        THEN substr(module, 1, instr(module, '.') - 1)
                        ELSE module END AS module_root,
                   COUNT(*) AS declarations, COUNT(informalization) AS descriptions,
                   COUNT(informalization_embedding) AS vectors
            FROM declarations GROUP BY module_root ORDER BY module_root
        """)]
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    if integrity != "ok" or counts["pending_llm"] or counts["pending_vectors"]:
        raise RuntimeError("Final database is incomplete or failed integrity checking.")
    database_sha = sha256(database)
    if database_sha != indexes["database_sha256"]:
        raise RuntimeError("Database changed since index construction/acceptance.")
    if counts["declarations"] != embedding["after"]["declarations"]:
        raise RuntimeError("Embedding report count does not match final database.")
    if any(value != counts["declarations"] for value in (
        counts["descriptions"], counts["vectors"],
        indexes["validation"]["faiss_vectors"],
        acceptance["indexes"]["bm25_documents"], acceptance["indexes"]["faiss_vectors"],
    )):
        raise RuntimeError("Final reports disagree on full database/index coverage.")
    target = args.snapshot / "build-manifest.json"
    manifest = load(target)
    provenance = args.snapshot / "provenance"
    provenance.mkdir(exist_ok=True)
    save(provenance / "generation-manifest.json", generation_manifest)
    save(provenance / "generation-report.json", generation)
    save(provenance / "acceptance-report.json", acceptance)
    # Keep the original extraction input references usable after moving the
    # snapshot directory. These are small audit artifacts, not database copies.
    for key, value in manifest.get("completion_inputs", {}).items():
        source = Path(value)
        if not source.is_absolute():
            source = args.snapshot / source
        source = source.resolve(strict=True)
        relative = Path("provenance") / f"initial-{key.replace('_', '-')}{source.suffix}"
        destination = args.snapshot / relative
        if source != destination:
            temporary = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
        manifest["completion_inputs"][key] = str(relative)
    manifest.setdefault("initial_partial_snapshot", json.loads(json.dumps({
        key: manifest.get(key)
        for key in ("status", "generated_at", "database", "counts", "indexes", "validation")
    })))
    observed_cache = embedding.get("official_cache_application", {})
    cache_hits = observed_cache.get("cache_hits")
    generated_vectors = observed_cache.get("generation_required")
    new_descriptions = counts["descriptions"] - initial.get("descriptions", 0)
    counts.update(
        reused_descriptions=initial.get("descriptions", 0),
        new_llm_descriptions=new_descriptions,
        retained_vectors=initial.get("vectors", 0),
        reused_vectors=(initial.get("vectors", 0) + cache_hits) if cache_hits is not None else None,
        embedding_cache_hits=cache_hits,
        new_embedding_vectors=generated_vectors,
        semantic_coverage_fraction=counts["vectors"] / counts["declarations"],
        by_module_root=by_module,
    )
    manifest.update(
        status="complete", completed_at=now(), full_semantic_ready=True,
        active_profile_changed=False,
        llm_calls=generation.get("http", {}).get("requests"),
        llm_call_count_semantics="HTTP request attempts recorded by the official generation adapter, including retries.",
        database={
            "filename": database.name, "bytes": database.stat().st_size,
            "sha256": database_sha, "quick_check": integrity,
            "source_fields_sha256": generation["final_database"]["source_fields_sha256"],
        },
        counts=counts,
        indexes={
            "bm25_documents": acceptance["indexes"]["bm25_documents"],
            "faiss_vectors": acceptance["indexes"]["faiss_vectors"],
            "build_report": "index-build-report.json",
            "status": indexes["status"],
        },
        enrichment={
            "description_report": "provenance/generation-report.json",
            "description_manifest": "provenance/generation-manifest.json",
            "embedding_report": "embedding-build-report.json",
            "new_descriptions": new_descriptions,
            "new_vectors_generated": generated_vectors,
            "vectors_added_from_exact_text_cache": cache_hits,
            "model": generation_manifest["model"],
            "endpoints": generation_manifest["endpoints"],
            "concurrency_per_endpoint": generation_manifest["concurrency_per_endpoint"],
            "generation_parameters": generation_manifest["generation_parameters"],
            "http": generation.get("http", {}),
            "description_elapsed_seconds": generation.get("elapsed_seconds"),
            "embedding_elapsed_seconds": embedding.get("elapsed_seconds"),
        },
        completion_report="provenance/completion-report.json",
        next_steps=[],
    )
    # The former zero described the pre-enrichment snapshot; do not invent an
    # encode-call count from a vector count or a configured batch size.
    manifest.pop("new_embedding_generation_calls", None)
    validation = manifest.setdefault("validation", {})
    validation.pop("optional_context_unavailable_in_smoke", None)
    validation.update(
        final_acceptance="provenance/acceptance-report.json",
        full_semantic_ready=True, network_attempts=len(acceptance["network_attempts"]),
        dvp_offline_requested_queries=[
            {"query": item["query"], **item["summary"]}
            for item in acceptance.get("queries", [])
        ],
        proof_acceptance=acceptance.get("proof_acceptance"),
        scope="Source/toolchain alignment, complete vectors/indexes, and offline exact-name/natural-language retrieval smoke checks; no semantic quality benchmark.",
    )
    save(target, manifest)
    return {
        "manifest": str(target), "database_sha256": database_sha,
        "counts": counts, "full_semantic_ready": True, "active_profile_changed": False,
    }


def supervise(args):
    status = {
        "state": "running", "phase": "waiting_for_generation", "pid": os.getpid(),
        "started_at": now(), "updated_at": now(), "stages": [],
        "snapshot": str(args.snapshot), "generation": str(args.generation),
        "profile_activated": False,
    }

    def publish():
        status["updated_at"] = now()
        save(args.run_directory / "completion-status.json", status)

    try:
        generation_manifest = verify_generation_inputs(args)
        generation = wait_for_generation(args, status, publish)
        python = args.python
        run_stage(args, "embeddings", [
            python, ROOT / "tools/leanexplore/build_leanexplore_embeddings.py",
            "--database", args.snapshot / "lean_explore.db",
            "--cache-database", generation_manifest["cache_database"],
            "--extractor-root", args.extractor_root,
            "--model", args.embedding_model,
            "--model-manifest", args.embedding_model_manifest,
            "--threads", str(args.threads),
        ], status, publish)
        run_stage(args, "indexes", [
            python, ROOT / "tools/leanexplore/build_leanexplore_indices.py",
            "--output-dir", args.snapshot, "--threads", str(args.threads),
            "--extractor-root", args.extractor_root,
        ], status, publish)
        run_stage(args, "acceptance", [
            python, args.acceptance_script,
            "--snapshot", args.snapshot, "--output", args.run_directory / "final.json",
        ], status, publish)
        status["phase"] = "updating_manifest"
        publish()
        status["result"] = update_manifest(args, generation, generation_manifest)
        status.update(state="completed", phase="completed", finished_at=now())
        publish()
        save(args.run_directory / "completion-report.json", status)
        save(args.snapshot / "provenance/completion-report.json", status)
    except BaseException as error:
        status.update(
            state="failed", failed_phase=status["phase"], finished_at=now(),
            error_type=type(error).__name__, error=str(error),
        )
        publish()
        save(args.run_directory / "completion-report.json", status)
        if (args.snapshot / "provenance").is_dir():
            save(args.snapshot / "provenance/completion-report.json", status)
        traceback.print_exc()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument("--generation", type=Path)
    parser.add_argument("--generation-pid", type=int, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--acceptance-script", type=Path, required=True,
                        help="Acceptance checker accepting --snapshot and --output; its JSON report must match update_manifest's contract.")
    parser.add_argument("--extractor-root", type=Path, required=True,
                        help="Clean LeanExplore source checkout at the pinned extractor commit.")
    parser.add_argument("--embedding-model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--embedding-model-manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--threads", type=int, default=16)
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--detach", action="store_true")
    args = parser.parse_args()
    if args.threads < 1 or args.generation_pid < 1 or not 1 <= args.poll_seconds <= 60:
        parser.error("Positive threads/PID and poll-seconds between 1 and 60 are required.")
    args.run_directory = args.run_directory.resolve(strict=True)
    args.generation = (args.generation or args.run_directory / "generation").resolve(strict=True)
    args.snapshot = args.snapshot.resolve(strict=True)
    args.python = args.python.absolute()
    if not args.python.is_file():
        parser.error("Python executable does not exist.")
    args.acceptance_script = args.acceptance_script.resolve(strict=True)
    args.extractor_root = args.extractor_root.resolve(strict=True)
    args.embedding_model = args.embedding_model.resolve(strict=True)
    args.embedding_model_manifest = args.embedding_model_manifest.resolve(strict=True)
    verify_generation_inputs(args)
    # Hold an inherited kernel lock across fork; a second watcher cannot start
    # an overlapping enrichment chain against this run.
    lock = (args.run_directory / "completion.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    existing = args.run_directory / "completion-report.json"
    if existing.exists() and load(existing).get("state") == "completed":
        parser.error("This completion run has already succeeded.")
    if args.detach:
        child = os.fork()
        if child:
            record = {"pid": child, "started_at": now(), "generation_pid": args.generation_pid}
            save(args.run_directory / "completion-launcher.json", record)
            print(json.dumps(record), flush=True)
            return
        os.setsid()
        with open(os.devnull, "rb") as null, (args.run_directory / "completion-console.log").open("ab", buffering=0) as log:
            os.dup2(null.fileno(), 0)
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))
    lock.flush()
    supervise(args)


if __name__ == "__main__":
    main()
