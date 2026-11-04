# Python Experiments

[Back to documentation](README.md)

The `python_execute` tool is enabled by default in `dvp prove` and `dvp eval`, and runs locally beside DVP using Landlock + seccomp.
It supports the Python standard library, **NumPy**, **SymPy**, and **mpmath**.

## Enable

The [full installation](installation.md) includes all three math packages. To add them to an existing DVP environment, install them and check the sandbox:

```bash
sudo apt-get install -y libseccomp2 libstdc++6 libgcc-s1 zlib1g
python -m pip install -e '.[python-experiments]'
python -m dvp.cli.check_python
```

Linux x86_64 and aarch64 are supported. The kernel and container must permit
Landlock and seccomp; root or user/mount namespaces are not required.
A failed check stops startup without falling back to unrestricted Python.
Run the check on each deployment machine.

These are the default options for `dvp prove` and `dvp eval`:

```bash
--python-execute true \
--python-timeout 30 \
--python-memory-mb 1024 \
--python-max-output-bytes 12000
```

Use `--python-execute false` only when you explicitly want a run without Python.
A missing package or failed sandbox check does not disable the tool automatically.
Use a new run directory after changing the executor, allowed packages, or limits.

## What the model can use

| Capability | Behavior |
| --- | --- |
| Libraries | Standard library, NumPy, SymPy, mpmath; other installed packages are unavailable |
| State | Fresh interpreter per call; Python variables do not persist |
| Files | Read/write within `Submission/experiments/`; files persist for the attempt |
| Runtime files | Read-only access to approved Python/package roots and required native libraries |
| Network, subprocesses, threads | Blocked; BLAS uses one thread |
| Host data | No granted access to other tasks, historical runs, or credentials; inherited environment and descriptors are cleared |

Example tool arguments:

```json
{"code": "import sympy as sp\nx = sp.symbols('x')\nprint(sp.factor(x**2 - 1))", "timeout": 5}
```

Only `code` and optional `timeout` are accepted. Code is limited to 65,536 UTF-8
bytes; the requested timeout cannot exceed the configured maximum. The model
cannot choose the interpreter or working directory. File tools can read saved
experiments via `Submission/experiments/...`.

Results include `success`, `returncode`, `stdout`, `stderr`, `timed_out`, and
`output_truncated`. Success means the program exited successfully and contributes
no proof acceptance evidence. Output overflow returns `tool_output_limit` with
partial output; it does not consume model-output truncation retries. Timeout
terminates the worker and returns captured output. Cancellation cleans up the worker.

The worker bounds wall/CPU time, memory, output, file size (16 MiB per file), and
open descriptors (64). It has no aggregate disk quota and does not hide all host
path metadata. Package functions requiring external programs, threads, or extra
dependencies are unavailable.

**This isolates Python only.** Lean, `#eval`, and Comparator use their separately
configured [execution mode](verification.md).

## Programmatic use

Pass `LocalPythonExecutor()` from `dvp.runtime.python_local` as
`RuntimeConfig(python_executor=...)`. Custom executors implement the
[`PythonExecutor` protocol](../src/dvp/runtime/python_execution.py) and must provide
equivalent isolation, bounded output, preflight, and cleanup.

Live sandbox checks are covered by `tests/runtime/test_python_local.py` and
`tests/runtime/test_python_math.py`.
