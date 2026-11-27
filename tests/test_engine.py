from __future__ import annotations

import importlib
import os
import sys
import types
from typing import Any

import pytest

from dex import engine
from tests.conftest import Dirs


def test_model_from_env(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    model = dirs.write("m.gguf", "x")
    monkeypatch.setenv("DEX_MODEL", str(model))
    assert engine.model_path() == str(model)
    monkeypatch.setenv("DEX_MODEL", str(dirs.home / "missing.gguf"))
    with pytest.raises(engine.ModelError, match="isn't a file"):
        engine.model_path()


def test_model_from_package(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(engine.ModelError, match="no model is installed"):
        engine.model_path()
    package = dirs.home / "site" / "dex_model"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "dex.gguf").write_text("x")
    monkeypatch.syspath_prepend(str(package.parent))
    importlib.invalidate_caches()
    assert engine.model_path() == str(package / "dex.gguf")


def _logical() -> int:
    if hasattr(os, "sched_getaffinity"):  # Linux
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def test_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEX_THREADS", "3")
    assert engine.default_threads() == 3
    monkeypatch.setenv("DEX_THREADS", "zero")
    assert 1 <= engine.default_threads() <= _logical()


def test_threads_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEX_THREADS", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    answers = iter(["", "4"])

    def run(cmd: list[str], **kwargs: Any) -> Any:
        return types.SimpleNamespace(stdout=next(answers))

    import subprocess

    monkeypatch.setattr(subprocess, "run", run)
    assert engine.default_threads() == min(4, _logical())


class FakeServer:
    reply: Any = {"choices": [{"text": "ls", "finish_reason": "stop"}]}  # noqa: RUF012
    fail = False

    def __init__(self, params: Any) -> None:
        if FakeServer.fail:
            raise RuntimeError("bad gguf")
        self.params = params
        self.requests: list[dict[str, Any]] = []

    def handle_completions(self, request: dict[str, Any]) -> Any:
        self.requests.append(request)
        return FakeServer.reply


@pytest.fixture
def xllamacpp(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    def params() -> Any:
        ns = types.SimpleNamespace
        return ns(model=ns(), cpuparams=ns(), cpuparams_batch=ns())

    module = types.ModuleType("xllamacpp")
    module.CommonParams = params  # type: ignore[attr-defined]
    module.Server = FakeServer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "xllamacpp", module)
    monkeypatch.setattr(FakeServer, "fail", False)
    return module


def test_llama_engine(xllamacpp: types.ModuleType) -> None:
    e = engine.LlamaEngine("/m.gguf", 4, "/run/x.http.sock")
    p = e._server.params
    assert (p.model.path, p.n_ctx, p.cpuparams.n_threads, p.hostnames) == (
        "/m.gguf", 1024, 4, ["/run/x.http.sock"],
    )  # fmt: skip
    assert e.complete("PROMPT", 200, ("\n\n",)) == ("ls", False)
    assert e._server.requests[0] == {
        "prompt": "PROMPT", "n_predict": 200, "temperature": 0.0, "stop": ["\n\n"],
        "cache_prompt": True,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("reply", "result"),
    [
        ({"choices": [{"text": "ls -", "finish_reason": "length"}]}, ("ls -", True)),
        ({"error": {"message": "context full"}}, "context full"),
        ({"weird": 1}, "unexpected reply"),
    ],
)
def test_llama_engine_replies(
    xllamacpp: types.ModuleType, monkeypatch: pytest.MonkeyPatch, reply: Any, result: Any
) -> None:
    monkeypatch.setattr(FakeServer, "reply", reply)
    e = engine.LlamaEngine("/m.gguf", 1, "/x")
    if isinstance(result, tuple):
        assert e.complete("P", 5, ()) == result
    else:
        with pytest.raises(engine.ModelError, match=result):
            e.complete("P", 5, ())


def test_llama_engine_load_errors(
    xllamacpp: types.ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeServer, "fail", True)
    with pytest.raises(engine.ModelError, match=r"couldn't load /m\.gguf: bad gguf"):
        engine.LlamaEngine("/m.gguf", 1, "/x")
    monkeypatch.setitem(sys.modules, "xllamacpp", None)
    with pytest.raises(engine.ModelError, match="isn't installed"):
        engine.LlamaEngine("/m.gguf", 1, "/x")
