from __future__ import annotations

import json
from pathlib import Path

from dex import prompt

EVAL = Path(__file__).parent.parent / "training" / "data" / "eval.jsonl"


def test_user_turn_without_context() -> None:
    assert prompt.user("  peek at sales.parquet ") == "<request>\npeek at sales.parquet\n</request>"


def test_user_turn_with_context() -> None:
    turn = prompt.user(
        "list buckets", ["AWS profile in use is etl", "  ", "Default AWS region is x"]
    )
    assert turn == (
        "AWS profile in use is etl\nDefault AWS region is x\n\n<request>\nlist buckets\n</request>"
    )


def test_render_is_chatml() -> None:
    text = prompt.render("<request>\nx\n</request>")
    assert text == (
        f"<|im_start|>system\n{prompt.SYSTEM}<|im_end|>\n"
        "<|im_start|>user\n<request>\nx\n</request><|im_end|>\n"
        "<|im_start|>assistant\n"
    )


def test_matches_the_training_data() -> None:
    """Rebuild every eval user turn from its parts: the bytes must be the same."""
    records = [json.loads(line) for line in EVAL.read_text().splitlines()]
    assert len(records) > 100
    for record in records:
        system, user, _ = record["messages"]
        assert system["content"] == prompt.SYSTEM
        head, _, rest = user["content"].rpartition("<request>\n")
        request = rest.removesuffix("\n</request>")
        lines = [line for line in head.split("\n") if line]
        assert prompt.user(request, lines) == user["content"]


def test_context_wording_matches_the_training_data() -> None:
    """Every context line dex can write starts the way some training line does."""
    seen = set()
    for line in EVAL.with_name("train.jsonl").read_text().splitlines():
        user = json.loads(line)["messages"][1]["content"]
        seen.update(user.rpartition("<request>")[0].strip().splitlines())
    prefixes = [
        "AWS profile in use is ", "Default AWS region is ", "Databricks CLI profile is ",
        "Snowflake connection is ", "Kafka bootstrap server is ", "kubectl context is ",
        "Kubernetes namespace is ", "GCP project is ", "dbt target is ", "Airflow DAGs here: ",
        "Working directory contains: ",
    ]  # fmt: skip
    for prefix in prefixes:
        assert any(s.startswith(prefix) for s in seen), prefix
