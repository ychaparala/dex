from __future__ import annotations

import json
from pathlib import Path

import pytest

from dex.parse import ParseError, clean, parse

DATA = Path(__file__).parent.parent / "training" / "data"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("ls -la", "ls -la"),
        ("```bash\nls -la\n```", "ls -la"),
        ("$ ls -la", "ls -la"),
        ("% ls -la", "ls -la"),
        ("`ls -la`", "ls -la"),
        ("\n\n  ls -la  \nsecond line", "ls -la"),
        ("echo `date`", "echo `date`"),
        ("```", ""),
    ],
)
def test_clean(text: str, line: str) -> None:
    assert clean(text) == line


def test_values_first() -> None:
    a = parse("""FILE=data.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" """)
    assert a.assigned == ("FILE", "LIMIT")
    assert a.line.startswith("FILE=data.parquet LIMIT=20 && duckdb")


def test_quoted_values_and_semicolons() -> None:
    a = parse("""A='x y'; B="z" ; C= && echo "$A" """)
    assert a.assigned == ("A", "B", "C")


def test_no_values() -> None:
    assert parse("aws s3 ls").assigned == ()


def test_inline_env_is_not_a_shell_value() -> None:
    assert parse("AWS_PROFILE=x aws s3 ls").assigned == ()


@pytest.mark.parametrize(
    ("line", "names"),
    [
        ("A=1 && PATH=/x && ls", ("A", "PATH")),
        ("export PATH=/x; ls", ("PATH",)),
        ("ls | grep x && Q=1 || R=2; echo", ("Q", "R")),
        ('A=1 A=2 && echo "a && B=1"', ("A",)),
    ],
)
def test_every_shell_assignment_is_found(line: str, names: tuple[str, ...]) -> None:
    assert parse(line).assigned == names


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no command"),
        ("```\n```", "no command"),
        ("echo 'unterminated", "complete command"),
        ("echo trailing\\", "complete command"),
        ("echo \x1b[31m", "control characters"),
        ("A=1 B=2", "runs no command"),
        ("A=1;", "runs no command"),
    ],
)
def test_rejects(text: str, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        parse(text)


def test_truncated() -> None:
    with pytest.raises(ParseError, match="cut off"):
        parse("echo hi", truncated=True)


def test_every_training_answer_parses() -> None:
    for name in ("train.jsonl", "eval.jsonl"):
        for line in (DATA / name).read_text().splitlines():
            answer = json.loads(line)["messages"][2]["content"]
            assert parse(answer).line == answer
