"""Tell the user how to install a CLI the command needs but they don't have.

dex finds the programs a line runs from its words and looks each one up on ``PATH``.
For a missing one it takes the install command for this OS from ``installs.json``,
which ``training/nl2sh.py installs`` writes from the ``install:`` entries in
``training/tools/*.yaml``, and prints it as a hint beside the command:
``duckdb not found, install with: brew install duckdb``. The command itself is
unchanged, and dex runs nothing.

The lookup is a few ``stat`` calls. ``shutil.which`` would do the same, but importing
shutil costs about 6 ms on every request.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

_FILE = os.path.join(os.path.dirname(__file__), "installs.json")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_PROGRAM = re.compile(r"[A-Za-z][\w.+-]*")
# Words that run the next word: that word is a command too. Shell keywords among them
# are in _BUILTINS, so they are never looked up.
_WRAPPERS = frozenset(
    {"sudo", "env", "nohup", "nice", "xargs", "watch", "time", "command", "exec", "builtin"}
    | {"if", "elif", "then", "else", "while", "until", "do", "!", "{"}
)
# Options of those wrappers that take a value, so ``sudo -u postgres psql`` runs psql.
_VALUE_OPTIONS = {
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-U", "-r", "-t"}),
    "xargs": frozenset({"-n", "-I", "-P", "-L", "-d", "-a", "-s", "-E"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "watch": frozenset({"-n"}),
}
# Shell builtins and keywords (bash and zsh): never on PATH, never missing.
_BUILTINS = frozenset({
    "time", "command", "exec", "builtin", "if", "elif", "then", "else", "fi", "while", "until",
    "do", "done", "for", "in", "case", "esac", "select", "function", "!", "{", "}", "[[", "]]",
    "[", "test", ".", "source", "alias", "unalias", "bg", "fg", "jobs", "wait", "cd", "pushd",
    "popd", "dirs", "echo", "printf", "read", "eval", "export", "unset", "set", "shift", "return",
    "exit", "break", "continue", "local", "declare", "typeset", "readonly", "integer", "float",
    "let", "trap", "type", "hash", "ulimit", "umask", "true", "false", "kill", "getopts",
    "history", "noglob", "nocorrect", "print", "whence", "where", "rehash", "setopt", "unsetopt",
    "autoload", "emulate", "zmodload",
})  # fmt: skip


def _substitution(line: str, start: int) -> int:
    """Return the index of the ``)`` that closes the ``(`` at ``start``, or -1."""
    depth, quote, i = 0, "", start
    while i < len(line):
        char = line[i]
        if char == "\\" and quote != "'":
            i += 1
        elif quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if not depth:
                return i
        i += 1
    return -1


def _mask_quoted(line: str) -> tuple[str, list[str]]:
    """Hide ``;&|()`` that are quoted or escaped, and pull out quoted ``$(...)``.

    POSIX shlex drops the quotes, so ``printf '%s' '|' jq`` would read as a pipe into jq:
    those characters become ``_``. A ``$(...)`` inside double quotes still runs, as in
    ``eval "$(aws configure export-credentials)"``, so its text is returned to be read
    on its own.

    Returns:
        The masked line, and the text of each double-quoted command substitution.
    """
    out, inner = [], []
    quote = ""  # the quote we are inside, if any
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and quote != "'":
            escaped = line[i + 1 : i + 2]
            out.append("\\" + ("_" if escaped and escaped in ";&|()" else escaped))
            i += 2
            continue
        close = _substitution(line, i + 1) if quote == '"' and line.startswith("$(", i) else -1
        if close >= 0:
            inner.append(line[i + 2 : close])
            out.append("_" * (close + 1 - i))
            i = close + 1
            continue
        literal = bool(quote)
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        out.append("_" if literal and char in ";&|()" else char)
        i += 1
    return "".join(out), inner


def commands(line: str) -> list[str]:
    """Return the programs a line runs, in order, without repeats.

    A program is the first word of each simple command (after ``&&``, ``||``, ``|``,
    ``;``, ``&``, ``(`` or ``$(``, also inside double quotes), skipping ``NAME=value``
    words, and the word after a wrapper such as ``sudo`` or ``xargs``. Builtins, paths
    and words built from ``$`` expansions are left out. Commands in a quoted string
    (``bash -c "..."``) aren't seen.

    Raises:
        ValueError: The line doesn't tokenize as shell.
    """
    masked, inner = _mask_quoted(line)
    lexer = shlex.shlex(masked, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    found: dict[str, None] = {}
    value = False  # the next word is a wrapper option's value
    wrapped = ""  # the wrapper the next command word follows
    at_command = True  # the next word is a command word
    after: list[bool] = []  # for each open "(": whether a command word follows its ")"
    previous = ""
    for token in lexer:
        if token and set(token) <= set(";&|()"):
            for n, char in enumerate(token):
                if char == "(":
                    # After "$(...)" the outer command carries on: still at its first
                    # word only if the substitution was a NAME=$(...) value.
                    substitution = n == 0 and previous.endswith("$")
                    after.append(substitution and at_command and previous != "$")
                    at_command = True
                elif char == ")":
                    at_command = after.pop() if after else True
                else:
                    at_command = True
                wrapped, value = "", False
            previous = token
            continue
        previous = token
        if not at_command:
            continue
        if wrapped and token.startswith("-"):
            value = token in _VALUE_OPTIONS.get(wrapped, ())
            continue
        if value or _NAME.match(token):
            value = False
            continue
        if token in _WRAPPERS:
            if token not in _BUILTINS:
                found[token] = None
            wrapped = token
            continue
        if token not in _BUILTINS and _PROGRAM.fullmatch(token):
            found[token] = None
        at_command, wrapped = False, ""
    for text in inner:
        found.update(dict.fromkeys(commands(text)))
    return list(found)


def find(name: str) -> str:
    """The path of the executable ``name`` in the first ``PATH`` folder with it, or ``""``."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        path = os.path.join(directory or ".", name)  # an empty entry is the current directory
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return ""


def which(name: str) -> bool:
    """Whether an executable file called ``name`` is in a ``PATH`` directory."""
    return bool(find(name))


def _tool_bin(option: str) -> str:
    """Where ``uv tool install`` or ``pipx install`` puts executables, or ``""`` for others.

    The same order uv and pipx document: ``UV_TOOL_BIN_DIR``, ``XDG_BIN_HOME``,
    ``XDG_DATA_HOME/../bin``, then ``~/.local/bin`` for uv; ``PIPX_BIN_DIR``, then
    ``~/.local/bin`` for pipx.
    """
    env = os.environ
    if option.startswith("uv tool "):
        data = env.get("XDG_DATA_HOME")
        found = env.get("UV_TOOL_BIN_DIR") or env.get("XDG_BIN_HOME")
        found = found or (os.path.join(os.path.dirname(data), "bin") if data else "")
    elif option.startswith("pipx "):
        found = env.get("PIPX_BIN_DIR", "")
    else:
        return ""
    return found or os.path.join(os.path.expanduser("~"), ".local", "bin")


def _on_path(directory: str) -> bool:
    real = os.path.realpath(directory)
    path = os.environ.get("PATH", "").split(os.pathsep)
    return any(os.path.realpath(d or ".") == real for d in path)


def _system() -> tuple[str, str]:
    """The key in ``installs.json`` for this OS, and its name for messages."""
    platform: str = sys.platform  # a variable, or mypy decides the branches for this machine
    if platform == "darwin":
        return "macos", "macOS"
    if platform.startswith("linux"):
        return "linux", "Linux"
    return "", platform


def _plan(line: str) -> list[tuple[list[str], str, str]]:
    """Each hint as (programs, text after "not found", the install command that is ready to run)."""
    missing = [name for name in commands(line) if not which(name)]
    if not missing:
        return []
    with open(_FILE, encoding="utf-8") as f:
        table: dict[str, dict[str, list[str]]] = json.load(f)
    key, system = _system()
    found: dict[str, tuple[list[str], str]] = {}  # hint text -> (programs, usable command)
    queue = [name for name in missing if name in table]
    for name in queue:  # grows: a missing installer gets its own hint after the CLI's
        entry = table[name]
        options = entry.get(key, []) + entry.get("any", []) if key else entry.get("any", [])
        usable = [o for o in options if all(map(which, commands(o)))]
        command = (usable or options or [""])[0]
        # apt-get is only on Debian and its derivatives, so say which Linux the hint is for.
        apt = command.startswith("sudo apt-get ")
        how = ", on Debian/Ubuntu install with: " if apt else ", install with: "
        if usable:
            how += command
            if (bin_dir := _tool_bin(command)) and not _on_path(bin_dir):
                how += f", then add {bin_dir} to PATH"
        elif options:
            needs = [c for c in commands(command) if not which(c)]
            how += f"{command} (needs {', '.join(needs)})"
            queue += [c for c in needs if c in table and c not in queue]
        elif docs := entry.get("docs"):
            how = f", see {docs[0]} to install {{it}}"
        else:
            how = f", and dex doesn't know how to install {{it}} on {system}"
        found.setdefault(how, ([], command if usable else ""))[0].append(name)
    return [
        (names, how.replace("{it}", "it" if len(names) == 1 else "them"), command)
        for how, (names, command) in found.items()
    ]


def hints(line: str) -> list[str]:
    """Return a line for the user about each program the command runs that isn't installed.

    Only programs in ``installs.json`` get a hint, so a shell function or alias that isn't
    on ``PATH`` (``conda``, ``nvm``) isn't reported missing. The hint gives the first
    install command for this OS (then the ones for any OS) whose own programs are all
    installed, or the first one and what it needs, or the tool's install page, or says
    that dex knows none. A missing installer it needs (uv, pipx, brew) gets its own hint
    on the next line. Programs with the same hint share one line.

    Raises:
        ValueError: The line doesn't tokenize as shell.
    """
    return [f"{', '.join(names)} not found{how}" for names, how, _ in _plan(line)]


def _brew_prefix() -> str:
    """Homebrew's prefix: ``HOMEBREW_PREFIX``, else the folder above the ``bin`` brew is in."""
    return os.environ.get("HOMEBREW_PREFIX") or os.path.dirname(os.path.dirname(find("brew")))


def _user_level(command: str) -> bool:
    """Whether dex may offer to run ``command``: no root, no tap or cask, no placeholder.

    uv and pipx install into the user's own tool folder, which has to be on ``PATH`` for
    the CLI to run afterwards. A Homebrew core formula installs into Homebrew's prefix,
    which has to be the user's to write.
    """
    words = shlex.split(command)
    if "<" in command or "sudo" in words or set(words) & {"|", "||", ";", "&"}:
        return False
    if command.startswith(("uv tool install ", "pipx install ")):
        return _on_path(_tool_bin(command))
    if command.startswith("brew install ") and "&&" not in words:
        formula = words[2:]
        if any(w.startswith("-") or "/" in w for w in formula):  # a cask, an option or a tap
            return False
        prefix = _brew_prefix()
        cellar = os.path.join(prefix, "Cellar")
        return bool(prefix) and os.access(cellar if os.path.isdir(cellar) else prefix, os.W_OK)
    return False


def offer(line: str) -> tuple[list[str], str] | None:
    """The first missing CLI in the line that dex may install, as (programs, command).

    The command is the one its hint gives, so the hint and the question agree. Only
    programs the line runs are offered, never the installer a hint asks for.

    Raises:
        ValueError: The line doesn't tokenize as shell.
    """
    wanted = set(commands(line))  # never an installer that only a hint needs
    for names, _, command in _plan(line):
        if command and set(names) <= wanted and _user_level(command):
            return names, command
    return None


def run(command: str) -> int:
    """Run an install command from ``installs.json``, its output on stderr; return its exit code.

    The command is split into words and each ``&&`` step runs in turn, without a shell,
    stopping at the first failure. Only a command :func:`offer` returned is passed here.
    """
    import subprocess  # nosec B404  # only after the user said yes

    step: list[str] = []
    for word in [*shlex.split(command), "&&"]:
        if word != "&&":
            step.append(word)
            continue
        step[0] = find(step[0]) or step[0]
        # nosec B603: a command from dex's own table, run without a shell
        code = subprocess.run(step, stdout=sys.stderr.fileno(), check=False).returncode  # nosec
        if code:
            return code
        step = []
    return 0
