# Missing CLI hint

Status: approved by the maintainer and implemented (PR #2). Written after the code, because PR #2 predates the design gate.
Design review done (see `review_log.md`); the maintainer chose its four open changes (H1 to H4), which are in.

## Problem

dex suggests commands for CLIs the user may not have. `dex first 10 rows of sales.parquet` gives a `duckdb -c ...`
line, and on a machine without duckdb it fails with "command not found", with nothing to say why or what to do.

## What the user sees

The command is printed exactly as before. For the programs in it that aren't on `PATH`, stderr says how to install
them on this OS. Programs with the same install command share a line, and the hints come after any destructive
warning:

```console
$ dex first 10 rows of sales.parquet
FILE=sales.parquet LIMIT=10 && duckdb -c "select * from '$FILE' limit $LIMIT"
dex: duckdb not found, install with: brew install duckdb
```

That is macOS. On Linux the same request says `install with: uv tool install duckdb-cli`.

- Hints are package-manager commands only (`brew`, `apt-get`, `snap`, `uv tool`, `pipx`), with no `-y`. Where a
  tool's only install on this OS is a vendor script (astro, databricks, rclone, helm, docker, aws, abctl and az on
  Linux), the hint gives its install page: `helm not found, see https://helm.sh/docs/intro/install/ to install it`.
- apt hints name the distribution: `jq not found, on Debian/Ubuntu install with: sudo apt-get install jq`.
- Only programs listed in `installs.json` get a hint. A shell function or alias isn't on `PATH` (`conda`, `nvm`), so
  checking every program would call it missing.
- When the installer itself (`brew`, `uv`, `sudo`, `apt-get` ...) is missing too, the hint names it:
  `... install with: <command> (needs uv)`. When dex knows how to get that installer (uv, pipx, brew, from
  `training/installers.yaml`), its own hint follows on the next line:
  `uv not found, see https://docs.astral.sh/uv/getting-started/installation/ to install it`. The maintainer asked for this
  after the review, when asking what happens without uv.
- When `uv tool install` or `pipx install` would put the CLI in a folder that isn't on `PATH`, the hint says so:
  `... install with: uv tool install csvkit, then add /home/me/.local/bin to PATH`.
- When a listed tool has neither a command nor a page for this OS: `duckdb not found, and dex doesn't know how to
  install it on freebsd14`.
  On an OS dex has no name for, the message uses Python's platform name (`win32`, `freebsd14`).
- `-q` hides the hints, like the other warnings.

## Side effects

None. dex runs nothing and changes nothing. What it reads:

- the words of the command it just generated, to find the programs it runs;
- `PATH`, with a few `stat` calls per program, and the same lookup for each installer a hint would use;
- `src/dex/installs.json`, shipped with dex and written from the tool configs, only when something is missing;
- `UV_TOOL_BIN_DIR`, `XDG_BIN_HOME`, `XDG_DATA_HOME`, `PIPX_BIN_DIR` and the home directory, to say where `uv tool` or
  `pipx` would put the CLI. That folder can appear in the hint, so a home path can show on stderr.

Startup is unchanged when everything is installed, and about 1 ms longer when a hint has to read the table.

## Product rules it touches

- **1, suggests and never acts:** the hint is text; nothing is installed.
- **2, only what was asked:** the command is unchanged; the hint goes to stderr.
- **5, read, never run:** `PATH` and environment variables only, no program is run to find a CLI or its version.
- **7, fast startup:** no new imports on the request path (`shutil.which` would cost 6 ms, so dex does the lookup).

## Alternatives

1. **Put the install step in the command** (`brew install duckdb && duckdb ...`), the first version of PR #2.
   Rejected: running a query would install software as a side effect, which breaks rules 1 and 2.
2. **Don't suggest commands whose CLI is missing.** Rejected on its own: when the request names a tool, or only one
   tool fits (kubectl, aws), there is nothing else to suggest, so dex would go silent. Steering the model toward
   installed tools when several fit is the follow-up change, with its own note.
3. **Say only "duckdb not found".** Cheaper (no install table), but leaves the user to look up the install for their
   OS. The table is generated from the tool configs and checked by `lint` and tests, so keeping it costs little.

4. **Say "duckdb not found" and add `dex install <tool>`** to print the install command on request. Keeps the
   normal output short, but costs a second command and a new subcommand to learn, for the one moment the user
   needs it. A hybrid is also possible: the hint for package-manager installs, a docs link for `curl | sh`
   installers (review finding H1, open).

The hint (this note) wins: the command stays what was asked, and the user learns why it would fail and how to fix it.
