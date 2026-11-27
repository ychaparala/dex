"""The training-data generator's value quoting, and the generated data it produced."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

TRAINING = Path(__file__).parent.parent / "training"


def _nl2sh() -> Any:
    spec = importlib.util.spec_from_file_location("nl2sh", TRAINING / "nl2sh.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


nl2sh = _nl2sh()


@pytest.mark.parametrize(
    ("value", "quoted"),
    [
        ("orders", "orders"),
        ("<bucket>", "<bucket>"),
        ("a b", "'a b'"),
        ("it's", '"it\'s"'),
        ("~", "~"),
        ("~/.dbt", "~/.dbt"),
        ("~/.config/kcat.conf", "~/.config/kcat.conf"),
        ("~/my dir/x", '"$HOME/my dir/x"'),
        ("~/a$b", '"$HOME/a\\$b"'),
        ("~user/x", "'~user/x'"),  # another user's home: left alone
    ],
)
def test_quote(value: str, quoted: str) -> None:
    assert nl2sh.quote(value) == quoted


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash isn't installed")
@pytest.mark.parametrize("value", ["~/.dbt", "~/my dir/x", "~/a$b"])
def test_quoted_home_paths_expand(value: str, tmp_path: Path) -> None:
    line = f'X={nl2sh.quote(value)} && printf %s "$X"'
    out = subprocess.run(  # nosec B603 B607  # a fixed shell and our own line
        ["bash", "-c", line],
        capture_output=True,
        text=True,
        check=True,
        env={"HOME": str(tmp_path)},
    ).stdout
    assert out == str(tmp_path) + value[1:]


def test_no_training_answer_quotes_a_leading_tilde() -> None:
    for name in ("train.jsonl", "eval.jsonl"):
        for line in (TRAINING / "data" / name).read_text().splitlines():
            answer = json.loads(line)["messages"][2]["content"]
            assert not nl2sh.QUOTED_TILDE.search(answer), answer


def test_namespace_hint_sets_the_commands_namespace() -> None:
    # Why dex parses kubeconfig: with no hint, answers pin -n default over the user's own namespace.
    hinted = 0
    for name in ("train.jsonl", "eval.jsonl"):
        for line in (TRAINING / "data" / name).read_text().splitlines():
            user, answer = (m["content"] for m in json.loads(line)["messages"][1:])
            for ctx in user.split("\n\n<request>")[0].splitlines():
                if ctx.startswith("Kubernetes namespace is "):
                    hinted += 1
                    assert f"NAMESPACE={ctx.split()[-1]} " in answer.split(" && ")[0] + " ", answer
    assert hinted


def test_scoring_keeps_quotes_and_word_boundaries() -> None:
    assert nl2sh.normalize("ls   -l  'a b'") == "ls -l 'a b'"
    assert nl2sh.normalize("`ls -l`") == "ls -l"
    assert nl2sh.normalize("cat '$FILE'") != nl2sh.normalize('cat "$FILE"')
    assert nl2sh.normalize("echo 'a b'") != nl2sh.normalize("echo a b")


def test_zero_replica_answers_are_destructive() -> None:
    for name in ("train.jsonl", "eval.jsonl"):
        for line in (TRAINING / "data" / name).read_text().splitlines():
            record = json.loads(line)
            request, answer = record["messages"][1]["content"], record["messages"][2]["content"]
            if "kubectl scale" in answer and ("REPLICAS=0 " in answer or "zero" in request):
                assert "REPLICAS=0 " in answer, request
                assert record["meta"]["destructive"], request


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, install: Any) -> Path:
    cfg = {"binaries": ["tool"], "versions": [{"version": "1.0", "install": "pip:tool==1.0"}]}
    monkeypatch.setattr(nl2sh, "CACHE", tmp_path)
    monkeypatch.setattr(nl2sh, "load_tools", lambda only=None: {"tool": cfg})
    monkeypatch.setattr(nl2sh, "install_pip", install)
    nl2sh.setup_envs(type("Args", (), {"only": None})())
    return tmp_path / "envs" / "tool-1.0"


def test_setup_envs_redoes_an_env_without_its_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "envs" / "tool-1.0" / "bin").mkdir(parents=True)
    calls = []

    def install(v: Any, spec: str, d: Path, uv: Any) -> bool:
        calls.append(spec)
        (d / "bin").mkdir(parents=True)
        (d / "bin" / "tool").touch()
        return True

    env = _setup(monkeypatch, tmp_path, install)
    assert calls == ["pip:tool==1.0"]
    assert (env / "bin" / "tool").exists()
    _setup(monkeypatch, tmp_path, install)
    assert calls == ["pip:tool==1.0"]  # complete now, so not installed again


def test_setup_envs_removes_a_failed_install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def install(v: Any, spec: str, d: Path, uv: Any) -> bool:
        (d / "bin").mkdir(parents=True)
        raise subprocess.CalledProcessError(1, "pip")

    with pytest.raises(subprocess.CalledProcessError):
        _setup(monkeypatch, tmp_path, install)
    assert not (tmp_path / "envs" / "tool-1.0").exists()


def test_lint_flags_an_intent_that_names_another_value() -> None:
    template = {
        "intents": ["list {STATE} runs", "which runs are still running", "show failed runs"],
        "vars": [{"name": "STATE", "default": "FAILED", "samples": ["RUNNING", "TIMED_OUT"]}],
    }
    assert nl2sh.named_values(template) == [
        "intent 'which runs are still running' names STATE=RUNNING but gets the default FAILED"
    ]


def test_template_vars_avoid_names_the_shell_uses() -> None:
    """COLUMNS=id,name in zsh sets the terminal width to 0, and dex warns on every such answer."""
    assert nl2sh.reserved_name("COLUMNS")
    assert nl2sh.reserved_name("BASH_ENV")
    assert not nl2sh.reserved_name("COLUMN_LIST")
    for tool in nl2sh.load_tools().values():
        for template in tool["templates"]:
            for var in template.get("vars", []):
                assert not nl2sh.reserved_name(var["name"]), (template["id"], var["name"])


def test_every_tool_config_passes_lint() -> None:
    assert nl2sh.lint(nl2sh.load_tools()) == []


def test_installs_json_matches_the_tool_configs() -> None:
    """dex ships the install commands as JSON: rerun `python nl2sh.py installs` after a change."""
    assert json.loads(nl2sh.INSTALLS.read_text()) == nl2sh.installs(nl2sh.install_sources())


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"macos": ["brew install x"]}, "no source"),
        ({"macos": "brew install x", "source": ["u"]}, "one-line commands"),
        ({"windows": ["winget install x"], "source": ["u"]}, "unknown keys"),
        ({"binaries": ["y"], "any": ["pipx install x"], "source": ["u"]}, "y isn't in binaries"),
    ],
)
def test_installs_rejects_bad_entries(entry: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        nl2sh.installs({"x": {"binaries": ["x"], "install": [entry]}})


def test_installs_rejects_two_ways_for_one_binary() -> None:
    entry = {"source": ["u"]}
    tools = {
        "a": {"binaries": ["aws"], "install": [{**entry, "macos": ["brew install awscli"]}]},
        "b": {"binaries": ["aws"], "install": [{**entry, "macos": ["pipx install awscli"]}]},
    }
    with pytest.raises(ValueError, match="installed differently"):
        nl2sh.installs(tools)


def test_build_part_validates_each_command_once_per_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = dict(nl2sh.load_tools()["jq"], versions=[{"version": "1.0"}, {"version": "2.0"}])
    for v in ("1.0", "2.0"):
        (tmp_path / "envs" / f"jq-{v}" / "bin").mkdir(parents=True)
        (tmp_path / "envs" / f"jq-{v}" / "bin" / cfg["binaries"][0]).touch()
    monkeypatch.setattr(nl2sh, "CACHE", tmp_path)
    monkeypatch.setattr(nl2sh, "PARTS", tmp_path / "parts")
    calls = []

    class Val:
        def run(self, p: dict[str, Any], bindir: Path) -> tuple[str, str, None]:
            calls.append((nl2sh.concrete(p)[0], bindir.parent.name))
            return "fake", "fail" if bindir.parent.name == "jq-2.0" else "ok", None

    s = nl2sh.build_part("jq", cfg, Val())
    # each distinct command is validated once on each of the two versions
    assert len(calls) == len(set(calls)) == 2 * len({c for c, _ in calls})
    rows = [json.loads(line) for line in (tmp_path / "parts" / "jq.jsonl").read_text().splitlines()]
    plain = [r for r in rows if r["kind"] != "version"]
    assert s["per_version_status"] == {"1.0": {"ok": len(plain)}, "2.0": {"fail": len(plain)}}
    assert all(
        r["validation"] == "ok-some-versions" and r["valid_versions"] == ["1.0"] for r in rows
    )
    assert all("_cmd" not in r for r in rows)


def test_merge_holds_out_every_variant_of_an_intent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "parts").mkdir()
    monkeypatch.setattr(nl2sh, "PARTS", tmp_path / "parts")
    monkeypatch.setattr(nl2sh, "DATA", tmp_path)
    row = {
        "tool": "t",
        "template_id": "t-a",
        "group": "",
        "destructive": False,
        "verified": "docs",
        "validator": "bash",
        "validation": "ok",
        "cmd": "true",
    }
    rows = [
        dict(
            row, intent_idx=i, kind=k, text=f"do {i}", ctx=["Context: x"] if k == "context" else []
        )
        for i in range(3)
        for k in ("plain", "context")
    ]
    (tmp_path / "parts" / "t.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def run() -> tuple[str, str]:
        nl2sh.merge({"t": {}})
        return (tmp_path / "train.jsonl").read_text(), (tmp_path / "eval.jsonl").read_text()

    train, evals = run()
    assert run() == (train, evals)  # deterministic
    assert "do 2" in evals and "do 2" not in train
    assert train.count("\n") == 4  # both variants of intents 0 and 1


def test_parse_line_splits_values_and_resists_redos() -> None:
    line = 'Q=\'a b\' R="x \\" y" N=20 && echo $Q'
    assert nl2sh.parse_line(line) == ({"Q": "a b", "R": 'x \\" y', "N": "20"}, "echo $Q")
    # CodeQL py/redos: this used to backtrack exponentially; it must finish (and not match) at once.
    assert nl2sh.parse_line("A=" + '"" A=' * 5000) == ({}, "A=" + '"" A=' * 5000)


FAKE_COBRA = """#!/bin/sh
echo "$*" >> "$0.log"
case "$*" in
  "--help") printf 'Usage:\\n  fake [command]\\n\\nAvailable Commands:\\n  run  Run an image\\n' ;;
  *--bogus*) echo 'Error: unknown flag: --bogus'; exit 1 ;;
  "run --help"*) printf 'Usage:\\n  fake run [flags] IMAGE [COMMAND]\\n' ;;
  *) printf 'Usage:\\n  fake [command]\\n\\nAvailable Commands:\\n  run  Run an image\\n' ;;
esac
"""


@pytest.mark.skipif(shutil.which("sh") is None, reason="sh isn't installed")
def test_help_parse_puts_help_before_passed_on_arguments(tmp_path: Path) -> None:
    exe = tmp_path / "fake"
    exe.write_text(FAKE_COBRA)
    exe.chmod(0o755)
    v = nl2sh.Validators(None, True)
    assert v.help_parse(str(exe), ["fake", "run", "--rm", "alpine", "echo", "hi"]) == ("ok", None)
    # --help right after the command path, so it can't reach the program the command runs
    assert "run --help --rm alpine echo hi" in (tmp_path / "fake.log").read_text().splitlines()
    assert v.help_parse(str(exe), ["fake", "run", "--bogus", "alpine"])[0] == "fail"
    assert v.help_parse(str(exe), ["fake", "bogus", "x"]) == (
        "fail",
        "unknown command 'bogus' under fake",
    )


def test_usage_depth_counts_command_words() -> None:
    assert nl2sh.usage_depth("Usage:  docker image ls [OPTIONS]") == 3
    assert nl2sh.usage_depth("Usage:\n  databricks fs [command]\n") == 2
    assert nl2sh.usage_depth("no help here") is None
