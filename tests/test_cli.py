from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dex import __version__, cli, client, engine, install
from tests.conftest import Dirs, FakeEngine


@pytest.fixture(autouse=True)
def installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every CLI counts as installed, and uv's and pipx's folders are on PATH."""
    monkeypatch.setattr(install, "which", lambda name: True)
    monkeypatch.setattr(install, "_on_path", lambda directory: True)


@pytest.fixture
def model(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = dirs.write("model.gguf", "not a real model")
    monkeypatch.setenv("DEX_MODEL", str(path))
    return path


def answer(monkeypatch: pytest.MonkeyPatch, text: str, truncated: bool = False) -> list[str]:
    """Make the model answer ``text``; returns the prompts it was sent."""
    prompts: list[str] = []

    def fake(prompt: str, quiet: bool) -> tuple[str, bool]:
        prompts.append(prompt)
        return text, truncated

    monkeypatch.setattr(cli, "_generate", fake)
    return prompts


@pytest.mark.parametrize(
    ("args", "flags", "words"),
    [
        (["peek", "at", "x"], set(), ["peek", "at", "x"]),
        (["-q", "--no-context", "list", "-l"], {"quiet", "no_context"}, ["list", "-l"]),
        (["--", "-rf", "files"], set(), ["-rf", "files"]),
        (["find", "files", "-mtime", "+7"], set(), ["find", "files", "-mtime", "+7"]),
        (["-", "x"], set(), ["-", "x"]),
    ],
)
def test_parse_args(args: list[str], flags: set[str], words: list[str]) -> None:
    opts = cli.parse_args(args)
    assert opts.flags == flags
    assert opts.words == words


def test_parse_args_init() -> None:
    assert cli.parse_args(["--init", "zsh"]).init == "zsh"
    assert cli.parse_args(["--init=zsh"]).init == "zsh"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--vers"], "unknown option"),
        (["--init"], "needs a shell"),
        (["--init", "fish"], "supports zsh"),
    ],
)
def test_parse_args_errors(args: list[str], message: str) -> None:
    with pytest.raises(cli.UsageError, match=message):
        cli.parse_args(args)


def test_help_and_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    assert "usage: dex" in capsys.readouterr().out
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"dex {__version__}\n"


def test_init(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--init", "zsh"]) == 0
    assert capsys.readouterr().out.startswith("# dex zsh integration:")


def test_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert cli.main(["--nope"]) == 2
    err = capsys.readouterr().err
    assert "say what you want" in err
    assert "try 'dex --help'" in err


def test_show_prompt(dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setenv("AWS_PROFILE", "etl")
    assert cli.main(["--show-prompt", "list", "s3", "buckets"]) == 0
    out = capsys.readouterr().out
    assert "AWS profile in use is etl\n\n<request>\nlist s3 buckets\n</request>" in out
    assert out.endswith("<|im_start|>assistant\n")
    assert cli.main(["--show-prompt", "--no-context", "list", "s3", "buckets"]) == 0
    assert "AWS profile" not in capsys.readouterr().out


def test_prints_the_command(dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    prompts = answer(monkeypatch, "```bash\nFILE=a.csv && duckdb -c \"from '$FILE'\"\n```")
    assert cli.main(["peek", "at", "a.csv"]) == 0
    out, err = capsys.readouterr()
    assert out == "FILE=a.csv && duckdb -c \"from '$FILE'\"\n"
    assert err == ""
    assert "<request>\npeek at a.csv\n</request>" in prompts[0]


def test_missing_cli_gets_a_hint(dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(install, "which", lambda name: name not in ("duckdb", "csvlook"))
    line = "FILE=a.csv && duckdb -c \"from '$FILE'\" | csvlook"
    answer(monkeypatch, line)
    assert cli.main(["peek", "at", "a.csv"]) == 0
    out, err = capsys.readouterr()
    assert out == line + "\n"
    assert err == (
        "dex: duckdb not found, install with: brew install duckdb\n"
        "dex: csvlook not found, install with: uv tool install csvkit\n"
    )
    assert cli.main(["-q", "peek", "at", "a.csv"]) == 0
    assert capsys.readouterr().err == ""


def test_missing_cli_dex_does_not_know_gets_no_hint(
    dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(install, "which", lambda name: name != "mlr")
    answer(monkeypatch, "mlr --icsv head a.csv")
    assert cli.main(["peek", "at", "a.csv"]) == 0
    assert capsys.readouterr() == ("mlr --icsv head a.csv\n", "")


def test_missing_cli_with_only_an_install_page(
    dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(install, "which", lambda name: name != "helm")
    answer(monkeypatch, "helm list")
    assert cli.main(["show", "releases"]) == 0
    assert capsys.readouterr().err == (
        "dex: helm not found, see https://helm.sh/docs/intro/install/ to install it\n"
    )


class Terminal:
    """stdin with a typed answer, seen as a terminal."""

    def __init__(self, reply: str | BaseException) -> None:
        self.reply = reply

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply


@pytest.fixture
def asking(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """duckdb is missing and offered; returns the CLIs dex installed."""
    ran: list[str] = []
    monkeypatch.setattr(cli, "_asking", lambda: True)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(install, "which", lambda name: name != "duckdb" or bool(ran))
    monkeypatch.setattr(install, "_user_level", lambda command: True)

    def run(command: str) -> int:
        ran.append("duckdb")
        return 0

    monkeypatch.setattr(install, "run", run)
    answer(monkeypatch, "duckdb -c x")
    return ran


QUESTION = "dex: duckdb isn't installed. Install it with: brew install duckdb"
QUESTION += " (Homebrew may update other packages)? [y/N] "


@pytest.mark.usefixtures("dirs")
def test_install_offer_yes_installs_then_answers(
    asking: list[str], monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal("y\n"))
    assert cli.main(["peek"]) == 0
    assert asking == ["duckdb"]
    assert capsys.readouterr() == ("duckdb -c x\n", QUESTION)  # installed, so no hint


@pytest.mark.usefixtures("dirs")
@pytest.mark.parametrize("reply", ["\n", "no\n", "", KeyboardInterrupt()])
def test_install_offer_anything_else_is_no(
    asking: list[str], monkeypatch: pytest.MonkeyPatch, capsys: Any, reply: Any
) -> None:
    monkeypatch.setattr("sys.stdin", Terminal(reply))
    assert cli.main(["peek"]) == 0
    assert asking == []
    out, err = capsys.readouterr()
    assert out == "duckdb -c x\n"
    assert err.startswith(QUESTION)
    assert err.endswith("dex: duckdb not found, install with: brew install duckdb\n")


@pytest.mark.usefixtures("dirs")
@pytest.mark.parametrize(
    ("result", "said"),
    [
        (5, "install failed (exit 5)"),
        (KeyboardInterrupt(), "install stopped"),
        (OSError("bad interpreter"), "install failed: bad interpreter"),
    ],
)
def test_install_offer_failure_still_answers(
    asking: list[str], monkeypatch: pytest.MonkeyPatch, capsys: Any, result: Any, said: str
) -> None:
    def run(command: str) -> int:
        if isinstance(result, BaseException):
            raise result
        return int(result)

    monkeypatch.setattr(install, "run", run)
    monkeypatch.setattr("sys.stdin", Terminal("y\n"))
    assert cli.main(["peek"]) == 0
    out, err = capsys.readouterr()
    assert out == "duckdb -c x\n"
    assert f"dex: {said}\n" in err
    assert err.endswith("dex: duckdb not found, install with: brew install duckdb\n")


def test_asking_only_in_the_zsh_integration_at_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", Terminal(""))
    monkeypatch.setattr("sys.stderr", Terminal(""))
    monkeypatch.setenv("DEX_ASK", "1")
    assert cli._asking()
    monkeypatch.setenv("DEX_ASK", "0")
    assert not cli._asking()
    monkeypatch.setenv("DEX_ASK", "1")
    monkeypatch.setattr(Terminal, "isatty", lambda self: False)
    assert not cli._asking()


@pytest.mark.usefixtures("dirs")
def test_quiet_never_asks(asking: list[str], monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr("sys.stdin", Terminal("y\n"))
    assert cli.main(["-q", "peek"]) == 0
    assert capsys.readouterr() == ("duckdb -c x\n", "")
    assert asking == []


def test_destructive(dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    answer(monkeypatch, "rm -rf ./out")
    assert cli.main(["clean", "out"]) == 3
    out, err = capsys.readouterr()
    assert out == "rm -rf ./out\n"
    assert "looks destructive (rm)" in err
    assert cli.main(["-q", "clean", "out"]) == 3
    assert capsys.readouterr().err == ""


def test_placeholders(dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    answer(monkeypatch, 'ID=<cluster-id> && aws emr describe-cluster --cluster-id "$ID"')
    assert cli.main(["describe", "my", "cluster"]) == 0
    assert "fill in <cluster-id> before you run it" in capsys.readouterr().err


def test_errors_exit_1(dirs: Dirs, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    answer(monkeypatch, "echo hi", truncated=True)
    assert cli.main(["say", "hi"]) == 1
    assert "dex: the answer was cut off" in capsys.readouterr().err
    answer(monkeypatch, "echo \x1b]0;pwned\x07")
    assert cli.main(["say", "hi"]) == 1
    assert "\x1b" not in capsys.readouterr().err


def test_model_missing(dirs: Dirs, capsys: Any) -> None:
    assert cli.main(["say", "hi"]) == 1
    assert "no model is installed" in capsys.readouterr().err


def test_interrupt(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    def stop(prompt: str, quiet: bool) -> tuple[str, bool]:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_generate", stop)
    assert cli.main(["say", "hi"]) == 130


def test_unexpected_errors_propagate(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(prompt: str, quiet: bool) -> tuple[str, bool]:
        raise RuntimeError("bug")

    monkeypatch.setattr(cli, "_generate", boom)
    with pytest.raises(RuntimeError):
        cli.main(["say", "hi"])


@pytest.mark.parametrize(("value", "minutes"), [("", 15.0), ("0", 0.0), (" 2.5 ", 2.5)])
def test_idle(monkeypatch: pytest.MonkeyPatch, value: str, minutes: float) -> None:
    monkeypatch.setenv("DEX_IDLE", value)
    assert cli._idle_minutes() == minutes


def test_idle_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEX_IDLE", "1e12")
    assert cli._idle_minutes() == cli.MAX_IDLE_MINUTES


@pytest.mark.parametrize(
    ("value", "threads"), [("", "auto"), ("0", "auto"), ("x", "auto"), ("4", "4")]
)
def test_threads(monkeypatch: pytest.MonkeyPatch, value: str, threads: str) -> None:
    monkeypatch.setenv("DEX_THREADS", value)
    assert cli._threads() == threads


@pytest.mark.parametrize("value", ["soon", "-1", "inf", "nan"])
def test_idle_bad(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DEX_IDLE", value)
    with pytest.raises(cli.UsageError, match="DEX_IDLE"):
        cli._idle_minutes()


def test_generate_through_the_warm_process(
    model: Path, monkeypatch: pytest.MonkeyPatch, runtime: Path
) -> None:
    seen: dict[str, Any] = {}

    def call(path: str, payload: dict[str, Any], command: list[str]) -> dict[str, Any]:
        seen.update(path=path, payload=payload, command=command)
        return {"text": "echo hi", "truncated": False}

    monkeypatch.setattr(client, "call", call)
    assert cli._generate("PROMPT", quiet=False) == ("echo hi", False)
    assert seen["payload"] == {
        "prompt": "PROMPT",
        "max_tokens": 200,
        "stop": ["\n\n", "</request>"],
    }
    assert Path(seen["path"]).parent == runtime / "dex"
    assert seen["command"][1:3] == ["-m", "dex.server"]


def test_generate_in_process(model: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEX_IDLE", "0")
    monkeypatch.setenv("DEX_THREADS", "2")
    fake = FakeEngine("ls")
    made: list[tuple[str, int, str]] = []

    def make(path: str, threads: int, http_socket: str) -> FakeEngine:
        made.append((path, threads, http_socket))
        return fake

    monkeypatch.setattr(engine, "LlamaEngine", make)
    assert cli._generate("PROMPT", quiet=False) == ("ls", False)
    assert made[0][:2] == (str(model), 2)
    assert fake.prompts == ["PROMPT"]


def test_generate_falls_back_when_the_dir_is_unusable(
    model: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    def unusable(model: str, threads: str) -> str:
        raise client.UnusableDirError("/tmp/dex-1 isn't a private directory owned by you")

    monkeypatch.setattr(client, "socket_path", unusable)
    monkeypatch.setattr(engine, "LlamaEngine", lambda *a: FakeEngine("ls"))
    assert cli._generate("PROMPT", quiet=False) == ("ls", False)
    assert "loading the model in this process instead" in capsys.readouterr().err


def test_stop(monkeypatch: pytest.MonkeyPatch, runtime: Path) -> None:
    (runtime / "dex").mkdir(mode=0o700)
    for name in ("a.sock", "a.http.sock", "a.lock", "b.sock"):
        (runtime / "dex" / name).write_text("")
    sent: list[str] = []

    def call(path: str, payload: dict[str, Any], command: None, timeout: float) -> dict[str, Any]:
        sent.append(Path(path).name)
        assert payload == {"op": "stop"}
        raise client.ServerError("the model process isn't running")

    monkeypatch.setattr(client, "call", call)
    assert cli.main(["--stop"]) == 0
    assert sent == ["a.sock", "b.sock"]


def test_request_path_imports_stay_light(dirs: Dirs, model: Path) -> None:
    """Startup time is a feature: keep slow modules off the request path."""
    import subprocess
    import sys

    slow = (
        "pathlib",
        "typing",
        "contextlib",
        "subprocess",
        "shutil",
        "xllamacpp",
        "importlib.util",
    )
    code = (
        "import sys\n"
        # a venv's site hook may load one first; forget it, so only dex can bring it back
        f"for m in {slow!r}: sys.modules.pop(m, None)\n"
        "from dex import cli, client\n"
        # a CLI nobody has, so the install table is read too
        "client.call = lambda *a, **k: {'text': 'no-such-cli x', 'truncated': False}\n"
        "cli.main(['--', 'first', 'rows', 'of', 'a.parquet', 'in', 's3', 'and', 'kafka'])\n"
        "print(' '.join(sorted(sys.modules)))\n"
    )
    out = subprocess.run(  # nosec B603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    loaded = set(out.splitlines()[-1].split())
    for module in slow:
        assert module not in loaded, module
