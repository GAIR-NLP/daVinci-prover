"""Fresh, source-bound development checks for local Lean import closures."""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any

from dvp.verification.comparator import lean_search_path, run_process
from dvp.verification.lean import mask_lean_comments_and_strings
from dvp.verification.modules import CandidateBundle


def dependency_placeholders(bundle: CandidateBundle) -> dict[str, Any]:
    """Report source holes even when compilation fails before elaborating them."""
    items = []
    total = 0
    for module in bundle.modules:
        masked = mask_lean_comments_and_strings(module.source)
        matches = list(re.finditer(r"(?<![\w'])(?:sorry|admit)(?![\w'])", masked))
        if not matches:
            continue
        ranges = []
        for match in matches[:3]:
            def position(offset: int) -> dict[str, int]:
                return {"line": masked.count("\n", 0, offset) + 1,
                        "column": offset - (masked.rfind("\n", 0, offset) + 1)}
            ranges.append({"kind": match.group(), "range": {
                "start": position(match.start()), "end": position(match.end()),
            }})
        items.append({"module": module.name, "file_path": str(module.relative_path),
                      "total": len(matches), "omitted": max(0, len(matches) - 3), "items": ranges})
        total += len(matches)
    return {"total": total, "items": items}


async def verify_bundle(
    verifier: Any, bundle: CandidateBundle, *, entry_path: Path,
    deadline: float, max_output_chars: int,
) -> dict[str, Any]:
    """Compile only the immutable snapshot, with one deadline for all modules."""
    evidence = {
        "source_bundle_sha256": bundle.sha256,
        "source_bundle_manifest": bundle.manifest(),
        "dependency_placeholders": dependency_placeholders(bundle),
    }
    with tempfile.TemporaryDirectory(prefix="dvp-modules-") as temporary:
        folder = Path(temporary)
        folder.chmod(0o755)
        source, build = folder / "source", folder / "build"
        source.mkdir(mode=0o755)
        build.mkdir(mode=0o755)
        build.chmod(0o755)
        if os.geteuid() == 0:
            os.chown(build, 65534, 65534)
        paths: dict[str, str] = {}
        for module in bundle.modules:
            path = source / module.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            parent = path.parent
            while parent != source:
                parent.chmod(0o755)
                parent = parent.parent
            path.write_text(module.source, encoding="utf-8")
            path.chmod(0o444)
            paths[str(path)] = str(verifier.config.module_root / module.relative_path)
            destination = (build / module.relative_path).with_suffix(".olean")
            destination.parent.mkdir(parents=True, exist_ok=True)
            parent = destination.parent
            while parent != build:
                parent.chmod(0o755)
                if os.geteuid() == 0:
                    os.chown(parent, 65534, 65534)
                parent = parent.parent
        entry = source / "__DvpEntry.lean"
        entry.write_text(bundle.entry_code, encoding="utf-8")
        entry.chmod(0o444)
        paths[str(entry)] = str(entry_path)
        helper = source / "__DvpDraft.lean"
        if verifier.config.allow_sorry:
            helper.write_text((Path(__file__).with_name("resources") / "LocalDraftVerifier.lean").read_text(), encoding="utf-8")
            helper.chmod(0o444)

        def remap(value: str) -> str:
            for temporary_path, original in paths.items():
                value = value.replace(temporary_path, original)
            return value

        outputs: list[str] = []
        warnings: list[str] = []
        compiled: list[str] = []

        def finish(result: dict[str, Any]) -> dict[str, Any]:
            entry_output = result.get("output", "")
            # Inspection commands run after the saved entry and all helpers.
            # Keep their output visible even when dependencies emit enough
            # warnings to exhaust the display budget. Diagnostics also remain
            # available in the structured errors/warnings fields.
            parts = ([entry_output, *outputs] if bundle.allow_entrypoint_import
                     else [*outputs, entry_output])
            output = "\n".join(parts).strip()
            if len(output) > max_output_chars:
                output = output[:max_output_chars] + "\n...<truncated>..."
            return {**result, **evidence, "compiled_modules": list(compiled), "output": output,
                    "warnings": [*warnings, *result.get("warnings", [])]}

        try:
            environment = {"LEAN_PATH": os.pathsep.join((str(build), lean_search_path(verifier._working_dir().resolve())))}
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            return finish({**verifier._module_error(f"DVP_SANDBOX_ERROR: {exc}"),
                           "_harness_infrastructure_error": True})

        async def check(command: list[str], *, draft: bool = False) -> dict[str, Any]:
            try:
                wrapped = verifier._sandbox_command(
                    command, read_only=(source,), writable=(build,), environment=environment,
                )
            except (OSError, ValueError, RuntimeError, KeyError) as exc:
                return {**verifier._module_error(f"DVP_SANDBOX_ERROR: {exc}"),
                        "_harness_infrastructure_error": True}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return verifier._timeout_result(command)
            captured = await run_process(wrapped, cwd=verifier._working_dir(), timeout=remaining)
            if captured is None:
                return verifier._timeout_result(command)
            status, stdout, stderr = captured
            stdout, stderr = remap(stdout), remap(stderr)
            if "DVP_SANDBOX_ERROR:" in stderr:
                return {"success": False, "errors": [stderr.strip()], "warnings": [],
                        "output": stdout + stderr, "command": command, "returncode": status,
                        "_harness_infrastructure_error": True}
            parser = verifier._local_draft_result if draft else verifier._local_compile_result
            return parser(stdout, stderr, returncode=status, command=command, max_output_chars=max_output_chars)

        for module in bundle.modules:
            path = source / module.relative_path
            destination = (build / module.relative_path).with_suffix(".olean")
            result = await check(["lean", "-R", str(source), "-o", str(destination), str(path)])
            if not result["success"]:
                return finish({**result, "failed_module": module.name})
            compiled.append(module.name)
            outputs.append(result.get("output", ""))
            warnings.extend(result.get("warnings", []))
        command = (["lean", "--run", str(helper), str(entry)] if verifier.config.allow_sorry
                   else ["lean", "-R", str(source), str(entry)])
        return finish(await check(command, draft=verifier.config.allow_sorry))
