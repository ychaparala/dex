from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from dex import client, server
from dex.engine import ModelError
from tests.conftest import FakeEngine


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory with a short path: Unix socket paths are limited to about 104 bytes."""
    with tempfile.TemporaryDirectory(prefix="dex-t", dir="/tmp") as d:  # nosec B108
        Path(d).chmod(0o700)
        yield Path(d)


class Running:
    def __init__(self, engine: Any, path: Path) -> None:
        self.path = path
        self.thread = threading.Thread(target=server.serve, args=(engine, path, 30.0), daemon=True)
        self.thread.start()
        for _ in range(500):
            if path.exists():
                return
            threading.Event().wait(0.01)
        raise AssertionError("server didn't start")


@pytest.fixture
def running(short_dir: Path) -> Iterator[Running]:
    r = Running(FakeEngine("echo hi"), short_dir / "s.sock")
    yield r
    if r.thread.is_alive():
        client.call(str(r.path), {"op": "stop"}, None, timeout=5)
        r.thread.join(5)


def test_round_trip(running: Running) -> None:
    reply = client.call(str(running.path), {"prompt": "P", "max_tokens": 5, "stop": ["\n"]}, None)
    assert reply == {"text": "echo hi", "truncated": False}
    assert oct(running.path.stat().st_mode & 0o777) == "0o600"


def test_liveness_check_and_bad_requests(running: Running) -> None:
    assert client._alive(str(running.path))
    with pytest.raises(client.ServerError, match="bad request"):
        client.call(str(running.path), {"max_tokens": 5}, None)
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(running.path))
        s.sendall(b"not json\n")
        assert b"bad request" in client.read_line(s, 1000)


def test_stop_removes_the_socket(running: Running) -> None:
    assert client.call(str(running.path), {"op": "stop"}, None) == {"ok": True}
    running.thread.join(5)
    assert not running.path.exists()
    with pytest.raises(client.ServerError, match="isn't running"):
        client.call(str(running.path), {"op": "stop"}, None)


def test_model_errors_are_replies(short_dir: Path) -> None:
    class Broken:
        def complete(self, prompt: str, max_tokens: int, stop: Sequence[str]) -> tuple[str, bool]:
            raise ModelError("decode failed")

    r = Running(Broken(), short_dir / "b.sock")
    with pytest.raises(client.ServerError, match="decode failed"):
        client.call(str(r.path), {"prompt": "P"}, None)
    client.call(str(r.path), {"op": "stop"}, None)
    r.thread.join(5)


def test_idle_timeout(short_dir: Path) -> None:
    path = short_dir / "i.sock"
    server.serve(FakeEngine(), path, 0.05)  # returns after 50 ms without a request
    assert not path.exists()


def test_does_not_remove_a_newer_socket(short_dir: Path) -> None:
    path = short_dir / "n.sock"
    old = threading.Thread(target=server.serve, args=(FakeEngine(), path, 0.3), daemon=True)
    old.start()
    for _ in range(500):
        if path.exists():
            break
        threading.Event().wait(0.01)
    path.unlink()
    with socket.socket(socket.AF_UNIX) as newer:
        newer.bind(str(path))  # a newer process took the name
        old.join(5)  # the old one idles out
        assert path.exists()


START = """
import sys
from pathlib import Path
from dex.server import serve
class E:
    def complete(self, prompt, max_tokens, stop):
        return "started", False
serve(E(), Path(sys.argv[1]), 30)
"""


def test_start_on_demand(short_dir: Path) -> None:
    path = short_dir / "w.sock"
    command = [sys.executable, "-c", START, str(path)]
    try:
        assert client.call(str(path), {"prompt": "P"}, command)["text"] == "started"
        client.start(str(path), command)  # already running: nothing to do
    finally:
        client.call(str(path), {"op": "stop"}, None)


def test_start_reports_a_crash(short_dir: Path) -> None:
    path = short_dir / "c.sock"
    command = [sys.executable, "-c", "import sys; print('no model here'); sys.exit(1)"]
    with pytest.raises(client.ServerError, match="exited \\(no model here"):
        client.call(str(path), {"prompt": "P"}, command)


def test_start_times_out(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "START_TIMEOUT", 0.2)
    command = [sys.executable, "-c", "import time; time.sleep(5)"]
    with pytest.raises(client.ServerError, match="didn't start"):
        client.start(str(short_dir / "t.sock"), command)


def test_stale_socket_is_replaced(short_dir: Path) -> None:
    path = short_dir / "s.sock"
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))  # bound but not listening: connections are refused
    stale.close()
    command = [sys.executable, "-c", START, str(path)]
    try:
        assert client.call(str(path), {"prompt": "P"}, command)["text"] == "started"
    finally:
        client.call(str(path), {"op": "stop"}, None)


def test_refuses_a_file_in_the_way(short_dir: Path) -> None:
    path = short_dir / "f.sock"
    path.write_text("not a socket")
    with pytest.raises(client.ServerError, match="isn't a socket of yours"):
        client._remove_stale(str(path))


def test_closed_without_a_reply_is_retried_once(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def reset(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append(1)
        raise ConnectionResetError

    monkeypatch.setattr(client, "_exchange", reset)
    with pytest.raises(client.ServerError, match="closed the connection"):
        client.call(str(short_dir / "x.sock"), {}, None)
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TimeoutError(), "no answer"),
        (PermissionError("denied"), "can't talk"),
        (ValueError("bad json"), "can't talk"),
    ],
)
def test_call_errors(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, message: str
) -> None:
    def fail(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise error

    monkeypatch.setattr(client, "_exchange", fail)
    with pytest.raises(client.ServerError, match=message):
        client.call(str(short_dir / "x.sock"), {}, None)


def test_busy_is_retried_until_the_deadline(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise BlockingIOError

    monkeypatch.setattr(client, "_exchange", busy)
    with pytest.raises(client.ServerError, match="busy"):
        client.call(str(short_dir / "x.sock"), {}, None, timeout=0.1)


def test_read_line_limit() -> None:
    a, b = socket.socketpair()
    with a, b:
        a.sendall(b"x" * 100)
        a.shutdown(socket.SHUT_WR)
        with pytest.raises(client.ServerError, match="too long"):
            client.read_line(b, 10)


def test_same_user() -> None:
    a, b = socket.socketpair()
    with a, b:
        assert client.same_user(a)
    with socket.socket(socket.AF_INET) as s:
        assert not client.same_user(s)


def test_private_dir(short_dir: Path) -> None:
    made = client._private_dir(str(short_dir / "new"))
    assert oct(Path(made).stat().st_mode & 0o777) == "0o700"
    (short_dir / "open").mkdir(mode=0o755)
    (short_dir / "open").chmod(0o755)
    (short_dir / "link").symlink_to(made)
    (short_dir / "file").write_text("")
    for name in ("open", "link", "file"):
        with pytest.raises(client.UnusableDirError):
            client._private_dir(str(short_dir / name))
    with pytest.raises(client.UnusableDirError, match="can't create"):
        client._private_dir(str(short_dir / "file" / "below"))


def test_runtime_dir(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    assert client.runtime_dir() == str(short_dir / "dex")
    picked: list[str] = []

    def record(path: str) -> str:
        picked.append(path)
        return path

    monkeypatch.setattr(client, "_private_dir", record)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/" + "x" * 120)  # too long for a socket path
    client.runtime_dir()
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("TMPDIR", str(short_dir))
    client.runtime_dir()
    assert picked == [f"/tmp/dex-{os.geteuid()}", str(short_dir / "dex")]  # nosec B108


def test_socket_path(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(short_dir))
    model = str(short_dir / "m.gguf")
    Path(model).write_text("a")
    one = client.socket_path(model, "auto")
    assert one == client.socket_path(model, "auto")
    assert one != client.socket_path(model, "4")
    os.utime(model, ns=(1, 1))
    assert one != client.socket_path(model, "auto")
    assert client.socket_path(str(short_dir / "missing"), "auto").endswith(".sock")


def test_server_command() -> None:
    cmd = client.server_command("/r/s.sock", "/m.gguf", "auto", 15.0)
    assert cmd == [
        sys.executable, "-m", "dex.server", "--socket", "/r/s.sock", "--model", "/m.gguf",
        "--threads", "auto", "--idle", "15.0",
    ]  # fmt: skip


def test_log_tail(short_dir: Path) -> None:
    log = short_dir / "x.log"
    assert client._log_tail(str(log)) == ""
    log.write_text("a\nb\nc\nd\n")
    assert client._log_tail(str(log)) == "b / c / d"


def test_server_main(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[tuple[str, int, str]] = []
    served: list[tuple[Path, float]] = []

    def make(path: str, threads: int, http_socket: str) -> FakeEngine:
        made.append((path, threads, http_socket))
        return FakeEngine()

    monkeypatch.setattr(server, "LlamaEngine", make)
    monkeypatch.setattr(server, "serve", lambda e, p, idle: served.append((p, idle)))
    sock = short_dir / "m.sock"
    args = ["--socket", str(sock), "--model", "/m.gguf", "--threads", "3", "--idle", "0.5"]
    assert server.main(args) == 0
    assert made == [("/m.gguf", 3, str(short_dir / "m.http.sock"))]
    assert served == [(sock, 30.0)]


def test_server_main_model_error(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(path: str, threads: int, http_socket: str) -> FakeEngine:
        raise ModelError("couldn't load /m.gguf")

    monkeypatch.setattr(server, "LlamaEngine", broken)
    args = ["--socket", str(short_dir / "e.sock"), "--model", "/m.gguf"]
    assert server.main([*args, "--threads", "auto", "--idle", "1"]) == 1
    assert "couldn't load" in capsys.readouterr().err
