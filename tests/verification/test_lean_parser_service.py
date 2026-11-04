"""Unix transport regressions; fake workers never start Lean or evaluations."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

import dvp.verification.parser_service as service
from dvp.verification.command_parser import LeanCommand, LeanCommandParseError, LeanCommandParserUnavailable


class FakeWorker:
    def __init__(self):
        self.process = None
        self.cold_starts = 0
        self.requests = []

    def warmup(self, timeout):
        if self.process is None:
            self.cold_starts += 1
            self.process = SimpleNamespace(poll=lambda: None)

    def parse(self, source, timeout, trusted_syntax, trusted_source):
        self.warmup(timeout)
        self.requests.append((source, trusted_syntax, trusted_source))
        if source == "syntax_error":
            raise LeanCommandParseError(["synthetic syntax diagnostic"])
        if source == "backend_failure":
            raise LeanCommandParserUnavailable("synthetic backend crash")
        if source == "slow":
            time.sleep(0.15)
        return [LeanCommand(start=0, end=len(source), source=source, kind="test.command",
                            ast=["source", source], fingerprint=hashlib.sha256(source.encode()).hexdigest(),
                            decl_name_parts=("target",), namespace=("Scope",))]


@pytest.fixture
def transport(monkeypatch):
    # A short path is necessary for the Unix-domain socket length limit.
    with tempfile.TemporaryDirectory(prefix="dps-", dir="/tmp") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        monkeypatch.setattr(service, "_private_root", lambda: root)
        monkeypatch.setattr(service, "environment_identity", lambda _path, **_kwargs: {"workspace": str(workspace)})
        monkeypatch.setattr(service, "parser_fingerprint", lambda: "parser-v1")
        monkeypatch.setattr(service, "service_fingerprint", lambda: "service-v1")
        servers = []

        def launch(_workspace, identity, _log):
            path = service._paths(identity)[0]
            worker = FakeWorker()
            server = service._ParserServer(path, identity, worker)
            def run():
                try:
                    server.serve_forever(poll_interval=0.01)
                finally:
                    server.server_close()
                    path.unlink(missing_ok=True)
            thread = threading.Thread(target=run, daemon=True)
            servers.append((server, worker, thread))
            thread.start()
            return SimpleNamespace(poll=lambda: None if thread.is_alive() else 0)

        monkeypatch.setattr(service, "_launch", launch)
        yield workspace, servers
        for server, _, thread in servers:
            if thread.is_alive():
                server.shutdown()
            thread.join(timeout=2)


def test_thirty_concurrent_clients_start_only_one_worker(transport):
    workspace, servers = transport
    with ThreadPoolExecutor(max_workers=30) as pool:
        responses = list(pool.map(lambda _: service._call(workspace, "warmup", 5), range(30)))
    assert len(servers) == 1
    assert servers[0][1].cold_starts == 1
    assert sum(response["daemon_started"] for response in responses) == 1
    assert sum(response["worker_reused"] for response in responses) == 29
    assert all(response["warmed"] for response in responses)
    assert service._paths(service._identity(workspace))[0].stat().st_mode & 0o777 == 0o600


def test_source_and_trusted_syntax_are_isolated_per_request(transport):
    workspace, servers = transport
    first = service._call(workspace, "parse", 5, source="α\r\nβ", trusted_syntax=["open scoped BigOperators"], trusted_source=True)
    second = service._call(workspace, "parse", 5, source="different", trusted_syntax=[], trusted_source=False)
    assert first["commands"][0]["source"] == "α\r\nβ"
    assert second["commands"][0]["source"] == "different"
    assert servers[0][1].requests == [
        ("α\r\nβ", ("open scoped BigOperators",), True), ("different", (), False),
    ]


def test_async_api_restores_tuple_fields_and_original_spans(transport):
    import asyncio
    workspace, _ = transport
    parsed = asyncio.run(service.parse_commands("α\r\nβ", workspace, timeout=5))
    assert parsed[0].source == "α\r\nβ"
    assert parsed[0].decl_name_parts == ("target",)
    assert parsed[0].namespace == ("Scope",)


def test_timeout_response_cannot_be_delivered_to_the_next_client(transport):
    workspace, _ = transport
    service._call(workspace, "warmup", 5)
    with pytest.raises(LeanCommandParserUnavailable, match="infrastructure"):
        service._call(workspace, "parse", 0.02, source="slow", trusted_syntax=[], trusted_source=False)
    result = service._call(workspace, "parse", 5, source="next", trusted_syntax=[], trusted_source=False)
    assert result["commands"][0]["source"] == "next"


def test_backend_errors_fail_closed_and_later_requests_can_recover(transport):
    workspace, servers = transport
    with pytest.raises(LeanCommandParseError, match="synthetic syntax"):
        service._call(workspace, "parse", 5, source="syntax_error", trusted_syntax=[], trusted_source=False)
    with pytest.raises(LeanCommandParserUnavailable, match="synthetic backend crash"):
        service._call(workspace, "parse", 5, source="backend_failure", trusted_syntax=[], trusted_source=False)
    assert service._call(workspace, "parse", 5, source="recovered", trusted_syntax=[], trusted_source=False)["success"]
    assert len(servers) == 1


def test_status_does_not_start_a_daemon_and_explicit_stop_allows_restart(transport):
    workspace, servers = transport
    assert not service.service_status(workspace)["running"]
    assert not servers
    service._call(workspace, "warmup", 5)
    assert service.service_status(workspace)["running"]
    assert service.stop_service(workspace, timeout=5)["stopping"]
    servers[0][2].join(timeout=2)
    assert not service.service_status(workspace)["running"]
    assert service._call(workspace, "warmup", 5)["daemon_started"]
    assert len(servers) == 2


def test_changed_identity_selects_new_daemon_and_old_snapshot_stays_frozen(transport, monkeypatch):
    workspace, servers = transport
    service._call(workspace, "warmup", 5)
    old_identity = service._identity(workspace)
    old_path = service._paths(old_identity)[0]
    monkeypatch.setattr(service, "service_fingerprint", lambda: "service-v2")
    service._call(workspace, "warmup", 5)
    assert len(servers) == 2
    assert service._paths(service._identity(workspace))[0] != old_path
    old = service._request_existing(old_path, old_identity, "status", time.monotonic() + 5)
    assert old["identity"]["service_sha256"] == "service-v1"


def test_non_socket_and_symlink_are_not_unlinked_or_reused(transport):
    workspace, servers = transport
    path = service._paths(service._identity(workspace))[0]
    destination = path.with_suffix(".other")
    destination.write_text("keep")
    path.symlink_to(destination)
    with pytest.raises(LeanCommandParserUnavailable, match="non-owned parser socket"):
        service._call(workspace, "warmup", 5)
    assert path.is_symlink()
    assert destination.read_text() == "keep"
    assert not servers
