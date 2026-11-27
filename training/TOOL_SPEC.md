# Tool config spec (tools/<name>.yaml)

One YAML file per CLI in `tools/`. The generator (`nl2sh.py build`) reads every file, expands templates into training
pairs, validates them, and writes train/eval JSONL. Add a CLI = add one file here (scaffold: `python nl2sh.py add-tool <name>`).

The target model is tiny (Qwen2.5-Coder-0.5B or smaller), so: favour common, everyday commands over rare flags; keep each
command short (ideally under ~150 characters before values, hard limit 300); one consistent style.

## File layout
```yaml
tool: duckdb                 # file name without .yaml
binaries: [duckdb]           # CLI binaries this file covers
category: databases          # orchestration|cloud|warehouses|databases|streaming|compute|ingestion|infra|files|http|setup
priority: core               # core (user's main stack, weighted up) or standard
target_pairs: 250            # rough number of training pairs wanted after expansion (core 200-400, standard 60-150)
version_note: "DuckDB 1.x CLI"   # which major version the syntax targets
version_cmd: duckdb --version     # optional: what dex runs at startup to learn the installed version
version_label: duckdb            # optional: name used in the "Installed versions:" context line
versions:                    # optional: pinned versions to validate against, newest (current) first
  - {version: "1.4.1", install: "pip:duckdb==1.4.1"}           # pip:<pkgs> [python: "3.12", constraints: <url>]
  - {version: "1.1.3", install: "go:github.com/org/repo@v1.1.3 (subdir ./cmd/x CGO)"}   # or go:<repo>@<tag>
install:                     # optional: how dex installs a missing CLI (see "Install commands" below)
  - macos: [brew install duckdb]
    linux: [brew install duckdb]
    docs: https://duckdb.org/docs/2.0/installation.html    # optional: the install page, for an OS with no command
    source: [https://github.com/Homebrew/homebrew-core/blob/HEAD/Formula/d/duckdb.rb]
sources:                     # every doc page / repo file you checked syntax against
  - url: https://duckdb.org/docs/stable/clients/cli/arguments
    license: "MIT (duckdb/duckdb-web repo)"   # license of that doc as stated by the vendor/repo, or "vendor docs, all rights reserved; facts only"
destructive_patterns:        # Python regexes; any match marks a pair destructive (in addition to the per-template flag)
  - '(?i)\bdrop\s+(table|view|schema|database)\b'
templates:
  - id: duckdb.parquet.head            # unique across the repo, dotted, starts with the tool name
    group: files                        # short sub-area
    intents:                            # 4-6 requests a data engineer would type, all meaning the same command
      - show the first {LIMIT} rows of {FILE}
      - peek at a parquet file
      - quickly read a parquet file with duckdb
      - what's in this parquet, first few rows
      - preview parquet data in the terminal
    vars:                               # editable values, in the order they appear at the start of the line
      - {name: FILE, default: data.parquet, samples: [sales_2025.parquet, events/part-0001.parquet], ctx: local_file}
      - {name: LIMIT, default: "20", samples: ["5", "50", "100"]}
    body: duckdb -c "select * from '$FILE' limit $LIMIT" | less -S
    destructive: false
    source: https://duckdb.org/docs/stable/clients/cli/arguments
    verified: docs                      # help | source | docs
    versions: ">=1.1"                   # optional: only valid on these pinned versions (e.g. "<3.0", ">=1.10,<2")
```

## Rules for templates
- intents: vary them — imperative, question, terse, with and without the tool name, casual. Lowercase is fine. An intent
  MAY mention a var as {VAR}; the generator fills a sample value and uses the same value in the command. At least one
  intent per template mentions no vars. Never mention a var that is not in `vars`.
- vars: every value a user would edit goes here and is referenced in body as "$VAR" (double-quote where splitting
  matters; inside a double-quoted SQL string use '$VAR' or $VAR as the SQL needs). `default` is a sensible concrete
  value (10, us-east-1, data.parquet) or an angle-bracket placeholder when none exists (<cluster-id>, <bucket>, <job-id>,
  lowercase-kebab). `samples`: 2-4 realistic fake values. `ctx` only when the value can come from the user's environment
  at runtime, one of: aws_profile, aws_region, emr_cluster, s3_bucket, local_file, databricks_profile,
  snowflake_connection, kafka_bootstrap, k8s_namespace, k8s_context, gcp_project, az_subscription, dbt_target,
  airflow_dag. Use at most 4 vars per template.
- body: one line of valid bash; no assignments (the generator writes `VAR1=... VAR2=... && body`). Fixed values stay
  inline. Pipes welcome where natural (| jq, | head, | less -S, | column -t, | sort | uniq -c, xargs) — about a quarter
  of templates. No sudo except package installs that need it.
- destructive: true for delete, drop, truncate, terminate, kill, overwrite, purge, destroy, uninstall, reset offsets,
  force operations that lose data or resources.
- Setup belongs in each tool's own file: installing the CLI (official method), configuring auth / profiles /
  connections, env vars, checking version, first login.
- Secrets only via env vars ("$DATABRICKS_TOKEN") or placeholders. No real account ids, keys, emails or hostnames —
  use example.com / placeholders.
- Write intents in your own words. Don't copy doc prose; command syntax is fact and fine to reproduce.

## Versions
- `versions:` lists the CLI versions the build checks every template against. Each one gets its own environment under
  `$NL2SH_CACHE/envs/<tool>-<version>/bin` (`python nl2sh.py setup-envs --only <tool>` makes it: pip installs into a
  venv, `go:` clones the official tag and runs `go build`). Without `versions:` the build uses whatever is on PATH.
- A template without a `versions:` spec is kept if it parses on at least one pinned version, and its pairs are tagged
  with the versions it parsed on (`meta.valid_versions`). With a spec, it must parse on every pinned version the spec
  covers, or it is dropped and the build prints why.
- When a template is only valid on some versions, the build writes some of its pairs with an extra context line
  `Installed versions: <version_label> <version>`, so the model learns to use the right syntax for the version dex
  detects at runtime with `version_cmd`.
- New release: `python nl2sh.py refresh <tool> --version X [--install SPEC]` adds it at the top of `versions:`, builds
  its environment, snapshots its command and flag tree, diffs that against the previous snapshot, re-hashes the doc pages
  in `sources:`, and rebuilds only that tool.

## Install commands
- `install:` tells dex how to put a missing CLI on PATH. When a command dex suggests uses a binary that isn't
  installed, dex prints the command unchanged and a hint on stderr (`duckdb not found, install with: brew install
  duckdb`). dex runs nothing.
- Each entry has commands under `macos`, `linux` and `any` (any OS, for Python packages: `uv tool install`, then
  `pipx install`), each a list in order of preference, and the `source` pages they come from (the vendor's install
  docs, or the Homebrew formula). The hint gives the first command for the OS, then for any OS, whose own programs
  (`brew`, `uv`, `curl`, `sudo` ...) are installed; with none, the first command and what it needs.
- `binaries` limits an entry to some of the file's binaries; without it, the entry covers them all. One file owns a
  binary's install commands (aws: `aws-core.yaml`); `lint` fails if two files install a binary differently.
- Package managers only: `brew`, `apt-get`, `snap`, `uv tool`, `pipx`. No downloaded script or installer
  (`curl ... | sh`), and no `-y`, so the user still confirms. Where the vendor's script is the only way for an OS, leave
  that OS out and set `docs:` to the install page: the hint then says `see <docs> to install it`. apt hints say
  "on Debian/Ubuntu".
- `installers.yaml` holds the same kind of entries for the installers themselves (uv, pipx, brew). When the
  chosen command needs one that is missing, dex prints its hint on the next line.
- Only commands that leave the CLI on PATH in the same shell, so dex's command works right after. A tarball unpacked in
  the working directory, or an installer that asks you to edit PATH, doesn't count: leave that OS out.
- After a change, run `python nl2sh.py installs`. It writes `../src/dex/installs.json`, which dex ships, and
  `tests/test_training.py` fails until it matches the configs.
