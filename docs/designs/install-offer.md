# Offer to install a missing CLI

Status: approved by the maintainer with option A ("dex runs it") and implemented (PR #4). The install is an intermediate
step: dex asks before the command is printed, so the command comes after the install. Reviewed once (see
`review_log.md`, "Review of install-offer"). Builds on `missing-cli-hint.md` (PR #2), whose hints stay as the
fallback.

## Problem

When a CLI the command needs is missing, dex prints a hint (`duckdb not found, install with: uv tool install
duckdb-cli`) and stops. The user has to copy the install command, run it, then go back to the suggested command.
The maintainer asked for dex to say the CLI is missing and ask "install it?" before it finishes, installing at user level by
default.

## When dex asks

All of these must hold, else dex prints the hint from PR #2 as today:

- **The zsh integration ran dex.** The `dex` function sets `DEX_ASK=1` for its own call. A plain `dex` in a
  script, a pipe or the user's own `x=$(dex ...)` never waits for an answer. The Ctrl-X Ctrl-D widget doesn't set
  it, because zle owns the terminal there. `-q` turns it off too.
- **stdin and stderr are terminals.**
- **The install is user level:**
  - `uv tool install` or `pipx install`, with uv or pipx on `PATH` and its tool folder on `PATH` (otherwise the
    installed CLI wouldn't run, so the hint stays, with its "add ... to PATH");
  - or `brew install` of a core formula (no cask, no `tap/` name), with `brew` on `PATH` and its prefix writable by
    the user (an `os.access` check, no program run).
  - Never a command with `sudo` (apt, snap), an install page, or a `<placeholder>` (dbt's `dbt-<adapter>`).
- **It is the first such CLI in the line.** One question per run. Other missing CLIs get their hints.

The command in the question is chosen by the same rule as the hint, limited to user-level commands, so the hint
and the question never disagree.

## When the installer itself is missing

The hint and the question take the first command for the OS, then for any OS, whose installer is on `PATH`
(PR #2's rule). For a Python CLI such as duckdb on Linux that order is `uv tool install`, then `pipx install`:

- **No uv, pipx present:** dex offers `pipx install duckdb-cli` instead.
- **Neither:** no question. Today the hint reads `duckdb not found, install with: uv tool install duckdb-cli (needs
  uv)` and stops there, so the user still has to find out how to get uv. That is a gap in PR #2 too.
- On macOS, brew comes first; without brew the hint reads `... brew install duckdb (needs brew)`, the same gap.

Proposed fix: give the installers their own entries in `installs.json` (uv: `brew install uv`, `pipx install uv`,
and its install page on Linux, where its official installer is a script; pipx: `brew install pipx`, `sudo apt-get
install pipx`; brew: its install page). When the chosen command needs a missing installer, dex adds that
installer's hint as a second line:

```console
dex: duckdb not found, install with: uv tool install duckdb-cli (needs uv)
dex: uv not found, see https://docs.astral.sh/uv/getting-started/installation/ to install it
```

dex never asks to install the installer: one question per run, about the CLI the command needs. `pip install --user`
isn't used as a fallback, because it mixes the CLI's dependencies into the user's Python. This fix needs no rule
change. The maintainer chose to land it in PR #2, and it is there (`training/installers.yaml`).

## What the user sees

The install is a step before the answer: dex asks once it knows which CLI the command needs, before it prints the
command. The command isn't repeated above the question, because it lands on the prompt right after:

```console
$ dex first 10 rows of sales.parquet
dex: duckdb isn't installed. Install it with: uv tool install duckdb-cli? [y/N] y
...uv's own output...
$ FILE=sales.parquet LIMIT=10 && duckdb -c "select * from '$FILE' limit $LIMIT"
```

After the install dex looks on `PATH` again, so a CLI that is now there gets no hint.

- **Enter, n or anything else:** no install, the hint is printed, and the command lands on the prompt as today.
- **Ctrl-C or Ctrl-D at the question:** same as n. dex catches it, so the command still lands on the prompt.
- The destructive warning, when there is one, prints before the question.

## Option A: dex runs the install

On **y**, dex runs the command as an argument list (split with `shlex`, no shell, each `&&` step in turn, stopping
at the first failure), using the installer found on `PATH`. Its stdout and stderr go to the terminal's stderr, so
nothing ends up on the prompt. Then the suggested command lands on the prompt.

- Install fails or the installer can't start: `dex: install failed (exit N)` or `dex: install failed: <reason>`, the command still lands on the prompt, dex's exit code is unchanged.
- Ctrl-C during the install: the installer gets it, dex prints `install stopped`, and the command still lands on the
  prompt. A half-finished `uv`, `pipx` or `brew` install is left to that tool, which cleans up or retries on the next
  run.
- `brew install` may run `brew update` first and upgrade formulae the new one depends on. The question says
  `(Homebrew may update other packages)` when the installer is brew.

Side effects: the installer downloads from the network and writes in the user's own folders. dex itself still
writes only its runtime files. The rule wording, now in `AGENTS.md`:

- **Rule 1:** "The one exception: when a CLI the command needs is missing, dex may ask to install it, and runs the
  install only after the user types y. The install comes from dex's own install table, never from the model, needs
  no root, and is shown in full in the question."
- **Rule 4:** "dex itself makes no network calls. The one exception is an install the user said yes to under rule
  1, which the installer runs."

## Option B: dex queues the install on the prompt (not chosen)

On **y**, dex prints the install command as a second stdout line, and the zsh function pushes the suggested
command, then the install, onto zsh's buffer stack (`print -z` twice). The next prompt shows
`uv tool install duckdb-cli`, ready to run with Enter or to edit. After it runs, the suggested command appears on the
following prompt.

- dex runs nothing and makes no network call. Rules 1, 2, 4 and 6 stay as written: the install is a separate line
  the user asked for and runs themselves, and the command itself is unchanged.
- Costs: the zsh function reads two stdout lines instead of one (only after a yes), and the user presses Enter once
  more. Outside the zsh integration there is no question, so nothing changes there.

## Product rules, both options

- **2, only what was asked:** kept. The suggested command is unchanged and nothing is chained onto it.
- **3, values first:** kept, for the same reason.
- **5, read, never run:** kept. Checking the brew prefix is an `os.access` call.
- **6, warn, don't block:** the question waits for an answer, but only in the zsh function, where the user is at
  the terminal, and Ctrl-C or Enter always gets the command.
- **7, fast startup:** no cost unless a CLI is missing; in option A, `subprocess` is imported only after a yes.
- **10, model output is untrusted:** the install command comes from `installs.json`, never from the model; the
  model only decides which CLI is missing.

## Alternatives

1. **Hints only** (PR #2 today). Keeps every rule but leaves the copy-and-run to the user.
2. **Put the install on the suggested line** (`uv tool install duckdb-cli && FILE=... && duckdb ...`) after a yes.
   Rejected: it chains a step onto the command (rule 2) and puts it before the values (rule 3), and the install
   reruns each time the line does.
3. **`dex install <tool>`,** a subcommand the hint points to. No question mid-request, but one more command to learn,
   and it needs the same rule 1 and 4 changes as option A.
4. **Option A,** what the maintainer described: one keypress and the CLI is installed.
5. **Option B,** proposed by the reviewer: the same question and one more Enter, with no rule change.

The reviewer recommended B, because dex would still never run anything. The maintainer chose A: one keypress and the CLI is
installed, then the command arrives. The install is shown in full in the question, comes only from dex's own table,
and needs no root, which is what the rule 1 exception allows.

## Defaults taken

- The default answer is `[y/N]`: Enter means no.
- `sudo` installs (apt, snap) are never offered; they keep the hint.
