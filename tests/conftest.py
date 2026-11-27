"""Shared fixtures: an empty home and working directory, and a fake model."""

from __future__ import annotations

import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from dex.safety import RESERVED

# Every variable dex reads, so a test never sees the developer's own setup.
DEX_VARS = (*RESERVED, "DEX_MODEL", "DEX_IDLE", "DEX_THREADS", "XDG_RUNTIME_DIR")


@dataclass
class Dirs:
    home: Path
    cwd: Path

    def write(self, relative: str, text: str = "", *, root: str = "home") -> Path:
        path = (self.home if root == "home" else self.cwd) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dirs:
    """A fresh home and working directory, with dex's environment variables unset."""
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    for name in DEX_VARS:
        if name not in ("PATH", "TERM", "LANG", "TMPDIR"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)
    return Dirs(home, cwd)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A short private directory that dex uses as its runtime base on Linux and macOS.

    Socket paths are limited to about 104 bytes, so it is under /tmp rather than
    pytest's long temporary paths. Returns the base; dex adds ``dex/`` to it.
    """
    with tempfile.TemporaryDirectory(prefix="dex-t", dir="/tmp") as d:  # nosec B108
        Path(d).chmod(0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", d)  # Linux
        monkeypatch.setenv("TMPDIR", d)  # macOS
        yield Path(d)


@dataclass
class FakeEngine:
    """Answers every prompt with ``text`` and records the prompts."""

    text: str = "echo hi"
    truncated: bool = False
    prompts: list[str] = field(default_factory=list)

    def complete(self, prompt: str, max_tokens: int, stop: Sequence[str]) -> tuple[str, bool]:
        self.prompts.append(prompt)
        return self.text, self.truncated
