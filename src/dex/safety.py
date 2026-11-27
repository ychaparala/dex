"""Flag commands that delete, drop, terminate or overwrite things.

This is a denylist, so it warns rather than blocks: the command still goes on the
prompt for the user to read, but dex prints a warning and exits with code 3. False
positives are acceptable and a missed destructive command is not, so the rules lean
broad. Rules match anywhere in the line, including SQL inside quotes and commands
inside ``bash -c '...'`` or ``ssh host "..."``. They also run on the line with its
up-front values substituted, so ``REPLICAS=0 && kubectl scale --replicas="$REPLICAS"``
is caught like ``--replicas=0``.

A denylist can't see through obfuscation such as ``$(echo rm)``. Keeping
attacker-chosen text out of the prompt (see :mod:`dex.context`) is the defence there.
Add a rule here when you add a CLI with its own destructive verbs.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Mapping

# A command word starts the line or follows a separator, a quote, sudo, env or a path.
_START = r"(?:^|[\s;&|(`\"'])(?:sudo\s+|command\s+|exec\s+|env\s+)*(?:\\|/[\w./-]*/)?"
_SQL_OBJECTS = (
    "table|view|schema|database|warehouse|stage|stream|task|pipe|user|role|index"
    "|catalog|volume|function|procedure|integration|share|materialized|column|partition"
)
_VERBS = "delete|destroy|terminate|purge|deregister|uninstall|wipe|remove|drop|cancel|abort|stop"
_REMOVE_SUBCOMMANDS = (
    r"aws\s+s3\s+(?:rm|rb)"
    r"|gsutil\s+(?:-m\s+)?rm"
    r"|gcloud\s+storage\s+rm"
    r"|azcopy\s+(?:rm|remove)"
    r"|databricks\s+(?:fs|workspace)\s+(?:rm|delete)"
    r"|docker\s+(?:(?:container|image|volume|network|system|builder|buildx)\s+)?(?:rm|rmi|prune)"
    r"|snow\s+stage\s+remove"
)

# Each rule: a name for the warning, needles, and a pattern. A pattern is compiled and
# run only when one of its needles is in the lowercased line, which keeps startup fast:
# compiling every pattern up front costs about 5 ms on each request. Every match of a
# pattern must contain one of its needles; tests/test_safety.py checks the rules
# against every destructive pair in the training data.
RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # Files and the shell
    ("rm", ("rm",), rf"{_START}rm(?=\s|$)"),
    ("hdfs rm", ("-rm",), r"\s-rm(?:r|dir)?(?=\s)"),
    ("hdfs put -f", ("-put", "-copyfromlocal"), r"\s-(?:put|copyFromLocal)\s+(?:-\w+\s+)*-f\b"),
    (
        "shred, dd, mkfs, wipefs",
        ("shred", "wipefs", "mkfs", "of="),
        rf"{_START}(?:shred|wipefs|mkfs(?:\.\w+)?)\b|\bdd\s+[^|]*\bof=",
    ),
    ("truncate", ("truncate",), rf"{_START}truncate\s+.*-s"),
    ("find -delete", ("-delete",), r"\bfind\b.*\s-delete\b"),
    ("mv", ("mv",), rf"{_START}mv(?=\s|$)"),
    ("cp -f", ("cp",), rf"{_START}cp\s+(?:-\w*\s+)*-\w*f"),
    ("tee", ("tee",), rf"{_START}tee(?!\s+(?:-\w*\s+)*-\w*a)\s"),
    ("chmod or chown -R", ("chmod", "chown"), rf"{_START}(?:chmod|chown)\s+(?:\S+\s+)*-\w*R"),
    ("crontab -r", ("crontab",), r"\bcrontab\s+(?:-\w+\s+)*-r\b"),
    ("kill", ("kill",), rf"{_START}(?:kill|pkill|killall)\b|\s--?kill\b"),
    ("pipe into a shell", ("|",), r"\|\s*(?:sudo\s+)?(?:sh|bash|zsh|python3?)\b"),
    (
        "sync --delete",
        ("sync",),
        r"\b(?:rsync|sync)\b.*\s--(?:delete[\w-]*|remove-source-files)(?=\s|=|$)",
    ),
    ("rclone sync or move", ("rclone",), r"\brclone\s+(?:sync|move|purge|delete|deletefile)\b"),
    (
        "in-place edit",
        ("sed", "yq", "perl"),
        r"\b(?:sed|yq|perl)\b[^|]*\s(?:-\w*i\b|--in-place\b|--inplace\b)",
    ),
    ("http delete", ("delete",), r"-X\s*DELETE\b|--request\s+DELETE\b"),
    # SQL
    ("sql drop", ("drop",), rf"(?i)\bdrop\s+(?:{_SQL_OBJECTS})\b"),
    ("sql alter drop", ("alter",), r"(?i)\balter\s+\w+\s+.*\bdrop\b"),
    ("sql truncate", ("truncate",), r"(?i)\btruncate\s+(?:table\s+)?[\w.\"$]"),
    ("sql delete", ("delete",), r"(?i)\bdelete\s+from\b"),
    # The WHERE must belong to the same statement: not after a `;`, not inside a 'literal'.
    (
        "sql update without where",
        ("update",),
        r"(?i)\bupdate\s+[\w.\"$]+\s+set\b(?!(?:[^;']|'[^']*')*\bwhere\b)",
    ),
    (
        "sql overwrite",
        ("overwrite", "replace"),
        r"(?i)\binsert\s+overwrite\b|\bcreate\s+or\s+replace\b",
    ),
    ("sql copy to", ("copy",), r"(?i)\bcopy\b.*\bto\s+['\"@$\w]"),
    ("snowflake remove", ("remove",), r"(?i)\bremove\s+@"),
    ("redis flush", ("flush",), r"(?i)\bflush(?:all|db)\b"),
    (
        "database restore",
        ("pg_restore", "mysql", "psql"),
        r"\bpg_restore\b(?![^|]*\s(?:-l|--list)\b)|\|\s*(?:mysql|psql)\b|\b(?:mysql|psql)\b[^|]*\s<\s*(?![A-Za-z][\w.:/-]*>)[^\s<]",
    ),
    # CLI verbs: aws s3api delete-bucket, databricks clusters delete, kafka-delete-records.sh, ...
    (
        "delete verb",
        tuple(_VERBS.split("|")),
        rf"(?i)(?:^|[\s\"'/])(?:[\w-]*-)?(?:{_VERBS})(?:[-.][\w.-]*)?(?=[\s\"']|$)",
    ),
    (
        "remove subcommand",
        ("rm", "rb", "remove", "delete", "prune"),
        rf"\b(?:{_REMOVE_SUBCOMMANDS})\b",
    ),
    ("gsutil rsync -d", ("gsutil",), r"\bgsutil\s+(?:-m\s+)?rsync\s+(?:-\w+\s+)*-\w*d"),
    ("bq replace or force", ("bq",), r"\bbq\b.*(?:\s--replace\b|\s-f\b|\s--force\b)"),
    (
        "force",
        ("--force", "--yes-i-really"),
        r"\s(?:--force|--force-delete|--yes-i-really-mean-it)(?=\s|=|$)",
    ),
    ("kafka-configs --alter", ("kafka-configs",), r"\bkafka-configs(?:\.sh)?\b.*\s--alter\b"),
    (
        "kafka reset offsets",
        ("--reset-offsets", "seek"),
        r"--reset-offsets\b|\brpk\s+group\s+seek\b",
    ),
    ("kafka storage format", ("kafka-storage",), r"\bkafka-storage(?:\.sh)?\s+format\b"),
    ("kubectl drain", ("drain", "cordon"), r"\bkubectl\b.*\s(?:drain|cordon)\b"),
    ("scale to zero", ("--replicas",), r"--replicas[=\s]+[\"']?0\b"),
    ("compose down -v", ("compose",), r"\bdocker[\s-]compose\b.*\bdown\b.*\s(?:-v|--volumes)\b"),
    ("airflow db reset/clean", ("airflow",), r"\bairflow\s+db\s+(?:reset|clean)\b"),
    ("airflow tasks clear", ("airflow",), r"\bairflow\s+tasks\s+clear\b"),
    ("dbt full refresh or clean", ("--full-refresh", "dbt"), r"--full-refresh\b|\bdbt\s+clean\b"),
    (
        "git history rewrite",
        ("git",),
        r"\bgit\s+(?:push\s+.*(?:--force|-f\b)|reset\s+--hard|clean\s+-\w*f"
        r"|branch\s+.*-D\b|stash\s+(?:drop|clear))",
    ),
    ("terraform destroy", ("terraform",), r"\bterraform\s+(?:destroy|apply\s+.*-destroy)\b"),
    (
        "overwrite mode",
        ("overwrite",),
        r"(?i)\bmode\s*[=(]\s*[\"']?overwrite\b|--mode[=\s]+overwrite\b|--overwrite\b",
    ),
)

# A redirect that truncates a file: `>`, `>|`, `2>` or `&>`, but not `>>`, `>&N` or
# /dev/null. Checked against the line with each quoted word, $((...)) and <placeholder>
# replaced by Q, so `awk '$3 > 1'` passes while `> "$OUT"` is still caught.
_REDIRECT = r"(?<![>&])(?:&|\d)?>(?![>&])\|?\s*(?!/dev/null\b)[^\s>&|]"
_QUOTED = r"""'[^']*'|"(?:[^"\\]|\\.)*"|\$\(\([^)]*\)\)|<[A-Za-z][\w.:/-]*>"""

# Variables a values-first line must never set: `VAR=value && cmd` keeps VAR in the shell.
RESERVED = frozenset(
    {
        "PATH", "HOME", "IFS", "HOST", "HOSTNAME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD",
        "PS1", "PS2", "PROMPT", "RPROMPT", "ZDOTDIR", "FPATH", "CDPATH", "TMPDIR", "LANG",
        "TERM", "EDITOR", "PAGER", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "BASH_ENV", "ENV",
        "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
        "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_CONFIG_FILE", "SNOWFLAKE_HOME", "SNOWFLAKE_DEFAULT_CONNECTION_NAME",
        "KAFKA_BOOTSTRAP_SERVERS", "BOOTSTRAP_SERVERS", "KUBECONFIG", "CLOUDSDK_CONFIG",
        "CLOUDSDK_CORE_PROJECT", "CLOUDSDK_ACTIVE_CONFIG_NAME", "GOOGLE_CLOUD_PROJECT",
        "DBT_TARGET", "AIRFLOW_HOME",
        # zsh and bash give these a meaning of their own: setting COLUMNS breaks the
        # line editor, setting SECONDS resets the shell's timer.
        "COLUMNS", "LINES", "SECONDS", "TMOUT", "RANDOM", "REPLY", "HISTFILE", "HISTSIZE",
        "SAVEHIST", "HISTCONTROL", "MAIL", "MAILPATH", "MANPATH", "WORDCHARS", "KEYTIMEOUT",
        "PS3", "PS4", "TZ", "OPTIND", "OPTARG", "PROMPT_COMMAND", "PROMPT_EOL_MARK",
    }
)  # fmt: skip
_RESERVED_PREFIXES = ("LD_", "DYLD_", "DEX_", "LC_", "BASH_", "ZSH_")
_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.S)
_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expanded(line: str) -> str:
    """Return the line with its up-front values put in place of their references.

    ``REPLICAS=0 && kubectl scale --replicas="$REPLICAS"`` becomes
    ``... --replicas="0"``, so the rules see the value the command would run with.
    A line without a values-first prefix comes back unchanged.
    """
    head, sep, rest = line.partition("&&")
    if not sep or "$" not in rest:
        return line
    try:
        words = shlex.split(head)
    except ValueError:
        return line
    values = {m[1]: m[2] for w in words if (m := _NAME.fullmatch(w))}
    if not values or len(values) != len(words):
        return line
    return head + sep + _REF.sub(lambda m: values.get(m[1] or m[2], m[0]), rest)


def check(
    line: str, assigned: Iterable[str] = (), environ: Mapping[str, str] | None = None
) -> list[str]:
    """Return the names of every rule the line matches.

    Args:
        line: A full command line.
        assigned: Names the line sets up front, from :func:`dex.parse.parse`.
        environ: The user's environment; an assigned name already set there is flagged.

    Returns:
        Matching rule names, or an empty list when the line looks safe.
    """
    low = line.lower()
    full = _expanded(line)
    hits = [
        name
        for name, needles, pattern in RULES
        if any(n in low for n in needles)
        and (re.search(pattern, line) or (full is not line and re.search(pattern, full)))
    ]
    if ">" in line and re.search(_REDIRECT, re.sub(_QUOTED, "Q", line)):
        hits.append("redirect")
    env = environ or {}
    for var in assigned:
        if var in env or var in RESERVED or var.startswith(_RESERVED_PREFIXES):
            hits.append(f"overwrites ${var}")
    return hits
