"""The zsh integration, driven in a real interactive zsh on a pseudo-terminal."""

from __future__ import annotations

import os
import pty
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "src" / "dex" / "shell" / "dex.zsh"
PLUGIN = ROOT / "shell" / "zsh" / "dex.plugin.zsh"
ZSH = shutil.which("zsh")

pytestmark = pytest.mark.skipif(ZSH is None, reason="zsh isn't installed")

FAKE_DEX = """#!/bin/sh
case "$*" in
  *"first rows"*)
    echo 'FILE=<file> LIMIT=10 && duckdb -c "from x"'
    echo 'dex: fill in <file>' >&2 ;;
  *"clean up"*) echo 'rm -rf out'; echo 'dex: warning: destructive' >&2; exit 3 ;;
  *"fail"*) echo 'dex: no model' >&2; exit 1 ;;
  --version) echo 'dex 9.9' ;;
  --init*) cat "$DEX_SCRIPT" ;;
  *) printf 'echo %s\\n' "$*" ;;
esac
"""


def test_syntax() -> None:
    assert ZSH
    for path in (SCRIPT, PLUGIN):
        subprocess.run([ZSH, "-n", str(path)], check=True)  # nosec B603


class Shell:
    """An interactive ``zsh -f`` with the integration loaded and a fake dex on PATH.

    Keys are sent only while the line editor is active: typed ahead in the terminal's
    cooked mode, control keys are taken by the terminal itself (macOS reads Ctrl-T as
    a status request and Ctrl-D as end of line) and never reach zle. A zle-line-init
    hook counts each new prompt in a file, so the test knows when zle is reading.
    """

    def __init__(self, tmp: Path, setup: str) -> None:
        assert ZSH
        self.out = tmp / "out.txt"
        self.out.write_text("")
        self.prompts = tmp / "prompts.txt"
        self.prompts.write_text("")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "dex"
        fake.write_text(FAKE_DEX)
        fake.chmod(0o755)
        (tmp / "a.csv").write_text("")
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp),
            "TERM": "xterm",
            "OUT": str(self.out),
            "PROMPTS": str(self.prompts),
            "DEX_SCRIPT": str(SCRIPT),
        }
        self.fd, child = pty.openpty()
        os.set_blocking(self.fd, False)
        self.proc = subprocess.Popen(  # nosec B603
            [ZSH, "-f", "-i"], stdin=child, stdout=child, stderr=child, cwd=tmp, env=env
        )
        os.close(child)
        self.lines = 0
        self.seen_prompts = 0
        # Plain text only until the hook is in place; it fires from the next prompt on.
        self.send(
            "PS1='> '\n"
            f"{setup}\n"
            '_dump() { print -r -- "BUF[$BUFFER] CUR[$CURSOR]" >> $OUT }\n'
            "zle -N _dump; bindkey '^T' _dump\n"
            "zle-line-init() { print -r -- p >> $PROMPTS }; zle -N zle-line-init\n"
        )
        self.next_prompt()

    def send(self, keys: str) -> None:
        os.write(self.fd, keys.encode())

    def _poll(self, path: Path, have: int, what: str) -> list[str]:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                while os.read(self.fd, 65536):  # keep the terminal drained
                    pass
            except (BlockingIOError, OSError):
                pass
            lines = path.read_text().splitlines()
            if len(lines) > have:
                return lines
            time.sleep(0.02)
        raise AssertionError(f"no {what}; output so far: {self.out.read_text()!r}")

    def next_prompt(self) -> None:
        """Wait until zle starts reading the next command line."""
        self._poll(self.prompts, self.seen_prompts, "prompt")
        self.seen_prompts += 1

    def wait(self) -> str:
        """Wait for the next line in the output file and return it."""
        lines = self._poll(self.out, self.lines, "output")
        self.lines += 1
        return lines[self.lines - 1]

    def buffer_after(self, keys: str) -> str:
        """Type ``keys`` at the prompt, then return the line editor's buffer and cursor."""
        self.send(keys)
        if keys.endswith("\r"):  # a command ran: wait for the prompt after it
            self.next_prompt()
        self.send("\x14")  # Ctrl-T: dump the buffer
        line = self.wait()
        self.send("\x15")  # Ctrl-U: clear it
        return line

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait()
        os.close(self.fd)


@pytest.fixture
def shell(tmp_path: Path) -> Iterator[Shell]:
    s = Shell(tmp_path, f"source {SCRIPT}")
    yield s
    s.close()


def test_function_puts_the_command_on_the_next_prompt(shell: Shell) -> None:
    assert shell.buffer_after("dex peek at *.csv\r") == "BUF[echo peek at *.csv] CUR[18]"


def test_function_passes_info_options_through(shell: Shell) -> None:
    assert shell.buffer_after("dex --version\r") == "BUF[] CUR[0]"
    assert shell.buffer_after("dex fail please\r") == "BUF[] CUR[0]"


def test_widget_replaces_the_request(shell: Shell) -> None:
    line = shell.buffer_after("show first rows\x18\x04")
    assert line == 'BUF[FILE=<file> LIMIT=10 && duckdb -c "from x"] CUR[5]'
    assert shell.buffer_after("clean up out\x18\x04") == "BUF[rm -rf out] CUR[10]"
    assert shell.buffer_after("this will fail\x18\x04") == "BUF[this will fail] CUR[14]"


def test_plugin_checks_what_it_evaluates(tmp_path: Path) -> None:
    # Outside a checkout the plugin falls back to `dex --init zsh`, which the fake answers.
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    shutil.copy(PLUGIN, plugin_dir / "dex.plugin.zsh")
    s = Shell(tmp_path, f"source {plugin_dir / 'dex.plugin.zsh'}")
    try:
        assert s.buffer_after("dex peek\r") == "BUF[echo peek] CUR[9]"
    finally:
        s.close()
