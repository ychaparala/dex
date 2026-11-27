# Contributing

## Setup

```sh
uv sync                    # Python 3.11+, dev tools, editable dex
uv run pre-commit install  # run the checks on every commit
```

## Checks

CI runs these, and pre-commit runs the fast ones:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy                # strict, over src and tests
uv run bandit -c pyproject.toml -r src
uv run pytest              # with coverage; fails under 90%
```

The tests need no model: a fake engine stands in. The zsh tests drive a real interactive zsh on a pseudo-terminal
and are skipped when zsh isn't installed.

## Conventions

- **The CLI's runtime is the standard library plus xllamacpp.** Add a runtime dependency only after a discussion.
- **Startup time is a feature.** Don't import `pathlib`, `typing`, `contextlib` or `subprocess` on the request path
  (`cli`, `client`, `context`, `engine`, `parse`, `prompt`, `safety`); a test checks this.
- **The prompt is a contract.** Change `prompt.py` or a context line's wording only together with the training data.
- **The denylist leans broad.** A false warning is fine; a missed destructive command isn't.
- **Model output is untrusted.** It's parsed and checked, never executed, and never trusted to be well-formed.
- Google-style docstrings, 100-character lines, type hints everywhere. AGENTS.md has dex's product rules, the
  design workflow and the general coding principles this repo follows.
- Use plain words in docs and messages, and say what something does rather than how clever it is.

## Releases

Bump `__version__` in `src/dex/__init__.py` and `version` in `pyproject.toml`, then `uv build`.
