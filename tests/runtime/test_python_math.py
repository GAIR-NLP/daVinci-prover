"""Exercise approved math packages inside the real Landlock/seccomp worker.

All experiment inputs and cross-task sentinels are synthetic. These tests do
not read evaluation datasets, historical proofs, or host credentials.
"""
from __future__ import annotations

import asyncio
import ctypes
import errno
from importlib import metadata
import json
import platform

import pytest

from dvp.runtime.python_execution import PythonExecutionRequest
from dvp.runtime.python_local import LocalPythonExecutor


_MATH_PACKAGES = {"numpy", "sympy", "mpmath"}


def _require_landlock() -> None:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        pytest.skip("The local Python executor requires Linux x86_64/aarch64")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(444, 0, 0, 1) < 0:
        error = ctypes.get_errno()
        if error in {errno.ENOSYS, errno.EOPNOTSUPP, errno.EPERM, errno.EACCES}:
            pytest.skip(f"Landlock is unavailable in this deployment (errno {error})")
        raise OSError(error, "Landlock ABI query failed")
    try:
        ctypes.CDLL("libseccomp.so.2")
    except OSError:
        pytest.skip("libseccomp.so.2 is unavailable in this deployment")


@pytest.fixture(scope="module")
def math_runner():
    _require_landlock()
    for name in sorted(_MATH_PACKAGES):
        try:
            metadata.version(name)
        except metadata.PackageNotFoundError:
            pytest.skip(f"The local math tests require installed {name}")
    executor = LocalPythonExecutor(timeout=15.0, max_output_bytes=8192)
    # Isolation or package-loading failures on a supported host must fail.
    asyncio.run(executor.preflight())
    return executor


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "task" / "experiments"
    path.mkdir(parents=True)
    return path


def _run(runner, workspace, code):
    return asyncio.run(runner.execute(PythonExecutionRequest(
        code=code, workspace=workspace, timeout=10.0,
        max_output_bytes=runner.max_output_bytes,
    )))


def _assert_success(result):
    assert result.status == "completed", result
    assert result.returncode == 0, result


def test_identity_records_the_approved_package_versions(math_runner):
    packages = math_runner.identity["packages"]
    assert isinstance(packages, list)
    assert len(packages) == len(_MATH_PACKAGES)
    versions = {item["name"]: item["version"] for item in packages}
    assert set(versions) == _MATH_PACKAGES
    assert versions == {name: metadata.version(name) for name in _MATH_PACKAGES}
    # Callers must not be able to alter the recorded execution identity.
    packages[0]["version"] = "changed-by-caller"
    assert all(item["version"] != "changed-by-caller"
               for item in math_runner.identity["packages"])


def test_math_packages_import_lazily_under_isolation(math_runner, workspace):
    result = _run(math_runner, workspace, """
import os, sys
assert sys.flags.isolated == 1
assert sys.flags.no_site == 1
assert not {"numpy", "sympy", "mpmath"}.intersection(sys.modules)
assert "sitecustomize" not in sys.modules
for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    assert os.environ[name] == "1", (name, os.environ.get(name))
import numpy, sympy, mpmath
assert int(numpy.dot([2, 3], [5, 7])) == 31
assert sympy.factorint(360) == {2: 3, 3: 2, 5: 1}
assert mpmath.sqrt(4) == 2
print("MATH READY")
""")
    _assert_success(result)
    assert result.stdout.strip() == "MATH READY"


def test_numpy_linear_algebra_and_persistent_numerical_files(math_runner, workspace):
    first = _run(math_runner, workspace, """
import numpy as np
a = np.array([[3.0, 1.0], [1.0, 2.0]])
assert np.allclose(a @ np.array([2.0, 3.0]), [9.0, 8.0])
assert np.allclose(np.linalg.solve(a, [9.0, 8.0]), [2.0, 3.0])
assert np.allclose(np.linalg.eigvalsh(a), [(5-np.sqrt(5))/2, (5+np.sqrt(5))/2])
u, s, vh = np.linalg.svd(a)
assert np.allclose((u * s) @ vh, a)
np.save("matrix.npy", a)
np.savez_compressed("bundle.npz", matrix=a, solution=np.array([2.0, 3.0]))
np.savetxt("matrix.csv", a, delimiter=",")
a.tofile("matrix.bin")
print("WRITTEN")
""")
    _assert_success(first)
    assert first.stdout.strip() == "WRITTEN"
    assert all((workspace / name).is_file() for name in (
        "matrix.npy", "bundle.npz", "matrix.csv", "matrix.bin",
    ))
    second = _run(math_runner, workspace, """
import numpy as np
expected = np.array([[3.0, 1.0], [1.0, 2.0]])
assert np.array_equal(np.load("matrix.npy", allow_pickle=False), expected)
assert np.array_equal(np.loadtxt("matrix.csv", delimiter=","), expected)
assert np.array_equal(np.fromfile("matrix.bin").reshape(2, 2), expected)
with np.load("bundle.npz", allow_pickle=False) as saved:
    assert np.array_equal(saved["matrix"], expected)
    assert np.array_equal(saved["solution"], [2.0, 3.0])
print("READ BACK")
""")
    _assert_success(second)
    assert second.stdout.strip() == "READ BACK"


def test_sympy_symbolic_work_and_numpy_lambdify(math_runner, workspace):
    result = _run(math_runner, workspace, """
import numpy as np
import sympy as sp
x = sp.symbols("x")
assert sp.factor(x**4 - 1) == (x - 1)*(x + 1)*(x**2 + 1)
assert sp.solve(x**2 - 5*x + 6, x) == [2, 3]
assert sp.integrate(x**2, (x, 0, 3)) == 9
assert sp.integrate(sp.sin(x), (x, 0, sp.pi)) == 2
f = sp.lambdify(x, sp.sin(x)**2 + sp.cos(x)**2 + x**2, modules="numpy")
points = np.array([0.0, 0.5, 1.0, 2.0])
assert np.allclose(f(points), 1 + points**2)
print("SYMBOLIC OK")
""")
    _assert_success(result)
    assert result.stdout.strip() == "SYMBOLIC OK"


def test_mpmath_high_precision_quadrature(math_runner, workspace):
    result = _run(math_runner, workspace, """
import mpmath as mp
mp.mp.dps = 70
integral = mp.quad(lambda x: 4 / (1 + x*x), [0, 1])
assert abs(integral - mp.pi) < mp.mpf("1e-65")
root = mp.findroot(lambda x: x*x - 2, (1, 2))
assert abs(root*root - 2) < mp.mpf("1e-65")
print("PRECISION OK")
""")
    _assert_success(result)
    assert result.stdout.strip() == "PRECISION OK"


@pytest.mark.parametrize("operation", [
    "np.fromfile(path, dtype=np.uint8)",
    "np.save(path, np.array([99.0]))",
])
@pytest.mark.parametrize("via_symlink", [False, True], ids=["sibling", "symlink"])
def test_numpy_cannot_read_or_overwrite_a_sibling_task(
    math_runner, workspace, tmp_path, operation, via_symlink,
):
    sibling = tmp_path / "other_task"
    sibling.mkdir()
    outside = sibling / "private.npy"
    payload = b"synthetic numerical cross-task sentinel"
    outside.write_bytes(payload)
    path = outside
    if via_symlink:
        path = workspace / "escape.npy"
        path.symlink_to(outside)
    result = _run(math_runner, workspace, f"""
import errno
import numpy as np
path = {str(path)!r}
try:
    {operation}
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES), error
    print("DENIED")
else:
    raise AssertionError("NumPy accessed a different task")
""")
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"
    assert outside.read_bytes() == payload


@pytest.mark.parametrize("name", ["scipy", "requests"])
def test_unapproved_installed_packages_are_unavailable(math_runner, workspace, name):
    try:
        installed = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        pytest.skip(f"This negative import check requires installed {name}")
    source = installed.locate_file(f"{name}/__init__.py").resolve()
    assert source.is_file(), source
    result = _run(math_runner, workspace, f"""
import importlib
from pathlib import Path
import numpy, sympy, mpmath
try:
    importlib.import_module({name!r})
except (ImportError, PermissionError):
    pass
else:
    raise AssertionError("unapproved package was importable")
# The package's installation directory must not become broadly readable just
# because it is a sibling of an approved math package.
try:
    Path({str(source)!r}).read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError("unapproved sibling package was readable")
print("UNAVAILABLE")
""")
    _assert_success(result)
    assert result.stdout.strip() == "UNAVAILABLE"


@pytest.mark.parametrize("operation", [
    "socket.socket()",
    "socket.socketpair()",
    "os.fork()",
    "os.execv('/bin/true', ['/bin/true'])",
    "subprocess.run(['/bin/true'], check=True)",
])
def test_network_and_process_denials_survive_math_imports(
    math_runner, workspace, operation,
):
    result = _run(math_runner, workspace, f"""
import errno, os, socket, subprocess
import numpy as np
import sympy, mpmath
# Load the native linear-algebra path before probing the syscall boundary.
assert np.allclose(np.linalg.solve([[2., 0.], [0., 4.]], [6., 8.]), [3., 2.])
try:
    {operation}
except OSError as error:
    assert error.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS), error
    print("DENIED")
else:
    raise AssertionError("math import enabled a forbidden execution capability")
""")
    _assert_success(result)
    assert result.stdout.strip() == "DENIED"


def test_thread_creation_is_denied_after_math_imports(math_runner, workspace):
    result = _run(math_runner, workspace, """
import _thread, threading
from pathlib import Path
import numpy as np
import sympy, mpmath
assert np.linalg.det(np.eye(4)) == 1
def forbidden_thread():
    Path("unexpected-thread.txt").write_text("thread ran")
for start in (
    lambda: _thread.start_new_thread(forbidden_thread, ()),
    lambda: threading.Thread(target=forbidden_thread).start(),
):
    try:
        start()
    except RuntimeError as error:
        assert "thread" in str(error).lower(), error
    else:
        raise AssertionError("math import enabled thread creation")
print("THREADS DENIED")
""")
    _assert_success(result)
    assert result.stdout.strip() == "THREADS DENIED"
    assert not (workspace / "unexpected-thread.txt").exists()


def test_untrusted_import_paths_and_startup_hooks_cannot_run(
    math_runner, workspace, tmp_path, monkeypatch,
):
    expected_versions = {
        item["name"]: item["version"] for item in math_runner.identity["packages"]
    }
    poison = tmp_path / "untrusted-site-packages"
    poison.mkdir()
    startup_marker = workspace / "startup-hook-ran.txt"
    module_marker = workspace / "shadow-module-ran.txt"
    startup_payload = (
        f"from pathlib import Path; Path({str(startup_marker)!r}).write_text('executed')\n"
    )
    module_payload = (
        f"from pathlib import Path; Path({str(module_marker)!r}).write_text('executed')\n"
        "raise RuntimeError('untrusted package shadow was executed')\n"
    )
    for directory in (poison, workspace):
        (directory / "sitecustomize.py").write_text(startup_payload)
        (directory / "usercustomize.py").write_text(startup_payload)
        (directory / "malicious.pth").write_text(
            f"import pathlib; pathlib.Path({str(startup_marker)!r}).write_text('executed')\n"
        )
        for name in _MATH_PACKAGES:
            (directory / f"{name}.py").write_text(module_payload)
    for name in _MATH_PACKAGES:
        fake_metadata = poison / f"{name}-999.0.dist-info"
        fake_metadata.mkdir()
        (fake_metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 999.0\n"
        )
    # Poison both supervisor discovery and child startup inputs. Registration
    # must select fixed trusted install roots without importing these modules.
    monkeypatch.syspath_prepend(str(poison))
    monkeypatch.setenv("PYTHONPATH", str(poison))
    monkeypatch.setenv("PYTHONUSERBASE", str(poison))
    monkeypatch.chdir(workspace)
    isolated = LocalPythonExecutor(timeout=15.0, max_output_bytes=8192)
    assert {
        item["name"]: item["version"] for item in isolated.identity["packages"]
    } == expected_versions
    result = _run(isolated, workspace, """
import json, sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
assert "sitecustomize" not in sys.modules and "usercustomize" not in sys.modules
import numpy, sympy, mpmath
assert numpy.arange(4).sum() == 6
assert sympy.factorint(12) == {2: 2, 3: 1}
assert mpmath.sqrt(9) == 3
print(json.dumps({name: sys.modules[name].__version__
                  for name in ("numpy", "sympy", "mpmath")}))
""")
    _assert_success(result)
    assert json.loads(result.stdout) == expected_versions
    assert not startup_marker.exists()
    assert not module_marker.exists()
