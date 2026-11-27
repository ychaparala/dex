"""The warm process: keeps the model loaded and generates on request.

Started by :mod:`dex.client` as ``python -m dex.server``. It binds its Unix socket
only once the model has loaded, so a socket that answers is a process that is ready.
It does nothing but generate: context, parsing and safety checks run in the dex
command, which sees the user's own shell.

Protocol: one JSON line each way per connection. A request is
``{"prompt": str, "max_tokens": int, "stop": [str]}`` or ``{"op": "stop"}``; the
reply is ``{"text": str, "truncated": bool}``, ``{"ok": true}`` or ``{"error": str}``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dex.client import ServerError, read_line, same_user
from dex.engine import LlamaEngine, ModelError, default_threads

if TYPE_CHECKING:
    from dex.engine import Engine

MAX_REQUEST_BYTES = 1 << 20
READ_TIMEOUT = 10.0


def _handle(engine: Engine, conn: socket.socket) -> bool:
    """Answer one connection. Returns False when asked to stop."""
    conn.settimeout(READ_TIMEOUT)
    if not same_user(conn):
        return True
    try:
        line = read_line(conn, MAX_REQUEST_BYTES)
    except (OSError, ServerError):
        return True
    if not line:  # a liveness check: connect and close
        return True
    reply: dict[str, Any]
    keep_going = True
    try:
        request = json.loads(line)
        if request.get("op") == "stop":
            reply, keep_going = {"ok": True}, False
        else:
            text, truncated = engine.complete(
                str(request["prompt"]),
                int(request.get("max_tokens", 200)),
                [str(s) for s in request.get("stop", [])],
            )
            reply = {"text": text, "truncated": truncated}
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        reply = {"error": f"bad request: {e}"}
    except ModelError as e:
        reply = {"error": str(e)}
    with contextlib.suppress(OSError):  # the client went away
        conn.sendall(json.dumps(reply).encode() + b"\n")
    return keep_going


def serve(engine: Engine, path: Path, idle_seconds: float) -> None:
    """Serve requests on ``path`` until idle for ``idle_seconds``, a stop request or SIGTERM.

    The socket is bound under a temporary name and renamed into place, so clients
    never see a half-made socket. On exit it is removed only if ``path`` still is the
    socket this process bound, so a newer process's socket is never deleted.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(tmp))
    finally:
        os.umask(old_umask)
    tmp.chmod(0o600)
    sock.listen(16)
    tmp.replace(path)
    bound = path.lstat()
    sock.settimeout(idle_seconds if idle_seconds > 0 else None)
    try:
        while True:
            try:
                conn, _ = sock.accept()
            except TimeoutError:
                break
            with conn:
                if not _handle(engine, conn):
                    break
    finally:
        sock.close()
        try:
            st = path.lstat()
            if (st.st_ino, st.st_dev) == (bound.st_ino, bound.st_dev):
                path.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    """Load the model and serve. Run by the dex command, not by people."""
    ap = argparse.ArgumentParser(prog="python -m dex.server")
    ap.add_argument("--socket", required=True, type=Path)
    ap.add_argument("--model", required=True, type=Path)
    ap.add_argument("--threads", required=True, help="a number, or auto")
    ap.add_argument("--idle", type=float, required=True, help="minutes")
    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    http_socket = str(args.socket.with_suffix(".http.sock"))
    try:
        threads = int(args.threads) if args.threads.isdigit() else 0
        threads = threads or default_threads()
        engine = LlamaEngine(str(args.model), max(threads, 1), http_socket)
    except ModelError as e:
        print(f"dex server: {e}", file=sys.stderr)
        return 1
    try:
        serve(engine, args.socket, args.idle * 60)
    finally:
        Path(http_socket).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
