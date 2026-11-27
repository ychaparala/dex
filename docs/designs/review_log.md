# Design review log

Draft: `design_draft.md` (first version). Three reviewers read only the draft and the code it references, each with one
lens. 29 findings; 24 accepted in full or in part, 5 declined. Section numbers refer to the revised `design_draft.md`.

## Correctness and edge cases

| # | Finding | Decision |
| --- | --- | --- |
| C1 | `print -z` interprets backslashes, so `\t`, `\n` and `\c` change the command | **Accepted.** `print -rz` and `print -rs` (§9). |
| C2 | zsh globs and redirects unquoted request words (`> 100`, `*.csv`, `what's`) | **Accepted.** `alias dex='noglob dex'`; docs point to the key binding or quotes for `> \| ; ' " #` (§9). |
| C3 | Request words starting with `-` are parsed as options (`-mtime +7`, `-h`) | **Accepted.** Options must come first; parsing stops at the first request word. The wrapper only looks at leading options (§3, §9). |
| C4 | Multi-line or truncated output prints a broken command with exit 0 | **Accepted.** Exit 1 on unbalanced quotes, a trailing `\`, or `finish_reason: "length"` (§6, §7). |
| C5 | Stripping backticks and `> ` corrupts `` echo `date` `` and `> out.txt` | **Accepted.** Backticks are stripped only when one pair wraps the whole line; `> ` is no longer a prompt marker (§7). |
| C6 | Proxy env vars route the "local" request through a proxy | **Accepted.** dex never uses proxies (§6). |
| C7 | Timeouts reported as "unreachable"; `HTTPException`, `ValueError` and Ctrl-C give tracebacks; timeout semantics unclear | **Accepted.** Separate timeout message; both caught as errors; Ctrl-C exits 130 quietly; timeout documented as per connect and per read (§6). |
| C8 | Context claims a profile is "in use" when the CLI would use another | **Accepted.** Only what the tool would really use: env var, `default`/`DEFAULT`, the configured default connection, the active gcloud config, the first kubeconfig file that sets `current-context`. No first-entry fallbacks (§5). JSON kubeconfigs are skipped, marked as a shortcut. |
| C9 | Context values with spaces, newlines, `</request>` or inline comments break the prompt or the command | **Accepted in part.** Values with whitespace, control characters, `<` or `>`, or over 64 characters are skipped; inline comments are dropped (§5). Not flagging the `A=1 cmd` env-prefix form, because it is a valid command. |
| C10 | Widget merges stderr into the buffer; `--init=zsh` form; `DEX_BIN` splitting; exit 3 swallowed; Ctrl-C in the widget | **Accepted.** stderr captured separately; leading `--opt=value` forms handled; `DEX_BIN` removed (P7); the wrapper returns dex's exit code (§9). |

## Security and safety

| # | Finding | Decision |
| --- | --- | --- |
| S1 | Generic destructive forms missed: `>` redirects, `truncate -s`, `/bin/rm`, `bash -c 'rm ...'`, `mv`, `cp -f`, `tee`, `chmod -R`, `crontab -r`, `wipefs`, `curl \| sh`, `curl -XDELETE` | **Accepted**, except `$(echo rm) -rf`: a denylist can't catch obfuscation, so sanitising context (S7) is the defence there (§8). |
| S2 | Tool-specific misses: `gsutil rsync -d`, `bq --replace`/`cp -f`, `snow stage remove`, `REMOVE @stage`, `kafka-delete-records`, `kafka-configs --alter`, `kubectl scale --replicas=0`/`drain`, `compose down -v`, `hdfs -put -f`, `rsync --remove-source-files`, `azcopy remove`, `dbt clean`, `git branch -D`/`stash drop`, `FLUSHALL`, `UPDATE` without `WHERE`, `ALTER ... DROP`, `COPY ... TO` | **Accepted** (§8). Generating safety tests from each tool config's destructive patterns is deferred until the generator lands in `training/`. |
| S3 | `VAR=value && cmd` overwrites variables in the user's shell (`AWS_PROFILE=prod`, `PATH=...`) | **Accepted.** Warn (exit 3) when an assigned name is already set in the environment or is on a reserved list (§8). The generator should avoid such names; passed on to the training-data work. |
| S4 | Context and the API key go through a proxy in clear text | **Accepted.** Same fix as C6. |
| S5 | Redirects bypass the scheme check and copy `Authorization`; remote plain-http endpoints accepted silently | **Accepted in part.** Redirects are refused; `DEX_API_KEY` is never sent over plain http to a non-loopback host. Not warning on every non-loopback endpoint, because the user set it on purpose (§6). |
| S6 | `print -z` changes the line after the check ran | **Accepted.** Same fix as C1. |
| S7 | Attacker-chosen file or profile names can inject prompt structure | **Accepted.** Same fix as C9. Model output is untrusted, and the denylist is the only check on it (§7). |
| S8 | Control characters and ANSI escapes in output can disguise a command | **Accepted.** Exit 1 on C0 (except tab), DEL or C1 characters in the line; error text is escaped before printing (§7). |
| S9 | Editing a value can make a command more destructive than what was checked | **Accepted in part.** `rm` is always flagged already. Using `${VAR:?}` in destructive templates is passed on to the training-data work. Declined for now: re-checking the edited buffer on Enter, which needs an accept-line hook nobody asked for. |
| S10 | Widget stderr in the buffer; the plugin evaluates whatever `dex` is on `PATH` (`pip install dex` is another project) | **Accepted.** stderr separate (C10); the plugin evaluates `--init` output only when it starts with dex's header line; docs say `dex-cli` (§9). |

## Simplicity (ponytail)

| # | Finding | Decision |
| --- | --- | --- |
| P1 | The hand-written assignment scanner exists only for one error check | **Accepted.** Replaced by one regex for leading assignments plus `shlex` for quote balance (§7). |
| P2 | gcp, airflow, kafka and dbt sources weren't asked for | **Declined.** The training generator's context table has all four, so dex must send them for the prompt to match. The DAG-id guess is marked as a shortcut. |
| P3 | ruff's `S` rules duplicate bandit, hence double suppressions | **Accepted.** Bandit stays; `S` is dropped from ruff (§11). |
| P4 | The Makefile duplicates uv and is wrong for dependency groups | **Accepted.** Only `make serve` stays; everything else is `uv sync`, `uv run`, `uv build` and pre-commit (§11). |
| P5 | `--context all`, `max_tokens`, endpoint suffix branches, `DEX_API_KEY` | **Accepted in part.** `--context auto/all/none` becomes `--no-context`; `max_tokens` is fixed; only a trailing `/v1` is tolerated, because OpenAI-compatible servers often advertise that URL. `DEX_API_KEY` stays: `llama-server --api-key` is a common setup. |
| P6 | Placeholder tool configs and the `train` extra come before the code | **Declined.** Both were asked for explicitly. |
| P7 | `DEX_NO_WRAP` and `DEX_BIN` | **Accepted.** Removed; `command dex` and `PATH` cover them. |
| P8 | The extension-extraction loop in keyword matching is dead code | **Accepted.** Removed. |
| P9 | The `Env` dataclass exists only for tests | **Accepted.** Sources read the real environment; tests use `monkeypatch` and `tmp_path`. |

## Passed on to the training-data work

- Variable names in templates should not collide with real environment variables (`AWS_PROFILE`, `PATH`, `HOST`, ...).
- Destructive templates should use `${VAR:?}` so an emptied value fails instead of widening the command.
- Context values that dex now skips (whitespace, control characters, `<`, `>`, over 64 characters) shouldn't appear
  in training context lines either.

## Second version: decisions from the maintainer

After the first review, the maintainer asked for three changes, folded into the second version of `design_draft.md`:

| Decision | Effect on the design |
| --- | --- |
| Try typer-slim, then drop Typer: "if all we support is just dex, and then the request, we don't need typer at all" | argparse, imported only when an option is given; hand-written zsh completion (§3, §10). |
| Latency "as fast as it can get with native Python" | The client imports only `os`, `sys`, `socket` and `json`; everything heavy runs in a warm process (§5). |
| No network and no API key: the model ships with the product | In-process llama.cpp in a warm local process over a Unix socket; the model as a separate `dex-model` package (§6, §11). |
| Limit the number of dependencies | Two runtime dependencies, the inference library and the model; each justified (§11). |

Findings from the first review that this makes moot: C6, S4 and S5 (proxies, redirects and the API key: there is no
HTTP any more) and the timeout half of C7. Their protections aren't needed because nothing is sent over a network.
The warm process is new attack surface on the local machine; §6 limits it to a `0700` directory and a `0600` socket
owned by the user, with no TCP port.

## Review of the second version

A fresh reviewer read only the second version, with three lenses (correctness, local security, simplicity), on the
new sections. 10 findings, all accepted.

| # | Finding | Decision |
| --- | --- | --- |
| N1 | An old process exiting after "restart" or idle can unlink a newer process's socket, leaving orphans | **Accepted.** Remove the socket only if the path is still the inode this process bound; clients retry once on a closed connection (§6). |
| N2 | One socket for every install and model makes a pipx install and a dev venv restart each other on every call | **Accepted.** Socket named by a hash of version, `sys.prefix`, model path, size and mtime, and threads; no "restart" message (§6). |
| N3 | Sending the whole environment to a long-lived process is more than needed, and context resolved there sees the server's home and cwd; `--show-prompt` would load the model | **Accepted.** Context, parsing and the denylist run in the dex process; only the rendered prompt crosses the socket (§5, §7, §9). |
| N4 | `sun_path` is 104 bytes on macOS, so long `TMPDIR`s, `tmp_path` and homes fail; NFS homes share sockets across hosts | **Accepted.** Per-platform locations, a length check with a fallback, never the home directory (§6). |
| N5 | No `lstat`/owner check on the socket itself; `sudo -E` mixes root and the user | **Accepted.** `lstat` checks against `geteuid()`, socket type and owner, peer uid on both sides (§6). |
| N6 | A child that dies at once still costs 30 s; llama.cpp's errors go to `/dev/null`; no read timeout; a stale lock file | **Accepted.** Poll the child, log its stderr, read timeouts, `EAGAIN` as busy, `flock` (§6). |
| N7 | argparse's abbreviations (`--vers`) disagree with the zsh wrapper's exact matches | **Accepted.** argparse dropped for an exact-match loop over leading options (§3). |
| N8 | Rendering ChatML through each library's template code can differ from the training render | **Accepted.** `prompt.py` renders the string itself and uses raw completion; a byte-for-byte test (§4). |
| N9 | Option D breaks the offline promise, and B depends on PyPI granting a request | **Accepted.** Option E, the GGUF split into wheels under 100 MB, as the fallback; D not planned (§11.2). |
| N10 | Threads at CPU count oversubscribe efficiency cores and hyperthreads | **Accepted.** Default to performance or physical cores (§3). |

Also raised: an accuracy gate and an Apple Silicon measurement before the model size and the warm process are final
(accepted, §5, and passed on to the training-data work); `json` imports `re`, so the import check in CI allows it.

## Review of the implementation

A code review of the whole diff before the first commit, at high effort. 10 findings, all fixed, each with a test.

| # | Finding | Fix |
| --- | --- | --- |
| R1 | A redirect to a quoted target (`> "$OUT_FILE"`) wasn't flagged, because quoted text was deleted before the redirect check; 112 training pairs write files this way | Quoted words are replaced by a stand-in word instead of deleted (§9). |
| R2 | zsh's own variables weren't reserved: `COLUMNS=...` breaks the line editor, `SECONDS=...` resets the timer, and the data teaches both | `COLUMNS`, `LINES`, `SECONDS`, `TZ`, `LC_*`, `ZSH_*` and others added; the clashing template variables were renamed and the generator's lint now rejects a reserved name. |
| R3 | `| sudo tee /etc/hosts` and `/usr/bin/tee` slipped past the tee rule | The rule starts like the others, after `sudo`, `env` or a path. |
| R4 | `| xargs rm` and `| xargs mv` at the end of a line weren't flagged | `rm` and `mv` may end the line. |
| R5 | `2>err.log` and `&>out` truncate files and weren't flagged | The redirect rule lets `N>` and `&>` through, still skipping `>>`, `>&N` and /dev/null. |
| R6 | Context values could hold backticks, `$`, quotes or `;` from a hostile file name | Values use the generator's safe characters only. |
| R7 | Only the leading group of assignments reached the shell-variable check, so `A=1 && PATH=/x && ls` and `export PATH=...` weren't flagged | `parse.assignments` walks every simple command in the line. |
| R8 | `DEX_IDLE=inf` crashed the warm process (socket timeout overflow); `nan` passed the check | Non-finite values are a usage error; values are capped at a year. |
| R9 | `docker container prune`, `network prune` and `builder prune` weren't flagged | Every `docker ... prune` and `... rm` is. |
| R10 | `DEX_THREADS=0` ran on one thread instead of choosing automatically | 0 means auto, in the dex command and the warm process. |

## Review of missing-cli-hint

A reviewer with fresh context read `missing-cli-hint.md`, AGENTS.md and the code it names. Verdict: approve with
fixes. Items marked "Maintainer" change the design and wait for a decision.

| # | Finding | Decision |
|---|---------|----------|
| H1 | Some hints are `curl ... \| sudo bash` installers (astro, databricks, rclone, helm). Printing one strains rule 1 even on stderr. Suggests package-manager commands only, and the docs link for script installs. | The maintainer chose the docs link. Every script install now gives the tool's install page (also docker, aws, abctl, az), and a test keeps `curl` out of shipped commands. |
| H2 | Hint commands chain steps, and the apt-get ones use `-y`. Not a rule 2 break (stderr), but `-y` skips the user's own check. | The maintainer chose to drop it. A test keeps `-y` out of shipped commands. |
| H3 | Linux is one OS: apt and snap are assumed, and `sudo` is assumed for root. Wrong on Fedora, Arch, Alpine. | The maintainer chose the wording: apt hints say "on Debian/Ubuntu". |
| H4 | Shell functions and aliases (`conda`, `nvm`) aren't on `PATH`, so they get a false "not found". | The maintainer chose this: only programs in `installs.json` get a hint. |
| H5 | The note left out part of what dex reads (`installs.json`, the uv and pipx env vars, installer lookups). | Note corrected. |
| H6 | The note said one line per program (they share lines), showed only macOS, didn't say raw platform names like `win32` show, nor that a home path can. | Note corrected. |
| H7 | Install hints printed before the destructive warning, so the warning could scroll past (rule 6). | Fixed: the warning prints first. |
| H8 | The dbt hint holds an `<adapter>` placeholder that the "fill in" line doesn't flag (rule 3). | Maintainer. Minor. |
| H9 | Alternative not considered: "not found" plus an explicit `dex install <tool>`, or a hybrid. | Added to the note as alternative 4. The maintainer has since asked for an interactive install offer; that gets its own note. |
| H10 | Package names (`duckdb-cli`, `get-helm-4`) weren't checked offline. | Taken from the sources named in each tool config, not rechecked. |

## Review of install-offer

A reviewer with fresh context read `install-offer.md`, AGENTS.md, `missing-cli-hint.md` and the code it names.
Verdict on the first draft: rework. Every finding is answered in the reworked note; the choice of option is the maintainer's.

| # | Finding | Decision |
|---|---------|----------|
| I1 | Running an installer reaches the network, and the note didn't admit rule 4. | Rule 4 wording added to option A. |
| I2 | The zsh function captures stdout, so the installer's output would land on the prompt. | Option A sends the installer's output to stderr. |
| I3 | With stdout captured, the user is asked before seeing the command. | Superseded by the maintainer: the install is an intermediate step, so the question comes first and the command lands after it, with no echo. |
| I4 | dbt's install holds `<adapter>` and can't run as is. | Commands with a placeholder are never offered. |
| I5 | `brew install` isn't always user level: casks can ask for a password, a prefix can belong to another user, taps add sources, and brew may update other formulae. | Core formulas only, prefix must be writable, and the question says Homebrew may update. |
| I6 | Ctrl-C at the question would abort the zsh function, and Ctrl-C during the install wasn't covered. | Both spelled out: the command still lands on the prompt. |
| I7 | Chaining the install onto the line breaks rules 2 and 3, though the note said it needed no rule change. | Corrected; that alternative is rejected. |
| I8 | A uv or pipx folder off `PATH` means the installed CLI won't run. | Not offered then; the hint stays. |
| I9 | The question blocks, and checking only stdin and stderr would hang a user's own `x=$(dex ...)`. | Asked only when the zsh function sets `DEX_ASK=1`. |
| I10 | Which CLI gets the one question, and that its installer must be present, weren't said. | First in the line, installer on `PATH`. |
| I11 | `--no-ask` isn't needed: the widget already redirects stderr. | Dropped; `DEX_ASK` replaces it. |
| I12 | The hint and the question could name different commands. | One rule picks both. |
| I13 | Security sound; say the command is split with `shlex` and run from the installer on `PATH`. | Stated. |
| I14 | Missed alternative: queue the install on zsh's buffer stack, so dex runs nothing and no rule changes. | Added as option B, which the note recommended. The maintainer chose option A, with the rule 1 and 4 wording. |
