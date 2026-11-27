"""Talk to the warm model process over a Unix socket, starting it when needed.

The warm process (:mod:`dex.server`) keeps the model loaded between calls. This
module is on every request's path, so it imports only cheap standard-library
modules: paths are plain strings because ``pathlib`` takes milliseconds to import,
and ``subprocess`` is imported only when the process has to be started.

One process serves one install and one model: the socket name is a checksum of the
dex version, ``sys.prefix``, the model file and the thread setting, so two installs
never restart each other and an old process after an upgrade just idles out.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import socket
import stat
import struct
import sys
import time
import zlib
from collections.abc import Sequence

from dex import __version__

# JSON objects as they come off the socket; ``object`` rather than ``typing.Any`` so
# this module doesn't import ``typing``.
Message = dict[str, object]

START_TIMEOUT = 30.0
REPLY_TIMEOUT = 60.0
MAX_REPLY_BYTES = 1 << 20
LOG_LIMIT = 1 << 20
# sun_path is 104 bytes on macOS and 108 on Linux, including the trailing NUL.
_SUN_PATH = 103 if sys.platform == "darwin" else 107
_SUFFIX = ".http.sock"  # the longest name dex puts next to the socket


class ServerError(Exception):
    """The warm process couldn't be reached or started, or it reported an error."""


class UnusableDirError(ServerError):
    """The runtime directory isn't private to this user, so dex won't use it."""


def _sibling(path: str, suffix: str) -> str:
    """Return ``path`` with its extension replaced: ``x.sock`` -> ``x.lock``."""
    return os.path.splitext(path)[0] + suffix


def _private_dir(path: str) -> str:
    """Create ``path`` mode 0700 if needed and check it is private to this user.

    Raises:
        UnusableDirError: It is a symlink, not a directory, owned by someone else,
            or open to the group or others.
    """
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError as e:
        raise UnusableDirError(f"can't create {path}: {e.strerror}") from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise UnusableDirError(f"{path} isn't a private directory owned by you")
    return path


def runtime_dir() -> str:
    """Return dex's private runtime directory, creating it if needed.

    Linux: ``$XDG_RUNTIME_DIR/dex``, else ``/tmp/dex-<uid>``. macOS: ``$TMPDIR/dex``,
    which is per user and local. Never the home directory, which may be on NFS.
    """
    fallback = f"/tmp/dex-{os.geteuid()}"  # nosec B108  # private, checked below
    if sys.platform == "darwin":
        base = os.environ.get("TMPDIR", "")
    else:
        base = os.environ.get("XDG_RUNTIME_DIR", "")
    if base and len(os.path.join(base, "dex")) + 20 + len(_SUFFIX) <= _SUN_PATH:
        return _private_dir(os.path.join(base, "dex"))
    return _private_dir(fallback)


def socket_path(model: str, threads: str) -> str:
    """Return the socket for this install, model and thread setting."""
    try:
        st = os.stat(model)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size = mtime = 0
    key = "\0".join((__version__, sys.prefix, model, str(size), str(mtime), threads))
    return os.path.join(runtime_dir(), f"{zlib.crc32(key.encode()):08x}.sock")


def _peer_uid(sock: socket.socket) -> int:
    """Return the uid of the process at the other end of a Unix socket, or -1."""
    try:
        if sys.platform == "darwin":
            # LOCAL_PEERCRED (1) at level SOL_LOCAL (0) returns a struct xucred.
            raw = sock.getsockopt(0, 1, struct.calcsize("IIh16I"))
            return int(struct.unpack_from("II", raw)[1])
        else:
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return int(struct.unpack("3i", raw)[1])
    except (OSError, struct.error, AttributeError):
        return -1


def same_user(sock: socket.socket) -> bool:
    """Whether the peer runs as this user."""
    return _peer_uid(sock) == os.geteuid()


def read_line(sock: socket.socket, limit: int) -> bytes:
    """Read up to a newline (dropped) or EOF; refuse more than ``limit`` bytes."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            raise ServerError("message too long")
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def _connect(path: str, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        if not same_user(sock):
            raise ServerError(f"{path} belongs to another user")
    except BaseException:
        sock.close()
        raise
    return sock


def _exchange(path: str, payload: Message, timeout: float) -> Message:
    with _connect(path, timeout) as sock:
        sock.sendall(json.dumps(payload).encode() + b"\n")
        line = read_line(sock, MAX_REPLY_BYTES)
    if not line:
        raise ConnectionResetError(errno.ECONNRESET, "closed without a reply")
    reply = json.loads(line)
    if not isinstance(reply, dict):
        raise ValueError("the reply isn't a JSON object")
    return reply


def _alive(path: str) -> bool:
    try:
        _connect(path, 2.0).close()
    except (OSError, ServerError):
        return False
    return True


def _remove_stale(path: str) -> None:
    """Remove a socket that nothing answers on, if it is ours."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(st.st_mode) and st.st_uid == os.geteuid():
        try:  # noqa: SIM105  # contextlib.suppress would cost an import
            os.unlink(path)
        except FileNotFoundError:
            pass
    else:
        raise ServerError(f"{path} exists and isn't a socket of yours")


def _log_tail(log: str) -> str:
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            lines = f.read().strip().splitlines()
    except OSError:
        return ""
    return " / ".join(lines[-3:])[-300:]


def start(path: str, command: Sequence[str]) -> None:
    """Start the warm process for ``path`` unless one is already answering.

    An ``flock`` on a lock file next to the socket keeps two calls from starting two
    processes; the lock goes away with its holder, even after a crash.

    Raises:
        ServerError: The process exited or didn't come up in time.
    """
    lock = os.open(_sibling(path, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if _alive(path):
            return
        _remove_stale(path)
        log = _sibling(path, ".log")
        with open(log, "ab") as out:
            if out.tell() > LOG_LIMIT:
                out.truncate(0)
            import subprocess  # nosec B404  # only when starting the process

            proc = subprocess.Popen(  # nosec B603  # our own interpreter and module
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
                start_new_session=True,
                close_fds=True,
            )
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if os.path.exists(path) and _alive(path):
                return
            if proc.poll() is not None:
                tail = _log_tail(log)
                raise ServerError(f"the model process exited ({tail or 'no output'}; log: {log})")
            time.sleep(0.02)
        raise ServerError(
            f"the model process didn't start within {START_TIMEOUT:.0f}s (log: {log})"
        )
    finally:
        os.close(lock)


def server_command(path: str, model: str, threads: str, idle_minutes: float) -> list[str]:
    """Return the command that starts the warm process."""
    return [
        sys.executable, "-m", "dex.server", "--socket", path, "--model", model,
        "--threads", threads, "--idle", str(idle_minutes),
    ]  # fmt: skip


def call(
    path: str,
    payload: Message,
    command: Sequence[str] | None,
    timeout: float = REPLY_TIMEOUT,
) -> Message:
    """Send one request, starting the process with ``command`` if nothing answers.

    A connection closed without a reply is retried once (the process may have been
    idling out). A full listen queue counts as busy and is retried until ``timeout``.

    Raises:
        ServerError: The process couldn't be reached, or it replied with an error.
    """
    deadline = time.monotonic() + timeout
    retried = False
    while True:
        try:
            reply = _exchange(path, payload, timeout)
        except (FileNotFoundError, ConnectionRefusedError):
            if command is None:
                raise ServerError("the model process isn't running") from None
            start(path, command)
            continue
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise ServerError("the model process is busy") from None
            time.sleep(0.05)
            continue
        except (ConnectionResetError, BrokenPipeError):
            if retried:
                raise ServerError("the model process closed the connection") from None
            retried = True
            continue
        except TimeoutError:
            raise ServerError(f"no answer from the model process within {timeout:.0f}s") from None
        except (OSError, ValueError) as e:
            raise ServerError(f"can't talk to the model process: {e}") from None
        if "error" in reply:
            raise ServerError(str(reply["error"]))
        return reply
