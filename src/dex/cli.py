"""The ``dex`` command: turn a plain-English request into one editable shell line.

Startup time matters because dex runs on every request, so this module parses its
few options by hand and imports the rest of dex only on the path that needs it.
The model runs in a warm background process (:mod:`dex.server`); everything that
depends on the user's shell (context, parsing, the safety check) runs here.
"""

from __future__ import annotations

import math
import os
import re
import sys

from dex import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DESTRUCTIVE = 3
EXIT_INTERRUPTED = 130
DEFAULT_IDLE_MINUTES = 15.0
MAX_IDLE_MINUTES = 525_600.0  # a year: socket timeouts overflow far above this
SHELLS = ("zsh",)

# The example in the help is a shell line, not SQL built from input.
HELP = f"""\
usage: dex [OPTIONS] REQUEST...

Turn a plain-English request into one ready-to-edit shell command. The values you
are likely to change come first, then the command:

  $ dex first 10 rows of sales.parquet
  FILE=sales.parquet LIMIT=10 && duckdb -c "select * from '$FILE' limit $LIMIT"

dex never runs the command. With the zsh integration it lands on your next prompt.
If a CLI the command uses isn't installed, dex says how to install it. With the zsh
integration it first asks whether to install it, when that needs no sudo.
Options must come before the request.

options:
  --no-context    send no facts about the local environment (profiles, files)
  --show-prompt   print the prompt instead of generating
  -q, --quiet     no warnings on stderr
  --stop          stop the background model process(es)
  --init SHELL    print the shell integration ({", ".join(SHELLS)}): eval "$(dex --init zsh)"
  -h, --help      show this help
  --version       show the version

environment:
  DEX_MODEL       path to a GGUF model (default: the one from the dex-model package)
  DEX_IDLE        minutes the model stays loaded after the last request (default
                  {DEFAULT_IDLE_MINUTES:g}; 0 loads it on every call)
  DEX_THREADS     inference threads (default: performance or physical cores)
  DEX_ASK         1 lets dex ask to install a missing CLI (the zsh integration sets it)

exit codes:
  0 ok, 1 error, 2 usage, 3 the command looks destructive, 130 interrupted
"""  # nosec B608

_FLAGS = {
    "--no-context": "no_context",
    "--show-prompt": "show_prompt",
    "-q": "quiet",
    "--quiet": "quiet",
    "--stop": "stop",
    "-h": "help",
    "--help": "help",
    "--version": "version",
}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_PLACEHOLDER = re.compile(r"<[A-Za-z][\w.:/-]*>")


class UsageError(Exception):
    """The command line is wrong."""


class Options:
    """Parsed options and the request words."""

    __slots__ = ("flags", "init", "words")

    def __init__(self, flags: frozenset[str], init: str, words: list[str]) -> None:
        """Hold the parsed command line."""
        self.flags, self.init, self.words = flags, init, words


def parse_args(args: list[str]) -> Options:
    """Split leading options from the request.

    Option names must match exactly (no abbreviations). Parsing stops at ``--`` or at
    the first word that doesn't start with ``-``, so ``dex find files -mtime +7`` keeps
    ``-mtime +7`` in the request.

    Raises:
        UsageError: An unknown option, or ``--init`` without a shell.
    """
    flags: set[str] = set()
    init = ""
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        if arg in _FLAGS:
            flags.add(_FLAGS[arg])
        elif arg == "--init" or arg.startswith("--init="):
            if arg == "--init":
                i += 1
                if i == len(args):
                    raise UsageError("--init needs a shell, for example: --init zsh")
                init = args[i]
            else:
                init = arg.split("=", 1)[1]
            if init not in SHELLS:
                raise UsageError(f"--init supports {', '.join(SHELLS)}, not {init!r}")
        else:
            raise UsageError(f"unknown option {arg!r} (options go before the request)")
        i += 1
    return Options(frozenset(flags), init, args[i:])


def _say(message: str) -> None:
    """Print ``dex: message`` on stderr, with control characters escaped."""
    text = _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", message)
    sys.stderr.write(f"dex: {text}\n")


def _idle_minutes() -> float:
    raw = os.environ.get("DEX_IDLE", "").strip()
    if not raw:
        return DEFAULT_IDLE_MINUTES
    try:
        value = float(raw)
    except ValueError:
        raise UsageError(f"DEX_IDLE must be a number of minutes, not {raw!r}") from None
    if not math.isfinite(value) or value < 0:
        raise UsageError(f"DEX_IDLE must be 0 or more minutes, not {raw!r}")
    return min(value, MAX_IDLE_MINUTES)


def _threads() -> str:
    """``DEX_THREADS`` as a positive number, else ``auto`` (0 or junk mean auto too)."""
    raw = os.environ.get("DEX_THREADS", "").strip()
    return raw if raw.isdigit() and int(raw) > 0 else "auto"


def _generate(text: str, quiet: bool) -> tuple[str, bool]:
    """Run the prompt through the model: in the warm process, or here if that's off."""
    from dex import engine, prompt

    model = engine.model_path()
    idle = _idle_minutes()
    threads = _threads()
    payload = {"prompt": text, "max_tokens": prompt.MAX_TOKENS, "stop": list(prompt.STOP)}
    if idle > 0:
        from dex import client

        try:
            path = client.socket_path(model, threads)
        except client.UnusableDirError as e:
            if not quiet:
                _say(f"{e}; loading the model in this process instead")
        else:
            command = client.server_command(path, model, threads, idle)
            reply = client.call(path, payload, command)
            return str(reply.get("text", "")), bool(reply.get("truncated"))
    import tempfile

    n = int(threads) if threads != "auto" else engine.default_threads()
    with tempfile.TemporaryDirectory(prefix="dex-") as private:  # for xllamacpp's listener
        local = engine.LlamaEngine(model, n, os.path.join(private, "http.sock"))
        return local.complete(text, prompt.MAX_TOKENS, prompt.STOP)


def _stop() -> int:
    """Stop every warm process of this user, whatever model or install it serves."""
    from dex import client

    directory = client.runtime_dir()
    for name in sorted(os.listdir(directory)):
        if name.endswith(".sock") and not name.endswith(".http.sock"):
            try:  # noqa: SIM105  # contextlib.suppress would cost an import
                client.call(os.path.join(directory, name), {"op": "stop"}, None, timeout=5.0)
            except client.ServerError:  # already gone, or not answering
                pass
    return EXIT_OK


def _init(shell: str) -> int:
    from importlib.resources import files

    sys.stdout.write(files("dex").joinpath("shell", f"dex.{shell}").read_text(encoding="utf-8"))
    return EXIT_OK


def _asking() -> bool:
    """Whether dex may ask a question: only the zsh integration's ``dex`` sets DEX_ASK=1."""
    return os.environ.get("DEX_ASK") == "1" and sys.stdin.isatty() and sys.stderr.isatty()


def _yes(question: str) -> bool:
    """Ask on stderr, read a line from stdin: y or yes is yes, anything else is no."""
    sys.stderr.write(f"dex: {question} [y/N] ")
    sys.stderr.flush()
    try:
        reply = sys.stdin.readline()
    except KeyboardInterrupt:
        reply = ""
    if not reply.endswith("\n"):  # Ctrl-C or Ctrl-D left the cursor on the question
        sys.stderr.write("\n")
    return reply.strip().lower() in ("y", "yes")


def _offer_install(line: str) -> None:
    """Before the command is printed, offer to install the first missing CLI it needs."""
    from dex import install

    found = install.offer(line)
    if not found:
        return
    names, command = found
    one = len(names) == 1
    brew = " (Homebrew may update other packages)" if command.startswith("brew ") else ""
    question = (
        f"{', '.join(names)} {'is' if one else 'are'}n't installed. "
        f"Install {'it' if one else 'them'} with: {command}{brew}?"
    )
    if not _yes(question):
        return
    try:
        code = install.run(command)
    except KeyboardInterrupt:
        _say("install stopped")
        return
    except OSError as e:  # the installer couldn't start; the command still comes
        _say(f"install failed: {e}")
        return
    if code:
        _say(f"install failed (exit {code})")


def run(opts: Options) -> int:
    """Do what the options and request ask for and return the exit code."""
    if "help" in opts.flags:
        sys.stdout.write(HELP)
        return EXIT_OK
    if "version" in opts.flags:
        print(f"dex {__version__}")
        return EXIT_OK
    if opts.init:
        return _init(opts.init)
    if "stop" in opts.flags:
        return _stop()
    request = " ".join(opts.words).strip()
    if not request:
        raise UsageError("say what you want to do, for example: dex peek at sales.parquet")

    from dex import prompt

    if "no_context" in opts.flags:
        lines: list[str] = []
    else:
        from dex import context

        lines = context.gather(request)
    text = prompt.render(prompt.user(request, lines))
    if "show_prompt" in opts.flags:
        sys.stdout.write(text)
        return EXIT_OK

    from dex import install, parse, safety

    quiet = "quiet" in opts.flags
    output, truncated = _generate(text, quiet)
    answer = parse.parse(output, truncated=truncated)
    hits = safety.check(answer.line, answer.assigned, os.environ)
    if not quiet and hits:
        _say(f"warning: this looks destructive ({', '.join(hits)}). Read it before you run it.")
    if not quiet and _asking():
        _offer_install(answer.line)
    print(answer.line)
    if not quiet:
        for hint in install.hints(answer.line):  # after an install, so it counts
            _say(hint)
        if placeholders := _PLACEHOLDER.findall(answer.line):
            _say(f"fill in {' '.join(dict.fromkeys(placeholders))} before you run it")
    return EXIT_DESTRUCTIVE if hits else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point of the ``dex`` console script."""
    try:
        return run(parse_args(sys.argv[1:] if argv is None else argv))
    except UsageError as e:
        _say(str(e))
        _say("try 'dex --help'")
        return EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except Exception as e:
        from dex import client, engine, parse

        if isinstance(e, (parse.ParseError, engine.ModelError, client.ServerError, OSError)):
            _say(str(e))
            return EXIT_ERROR
        raise


if __name__ == "__main__":
    sys.exit(main())
