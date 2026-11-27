# dex design

Status: approved by the maintainer, second version, and implemented. Both versions had fresh reviews (findings and decisions in
`review_log.md`; ids like C4, S3 or N2 point there). This version folds in three decisions from the maintainer: no Typer, as few
runtime dependencies as possible, and a model that ships with dex and runs offline. Measurements behind the choices
are in §5 and §11.

## 1. Overview

`dex` is a Python CLI. It turns a plain-English request, plus a few facts about the local environment, into one
values-first shell line:

```
FILE=sales.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" | less -S
```

The model (a fine-tuned Qwen2.5-Coder-0.5B or smaller, quantized to GGUF) is installed with dex and runs on the
user's machine through llama.cpp, inside a Python process. dex makes no network connections and needs no API key or
server setup. A zsh integration puts the line on the next prompt for editing. dex never executes anything. Model
output is untrusted: dex validates its shape and checks it against a denylist, and the user reads it before pressing
Enter.

## 2. Package layout

```
src/dex/
  __init__.py     __version__ only
  cli.py          entry point: reads options, gathers context, parses and checks the answer (§3, §5)
  client.py       sends the prompt to the warm process, starting it if needed (§6)
  server.py       the warm process: holds the model and generates, nothing else
  engine.py       loads the GGUF and generates; the only module that imports the inference library
  context.py      context sources: read config files and env vars, return prose lines
  prompt.py       system prompt and user-turn format (the contract with the training data)
  parse.py        validate model output and return one clean line
  safety.py       destructive-command denylist
  shell/dex.zsh   zsh integration and its hand-written completion, printed by `dex --init zsh`
  py.typed
shell/zsh/dex.plugin.zsh   oh-my-zsh wrapper around src/dex/shell/dex.zsh
training/                  tool configs, fine-tuning, GGUF export into the model package
tests/                     pytest, one file per module, a fake engine (no model needed)
docs/                      architecture, adding a tool, training, designs/
```

Distribution name `dex-cli` (the name `dex` is a different project on PyPI), import name `dex`, console script
`dex`. Built with `uv_build`; installable with `uv tool install dex-cli`, `pipx` or `pip`. Runtime dependencies are
in §11. Training dependencies are the `train` extra; dev tools are the `dev` dependency group. `uv.lock` is
committed.

## 3. CLI surface

```
dex [OPTIONS] REQUEST...
```

| Option | Meaning |
| --- | --- |
| `--no-context` | send no local context |
| `--show-prompt` | print the prompt instead of generating |
| `-q`, `--quiet` | no warning on stderr for destructive commands |
| `--stop` | stop every warm process of this user now (any install or model) |
| `--init zsh` | print the zsh integration |
| `-h`, `--help`, `--version` | the usual |

| Env var | Default | Meaning |
| --- | --- | --- |
| `DEX_MODEL` | the bundled model | path to another GGUF, for development or a custom model |
| `DEX_IDLE` | `15` | minutes the warm process stays after its last request; `0` loads the model in the dex process on every call and starts no warm process |
| `DEX_THREADS` | performance cores (macOS `hw.perflevel0.physicalcpu`), else physical cores | inference threads (N10) |

Typer is gone, and so is argparse (N7): with seven options, a short loop over the leading arguments is simpler and
starts faster. It matches option names exactly (no abbreviations, so `--vers` is a usage error rather than a
surprise), stops at the first word that doesn't start with `-`, so `dex find files -mtime +7` keeps `-mtime +7` in
the request (C3), and treats `--` as the end of options. Request words are joined with spaces. `--help` prints a
fixed text.

stdout carries only the command line, so `$(dex ...)` captures it cleanly. Warnings and errors go to stderr, with
control characters escaped (S8).

Exit codes: `0` ok; `1` error (model missing or failed to load, unusable output); `2` usage; `3` a command was
printed and it matched the denylist; `130` interrupted with Ctrl-C, without a traceback (C7).

## 4. Prompt contract

Must match the dataset generator byte for byte, or the tiny model loses accuracy:

- System: `Turn the request into one bash command. Put editable values first as VAR=value, then && and the command.`
- User: zero or more context lines, a blank line if there were any, then `<request>\n{request}\n</request>`.
- `prompt.py` renders the ChatML string itself (`<|im_start|>system\n...<|im_end|>\n<|im_start|>user\n...`) and
  sends it as a raw completion, so the bytes don't depend on the inference library's template code (N8). A test
  compares it byte for byte with the training generator's rendering.
- Generation: greedy (temperature 0), at most 200 tokens, stop at `\n\n` or `</request>`.

## 5. Request path and latency

```
dex (standard library only)                               warm process (holds the model)
  read argv, gather context, build the prompt
  connect to the Unix socket (start the process if absent) ──► generate
  parse the text, run the denylist ◄── text and stop reason ◄──
  print the line
```

dex does everything that depends on the caller (cwd, environment, files) in its own process, so context and the
shell-variable check see exactly what the user's shell sees, `--show-prompt` never starts the model, and only the
rendered prompt crosses the socket (N3). The warm process only generates. On the request path dex imports only
`os`, `sys`, `socket`, `json`, `re`, `shlex` and a few small C modules. It avoids `pathlib` (6.6 ms to import on
Python 3.13), `typing` (2.7 ms) and `contextlib`, so paths are strings and records are small classes. Config files
are read with a few lines of string handling rather than `configparser` or `tomllib`. The denylist compiles a rule's
pattern only when one of its literal needles is in the line, because compiling all of them up front took 5 ms.

**Measurements.** Linux, 4 vCPU Xeon at 2.1 GHz, Python 3.13, llama-cpp-python 0.3.38 built from source with AVX2.
The model is a stand-in with exactly the shapes of Qwen2.5-0.5B (24 layers, 896 wide, 151,936-token vocabulary) in
Q4_0, 349 MB, with random weights; Hugging Face is blocked from this machine, and shapes, not weights, set the speed.
Each call is a 96-token prompt and a 32-token answer, about the size of the example in §1. Medians.

CLI startup, model excluded:

| | Time |
| --- | --- |
| bare `python -c pass` in a venv | 16 ms |
| Typer 0.29.3, `dex --help` / request path | 172 / 208 ms |
| typer-slim 0.21.2 (the last release that is really slim; 0.22 and later just install Typer), `--help` / request path | 76 / 132 ms |
| argparse prototype from the first version, `--help` / request path (urllib to a server) | 31 / 100 ms |
| prototype client over the Unix socket, no context | 26 ms |
| the real `dex` before trimming imports (pathlib, typing, all regexes compiled) | 32 ms `--help`, 50 ms request path |
| **the real `dex`, as implemented**, `--help` / `--show-prompt` / request path against an instant fake model | **21 / 22 / 31 ms** |

End to end, including inference:

| | Time |
| --- | --- |
| load the model in the dex process on every call | 1,580 ms |
| warm process, first call (starts it, loads the model) | 1,570 ms |
| warm process, later calls | 1,180 ms |
| warm process, later calls, reusing the cached system-prompt prefix | **1,060 ms** |
| the same with xllamacpp 2026.11.12361, the library dex uses (§11.1) | **950 ms** |

Where the time goes on a cold call: importing llama-cpp-python (it imports numpy) about 110 ms, loading the 349 MB
model through mmap about 260 ms, the prompt about 350 ms (96 tokens), and about 25 ms per answer token. The warm
process removes the import and load and, because every prompt starts with the same system prompt, skips evaluating
that prefix again: about a third faster than loading every call. It is the default; `DEX_IDLE=0` turns it off.

This VM is slow for inference; an Apple Silicon laptop is typically several times faster per token, and I couldn't
measure one here. Before the 0.5B model and the warm process are final, they are measured on an Apple Silicon Mac
next to an accuracy gate (the pass rate on held-out requests), because a faster machine shrinks the warm process's
advantage and a smaller model may be accurate enough. With the real model the answer length, not the CLI, decides
the time, so the training data should keep commands short.

## 6. Warm process

- **One process per install and model (N2).** The socket name is a hash of the dex version, `sys.prefix`, the model
  file's path, size and mtime, and the thread count. A pipx install and a dev venv, or two `DEX_MODEL` values, each
  get their own process instead of restarting each other. An old process after an upgrade is never asked again and
  exits when its idle time runs out.
- **Location (N4).** Linux: `$XDG_RUNTIME_DIR/dex/`, else `/tmp/dex-<uid>/`. macOS: `$TMPDIR/dex/` (per-user, `0700`
  and local). dex checks the socket path fits in `sun_path` (104 bytes on macOS, 108 on Linux) before using it, and
  falls back to `/tmp/dex-<uid>/` if not. Never a home directory, which may be on NFS.
- **Permissions (N5).** dex creates the directory `0700` and, with `lstat` (so a symlink fails), refuses it unless
  it is a real directory owned by `geteuid()` with no group or other access. The socket must be a socket owned by
  the same uid. Each side checks the other's uid on connect with the platform's peer-credential option
  (`SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS) and drops a mismatch. Under `sudo -E`, root's check fails against the user's
  directory and dex runs in process instead. Unix sockets only, no TCP port, so nothing on the network can reach
  it.
- **Start (N6).** If nothing answers, dex takes an `flock` on a lock file next to the socket (released when its
  holder dies, even after a crash), checks the socket again, and starts `python -m dex.server` detached in its own
  session, with stdin on `/dev/null` and stdout and stderr in `server.log` (capped at 1 MiB, so llama.cpp's own
  errors land there). It then waits for the socket, polling the child: if the child exits, dex fails at once with
  the last lines of the log; after 30 s it gives up. If the warm process can't be used at all, dex loads the model
  in its own process and says so on stderr once.
- **Protocol.** One JSON line each way per connection. Request: the rendered prompt (§4) and the generation
  settings. Reply: the generated text and whether generation stopped at the token limit, or an error. Every read
  has a timeout (60 s by default) so a hung process can't freeze the shell or the key binding; a full listen queue
  (`EAGAIN`) counts as busy and is retried, not as a missing process.
- **Life (N1).** One request at a time; later ones wait in the listen queue. The process exits after `DEX_IDLE`
  minutes without a request, on `dex --stop`, or on SIGTERM. Before exiting it removes the socket only if the path
  still points at the inode it bound, so it can never delete a newer process's socket. A client that sees the
  connection closed without a reply retries once. Memory while it runs: about 630 MB resident, of which 365 MB is
  the mmap'd model file (shared and reclaimable page cache) and the rest llama.cpp's repacked weights and buffers.
- **Platforms.** Linux and macOS. Windows is out of scope, as before.

## 7. Context gathering

Unchanged from the first version. It runs in the dex process, so it sees the user's own cwd, environment and
home directory.

A source is `(name, keywords, lines() -> list[str])`. A source runs when one of its keywords is a word of the request
(multi-word keywords match as substrings); `sales.parquet` splits into `sales` and `parquet`. `--no-context` runs
none. Sources read env vars and small config files only (capped at 256 KiB), never run programs or touch the network,
and return nothing when a file is missing or unreadable.

A source reports only what the tool itself would use, and nothing when that is uncertain; there are no "first entry"
fallbacks (C8). Every value is checked before it is used: values with whitespace, control characters, `<` or `>`,
or longer than 64 characters are skipped, and inline `#` or `;` comments in config files are dropped (C9, S7).

| Source | Reads | Line |
| --- | --- | --- |
| aws | `AWS_PROFILE` or `AWS_DEFAULT_PROFILE`, else `default` if `[default]` or `[profile default]` exists; region from `AWS_REGION`, `AWS_DEFAULT_REGION` or that profile in `AWS_CONFIG_FILE` or `~/.aws/config` | `AWS profile in use is X`, `Default AWS region is Y` |
| databricks | `DATABRICKS_CONFIG_PROFILE`, else `DEFAULT` if it exists in `DATABRICKS_CONFIG_FILE` or `~/.databrickscfg` | `Databricks CLI profile is X` |
| snowflake | `SNOWFLAKE_DEFAULT_CONNECTION_NAME`, else `default_connection_name` in `config.toml`, else a connection named `default` (under `SNOWFLAKE_HOME` or the standard locations) | `Snowflake connection is X` |
| kafka | `KAFKA_BOOTSTRAP_SERVERS` or `BOOTSTRAP_SERVERS` (first server) | `Kafka bootstrap server is X` |
| kubernetes | the first file in `KUBECONFIG` (or `~/.kube/config`) that sets `current-context`, and that context's namespace; JSON kubeconfigs are skipped (shortcut) | `kubectl context is X`, `Kubernetes namespace is Y` |
| gcp | `CLOUDSDK_CORE_PROJECT` or `GOOGLE_CLOUD_PROJECT`, else `project` in the active configuration (`CLOUDSDK_ACTIVE_CONFIG_NAME` or `active_config`, under `CLOUDSDK_CONFIG` or `~/.config/gcloud`) | `GCP project is X` |
| dbt | `DBT_TARGET` | `dbt target is X` |
| airflow | `*.py` stems in `$AIRFLOW_HOME/dags` or `./dags`, first 5 (shortcut: file names stand in for DAG ids) | `Airflow DAGs here: a b` |
| files | the working directory, data files first, first 8, no dotfiles | `Working directory contains: a.parquet b/` |

The wording and the set of sources match the generator's context table.

## 8. Output parsing

`parse.parse()` turns the model's text into one line or raises `ParseError` (exit 1):

1. Take the first non-empty line that isn't a code fence; strip a leading `$ ` or `% `, and strip backticks only
   when one pair wraps the whole line (C5).
2. Reject the line if it holds C0 control characters other than tab, DEL or C1 characters (S8), if its quotes don't
   balance or it ends in `\` (`shlex.split` fails; C4), if generation stopped at the token limit (C4), or if it
   holds only assignments and no command.
3. Return the line as is, plus the names of the leading assignments. Those are found with one regex: `NAME=value`
   tokens (`NAME` is `[A-Z_][A-Z0-9_]*`; values bare, single- or double-quoted) separated by spaces or `;`, followed by
   `&&` or `;` (P1). The safety check uses the names (§9).

A line that doesn't start with assignments, or uses the env-prefix form `A=1 cmd`, is still printed as is.

## 9. Destructive-command check

`safety.check(line, assigned, environ)` runs in the dex process and returns the names of matching rules; any match means a stderr warning
(unless `-q`) and exit 3. It warns rather than blocks. False positives are acceptable, misses are not. Rules, checked
against the whole line including SQL inside quotes:

- **Files and shell:** `rm` anywhere (`/bin/rm`, `\rm`, inside `bash -c '...'` or `ssh h "..."`), `hdfs -rm`,
  `-put -f`, shred, dd `of=`, mkfs, wipefs, `truncate -s`, `find -delete`, `mv`, `cp -f`, `tee` without `-a`,
  `>` and `>|` redirects except to `/dev/null` or `&N`, `chmod`/`chown -R`, `crontab -r`, kill, piping into `sh`,
  `bash`, `zsh` or `python`, `rsync --delete`/`--remove-source-files`, `curl -X DELETE` (S1).
- **SQL:** drop, truncate, delete from, `insert overwrite`, `create or replace`, `alter ... drop`, `update ... set`
  without `where`, `copy ... to`, Snowflake `remove @stage`, `flushall`/`flushdb` (S2).
- **CLIs:** generic verbs (delete, destroy, terminate, purge, deregister, uninstall, wipe, remove), also inside
  hyphenated or suffixed names such as `delete-bucket` and `kafka-delete-records.sh`; remove subcommands of aws s3,
  gsutil, gcloud storage, azcopy, databricks fs and docker; `gsutil rsync -d`; `bq --replace` and `bq cp -f`;
  `--force` and `-f` with bq; `kafka-configs --alter`; Kafka offset resets; `kubectl drain`, `scale --replicas=0`;
  `docker compose down -v`; `airflow db reset/clean`, `airflow tasks clear`; `dbt --full-refresh`, `dbt clean`;
  git history rewrites, `branch -D`, `stash drop`; terraform destroy; overwrite modes (S2).
- **Shell variables:** an assigned name that is already set in the caller's environment, or is on a reserved list
  (`PATH`, `HOME`, `IFS`, `HOST`, `USER`, `SHELL`, `PS1`, `PROMPT`, `ZDOTDIR`, `LD_*`, `PYTHONPATH`, and every
  variable the context sources read), because `VAR=value && cmd` keeps the value in the user's shell (S3).

Added in implementation, after running every training answer through the rules: `cancel`, `abort`, `stop` and
`drop` as CLI verbs (`databricks jobs cancel-run`, `aws athena stop-query-execution`, `dlt pipeline drop`),
`spark-submit --kill`, `rpk group seek`, `kafka-storage format`, in-place edits (`sed -i`, `yq -i`, `perl -i`),
`rclone sync`, `move` and `purge`, and database restores (`pg_restore` without `-l`, `mysql` or `psql` reading a
file or a pipe). With them all 392 pairs the data marks destructive warn; `tests/test_safety.py` keeps it that way.
About 5% of the other pairs warn too (mostly redirects that write a file, `curl ... | sh` installers and
`copy ... to`), which the "false positives are acceptable" rule allows. `<placeholders>` are not redirects.

The code review of the implementation added: redirects to a quoted target (`> "$OUT"`), `2>` and `&>`; `tee`
after `sudo` or a path; `rm` and `mv` at the end of a line (`| xargs rm`); every `docker ... prune`; zsh's own
variables (`COLUMNS`, `SECONDS`, `LINES`, `TZ`, `LC_*` and others) on the reserved list; and the shell-variable
check now sees every assignment that persists, not only the leading group (`A=1 && PATH=/x && ls`,
`export PATH=...`). Context values are limited to the generator's safe characters (`A-Z a-z 0-9 _ . / : @ % + , =
-`), so a hostile file name can't put backticks or `$(...)` in front of the model. Template variables that clashed
with a reserved name were renamed (`HOST` to `CONN_HOST` or `WORKSPACE_HOST`, `ENV` to `COMPOSER_ENV` or
`MELTANO_ENV`, `SECONDS` to `EXPIRES_IN`, `COLUMNS` to `COLUMN_LIST`), and the generator's lint now rejects such a
name. The 16 pairs that still warn are the ones that ask to `export AWS_PROFILE` and `AWS_REGION`, where the
warning is the point.

Obfuscated commands (`$(echo rm)`) are out of reach of a denylist; keeping attacker-chosen text out of the prompt
(§7) is the defence there.

## 10. zsh integration

`dex --init zsh` prints `dex.zsh`, loaded with `eval "$(dex --init zsh)"`; oh-my-zsh users link `shell/zsh/` as a
plugin, which sources the file from a checkout or evaluates `dex --init zsh` only when its first line is dex's header
(S10). It defines:

- `alias dex='noglob dex'`, so `*`, `?` and `[` reach dex unchanged (C2). Requests with `> | ; ' " #` still need
  quotes or the key binding.
- a `dex` function: when stdout is a terminal and no leading option is an info option (`-h`, `--help`, `--version`,
  `--show-prompt`, `--init`, `--stop`, with `--opt=value` forms), it runs `command dex`, and on exit 0 or 3 pushes
  the line onto the next prompt with `print -rz` (C1, S6). It returns dex's exit code. Otherwise it calls
  `command dex`.
- a zle widget bound to `^X^D` (`DEX_KEY`, empty to disable) that replaces the typed request with the command,
  records `# dex: <request>` in history with `print -rs`, captures stderr separately and shows it with `zle -M`, so
  stray warnings never enter the buffer (C10, S10).
- completion for the options, from the hand-written `_dex` (Typer's generated completion is gone with Typer).

## 11. Dependencies and packaging

**Runtime dependencies: two, and both are the model.** The CLI itself is standard library only.

| Dependency | Why it can't be avoided |
| --- | --- |
| `xllamacpp` | runs the GGUF in process (§11.1). Writing our own bindings means building and shipping llama.cpp for every platform ourselves. |
| `dex-model` | the model file (§11.2). Data only, no code. |

Gone compared with the first version: `typer` (and through it pydantic, rich, shellingham and annotated-doc),
urllib, the endpoint and API key settings, and the need to run `llama-server`. The Makefile's `serve` target goes too.

### 11.1 Inference library

| | llama-cpp-python 0.3.38 | xllamacpp 2026.11.12361 |
| --- | --- | --- |
| Install | source only on PyPI: needs a C compiler and CMake, and took 2.5 minutes here | prebuilt abi3 wheels (9 to 37 MB) for macOS arm64 and x86_64, Linux x86_64 and aarch64, Windows; installed in under a second |
| Pulls in | numpy, jinja2, markupsafe, diskcache, typing-extensions | nothing |
| Import | 110 ms | 20 ms |
| Model load | 260 ms | 620 ms |
| Warm call, 96-token prompt, 32-token answer | 1,180 ms | 1,070 ms |
| Same, reusing the cached system-prompt prefix | 1,060 ms | **950 ms** |
| License | MIT | MIT |

**Chosen: xllamacpp**, measured after approval on the same stand-in model and machine. `pip install dex-cli` needs no
compiler, it adds no other packages, and warm calls are about 10% faster. Its slower load only affects the first
call. xllamacpp wraps llama.cpp's server code and by default also listens on a random TCP port on 127.0.0.1; dex
points that listener at a Unix socket inside its own `0700` runtime directory, so the warm process opens no TCP
port (checked). Its Linux CPU wheels are built for broad compatibility rather than each machine's newest
instructions; macOS wheels are fully optimized. `engine.py` keeps the choice to one file.

### 11.2 Shipping the model

A Qwen2.5-Coder-0.5B Q4_K_M GGUF is about 400 MB. PyPI's default limit is 100 MB per file (200 MB for projects owned
by an organization) and 10 GB per project; an increase is requested on PyPI's support tracker after one release under
the limit has been uploaded, and is granted case by case.

| Option | Offline after install | Cost |
| --- | --- | --- |
| A. Model inside the `dex-cli` wheel, with a file-limit increase | yes | every CLI release re-uploads 400 MB, and the 10 GB project limit lasts about 25 releases; depends on PyPI granting the request |
| B. Separate `dex-model` package that `dex-cli` depends on, with a file-limit increase for it | yes | one request; the model uploads only when it changes; CLI releases stay small |
| C. A smaller model under 100 MB | yes | Qwen2.5-0.5B can't get there: its 151,936-token vocabulary alone is 136M of its 494M parameters, about 77 MB at 4 bits. Under 100 MB means a model of about 135M parameters (for example SmolLM2-135M at about 100 MB in Q4_K_M), which would also be about three times faster but may not be accurate enough; only training will tell |
| D. Download once on first run (`dex setup`), from GitHub Releases, pinned by SHA-256 | no: one download after install, then offline | no PyPI request; breaks behind firewalls and in air-gapped installs, which is exactly where offline matters |
| E. The GGUF split with llama.cpp's `gguf-split` into parts under 100 MB, one wheel package each (`dex-model-1` to `dex-model-5`), which `dex-model` depends on (N9) | yes | no PyPI request needed; five extra package names; llama.cpp loads split files natively |

**Recommendation: B, with E as the fallback.** B keeps the promise that nothing touches the network after
`pip install`, puts one large file on PyPI only when the model changes, and lets the CLI ship as often as it likes.
If PyPI declines the request, E keeps the same promise without one. The training work should also try C, because a
model that small would need neither and would cut latency. D is not planned, because it breaks the offline promise.
The model packages ship wheels only, never an sdist.

`dex-model` installs the GGUF as package data; `engine.py` finds it with `importlib.resources`, and `DEX_MODEL`
overrides it. Its version is pinned exactly by `dex-cli`, because the prompt contract and the model are trained
together.

## 12. Training layout

`training/tools/<name>.yaml` per CLI (doc sources, templates with intents and values, destructive patterns, target
count), `training/data/` for generated JSONL (git-ignored), `training/finetune/` for LoRA, merge and GGUF export into
the `dex-model` package. The generator comes from separate work and is not part of this change; two placeholder
configs show the shape. Notes for that work are at the end of `review_log.md`.

## 13. Quality gates

- ruff: lint rules E, W, F, I, B, UP, SIM, N, D (Google docstrings), RUF and PTH, plus `ruff format`; line length
  100; target Python 3.11. Security rules come from bandit, not ruff's `S` set (P3).
- mypy `--strict` on `src` and `tests`; `py.typed` ships.
- bandit on `src`, configured in `pyproject.toml`; each finding fixed or suppressed with a narrow `# nosec` and a
  reason.
- pytest with branch coverage, `fail_under = 90`.
- pre-commit: hygiene hooks, ruff, ruff-format, and local mypy and bandit hooks run through uv.
- CI (GitHub Actions, `astral-sh/setup-uv`): lint, format, types and bandit once; tests on Python 3.11 to 3.15;
  `zsh -n` on the shell files; a startup check that fails if `dex` imports the inference library or anything else
  beyond its allowed modules on the request path.
- No Makefile (P4): `uv sync`, `uv run` and `uv build` cover everything.

## 14. Testing

One test file per module. A fake engine (returns set text) stands in for the model, so tests and CI need no model
file. The client and server tests run a real warm process (with the fake engine) on a short temporary socket directory:
start on demand, two clients racing to start it, a child that dies at once, idle exit without removing a newer
socket, `--stop`, a symlinked or group-writable directory, and a path too long for `sun_path`. Context tests point `HOME`, the working
directory and env vars at `tmp_path` with `monkeypatch` (P9). Safety tests hold lists of commands that must and must
not match, including every example from the review. A zsh test runs the function under `zsh -f` and checks that a
line with `\n` and `\c` reaches the buffer unchanged. One optional test, skipped without `DEX_MODEL`, runs a real
GGUF end to end.
