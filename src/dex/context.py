"""Gather small facts from the local environment for the model.

Things like the AWS profile, the Snowflake connection or the files in the working
directory let the model fill values with real ones instead of placeholders.

Each source returns at most a few plain-prose lines. The wording must match the
context lines in the training data (``training/nl2sh.py``), so change both together.
A source reports only what the tool itself would use, and nothing when that is
uncertain. Sources read environment variables and small config files only; they
never run other programs or touch the network, so dex stays fast. For the same
reason this module uses ``os.path`` rather than ``pathlib``, which takes several
milliseconds to import.

To add a source, write a function that returns lines, register it in
:data:`SOURCES` with its keywords, and add a test.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence

_MAX_CONFIG_BYTES = 256 << 10
_MAX_VALUE = 64
# The same characters the training generator allows in values: nothing a shell would
# expand or split on, so a hostile file name can't become a command substitution.
_SAFE = re.compile(r"[A-Za-z0-9_./:@%+,=-]+")
_COMMENT = re.compile(r"\s[#;].*$")
_DATA_EXT = frozenset(
    {".parquet", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".avro", ".orc", ".duckdb"}
    | {".db", ".sqlite", ".xlsx", ".gz", ".sql"}
)


def _ok(value: str) -> str:
    """Return the value if it is safe to put in the prompt, else an empty string.

    Only letters, digits and ``_ . / : @ % + , = -`` pass, up to 64 characters, so a file
    or profile name can't change the prompt's shape or smuggle in shell syntax such as
    backticks, ``$`` or quotes that the model might copy into the command.
    """
    return value if len(value) <= _MAX_VALUE and _SAFE.fullmatch(value) else ""


def _env(*keys: str) -> str:
    """Return the first non-empty environment value among ``keys``."""
    for key in keys:
        if value := os.environ.get(key, "").strip():
            return value
    return ""


def _home(*parts: str) -> str:
    return os.path.join(os.path.expanduser("~"), *parts)


def _path(env_key: str, *default: str) -> str:
    """Return the path in ``env_key``, else ``~/<default...>``."""
    value = _env(env_key)
    return os.path.expanduser(value) if value else _home(*default)


def _read(path: str) -> str:
    """Read a small config file, or return an empty string when it can't be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(_MAX_CONFIG_BYTES)
    except OSError:
        return ""


def _sections(text: str) -> list[str]:
    """Return the ``[section]`` names of an ini- or toml-style file, in order."""
    out = []
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw).strip()
        if line.startswith("[") and line.endswith("]"):
            out.append(line[1:-1].strip())
    return out


def _value(text: str, section: str, key: str) -> str:
    """Return ``key`` inside ``[section]``; an empty section means before any header."""
    inside = section == ""
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw).strip()
        if line.startswith("[") and line.endswith("]"):
            inside = line[1:-1].strip() == section
        elif inside and "=" in line:
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("\"'")
    return ""


def aws() -> list[str]:
    """The AWS profile and region the AWS CLI would use."""
    cfg = _read(_path("AWS_CONFIG_FILE", ".aws", "config"))
    profile = _env("AWS_PROFILE", "AWS_DEFAULT_PROFILE")
    if not profile:
        creds = _read(_path("AWS_SHARED_CREDENTIALS_FILE", ".aws", "credentials"))
        has_default = "default" in _sections(creds) or {"default", "profile default"} & set(
            _sections(cfg)
        )
        profile = "default" if has_default else ""
    region = _env("AWS_REGION", "AWS_DEFAULT_REGION")
    if not region and profile:
        region = _value(cfg, "default" if profile == "default" else f"profile {profile}", "region")
        if not region and profile == "default":
            region = _value(cfg, "profile default", "region")
    out = []
    if profile := _ok(profile):
        out.append(f"AWS profile in use is {profile}")
    if region := _ok(region):
        out.append(f"Default AWS region is {region}")
    return out


def databricks() -> list[str]:
    """The Databricks CLI profile: ``DATABRICKS_CONFIG_PROFILE``, else ``DEFAULT`` if it exists."""
    profile = _env("DATABRICKS_CONFIG_PROFILE")
    if not profile and "DEFAULT" in _sections(
        _read(_path("DATABRICKS_CONFIG_FILE", ".databrickscfg"))
    ):
        profile = "DEFAULT"
    return [f"Databricks CLI profile is {p}"] if (p := _ok(profile)) else []


def _snowflake_dir() -> str:
    if home := _env("SNOWFLAKE_HOME"):
        return os.path.expanduser(home)
    for parts in (
        (".snowflake",),
        (".config", "snowflake"),
        ("Library", "Application Support", "snowflake"),
    ):
        if os.path.isdir(candidate := _home(*parts)):
            return candidate
    return ""


def snowflake() -> list[str]:
    """The Snowflake CLI's default connection."""
    conn = _env("SNOWFLAKE_DEFAULT_CONNECTION_NAME")
    directory = _snowflake_dir()
    if not conn and directory:
        cfg = _read(os.path.join(directory, "config.toml"))
        conn = _value(cfg, "", "default_connection_name")
        if not conn and (
            "connections.default" in _sections(cfg)
            or "default" in _sections(_read(os.path.join(directory, "connections.toml")))
        ):
            conn = "default"
    return [f"Snowflake connection is {c}"] if (c := _ok(conn)) else []


def kafka() -> list[str]:
    """The first Kafka bootstrap server from the environment."""
    servers = _env("KAFKA_BOOTSTRAP_SERVERS", "BOOTSTRAP_SERVERS")
    return [f"Kafka bootstrap server is {s}"] if (s := _ok(servers.split(",")[0])) else []


def _yaml_scalar(line: str) -> str:
    return _COMMENT.sub("", line.split(":", 1)[1]).strip().strip("\"'")


def _kube_namespace(text: str, context: str) -> str:
    """Return the namespace of ``context`` in a block-style kubeconfig."""
    in_contexts = False
    item: list[str] = []
    items: list[list[str]] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw.startswith((" ", "-")):
            in_contexts = raw.startswith("contexts:")
            continue
        if in_contexts:
            if raw.startswith("- "):
                items.append(item := [])
            item.append(raw.strip().removeprefix("- "))
    for lines in items:
        fields = {ln.split(":", 1)[0]: _yaml_scalar(ln) for ln in lines if ":" in ln}
        if fields.get("name") == context:
            return fields.get("namespace", "")
    return ""


def kubernetes() -> list[str]:
    """The current kubectl context and its namespace.

    Reads the first file in ``KUBECONFIG`` (or ``~/.kube/config``) that sets
    ``current-context``. JSON kubeconfigs are skipped (a shortcut: they are rare).
    """
    files = [f for f in _env("KUBECONFIG").split(os.pathsep) if f] or [_home(".kube", "config")]
    for name in files:
        text = _read(os.path.expanduser(name))
        for raw in text.splitlines():
            if raw.startswith("current-context:"):
                context = _ok(_yaml_scalar(raw))
                if not context:
                    return []
                out = [f"kubectl context is {context}"]
                if namespace := _ok(_kube_namespace(text, context)):
                    out.append(f"Kubernetes namespace is {namespace}")
                return out
    return []


def gcp() -> list[str]:
    """The GCP project from the environment or the active gcloud configuration."""
    project = _env("CLOUDSDK_CORE_PROJECT", "GOOGLE_CLOUD_PROJECT")
    if not project:
        directory = _path("CLOUDSDK_CONFIG", ".config", "gcloud")
        active = _env("CLOUDSDK_ACTIVE_CONFIG_NAME")
        active = active or _read(os.path.join(directory, "active_config")).strip()
        if active := _ok(active or "default"):
            cfg = _read(os.path.join(directory, "configurations", f"config_{active}"))
            project = _value(cfg, "core", "project")
    return [f"GCP project is {p}"] if (p := _ok(project)) else []


def dbt() -> list[str]:
    """The dbt target from ``DBT_TARGET``. ``profiles.yml`` isn't parsed (a shortcut)."""
    return [f"dbt target is {t}"] if (t := _ok(_env("DBT_TARGET"))) else []


def airflow() -> list[str]:
    """DAG file names under ``$AIRFLOW_HOME/dags`` or ``./dags``.

    A shortcut: file names stand in for DAG ids, which usually match.
    """
    home = _env("AIRFLOW_HOME")
    for directory in ([os.path.join(os.path.expanduser(home), "dags")] if home else []) + ["dags"]:
        try:
            names = sorted(
                e.name[:-3]
                for e in os.scandir(directory)
                if e.name.endswith(".py") and not e.name.startswith(("_", "."))
            )
        except OSError:
            continue
        names = [n for n in names if _ok(n)]
        if names:
            return ["Airflow DAGs here: " + " ".join(names[:5])]
    return []


def files() -> list[str]:
    """A short listing of the working directory, data files first, no dotfiles."""
    try:
        entries = sorted(os.scandir("."), key=lambda e: e.name)
    except OSError:
        return []
    data, other = [], []
    for e in entries:
        if e.name.startswith(".") or not _ok(e.name):
            continue
        try:
            is_dir = e.is_dir()
        except OSError:
            continue
        if is_dir:
            other.append(e.name + "/")
        elif os.path.splitext(e.name)[1].lower() in _DATA_EXT:
            data.append(e.name)
        else:
            other.append(e.name)
    listing = (data + other)[:8]
    return ["Working directory contains: " + " ".join(listing)] if listing else []


class Source:
    """One kind of context.

    A plain class rather than a NamedTuple: importing ``typing`` would add to every
    request's startup time.

    Attributes:
        name: Short name, used in docs and tests.
        keywords: Any of these in the request turns the source on. Multi-word
            keywords match as substrings.
        lines: Reads the environment and returns context lines.
    """

    __slots__ = ("keywords", "lines", "name")

    def __init__(self, name: str, keywords: Sequence[str], lines: Callable[[], list[str]]) -> None:
        """Make a source."""
        self.name, self.keywords, self.lines = name, keywords, lines


SOURCES: list[Source] = [
    Source(
        "aws",
        [
            "aws", "s3", "emr", "athena", "glue", "lambda", "redshift", "kinesis", "dynamodb",
            "msk", "sqs", "sns", "ec2", "iam", "cloudwatch", "sagemaker", "mwaa", "bucket",
            "buckets", "stepfunctions", "step function", "step functions",
        ],
        aws,
    ),
    Source("databricks", ["databricks", "dbfs", "unity catalog", "dlt", "bundle"], databricks),
    Source("snowflake", ["snowflake", "snow", "snowsql", "snowpark"], snowflake),
    Source("kafka", ["kafka", "confluent", "topic", "topics", "consumer", "producer"], kafka),
    Source(
        "kubernetes",
        ["kubectl", "k8s", "kubernetes", "pod", "pods", "helm", "namespace"],
        kubernetes,
    ),
    Source("gcp", ["gcloud", "gcp", "bigquery", "bq", "gcs", "gsutil", "dataproc"], gcp),
    Source("dbt", ["dbt"], dbt),
    Source("airflow", ["airflow", "dag", "dags", "astro", "astronomer"], airflow),
    Source(
        "files",
        [
            "file", "files", "parquet", "csv", "tsv", "json", "jsonl", "ndjson", "avro", "orc",
            "xlsx", "duckdb", "rows", "columns", "schema", "jq", "head", "peek", "preview",
            "here", "folder", "directory",
        ],
        files,
    ),
]  # fmt: skip

_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def wanted(request: str) -> list[Source]:
    """Return the sources whose keywords appear in the request.

    Words are split on anything but letters, digits and inner hyphens, so
    ``sales.parquet`` gives ``sales`` and ``parquet``.
    """
    low = request.lower()
    words = set(_WORD.findall(low))
    return [s for s in SOURCES if any((k in low) if " " in k else (k in words) for k in s.keywords)]


def gather(request: str) -> list[str]:
    """Return the context lines for a request, in source order."""
    out: list[str] = []
    for source in wanted(request):
        out.extend(source.lines())
    return out
