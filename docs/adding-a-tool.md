# Adding a tool

Teaching dex a new CLI is mostly data: one YAML file. Code changes are needed only when the tool has local context
worth reading or destructive verbs the denylist doesn't know.

1. **Scaffold the config.** From `training/`:

   ```sh
   uv run --with pyyaml python nl2sh.py add-tool flyte --binary pyflyte --category orchestration
   ```

   This writes `training/tools/flyte.yaml`.

2. **Write the templates.** Each template is one line of bash with values as `$VAR`, four to six request
   phrasings, the doc page or source file it follows, and `destructive: true` where it deletes, drops, stops or
   overwrites something. [training/TOOL_SPEC.md](../training/TOOL_SPEC.md) lists every field and rule. Keep
   commands short and to common flags: a small model guesses at rare ones.

3. **Install commands.** Add `install:` with the official package-manager command per OS (Homebrew on macOS, apt
   or snap on Linux, `uv tool install` for a Python package) and `docs:`, the install page, where the only way is
   a vendor script. Then run
   `uv run --with pyyaml python nl2sh.py installs`. dex uses it for the install hint it prints when the CLI is
   missing. TOOL_SPEC.md, "Install commands", has the rules.

4. **Check and build.**

   ```sh
   uv run --with pyyaml python nl2sh.py lint
   uv run --with pyyaml python nl2sh.py build --only flyte
   ```

   If the CLI can be installed and has a parser the validators know (aws, click, argparse, cobra), add it to the
   validator map in `nl2sh.py` so its pairs are checked by the real parser. Commit the rebuilt `training/data/`.

5. **Destructive verbs.** Run `uv run pytest tests/test_safety.py`. `test_every_destructive_training_pair_is_flagged`
   fails if a template marked destructive doesn't trip the denylist; add a rule to `RULES` in `src/dex/safety.py`
   with literal needles that every match must contain.

6. **Context (optional).** If the tool has a current profile, connection or target the user would otherwise type,
   add a source in `src/dex/context.py`: a function that returns lines, registered in `SOURCES` with its keywords.
   Use wording that the training data uses for the same fact, and add the lines to the generator's context table
   so the model sees them in training. Read env vars and small files only, never run the tool.
