# Python experiments

Run DVP, Lean, and Python on the **same machine**. The optional `python_execute`
tool uses a bundled local Landlock + seccomp worker; no Python server, port,
remote connection, or separately written adapter is required.

## Enable

Install the optional math packages in your DVP Python environment. Enabling the
local Python worker requires all three: NumPy, SymPy, and mpmath. On Linux,
install the seccomp runtime library if needed (supported architectures: x86_64
and aarch64):

```bash
sudo apt-get install -y libseccomp2 libstdc++6 libgcc-s1 zlib1g
python -m pip install -e '.[python-experiments]'
python -m dvp.cli.check_python
```

The check runs synthetic experiments and verifies the sandbox before any model
request. Landlock must be available in the running kernel and permitted by the
container configuration. No root privileges or mount/user namespaces are needed
by this Python worker. A failed check stops startup; it never falls back to
unrestricted Python.

Add the following to your existing `dvp eval`, `dvp prove`, or
`python -m dvp.cli.evaluate` command:

```bash
--python-execute true
```

Optional limits:

```bash
--python-timeout 30 \
--python-memory-mb 1024 \
--python-max-output-bytes 12000
```

Python remains disabled by default; no additional CLI flag is needed for the
math packages. Evaluation manifests record the enabled executor's identity and
limits; changing them requires a new run rather than resuming an existing one.
The math-package sandbox uses runtime policy v2, so start a new run after
upgrading from the standard-library-only worker. Its identity fingerprints the
worker, supervisor, and interpreter, Python/kernel versions, and the allowed
packages' versions, paths, and metadata. It does not hash the entire OS or every
file in the standard library and installed packages.

## What the model can use

The worker provides the **Python standard library** (for example `math`,
`itertools`, `collections`, `fractions`, `decimal`, and `random`) plus only these
third-party packages:

- **NumPy** for vectorized arithmetic and linear algebra.
- **SymPy** for symbolic expressions, simplification, and algebra.
- **mpmath** for high-precision numerical calculations.

Other installed packages, networking, subprocesses, and threads remain
unavailable. BLAS runs with one thread. These capabilities do not imply support
for every package function: operations requiring compilation in a subprocess,
external programs, or additional packages are unavailable. The tool description
tells the model the package allowlist and execution limits.

Example tool arguments:

```json
{"code": "import numpy as np\nimport sympy as sp\nimport mpmath as mp\nprint(np.linalg.solve(np.array([[2., 1.], [1., 3.]]), np.array([1., 2.])))\nx = sp.symbols('x')\nprint(sp.factor(x**2 - 1))\nmp.mp.dps = 50\nprint(mp.sqrt(2))", "timeout": 5}
```

Only `code` and an optional `timeout` are accepted. Code is limited to 65,536
UTF-8 bytes; the timeout cannot exceed the operator's configured maximum. The
model cannot choose the interpreter, environment, or working directory.

Each call starts a fresh interpreter. Files persist within the current attempt
at `<attempt>/workspaces/Submission/experiments/` and can be read by the file
tools using `Submission/experiments/...`; Python variables do not persist.
The worker permits reads and writes in this directory and read-only access to
approved standard-library runtime files and the allowlisted package roots and
their required native libraries. It does not grant access to arbitrary
`site-packages` directories or load Python `.pth` files. Other task files,
historical runs, and host credentials are not granted access. Environment
variables and inherited file descriptors are cleared before execution; the
worker supplies the fixed settings needed to keep numerical libraries on one
thread.

Landlock enforces file access and seccomp limits system calls. The worker blocks
creation of processes and threads, so timeout, output overflow, or cancellation
can terminate and reap the entire computation by killing its single process.
Capture is bounded while running; wall time, CPU time, memory, file size, and
open descriptors also have limits. Files are capped at 16 MiB each and the
process at 64 open descriptors; this does not impose an aggregate disk quota
across files or calls. This is an experiment worker, not a general
shell or a virtual machine; file metadata visibility is not a confidentiality
guarantee for host path names.

Results contain `success`, `returncode`, `stdout`, `stderr`, `timed_out`, and
`output_truncated`. Python success means the program exited successfully. It
consumes an ordinary tool call, contributes no Lean verification evidence, and
does not replace the final Comparator acceptance check. Program output is
always treated as text, even if it resembles an acceptance result.

Output overflow returns `error_kind: "tool_output_limit"` with partial console
output. It stays a normal tool result and does not consume the model's output
truncation retries or discard its assistant history.

On timeout, the local executor kills and reaps the worker and returns output
already captured within the byte limit. Standard output and standard error are
unbuffered, so ordinary progress prints can survive a timeout. The tool's fallback
watchdog allows two additional seconds for cleanup and result delivery; the
worker still receives and enforces the original execution limit. External task
cancellation propagates after cleanup rather than becoming a Python timeout.

**This option isolates Python only.** Lean compilation, `#eval`, and Comparator
continue to use their separately configured execution policies. Enabling this
option does not make an unsandboxed Lean deployment isolated.

## Programmatic use

Pass `LocalPythonExecutor()` from `dvp.runtime.python_local` as
`RuntimeConfig(python_executor=...)`. Custom deployments can still implement the
[`PythonExecutor` protocol](../src/dvp/runtime/python_execution.py); they are
responsible for equivalent isolation, bounded capture, preflight, and cleanup.

The live tests in `tests/runtime/test_python_local.py` and
`tests/runtime/test_python_math.py` exercise real restricted processes using
synthetic inputs. They cover numerical and symbolic calculations, permitted task writes,
denied cross-task reads and writes, symlink escapes, environment/FD leakage,
network/process denial, output bounds, timeout, and cancellation. Run the
startup check again on each deployment machine; a successful check on one host
does not establish another host's kernel capabilities.
