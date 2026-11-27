# Architecture

How a request becomes a command line. The reasoning behind each choice, with measurements, is in
[designs/design_draft.md](designs/design_draft.md).

## The request path

1. **`cli.py`** reads the leading options by hand (no argparse: seven options, and every millisecond of startup
   counts) and joins the rest into the request.
2. **`context.py`** picks the sources whose keywords appear in the request and asks each for its lines, such as
   `AWS profile in use is etl`. Sources read env vars and small config files only. Values may use only
   letters, digits and `_ . / : @ % + , = -`, up to 64 characters, so a file name can't change the prompt or slip
   shell syntax to the model.
3. **`prompt.py`** renders the system prompt, the context lines and the request in ChatML. These bytes must match
   the training data; `tests/test_prompt.py` compares them with `training/data/eval.jsonl`.
4. **`client.py`** sends the prompt to the warm process over a Unix socket, starting it if nothing answers.
5. **`server.py`** (the warm process) generates with **`engine.py`**, the only module that imports the inference
   library, and sends back the text and whether it hit the token limit.
6. **`parse.py`** takes the first line, strips code fences and prompt markers, and rejects answers that were cut
   off, hold control characters, don't tokenize as shell, or set values without running anything.
7. **`safety.py`** checks the line against a denylist and the assigned names against variables a shell must keep.
   A match is a warning on stderr and exit code 3, never a block.
8. **`install.py`** finds the programs the line runs (the first word of each command, and the word after `sudo`,
   `xargs` and the like) and looks each one up on `PATH`. For a missing one it prints a hint on stderr with the
   install command for this OS, from `installs.json` (written from the tool configs by `training/nl2sh.py
   installs`): `duckdb not found, install with: brew install duckdb`. A tool with no package-manager install for
   this OS gets its install page instead, and a program `installs.json` doesn't list (a shell function, say) gets
   no hint. The line itself is unchanged. When the zsh function ran dex (`DEX_ASK=1`, stdin and stderr are
   terminals), dex first asks whether to install the first missing CLI whose install needs no root (uv tool or pipx
   with its folder on `PATH`, or a core brew formula with a writable prefix). On y it runs that install without a
   shell, its output on stderr, before the line is printed, and the hints are worked out after it.
9. `cli.py` prints the line. The zsh integration puts it on the next prompt.

Everything that depends on the caller (working directory, environment, files) runs in the dex process, so the warm
process only ever generates.

## Startup time

The request path imports only `os`, `sys`, `socket`, `json`, `re`, `shlex` and small C modules. `pathlib`, `typing`
and `contextlib` stay out (they cost about 10 ms together), which is why paths in those modules are strings and
records are small classes. `shutil` costs 6 ms, so `install.py` looks programs up on `PATH` itself. A denylist rule's regex is compiled only when one of its literal needles is in the line.
`tests/test_cli.py::test_request_path_imports_stay_light` fails if a slow module creeps back in.

Measured on a 4 vCPU Linux VM with Python 3.13: `dex --help` 21 ms, a request against an instant fake model 31 ms,
bare `python -c pass` 13 ms. Inference with a 0.5B-shaped model takes about a second on the same VM.

## The warm process

- One per install and model: the socket name is a checksum of the dex version, `sys.prefix`, the model file and the
  thread setting. An old process after an upgrade is never asked again and idles out.
- Lives in `$XDG_RUNTIME_DIR/dex` (Linux), else `/tmp/dex-<uid>`, or `$TMPDIR/dex` (macOS). dex refuses the
  directory unless it is a real directory owned by the user with mode 0700, and both sides check the other's uid on
  connect. No TCP port is opened: xllamacpp's own HTTP listener is put on a Unix socket in the same directory.
- Started under an `flock`, detached, with output in `<name>.log`. It binds a temporary name and renames it into
  place, so a socket that answers belongs to a process whose model is loaded.
- Exits after `DEX_IDLE` minutes without a request, on `dex --stop` or on SIGTERM, and removes its socket only if
  the path still points at the socket it bound.
- If the directory can't be used, dex loads the model in its own process and says so.

## The model

A fine-tuned Qwen2.5-Coder-0.5B, quantized to GGUF, run by llama.cpp through
[xllamacpp](https://github.com/xorbitsai/xllamacpp) (MIT, prebuilt wheels, no dependencies). It will ship as a
separate `dex-model` package holding `dex_model/dex.gguf`, so a dex release doesn't re-download the model; see §11 of
the design for the PyPI size limits and the fallback. Generation is greedy, at most 200 tokens, and stops at a blank
line or `</request>`.
