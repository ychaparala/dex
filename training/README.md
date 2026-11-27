# training: intent to command data for data engineering CLIs

Training data for a tiny local model (Qwen2.5-Coder-0.5B or smaller, LoRA, served by llama.cpp) that turns a typed
request like "submit a step" into one runnable command like `aws emr add-steps ...`. Every pair comes from a template
written against the CLI's official docs or source, and is checked offline against the real CLI parser where one can be
run here.

```
python nl2sh.py build                 # expand tools/*.yaml into data/train.jsonl and data/eval.jsonl
python nl2sh.py build --only kafka    # rebuild one tool (other tools' parts are reused)
python nl2sh.py add-tool flyte --binary pyflyte --category orchestration
python nl2sh.py lint | stats
python nl2sh.py setup-envs [--only dbt]          # create the pinned CLI versions used for validation
python nl2sh.py snapshot [--only dbt]            # record each pinned version's command and flag tree
python nl2sh.py refresh dbt --version 1.13.0 --install "pip:dbt-core==1.13.0"
python nl2sh.py predict --endpoint http://127.0.0.1:8080/v1   # run data/eval.jsonl through a llama.cpp server
python nl2sh.py score predictions.jsonl
```

Needs Python 3.10+ and PyYAML. Validation also uses whichever CLIs are installed (see Validation below); without them
the build still runs and checks bash syntax only. `NL2SH_CACHE` (default `~/.cache/nl2sh`) holds the per-version
environments.

## Format

One rigid shape, so a small model has one thing to learn.

- System prompt (short on purpose): `Turn the request into one bash command. Put editable values first as VAR=value, then && and the command.`
- User message: optional context lines, a blank line, then the request:
  ```
  AWS profile in use is data-prod
  Running EMR cluster is j-2AXXXXXXGAPLF (etl-nightly)

  <request>
  add a step and give me back the step id
  </request>
  ```
- Assistant: editable values first, then the command:
  ```
  CLUSTER_ID=j-2AXXXXXXGAPLF SCRIPT='s3://<bucket>/jobs/etl.py' && aws emr add-steps --cluster-id "$CLUSTER_ID" --steps "Type=Spark,Name=spark-step,ActionOnFailure=CONTINUE,Args=[--deploy-mode,cluster,$SCRIPT]" --query 'StepIds[0]' --output text
  ```

The joiner is `&&`, not `;`. When a value is unknown the model writes a placeholder such as `CLUSTER_ID=<cluster-id>`.
A bare placeholder is a redirect to bash, not a reliable stop. `X=<cid> && cmd` is a syntax error, but
`FILE=<file> LIMIT=10 && cmd` reads from a file named `file` and writes to a file named `LIMIT=10`: it fails only when
`file` doesn't exist, and when it does, `cmd` runs with `LIMIT` unset. A placeholder inside quotes
(`'s3://<bucket>/jobs/etl.py'`) is just text. dex doesn't rely on any of this: it never runs a line itself, it warns
when values still contain `<...>`, and it leaves the line in the buffer with the cursor on the first placeholder.
`&&` still matters for a value that fails for real, which `;` would ignore.

Each JSONL record is `{"messages": [system, user, assistant], "meta": {...}}`. `meta` holds `tool`, `template_id`,
`group`, `destructive`, `variant` (plain, context, version context), `valid_versions`, `checked_versions`, `verified`
(how the template was checked: help, source or docs), `validator` and `validation`. Training uses `messages` only.
`destructive: true` marks deletes, drops, terminations, overwrites, offset resets and the like, so dex can ask before
running them.

## Counts

| Tool | Priority | Templates | Train | Eval | Parser-checked pairs |
|---|---|---|---|---|---|
| airflow | core | 73 | 413 | 20 | 427 |
| astro | core | 39 | 165 | 8 | 147 |
| aws-core | core | 33 | 195 | 9 | 94 |
| aws-data | core | 41 | 223 | 11 | 227 |
| aws-emr | core | 53 | 353 | 17 | 314 |
| aws-s3 | core | 26 | 235 | 11 | 236 |
| confluent | core | 47 | 233 | 11 | 229 |
| curl | core | 49 | 242 | 11 | 103 |
| databricks | core | 54 | 332 | 16 | 327 |
| dbt | core | 54 | 300 | 14 | 300 |
| duckdb | core | 43 | 282 | 14 | 249 |
| kafka | core | 60 | 278 | 13 | 9 |
| snowflake-cli | core | 27 | 185 | 9 | 173 |
| snowsql | core | 17 | 112 | 5 | 0 |
| spark | core | 56 | 395 | 19 | 283 |
| airbyte | standard | 12 | 48 | 2 | 0 |
| argo | standard | 27 | 121 | 3 | 120 |
| azure | standard | 20 | 108 | 2 | 0 |
| clickhouse | standard | 11 | 62 | 2 | 0 |
| csvkit | standard | 17 | 98 | 2 | 0 |
| dagster | standard | 17 | 85 | 2 | 0 |
| dlt | standard | 15 | 69 | 2 | 0 |
| docker | standard | 19 | 89 | 2 | 0 |
| flink | standard | 19 | 98 | 3 | 0 |
| gcloud | standard | 47 | 224 | 5 | 0 |
| helm | standard | 14 | 72 | 2 | 0 |
| jq | standard | 25 | 140 | 3 | 14 |
| kcat | standard | 23 | 109 | 3 | 10 |
| kubectl | standard | 33 | 158 | 4 | 0 |
| meltano | standard | 19 | 74 | 2 | 0 |
| mysql | standard | 12 | 70 | 2 | 0 |
| prefect | standard | 21 | 94 | 2 | 0 |
| psql | standard | 22 | 108 | 2 | 0 |
| rclone | standard | 11 | 59 | 2 | 0 |
| rpk | standard | 22 | 101 | 2 | 99 |
| sqlite3 | standard | 10 | 74 | 2 | 8 |
| terraform | standard | 16 | 77 | 2 | 6 |
| trino | standard | 9 | 44 | 2 | 0 |
| yq | standard | 10 | 74 | 2 | 0 |
| total | | 1123 | 6199 | 245 | 3375 |

6,199 training and 245 eval pairs from 1,123 templates across 39 tool files; 4,131 pairs (two thirds) are for the
core tools. 3,375 pairs were parsed by the real CLI (the rest are syntax-checked only, see Validation); 392 are marked
destructive. Two templates were dropped by the parsers (wrong flags caught before they reached the data).

## Where the commands come from

Each `tools/<name>.yaml` lists the doc pages and source files its templates were written from (`sources:` with the
license each states), and each template names its own `source` and how it was checked (`verified: help | source |
docs`). Commands follow the docs; the request phrasings are original. Docs that are vendor-owned without an open
license are used for command syntax only, which is factual, and no doc prose is copied. Where a vendor's docs site
refused automated fetches, the templates were written from the CLI's source or `--help` output instead.

Licenses of the sources by tool:

| Tool | Sources |
|---|---|
| airflow, argo, dagster, dlt, prefect, spark, flink, kafka, dbt, snowflake-cli, kubectl, gcloud, helm, docker, trino | Apache-2.0 repos and docs (plus vendor docs for some) |
| aws-* | aws-cli source (Apache-2.0) and AWS docs (syntax only) |
| astro | Apache-2.0 with Commons Clause (astro-cli repo) |
| confluent | Confluent Community License (cli repo) and Confluent docs (syntax only) |
| databricks | Databricks License (databricks/cli repo), syntax only |
| rpk | Business Source License 1.1 (redpanda repo) and Redpanda docs (syntax only) |
| terraform | BUSL-1.1 (hashicorp web docs) |
| snowsql, clickhouse | vendor docs, syntax only |
| duckdb, kcat, csvkit, rclone, jq, yq, meltano, airbyte, azure, psql, mysql, sqlite3 | MIT, BSD, CC BY, PostgreSQL, public domain, GPL-2.0 (mysql man pages, syntax only) |

## Validation

Every pair is checked with `bash -n` after its values are filled in. On top of that, where the CLI can be run here,
the first command of the line is parsed by the real CLI, offline, without credentials and without contacting any
service:

| Validator | Tools | How |
|---|---|---|
| aws-parse | aws-core, aws-data, aws-emr, aws-s3 | AWS CLI run with fake keys and `--endpoint-url http://127.0.0.1:9`, so argument parsing runs and the request fails locally. v2-only commands are skipped (the pip-installable CLI is v1). |
| airflow-argparse | airflow | Airflow's own `get_parser().parse_args()`, on 3.4.0 and 2.10.5 |
| dbt-click, snow-click | dbt, snowflake-cli | Click `make_context` walk of the real command tree (dbt 1.12.7 and 1.9.11, snow 3.7.1) |
| spark-submit-opts | spark | options checked against `spark-submit --help` from pyspark 4.3.0 and 3.5.10 |
| *-help-parse | databricks, confluent, astro, argo, rpk | the real binary run with the command's flags plus `--help`; cobra parses flags before printing help, so an unknown flag or subcommand fails. Built from the official source tags: databricks 1.28.0 and 0.299.2, confluent 4.83.0, astro 1.48.0, argo 4.1.7 and 3.7.18, rpk 26.2.2 |
| duckdb-sql-parse | duckdb | every SQL statement through DuckDB's parser (`json_serialize_sql`) |
| jq-compile | any pair with a jq filter | `jq -n` compiles the filter |
| bash | everything else | syntax only; these tools were written from docs and source and are not parser-checked here |

Command text is split into words by bash with an empty PATH, no inherited environment and a scratch working directory,
so a `$(...)` inside a command can't run anything while the build checks it.

A template that fails its parser is dropped and the build prints why, so a wrong flag never reaches the data.

## Versions

CLIs change flags between releases, so the data is version aware.

1. Pinned versions. `versions:` in a tool's yaml lists the releases to check, newest first. Today: airflow 3.4.0 and
   2.10.5, dbt 1.12.7 and 1.9.11, spark 4.3.0 and 3.5.10, databricks 1.28.0 and 0.299.2, argo 4.1.7 and 3.7.18,
   snowflake-cli 3.7.1, confluent 4.83.0, astro 1.48.0, rpk 26.2.2. `setup-envs` builds each one in its own environment
   (pip venvs, or `go build` from the official tag for Go CLIs).
2. Checked against each version's real parser. The build validates every template on every pinned version.
3. Tagged pairs. `meta.valid_versions` lists the versions a pair parsed on. A template that only works on some versions
   is kept and tagged (for example Airflow 2 `dags trigger` syntax is tagged 2.10.5 only). A template whose own
   `versions:` spec says it should work on a version, and doesn't, is dropped.
4. Version in the prompt. For version-specific templates the build adds pairs with a context line such as
   `Installed versions: airflow 2.10.5`, so the model learns to switch syntax on it.
5. Runtime in dex. At startup (and cached per shell session) dex runs each tool's `version_cmd` (for example
   `airflow version`, `dbt --version`, `databricks --version`) for the CLIs it finds on PATH, keeps the version
   number, and passes `Installed versions: <version_label> <version>, ...` as a context line above the request.
   Only tools relevant to the request need to be listed, which keeps the prompt short.
6. Refresh. `refresh <tool> --version X` adds the release to `versions:`, builds it, snapshots its full command and
   flag tree to `snapshots/<tool>/<version>.json`, diffs it against the previous version
   (`snapshots/<tool>/diff-A-to-B.json`: commands and flags added and removed), lists templates that use a removed
   flag, re-hashes the doc pages in `sources:` and reports which changed, then rebuilds only that tool. Templates are
   only rewritten by hand; the diff says where to look.

Tools without `versions:` are checked against whatever is on PATH, and their `version_note` says which major version
the syntax targets.

## Adding a tool

1. `python nl2sh.py add-tool <name> --binary <cli> --category <category>` writes `tools/<name>.yaml` from a scaffold.
2. Fill in sources, templates (4 to 6 phrasings each, values as `$VAR`, one line of bash) and, if the CLI can be
   installed, `versions:` and `version_cmd`. `TOOL_SPEC.md` has every field and the rules.
3. `python nl2sh.py lint`, then `python nl2sh.py build --only <name>`. If the CLI has a parser the validator knows
   (aws, click, argparse, cobra), add it to the validator map in `nl2sh.py` so its pairs are parser-checked.

## Keeping the model small

- Short fixed system prompt and one output shape; nothing to learn about formatting.
- Commands are short: the longest assistant line is under 400 characters (about 130 tokens), the average under 100,
  so a whole example fits in 256 tokens. Training at a 512-token context is enough.
- At most 4 editable values per command, and common flags only. Rare flags make the model guess; the user can add them.
- Core tools (AWS, Airflow and Astro, Databricks, Snowflake, Confluent and Kafka, Spark, DuckDB, dbt, curl) get more
  pairs than the rest.
- Size against accuracy: 0.5B is the target. It should handle the core tools well; it will be weaker on rare
  subcommands and on long multi-pipe commands. 1.5B costs about three times the memory and latency for better recall
  of less common commands. `predict` and `score` on `data/eval.jsonl` give per-tool numbers to decide with (exact
  match, normalized match, right command head, flag F1, values match). Eval holds out whole request phrasings the model
  never saw, so it measures generalization, not memory.
- Output length: dex must allow about 200 new tokens per answer; a 64-token limit cuts off longer commands.

## How this fits into dex

This folder is the training half of dex. The prompt contract above is shared with `src/dex/prompt.py` (the system
prompt and the user turn) and `src/dex/context.py` (the wording of each context line); `tests/test_prompt.py` checks
dex's user turn against records in `data/eval.jsonl`, so a change on one side fails the tests until the other side
follows.

- `data/` is committed, so fine-tuning needs neither the validators nor the per-version CLI environments. Rebuild it
  with `python nl2sh.py build` after changing `tools/`, and commit the result with the change.
- `install:` in each tool config says how to install its CLI per OS. `python nl2sh.py installs` writes those to
  `src/dex/installs.json`, which dex ships and uses for its install hint when a CLI is missing
  (`TOOL_SPEC.md`, "Install commands").
- The per-version CLI environments live in `NL2SH_CACHE`, outside the repo.
- Install the training dependencies with `uv sync --extra train` (PyYAML, plus the fine-tuning stack once it lands).
- The generator is excluded from ruff and bandit in `pyproject.toml`: it shells out to the real CLIs on purpose, and
  it keeps its own style.

Not in dex yet: the `Installed versions:` line (step 5 under Versions), and the context lines for the main S3 bucket,
the running EMR cluster and the Azure subscription. Until they land, the model sees them only in training.
