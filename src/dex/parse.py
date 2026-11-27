"""Turn the model's text into one clean, editable command line.

The expected shape is values first, then the command::

    FILE=data.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" | less -S

The line itself is returned unchanged. Parsing only rejects output that would be
unsafe or broken to put on a prompt, and finds the names the line would set in the
user's shell, for the shell-variable check in :mod:`dex.safety`.
"""

from __future__ import annotations

import re
import shlex

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?==)")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")
# Commands that set a variable in the shell itself rather than for one command.
_DECLARE = frozenset({"export", "declare", "typeset", "readonly", "local", "integer", "float"})
_PROMPT_MARKERS = ("$ ", "% ")


class ParseError(ValueError):
    """The model's text holds no usable command."""


class Answer:
    """A checked command line (a plain class: importing ``typing`` costs startup time).

    Attributes:
        line: The command, exactly as it is shown to the user.
        assigned: Names the line sets in the shell itself, in order: bare ``VAR=value``
            commands anywhere in the line and names after ``export`` and the like.
            ``VAR=value cmd`` sets VAR for one command only and isn't listed.
    """

    __slots__ = ("assigned", "line")

    def __init__(self, line: str, assigned: tuple[str, ...]) -> None:
        """Make an answer."""
        self.line, self.assigned = line, assigned


def clean(text: str) -> str:
    """Return the first line of the text that isn't a code fence, without prompt markers.

    Args:
        text: The model's raw text.

    Returns:
        One line, possibly empty.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("```"):
            continue
        for marker in _PROMPT_MARKERS:
            if line.startswith(marker):
                line = line[len(marker) :].lstrip()
        if len(line) > 1 and line[0] == line[-1] == "`" and line.count("`") == 2:
            line = line[1:-1].strip()
        return line
    return ""


def assignments(line: str) -> tuple[list[str], bool]:
    """Return the names a line sets in the shell, and whether it runs any command.

    The line is split into simple commands at ``&&``, ``||``, ``;``, ``|`` and ``&``.
    A command made only of ``NAME=value`` words sets those names in the shell; so does
    ``export NAME=value``.

    Raises:
        ValueError: The line doesn't tokenize as shell (unbalanced quotes).
    """
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    names: list[str] = []
    runs = False
    command: list[str] = []
    for token in [*lexer, ";"]:
        if token and set(token) <= set(";&|()"):
            if command:
                found = [m.group() for w in command if (m := _NAME.match(w))]
                if len(found) == len(command):
                    names.extend(found)
                else:
                    runs = True
                    if command[0] in _DECLARE:
                        names.extend(m.group() for w in command[1:] if (m := _NAME.match(w)))
            command = []
        else:
            command.append(token)
    return names, runs


def parse(text: str, *, truncated: bool = False) -> Answer:
    """Check the model's text and return the command line.

    Args:
        text: The model's raw text.
        truncated: Whether generation stopped at the token limit.

    Returns:
        The line and the names it assigns up front.

    Raises:
        ParseError: The answer was cut off, holds control characters, has
            unbalanced quotes or a trailing backslash, or holds no command.
    """
    if truncated:
        raise ParseError("the answer was cut off")
    line = clean(text)
    if not line:
        raise ParseError("the model returned no command")
    if _CONTROL.search(line):
        raise ParseError("the answer holds control characters")
    try:
        shlex.split(line)
        names, runs = assignments(line)
    except ValueError as e:
        raise ParseError(f"the answer isn't a complete command ({e})") from None
    if not runs:
        raise ParseError("the answer sets values but runs no command")
    return Answer(line, tuple(dict.fromkeys(names)))
