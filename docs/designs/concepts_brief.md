# dex concepts brief

Status: agreed direction, input to `design_draft.md`. Revised for the offline, argparse and minimal-dependency decisions.

## Problem

Data engineers move between a dozen CLIs (Airflow and Astronomer, Databricks, Snowflake, Confluent and Kafka, AWS,
DuckDB, dbt, kubectl, jq). Each has its own flags, and the common jobs (peek at a Parquet file, list a bucket, trigger
a DAG, tail a topic) mean a trip to the docs. dex turns a plain-English request into one ready-to-edit shell command.

## Users

- Data engineers working in a terminal, mostly zsh on macOS and Linux.
- They know Python and expect `pip install`, `pipx` or `uv tool install`.
- They want a command they can read and change, not one that runs for them.

## Goals

1. **Values first.** The output is one line: the values a user is likely to change come first as shell variables,
   then `&&` and the command, pipes allowed:
   `FILE=sales.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" | less -S`.
2. **Editable before it runs.** In zsh the line lands on the next prompt; nothing ever executes on its own.
3. **Tiny local model, offline.** Qwen2.5-Coder-0.5B or smaller, LoRA fine-tuned, quantized to GGUF, installed with
   dex and run in process through llama.cpp. No network, no API key, no server to set up.
4. **Fast.** dex runs on every request, so startup stays low and the model call dominates.
5. **Real values when known.** Fill placeholders from local context: AWS profile and region, Databricks profile,
   Snowflake connection, kubectl context and the files in the working directory.
6. **Warn on destructive commands.** Deletes, drops, truncates, terminates, purges, offset resets and overwrites get
   a warning and a distinct exit code.
7. **Easy to extend.** Adding a CLI is one YAML file for training, plus optional context and safety rules.

## Constraints

- A 0.5B model learns its format from the training data, so the CLI prompt must match the dataset generator exactly.
- Install with pip, pipx or uv; as few runtime dependencies as possible (only what runs the model), and training
  dependencies are an optional extra.
- Python 3.11 and newer (3.10 reached end of life in October 2026; the training stack needs 3.11).
- Coding standards: ruff (lint and format), mypy strict, bandit, pytest with coverage, pre-commit, CI gates.
- Follow the ponytail principles (`AGENTS.md`): the smallest complete change, standard library first.

## Options considered

| Question | Options | Choice and why |
| --- | --- | --- |
| Language | Go binary; Python | **Python.** The users know it and expect pip; the training side is Python anyway. Go would start faster, but the model call dominates. |
| CLI framework | argparse; Typer; typer-slim | **argparse**, imported only when an option is given. Typer and typer-slim were measured and cost 45 to 180 ms of startup and several dependencies for a CLI that is mostly `dex <request>`. |
| Model runtime | Ollama; llama-server; llama.cpp in process, loaded every call; llama.cpp in a warm local process | **In a warm local process** that dex starts itself and reaches over a Unix socket. Offline, nothing to set up, and about a third faster than loading every call. |
| Output format | JSON with fields; free text; values-first shell line | **Values-first line**: pasteable as is, and a tiny model learns one line more reliably than JSON. |
| Safety | Block destructive commands; ask the model to flag them; local denylist | **Local denylist that warns.** Doesn't depend on the model, and the user still sees the command they asked for. |
| Shell integration | Run the command; copy to clipboard; `print -z` | **`print -z` and a zle widget**: the line becomes the next prompt's buffer, edited before Enter. |
| Context | Run each CLI (`aws configure list`, ...); read config files | **Read config files and env vars only.** Running CLIs would add seconds. |

## Out of scope for now

Other shells (bash, fish), executing commands, multi-command plans, Windows, and the dataset generator itself (built
separately and added to `training/` later).
