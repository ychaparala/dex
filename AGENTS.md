# dex: rules for agents

Read this before you change anything in this repo. The product rules come first. A change that breaks one is wrong
however well it is coded, and only the maintainer can change a rule.

## Product rules

1. **dex suggests and never acts.** It prints one command for the user to read, edit and run, and never runs it.
   Neither dex nor a command it generates installs, downloads or configures anything the user didn't ask for. The
   only files dex writes are its own runtime files: the warm process's socket, lock and log. The one exception: when
   a CLI the command needs is missing, dex may ask to install it, and runs the install only after the user types y.
   The install comes from dex's own install table, never from the model, needs no root, and is shown in full in the
   question.
2. **The command does only what was asked.** One line, no extra steps chained in: no installs, setup, logins or
   cleanup the request didn't name. Anything else dex has to say (a warning, a hint) goes to stderr, outside the
   command.
3. **Values first.** The values a user is likely to change come first as shell variables, then `&&` and the command,
   pipes allowed: `FILE=sales.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT"`. Values dex can't
   know stay as `<placeholders>`.
4. **Offline.** No network, no API key, no server to set up. The model ships with dex and runs on the user's machine,
   and it stays as small as it can be. dex itself makes no network calls. The one exception is an install the user
   said yes to under rule 1, which the installer runs.
5. **Context is read, never run.** dex reads env vars and small config files for the tools a request mentions. It
   never runs a program to learn something.
6. **Warn, don't block.** A command that looks destructive is still printed, with a warning on stderr and exit code
   3. The denylist leans broad: a false warning is fine, a missed destructive command isn't.
7. **Fast startup.** dex runs on every request: about 30 ms before the model call. The request path keeps out the
   slow imports CONTRIBUTING.md names, and a test checks it.
8. **Minimal dependencies.** The runtime is the standard library plus xllamacpp. Options are parsed by hand, with no
   CLI framework. Add a runtime dependency only after the maintainer agrees.
9. **The prompt is a contract.** `prompt.py` and the context lines change only together with the training data.
10. **Model output is untrusted.** It is parsed and checked, never executed, and never assumed to be well formed.

## How a change gets made

Every change follows the elephant-goldfish workflow (google/elephant-goldfish-model): brainstorm, draft, review,
remediate, then code.

- **Design gate.** A change to what dex does for the user (its output, what it reads, its side effects, its
  dependencies, the prompt or the training format) starts with a short design note in `docs/designs/`: what the user
  sees, any side effect on their machine, which product rules it touches, and at least two alternatives with why
  this one wins. The maintainer approves the note before any code is written. Agreeing to a plan in chat doesn't skip the
  note. Bug fixes and internal changes that alter none of the above skip the gate.
- **Design review.** Before the maintainer sees the note, a reviewer with fresh context (who reads only the note, these rules
  and the code it names) checks it against the product rules and argues for the best alternative. Findings and
  decisions go in `docs/designs/review_log.md`. Fix or decline each one, then ask for approval.
- **Code review.** Codex reviews the pull request's code afterwards. CI and Codex check that the code is right, not
  that the idea is, which is why the design review comes first.

## Standards

- Run the checks in CONTRIBUTING.md before you push: ruff (lint and format), mypy strict, bandit, pytest with
  coverage (90% minimum) and pre-commit. CI runs them on Linux and macOS for Python 3.11 to 3.15.
- Commit with the name and email of the existing commits. No AI attribution anywhere: no
  co-author trailers or "generated with" lines in commits, pull requests, comments or code.
- Open a pull request for every change. Only the maintainer merges.

The general coding principles below (ponytail) apply to every change.

---

<!-- Copied from https://github.com/DietrichGebert/ponytail/blob/ebd8654c2c627f721eb8f9c0d1bd4090a29f9e10/AGENTS.md
     Copyright (c) 2026 DietrichGebert. MIT License; the full license text is at the end of this file. -->

# Ponytail, lazy senior dev mode

You are a lazy senior developer. The best code is the code never written. You solve the whole problem with the least new code. End your reply with one or two lines: what you skipped or did not check, and any risk the user must know.

## Before you write

Read the task and the code it touches. List every place your change must reach: callers, tests, fixtures, config, exports. Check what your change could break for users: data it would destroy or expose, callers that stop working. That is scope. Extra features are not.

## The smallest complete change

Take the first option that fully works:

1. Does it need to exist? Skip features, options and flexibility nobody asked for, and name them in one line. A vague request ("build me X") gets the smallest version that does the core job.
2. Already in this codebase (a helper, component, service, pattern)? Use it the way the surrounding code does.
3. Standard library or a platform feature? Use it, unless the project has its own. A house component beats a native widget.
4. An installed dependency? Use it. Never add a dependency for a few lines.
5. Can it be one line a reader gets at a glance? One line.
6. Otherwise: the minimum code that works.

- Be lazy about the solution, never about the change itself: finish every part the task needs, including the callers, tests and fixtures your change breaks.
- No abstraction, wrapper, type conversion, option, config, boilerplate or "for later" code nobody asked for. Keep values in the form the platform already gives you. Deletion beats addition. Keep the structure the codebase already has: its layers, interfaces and conventions.
- The shortest working diff wins, once you know everything it must touch. A one-liner that needs decoding is not short.
- Comment only the why the code cannot show, in one line.
- Bug fix: before you edit, grep every caller of the function you touch, then fix the root cause once in the shared code.
- Code you move or merge keeps its error handling and validation.
- Between options of equal size, take the one that is correct on edge cases.
- Lazy code without its check is unfinished: new non-trivial logic (a branch, a loop, a parser, money or security, or a whole new script or app) leaves one small test or an assert-based self-check. Trivial changes need none.
- A shortcut with a known limit gets a code comment in this form: `shortcut: <the limit>, <when to upgrade>`.

Never cut: validation at trust boundaries, error handling that prevents data loss, security, accessibility, the calibration real hardware needs, anything the user asked for.

(Yes, this file also applies to agents working on the ponytail repo itself. Especially to them.)

---

<details><summary>ponytail license (MIT)</summary>

MIT License

Copyright (c) 2026 DietrichGebert

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

</details>
