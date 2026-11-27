#!/usr/bin/env python3
"""nl2sh dataset framework: per-tool configs (tools/*.yaml) -> version-checked train/eval JSONL.

Commands
  add-tool <name> [--binary B] [--category C]      scaffold tools/<name>.yaml
  lint                                             check every tool config
  installs                                         write each CLI's install commands to ../src/dex/installs.json
  setup-envs [--only a,b]                          install each pinned pip-installable CLI version into its own env
                                                   (others: put binaries in $NL2SH_CACHE/envs/<tool>-<version>/bin)
  snapshot [--only a,b]                            capture each pinned version's command/flag tree to snapshots/
  build [--only a,b] [--no-exec-validate]          expand + validate tools (all, or only those), then merge every
                                                   tool's part into data/train.jsonl and data/eval.jsonl
  refresh <tool> --version V [--install SPEC]      add a new CLI version: install, snapshot, diff flags against the
                                                   previous version, re-check doc pages, rebuild only that tool
  stats                                            counts per tool from the last build
  predict --endpoint URL [--model M] [--out F]     run data/eval.jsonl through an OpenAI-compatible server
  score <predictions.jsonl>                        score predictions against data/eval.jsonl

Environment: NL2SH_CACHE (default ~/.cache/nl2sh) holds the per-version envs. Needs Python 3.10+ and PyYAML.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / "tools"
INSTALLS = ROOT.parent / "src" / "dex" / "installs.json"
INSTALLERS = ROOT / "installers.yaml"  # how to get uv, pipx and brew themselves
INSTALL_OS = ("macos", "linux", "any")
DATA = ROOT / "data"
PARTS = DATA / "parts"
SNAP = ROOT / "snapshots"
CACHE = Path(os.environ.get("NL2SH_CACHE", Path.home() / ".cache" / "nl2sh"))

# One short, rigid system prompt. A tiny model learns the format from the data; the prompt only anchors it.
SYSTEM_PROMPT = ("Turn the request into one bash command. "
                 "Put editable values first as VAR=value, then && and the command.")

MAX_CMD_CHARS = 300
EVAL_TARGET = 240
SEED = 20261126

# Context kinds -> (prose line, pool of fake values). Prose labels, not key=value: whatisit measured that key=value
# context makes small models emit `$key` references.
CTX = {
    "aws_profile": ("AWS profile in use is {v}", ["data-prod", "analytics-dev", "etl", "sandbox"]),
    "aws_region": ("Default AWS region is {v}", ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2"]),
    "emr_cluster": ("Running EMR cluster is {v} ({n})", [("j-2AXXXXXXGAPLF", "etl-nightly"),
                                                         ("j-1K48XXXXXXHCB", "adhoc-spark"),
                                                         ("j-3Q9XXXXXXZ7TD", "reporting")]),
    "s3_bucket": ("Main S3 bucket is {v}", ["acme-data-lake", "analytics-raw", "etl-artifacts"]),
    "local_file": ("Working directory contains: {v} README.md notebooks/", None),
    "databricks_profile": ("Databricks CLI profile is {v}", ["DEFAULT", "prod", "dev-workspace"]),
    "snowflake_connection": ("Snowflake connection is {v}", ["dev", "prod_reader", "analytics"]),
    "kafka_bootstrap": ("Kafka bootstrap server is {v}", ["localhost:9092", "broker-1.internal:9092",
                                                          "pkc-xxxxx.us-east-1.aws.confluent.cloud:9092"]),
    "k8s_namespace": ("Kubernetes namespace is {v}", ["data", "airflow", "spark-jobs"]),
    "k8s_context": ("kubectl context is {v}", ["prod-eks", "staging", "kind-local"]),
    "gcp_project": ("GCP project is {v}", ["acme-analytics", "data-platform-dev"]),
    "az_subscription": ("Azure subscription is {v}", ["data-prod", "analytics-dev"]),
    "dbt_target": ("dbt target is {v}", ["dev", "prod", "ci"]),
    "airflow_dag": ("Airflow DAGs here: {v} cleanup_logs", ["daily_sales_etl", "ingest_events", "refresh_marts"]),
}
VERSION_LINE = "Installed versions: {items}"

DUMMY = {
    "cluster-id": "j-2AXXXXXXGAPLF", "step-id": "s-1XXXXXXXXXXXX", "bucket": "example-bucket",
    "profile": "default", "region": "us-east-1", "job-id": "123", "run-id": "456",
    "application-id": "00fxxxxxxxxxx", "job-run-id": "00fyyyyyyyyyy", "account-id": "123456789012",
    "role-arn": "arn:aws:iam::123456789012:role/example",
    "query-execution-id": "11111111-2222-3333-4444-555555555555",
    "statement-id": "11111111-2222-3333-4444-555555555555",
    "secret-id": "example/secret", "function-name": "example-fn", "instance-id": "i-0123456789abcdef0",
}

AWS_V2_ONLY = [("logs", "tail"), ("logs", "start-live-tail"), ("sso", "login"), ("sso", "logout"),
               ("configure", "sso"), ("configure", "list-profiles"), ("configure", "export-credentials"),
               ("emr-serverless",), ("ecr-public",), ("s3", "presign"), ("cli",)]

# Go CLIs built from source: checked by running the real binary with --help appended. Cobra (and urfave/cli) parse
# every flag before honouring --help, so an unknown subcommand or flag fails while a valid line prints help. No
# credentials are set and --help never contacts a service.
HELP_PARSE_BINS = {"databricks", "confluent", "astro", "argo", "rpk"}
# a help heading that lists subcommands: cobra's "Available Commands:", docker's "Commands:", kubectl's
# "Basic Commands (Beginner):"
HAS_SUBCOMMANDS = re.compile(r"(?m)^(?:[A-Z][\w ]* )?Commands\b[^\n]*:\s*$")


def usage_depth(help_text: str) -> int | None:
    """How many command words the first Usage: line names ('Usage:  docker image ls [OPTIONS]' is 3), or None."""
    m = re.search(r"Usage:\s*(\S[^\n]*)", help_text)
    if not m:
        return None
    words = m.group(1).split()
    return next((i for i, w in enumerate(words) if not re.fullmatch(r"[a-z0-9][\w.-]*", w)), len(words))

VAR_REF = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)\}?")
INTENT_REF = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
PLACEHOLDER = re.compile(r"^<([a-z0-9][a-z0-9-]*)>$")
SAFE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")
# a value that starts with a quoted ~ (VAR='~/x'): the shell never expands it, so the path is wrong
QUOTED_TILDE = re.compile(r"""(?:^|\s)[A-Z][A-Z0-9_]*=['"]~""")


# ----------------------------------------------------------------------------------------------------------- config
def load_tools(only: set[str] | None = None) -> dict[str, dict]:
    out = {}
    for p in sorted(TOOLS.glob("*.yaml")):
        if only and p.stem not in only:
            continue
        try:
            with p.open() as f:
                out[p.stem] = yaml.safe_load(f)
        except yaml.YAMLError as e:
            sys.exit(f"tools/{p.name}: YAML error: {e}")
    return out


def vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def version_ok(version: str, spec: str | None) -> bool:
    """spec: comma-separated constraints like '>=3.0', '<3.0', '>=4.2,<5'. Empty/None matches everything."""
    if not spec:
        return True
    v = vtuple(version)
    for c in str(spec).split(","):
        m = re.match(r"\s*(>=|<=|==|>|<)\s*([\d.]+)\s*$", c)
        if not m:
            raise ValueError(f"bad version spec {spec!r}")
        op, ref = m.group(1), vtuple(m.group(2))
        n = max(len(v), len(ref))
        a, b = v + (0,) * (n - len(v)), ref + (0,) * (n - len(ref))
        if not {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b}[op]:
            return False
    return True


def env_dir(tool: str, version: str) -> Path:
    return CACHE / "envs" / f"{tool}-{version}"


def env_bin(tool: str, version: str) -> Path | None:
    b = env_dir(tool, version) / "bin"
    return b if b.is_dir() else None


def quote(v: str) -> str:
    """Shell-quote a var value. <placeholders> stay bare so they stand out, but they don't reliably stop the line:
    bash reads them as redirects, so `FILE=<file> LIMIT=10 && cmd` runs if a file named `file` exists. dex never runs
    a line itself and puts the cursor on the first placeholder instead (see README.md).

    A leading ~ only expands outside quotes, so ~/path stays bare when the rest is safe (bash and zsh expand ~ right
    after the = of an assignment) and becomes "$HOME/path" otherwise."""
    v = str(v)
    if PLACEHOLDER.match(v) or SAFE.match(v):
        return v
    if v == "~" or v.startswith("~/"):
        rest = v[1:]
        if not rest or SAFE.match(rest):
            return v
        return '"$HOME' + re.sub(r'(["\\$`])', r"\\\1", rest) + '"'
    if "'" not in v:
        return f"'{v}'"
    return '"' + re.sub(r'(["\\$`])', r"\\\1", v) + '"'


def assemble(vars_: list[dict], values: dict[str, str], body: str) -> str:
    if not vars_:
        return body
    return " ".join(f"{v['name']}={quote(values[v['name']])}" for v in vars_) + " && " + body


def lint(tools: dict[str, dict]) -> list[str]:
    errs, ids = [], Counter()
    for name, cfg in tools.items():
        where = f"tools/{name}.yaml"
        for key in ("tool", "binaries", "category", "priority", "target_pairs", "sources", "templates"):
            if key not in cfg:
                errs.append(f"{where}: missing '{key}'")
        if cfg.get("tool") != name:
            errs.append(f"{where}: tool '{cfg.get('tool')}' != file name")
        for v in cfg.get("versions") or []:
            if "version" not in v:
                errs.append(f"{where}: a versions entry has no 'version'")
        for pat in cfg.get("destructive_patterns") or []:
            try:
                re.compile(pat)
            except re.error as e:
                errs.append(f"{where}: bad destructive_pattern {pat!r}: {e}")
        for t in cfg.get("templates") or []:
            tid = t.get("id", "?")
            ids[tid] += 1
            w = f"{where}:{tid}"
            for key in ("id", "intents", "body", "destructive"):
                if key not in t:
                    errs.append(f"{w}: missing '{key}'")
            if t.get("versions"):
                try:
                    version_ok("1.0", t["versions"])
                except ValueError as e:
                    errs.append(f"{w}: {e}")
            body = str(t.get("body", ""))
            if "\n" in body:
                errs.append(f"{w}: body has a newline")
            names = [v.get("name") for v in t.get("vars") or []]
            for v in t.get("vars") or []:
                if not re.match(r"^[A-Z][A-Z0-9_]*$", str(v.get("name"))):
                    errs.append(f"{w}: bad var name {v.get('name')!r}")
                elif reserved_name(str(v.get("name"))):
                    errs.append(f"{w}: var {v.get('name')} is a name the shell or a CLI already uses; rename it")
                if "default" not in v:
                    errs.append(f"{w}: var {v.get('name')} has no default")
                if v.get("ctx") and v["ctx"] not in CTX:
                    errs.append(f"{w}: unknown ctx {v['ctx']!r}")
            used = set(VAR_REF.findall(body))
            for n in names:
                if n not in used:
                    errs.append(f"{w}: var {n} not used in body")
            intents = t.get("intents") or []
            if len(intents) < 3:
                errs.append(f"{w}: only {len(intents)} intents")
            if intents and all(INTENT_REF.search(i) for i in intents):
                errs.append(f"{w}: every intent mentions a var")
            for i in intents:
                for r in INTENT_REF.findall(i):
                    if r not in names:
                        errs.append(f"{w}: intent mentions {{{r}}} not in vars")
                    elif not [v for v in t["vars"] if v["name"] == r][0].get("samples"):
                        errs.append(f"{w}: intent mentions {{{r}}} but it has no samples")
            errs += [f"{w}: {e}" for e in named_values(t)]
    for tid, c in ids.items():
        if c > 1:
            errs.append(f"duplicate template id {tid}")
    try:
        installs(tools)
    except ValueError as e:
        errs.append(str(e))
    return errs


def installs(tools: dict[str, dict]) -> dict[str, dict[str, list[str]]]:
    """Each binary's install commands per OS (macos, linux, any), and its install page (docs), from the tools'
    `install:` entries.

    dex uses the first command for the OS, then for any OS, whose own programs are on PATH, and the docs page when
    there is no command for the OS. Raises ValueError on a
    malformed entry or a binary two files install differently."""
    table: dict[str, dict[str, list[str]]] = {}
    for name, cfg in tools.items():
        for e in cfg.get("install") or []:
            where = f"tools/{name}.yaml: install"
            if set(e) - {"binaries", "source", "docs", *INSTALL_OS}:
                raise ValueError(f"{where}: unknown keys {sorted(set(e) - {'binaries', 'source', 'docs', *INSTALL_OS})}")
            if not e.get("source"):
                raise ValueError(f"{where}: an entry has no source")
            cmds = {k: e[k] for k in INSTALL_OS if e.get(k)}
            if not (cmds or "docs" in e) or any(not isinstance(v, list) or not all(isinstance(c, str) and "\n" not in c for c in v)
                               for v in cmds.values()):
                raise ValueError(f"{where}: an entry needs one-line commands under {', '.join(INSTALL_OS)}, or docs")
            if "docs" in e:
                if not (isinstance(e["docs"], str) and e["docs"].startswith("https://") and " " not in e["docs"]):
                    raise ValueError(f"{where}: docs must be one https URL")
                cmds["docs"] = [e["docs"]]
            for b in e.get("binaries") or cfg.get("binaries") or []:
                if b not in (cfg.get("binaries") or []):
                    raise ValueError(f"{where}: {b} isn't in binaries")
                if table.get(b, cmds) != cmds:
                    raise ValueError(f"{where}: {b} is installed differently in another tool file")
                table[b] = cmds
    return dict(sorted(table.items()))


def install_sources() -> dict[str, dict]:
    """The tool configs and installers.yaml: everything installs.json is written from."""
    with INSTALLERS.open() as f:
        return {**load_tools(), "installers": yaml.safe_load(f)}


def write_installs(_args) -> int:
    INSTALLS.write_text(json.dumps(installs(install_sources()), indent=1) + "\n")
    print(f"wrote {INSTALLS}")
    return 0


def reserved_name(name: str) -> bool:
    """Names dex warns about (dex.safety.RESERVED): setting one changes the user's shell, such as COLUMNS in zsh."""
    try:
        from dex.safety import _RESERVED_PREFIXES, RESERVED
    except ImportError:  # run outside the project env: fall back to the source tree
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from dex.safety import _RESERVED_PREFIXES, RESERVED
    return name in RESERVED or name.startswith(_RESERVED_PREFIXES)


ENUM = re.compile(r"^[A-Z]{3,}(?:_[A-Z]+)?$")


def named_values(t: dict) -> list[str]:
    """An intent without {VAR} gets VAR's default, so it must not name another of VAR's values.

    Checked for enum-like vars (FAILED, RUNNING, ...): "which runs are still running" with STATE
    defaulting to FAILED would teach the model the wrong filter."""
    errs = []
    for v in t.get("vars") or []:
        default, samples = str(v.get("default")), [str(x) for x in v.get("samples") or []]
        if not all(ENUM.match(x) for x in [default, *samples]):
            continue
        for i in t.get("intents") or []:
            if "{" + str(v.get("name")) + "}" in i:
                continue
            for x in samples:
                word = re.escape(x.lower().replace("_", " "))
                if x != default and re.search(rf"\b{word}\b", i.lower()):
                    errs.append(f"intent {i!r} names {v['name']}={x} but gets the default {default}")
    return errs


# --------------------------------------------------------------------------------------------------------- expansion
def stable_rng(*parts) -> random.Random:
    h = hashlib.sha256(("|".join(map(str, parts)) + str(SEED)).encode()).hexdigest()
    return random.Random(int(h[:16], 16))


def expand_template(t: dict, cfg: dict, n_wanted: int, rng: random.Random) -> list[dict]:
    """Every intent once (defaults or a sample), then context variants and resampled values up to the budget."""
    vars_ = t.get("vars") or []
    defaults = {v["name"]: str(v["default"]) for v in vars_}
    samples = {v["name"]: [str(s) for s in v.get("samples") or []] for v in vars_}
    ctxvars = [v for v in vars_ if v.get("ctx")]
    pats = [re.compile(p) for p in cfg.get("destructive_patterns") or []]
    out, seen = [], set()

    def make(i: int, kind: str, pick: dict[str, str], ctx_lines: list[str]):
        values = dict(defaults)
        values.update(pick)
        text = INTENT_REF.sub(lambda m: values[m.group(1)], t["intents"][i])
        cmd = assemble(vars_, values, t["body"])
        key = (tuple(ctx_lines), text, cmd)
        if key in seen:
            return
        seen.add(key)
        out.append({"ctx": list(ctx_lines), "text": text, "cmd": cmd, "intent_idx": i, "kind": kind,
                    "destructive": bool(t.get("destructive")) or any(p.search(cmd) for p in pats),
                    "values": values})

    def sample_for(intent: str) -> dict[str, str]:
        return {r: rng.choice(samples[r]) for r in INTENT_REF.findall(intent) if samples.get(r)}

    def ctx_variant(i: int):
        mentioned = set(INTENT_REF.findall(t["intents"][i]))
        free = [v for v in ctxvars if v["name"] not in mentioned]
        if not free:
            return
        pick, lines = sample_for(t["intents"][i]), []
        for v in free:
            line, pool = CTX[v["ctx"]]
            if v["ctx"] == "local_file":
                val = rng.choice(samples[v["name"]] or [str(v["default"])])
                lines.append(line.format(v=val))
            elif v["ctx"] == "emr_cluster":
                val, cname = rng.choice(pool)
                lines.append(line.format(v=val, n=cname))
            else:
                val = rng.choice(pool)
                lines.append(line.format(v=val))
            pick[v["name"]] = val
        rng.shuffle(lines)
        make(i, "context", pick, lines)

    for i, intent in enumerate(t["intents"]):
        make(i, "plain", sample_for(intent), [])
    order = list(range(len(t["intents"])))
    for _ in range(6):
        if len(out) >= n_wanted:
            break
        rng.shuffle(order)
        for i in order:
            if len(out) >= n_wanted:
                break
            if ctxvars and rng.random() < 0.6:
                ctx_variant(i)
            elif INTENT_REF.search(t["intents"][i]):
                make(i, "resample", sample_for(t["intents"][i]), [])
            elif ctxvars:
                ctx_variant(i)
    return out


def user_text(p: dict) -> str:
    ctx = list(p["ctx"])
    if p.get("version_line"):
        ctx.append(p["version_line"])
    return ("\n".join(ctx) + "\n\n" if ctx else "") + "<request>\n" + p["text"] + "\n</request>"


# -------------------------------------------------------------------------------------------------------- validation
def split_first_command(body: str) -> str:
    """Text up to the first unquoted |, ;, && or ||."""
    q, i, esc = None, 0, False
    while i < len(body):
        c = body[i]
        if esc:
            esc = False
        elif c == "\\" and q != "'":
            esc = True
        elif q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c in "|;" or body.startswith("&&", i):
            return body[:i].strip()
        i += 1
    return body.strip()


def _dummy_key(k: str) -> str:
    if k in DUMMY:
        return DUMMY[k]
    if k.endswith("-arn"):
        return "arn:aws:iam::123456789012:role/example"
    if k.endswith("-id"):
        return "example-id-123"
    return "example-" + k


def dummy_for(value: str) -> str:
    """Replace every <placeholder> in a value (whole or embedded, e.g. s3://<bucket>/logs) with a dummy."""
    return re.sub(r"<([a-z0-9][a-z0-9-]*)>", lambda m: _dummy_key(m.group(1)), value)


def concrete(p: dict) -> tuple[str, dict]:
    vals = {k: dummy_for(v) for k, v in p["values"].items()}
    return assemble(p["_vars"], vals, p["_body"]), vals


def words_of(text: str, vals: dict) -> list[str] | None:
    """argv of a command text with vars substituted, split by real bash."""
    # Splitting goes through eval, so command substitutions inside the text would run. Give bash an empty PATH,
    # no inherited environment (no credentials) and a throwaway working directory, so nothing external can run and
    # redirections only touch a scratch folder.
    prog = "set -f; eval 'set -- '\"$__NL2SH_CMD\"; printf '%s\\0' \"$@\""
    sandbox = tempfile.mkdtemp(prefix="nl2sh-split-")
    try:
        r = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-c", prog], cwd=sandbox,
                           env={**vals, "PATH": "/nonexistent", "HOME": sandbox, "__NL2SH_CMD": text},
                           capture_output=True, timeout=5)
    except subprocess.TimeoutExpired:
        return None
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return r.stdout.decode().split("\0")[:-1] if r.returncode == 0 else None


class Validators:
    """Offline checks against the real CLI parsers. Nothing here sends a request to a real service: aws gets fake
    credentials and a closed localhost endpoint; dbt/snow/airflow are parsed in-process without running a command;
    Go CLIs only ever get --help."""

    def __init__(self, bin_dir: str | None, exec_validate: bool):
        self.exec = exec_validate
        self.extra = bin_dir
        self.tmp = Path(tempfile.mkdtemp(prefix="nl2sh-val-"))
        self.spark_opts: dict[str, set] = {}
        self._duck = None
        self._help_cache: dict[tuple, tuple[int, str]] = {}
        (self.tmp / "home").mkdir()
        (self.tmp / "aws").mkdir()
        (self.tmp / "aws" / "config").write_text(
            "[default]\nregion = us-east-1\n\n" + "".join(
                f"[profile {p}]\nregion = us-east-1\n\n" for p in
                ["data-prod", "analytics-dev", "etl", "sandbox", "prod", "dev"]))

    def which(self, b: str, bindir: Path | None) -> str | None:
        path = os.pathsep.join([str(x) for x in (bindir, self.extra) if x] + [os.environ.get("PATH", "")])
        return shutil.which(b, path=path)

    def available(self, bins=("aws", "dbt", "snow", "airflow", "jq", "spark-submit")) -> dict[str, bool]:
        a = {b: bool(self.which(b, None)) for b in bins}
        a["duckdb"] = self.duck() is not None
        return a

    def bash_syntax(self, cmd: str) -> str | None:
        r = subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True)
        return None if r.returncode == 0 else r.stderr.strip()[:200]

    def aws(self, exe: str, argv: list[str]):
        svc = [a for a in argv[1:] if not a.startswith("-")][:2]
        for pat in AWS_V2_ONLY:
            if tuple(svc[:len(pat)]) == pat:
                return "skipped-v2-only", None
        if svc[:1] in (["configure"], ["help"]):
            return "skipped", None
        if "--cli-binary-format" in argv:
            return "skipped-v2-only", None
        env = {**os.environ, "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "example",
               "AWS_CONFIG_FILE": str(self.tmp / "aws" / "config"), "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
               "AWS_EC2_METADATA_DISABLED": "true", "AWS_MAX_ATTEMPTS": "1", "AWS_RETRY_MODE": "standard",
               "AWS_DEFAULT_REGION": "us-east-1", "AWS_PAGER": ""}
        cmd = [exe] + argv[1:] + ["--endpoint-url", "http://127.0.0.1:9", "--cli-connect-timeout", "1",
                                  "--cli-read-timeout", "1"]
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30, cwd=self.tmp,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return "ok", None
        err = r.stderr
        bad = re.search(r"(Unknown options:.*|aws: error:.*|usage: aws .*|Invalid choice.*|"
                        r"Error parsing parameter.*|the following arguments are required.*|"
                        r"argument .*: expected one argument|Parameter validation failed.*)", err)
        if bad and not re.search(r"Unable to load paramfile|Blob values must be a path to a file", err):
            return "fail", bad.group(0)[:200]
        return "ok", None

    def click_like(self, tool: str, exe: str, argv: list[str]):
        py = Path(exe).parent / "python"
        code = {"dbt": "from dbt.cli.main import cli as c",
                "snow": "from snowflake.cli._app.cli_app import CliAppFactory as F; import typer; "
                        "c = typer.main.get_command(F().create_or_get_app())"}[tool]
        script = (code + "\nimport sys, json, click\nargs=json.loads(sys.argv[1])\n"
                  "def walk(cmd, args, parent=None):\n"
                  "    ctx = cmd.make_context(cmd.name, list(args), parent=parent, resilient_parsing=False)\n"
                  "    rest = list(getattr(ctx, 'protected_args', [])) + list(ctx.args)\n"
                  "    if isinstance(cmd, click.Group) and rest:\n"
                  "        name, sub, rest2 = cmd.resolve_command(ctx, rest)\n"
                  "        walk(sub, rest2, ctx)\n"
                  "try:\n    walk(c, args)\n    print('OK')\n"
                  "except (click.UsageError, click.BadParameter) as e:\n    print('FAIL', e.format_message())\n"
                  "except click.exceptions.Exit:\n    print('OK')\n"
                  "except Exception as e:\n    print('OKX', type(e).__name__, e)\n")
        try:
            r = subprocess.run([str(py), "-c", script, json.dumps(argv[1:])], capture_output=True, text=True,
                               timeout=90, cwd=self.tmp,
                               env={**os.environ, "DBT_PROFILES_DIR": str(self.tmp), "SNOWFLAKE_HOME": str(self.tmp)})
        except subprocess.TimeoutExpired:
            return "skipped", None
        line = (r.stdout.strip().splitlines() or ["OKX " + r.stderr[-200:]])[-1]
        if line.startswith("FAIL"):
            if re.search(r"does not exist|No such file|is not a valid project|Path '", line):
                return "ok", None  # a missing local file/dir is not a syntax problem
            return "fail", line[5:200]
        return "ok", None

    def airflow(self, exe: str, argv: list[str]):
        py = Path(exe).parent / "python"
        script = ("import sys, json, io, contextlib\n"
                  "from airflow.cli.cli_parser import get_parser\n"
                  "p = get_parser(); err = io.StringIO()\n"
                  "try:\n    with contextlib.redirect_stderr(err):\n        p.parse_args(json.loads(sys.argv[1]))\n"
                  "    print('OK')\n"
                  "except SystemExit as e:\n"
                  "    print('OK' if e.code == 0 else 'FAIL ' + (err.getvalue().strip().splitlines() or ['?'])[-1])\n")
        try:
            r = subprocess.run([str(py), "-c", script, json.dumps(argv[1:])], capture_output=True, text=True,
                               timeout=180, cwd=self.tmp,
                               env={**os.environ, "AIRFLOW_HOME": str(self.tmp / ("af-" + Path(exe).parent.parent.name)),
                                    "AIRFLOW__CORE__LOAD_EXAMPLES": "False"})
        except subprocess.TimeoutExpired:
            return "skipped", None
        line = (r.stdout.strip().splitlines() or ["OKX " + r.stderr[-200:]])[-1]
        return ("fail", line[5:200]) if line.startswith("FAIL") else ("ok", None)

    def spark(self, exe: str, argv: list[str]):
        if exe not in self.spark_opts:
            r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=180)
            self.spark_opts[exe] = set(re.findall(r"^\s+(--[a-z-]+)", r.stdout + r.stderr, re.M)) | {"--help",
                                                                                                   "--version"}
        opts = self.spark_opts[exe]
        i = 1
        while i < len(argv):
            a = argv[i]
            if not a.startswith("--"):
                break  # primary resource reached; the rest are application args
            if a.split("=")[0] not in opts:
                return "fail", f"unknown spark-submit option {a}"
            i += 1 if ("=" in a or a in ("--verbose", "--supervise", "--help", "--version")) else 2
        return "ok", None

    def help_parse(self, exe: str, argv: list[str]):
        """Go CLIs (cobra): --help right after the command path; flags and subcommands are still parsed, nothing
        is executed."""
        if "--" in argv:  # everything after -- belongs to another program; check only our part
            argv = argv[:argv.index("--")]
        words = [a for a in argv[1:] if not a.startswith("-")]
        if Path(exe).name == "astro" and words[:2] in (["dev", "run"], ["dev", "bash"]):
            return "skipped-passthrough", None  # the rest of the line is an airflow command run in the container
        # Find the command path one word at a time and put --help straight after it: a --help at the end can land in
        # arguments passed on to another program (docker run IMAGE CMD --help) and run the command for real.
        try:
            code, out = self._help(exe, ["--help"])
            path: list[str] = []
            for w in argv[1:]:
                # a command without subcommands ends the walk: trying a positional (docker run IMAGE --help)
                # as a subcommand would run it
                if w.startswith("-") or not HAS_SUBCOMMANDS.search(out):
                    break
                c, o = self._help(exe, path + [w, "--help"])
                if c != 0 or usage_depth(o) != (usage_depth(out) or 0) + 1:
                    break
                path, out = path + [w], o
            rest = argv[1 + len(path):]
            if rest and not rest[0].startswith("-") and HAS_SUBCOMMANDS.search(out):
                return "fail", f"unknown command {rest[0]!r} under {' '.join([Path(exe).name] + path)}"
            code, out = self._help(exe, path + ["--help"] + rest)
        except subprocess.TimeoutExpired:
            return "skipped", None
        bad = re.search(r"(unknown (shorthand )?flag:.*|unknown command .*|Error: unknown.*|"
                        r"flag provided but not defined.*|invalid argument .* for .*flag.*|"
                        r"Error: accepts .* arg\(s\).*|flag needs an argument.*)", out)
        if re.search(r"no such file or directory", out, re.I) and not re.search(r"unknown (shorthand )?flag", out):
            return "ok", None  # a missing local @file named in the command is not a syntax problem
        if bad or code != 0:
            return "fail", (bad.group(0) if bad else out.strip().splitlines()[-1] if out.strip() else "exit "
                            + str(code))[:200]
        if usage_depth(out) is None:
            return "fail", "no usage printed"
        return "ok", None

    def _help(self, exe: str, args: list[str]) -> tuple[int, str]:
        """Run a Go CLI's help, cut off from real services: no PATH (so no kubectl plugin runs), a scratch HOME,
        and docker, kube and Kafka addresses that lead nowhere. Cached, since every command walks its path."""
        key = (exe, tuple(args))
        if key not in self._help_cache:
            home = self.tmp / "home"
            env = {"PATH": "", "HOME": str(home), "NO_COLOR": "1",
                   "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
                   "DOCKER_HOST": "unix:///nonexistent/docker.sock", "KUBECONFIG": str(home / "no-kubeconfig"),
                   "RPK_BROKERS": "127.0.0.1:9"}
            r = subprocess.run([exe] + args, env=env, capture_output=True, text=True, timeout=30, cwd=self.tmp,
                               stdin=subprocess.DEVNULL)
            self._help_cache[key] = (r.returncode, r.stdout + r.stderr)
        return self._help_cache[key]

    def jq(self, exe: str, cmd: str, vals: dict):
        bad = None
        for seg in re.split(r"\|\s*(?=jq\b)", cmd)[1:] + ([cmd] if cmd.lstrip().startswith("jq ") else []):
            words = words_of(split_first_command(seg), vals)
            if not words or words[0] != "jq":
                continue
            opts, filt, i = [], None, 1
            while i < len(words):
                w = words[i]
                if w in ("--arg", "--argjson") and i + 2 < len(words):
                    opts += words[i:i + 3]; i += 3; continue
                if w.startswith("-") and filt is None:
                    if w not in ("-s", "--slurp", "-R", "--raw-input", "-f", "--from-file", "-e"):
                        opts.append(w)
                    i += 1; continue
                if filt is None:
                    filt = w
                i += 1
            if filt is None:
                continue
            r = subprocess.run([exe, "-n"] + opts + [filt], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=10)
            if r.returncode == 3 or "compile error" in r.stderr:
                bad = (r.stderr.strip().splitlines() or ["jq compile error"])[0][:200]
        return ("fail", bad) if bad else ("ok", None)

    def duck(self):
        if self._duck is None:
            try:
                import duckdb  # type: ignore
                self._duck = duckdb.connect()
            except Exception:
                self._duck = False
        return self._duck.cursor() if self._duck else None  # one cursor per call: the validator runs threaded

    def duckdb_sql(self, argv: list[str]):
        con = self.duck()
        if not con:
            return "skipped", None
        sqls = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-c", "-s")]
        if not sqls:
            return "skipped", None
        for sql in sqls:
            if sql.lstrip().startswith("."):
                continue
            for stmt in [s for s in sql.split(";") if s.strip()]:
                j = json.loads(con.execute("select json_serialize_sql(?)", [stmt]).fetchone()[0])
                # json_serialize_sql only serializes SELECTs; other statements report a non-parser error, which
                # still means they parsed. Only a parser error is a syntax failure.
                if j.get("error") and j.get("error_type") == "parser":
                    return "fail", str(j.get("error_message"))[:200]
        return "ok", None

    def run(self, p: dict, bindir: Path | None) -> tuple[str, str, str | None]:
        """(validator, status, error). status: ok | fail | skipped..."""
        cmd, vals = concrete(p)
        e = self.bash_syntax(cmd)
        if e:
            return "bash", "fail", e
        argv = words_of(split_first_command(p["_body"]), vals)
        if not argv:
            return "bash", "ok", None
        b = argv[0]
        exe = self.which(b, bindir)
        if self.exec and exe:
            if b == "aws":
                return ("aws-parse",) + self.aws(exe, argv)
            if b in ("dbt", "snow"):
                return (b + "-click",) + self.click_like(b, exe, argv)
            if b == "airflow":
                return ("airflow-argparse",) + self.airflow(exe, argv)
            if b == "spark-submit":
                return ("spark-submit-opts",) + self.spark(exe, argv)
            if b in HELP_PARSE_BINS and "--help" not in argv and "-h" not in argv:
                return (b + "-help-parse",) + self.help_parse(exe, argv)
        if b == "duckdb":
            return ("duckdb-sql-parse",) + self.duckdb_sql(argv)
        jq = self.which("jq", bindir)
        if jq and re.search(r"(^|\|)\s*jq\b", cmd):
            return ("jq-compile",) + self.jq(jq, cmd, vals)
        return "bash", "ok", None


# ------------------------------------------------------------------------------------------------------------- build
def build_part(name: str, cfg: dict, val: Validators) -> dict:
    """Expand one tool, validate every distinct command on each pinned version, tag versions, write its part."""
    temps = cfg["templates"]
    per = max(1, round(cfg["target_pairs"] / max(1, len(temps))))
    pinned = [str(v["version"]) for v in cfg.get("versions") or []]
    envs = {v: env_bin(name, v) for v in pinned}
    # a version counts as checked only if its env really holds one of this tool's binaries
    checked = [v for v, b in envs.items() if b and any((b / x).exists() for x in cfg.get("binaries") or [])]
    pairs = []
    for t in temps:
        if len(str(t["body"])) > MAX_CMD_CHARS:
            continue
        for p in expand_template(t, cfg, max(per, len(t["intents"])), stable_rng(t["id"])):
            p.update({"tool": name, "template_id": t["id"], "group": t.get("group", ""),
                      "verified": t.get("verified", "docs"), "_vars": t.get("vars") or [], "_body": t["body"],
                      "_spec": t.get("versions")})
            pairs.append(p)

    tilde = sorted({p["template_id"] for p in pairs if QUOTED_TILDE.search(p["cmd"])})
    if tilde:
        sys.exit(f"{name}: a value starts with a quoted ~, which never expands: {', '.join(tilde)}")

    # validate distinct concrete commands, once per checked version (or once with PATH when nothing is pinned)
    targets = [(v, envs[v]) for v in checked] or [(None, None)]
    uniq = {}
    for p in pairs:
        p["_cmd"] = concrete(p)[0]
        uniq.setdefault(p["_cmd"], p)
    jobs = [(c, p, v, b) for c, p in uniq.items() for v, b in targets]
    res_of: dict[str, dict] = defaultdict(dict)  # concrete command -> {version: (validator, status, error)}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for (c, _, v, _), r in zip(jobs, ex.map(lambda j: val.run(j[1], j[3]), jobs)):
            res_of[c][v] = r

    dropped, per_version = {}, {str(v): Counter() for v, _ in targets}
    for p in pairs:
        res = res_of[p["_cmd"]]
        passing = [v for v, r in res.items() if r[1] != "fail"]
        for v, r in res.items():
            per_version[str(v)][r[1]] += 1
        p["validator"] = next(iter(res.values()))[0]
        statuses = [r[1] for r in res.values()]
        p["validation"] = ("fail" if not passing else "ok" if all(s == "ok" for s in statuses)
                           else "ok-some-versions" if "fail" in statuses else next(s for s in statuses if s != "ok"))
        expected = [v for v in pinned if version_ok(v, p["_spec"])]
        if checked:
            # a failure inside an explicit `versions:` spec drops the template; without a spec, a failure on some
            # versions only narrows valid_versions
            bad_expected = [v for v in checked if p["_spec"] and v in expected and res[v][1] == "fail"]
            if not passing or bad_expected:
                v0 = (bad_expected or checked)[0]
                dropped.setdefault(p["template_id"], f"{res[v0][0]} on {name} {v0}: {res[v0][2]}")
            p["valid_versions"] = [v for v in pinned if v in expected and (v not in checked or v in passing)]
            p["checked_versions"] = checked
        else:
            if p["validation"] == "fail":
                dropped.setdefault(p["template_id"], f"{p['validator']}: {res[None][2]}")
            p["valid_versions"] = expected if pinned else None
            p["checked_versions"] = []
    kept = [p for p in pairs if p["template_id"] not in dropped]

    # version-context variants: teach the model to read "Installed versions: ..." and pick matching syntax
    if len(pinned) > 1:
        label = cfg.get("version_label") or cfg["binaries"][0]
        extra = []
        for p in kept:
            vv = p.get("valid_versions") or []
            subset = 0 < len(vv) < len(pinned)
            rng = stable_rng("ver", p["template_id"], p["text"], p["cmd"])
            if vv and rng.random() < (0.6 if subset else 0.15):
                q = dict(p)
                q["version_line"] = VERSION_LINE.format(items=f"{label} {rng.choice(vv)}")
                q["kind"] = "version"
                extra.append(q)
        kept += extra

    PARTS.mkdir(parents=True, exist_ok=True)
    with (PARTS / f"{name}.jsonl").open("w") as f:
        for p in kept:
            f.write(json.dumps({k: v for k, v in p.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
    summary = {"templates": len(temps), "pairs": len(kept), "dropped_templates": dropped,
               "versions_pinned": pinned, "versions_checked": checked,
               "per_version_status": {v: dict(c) for v, c in per_version.items()}}
    (PARTS / f"{name}.summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def merge(tools: dict[str, dict]) -> dict:
    allp = []
    for name in tools:
        part = PARTS / f"{name}.jsonl"
        if part.exists():
            allp += [json.loads(l) for l in part.open()]
    # dedupe on the user message: one message must map to one command
    seen, final = set(), []
    for p in allp:
        k = user_text(p).lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(p)

    # eval: hold out whole intents (unseen phrasing) from a stratified sample of templates, core tools weighted up
    by_tool: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for p in final:
        by_tool[p["tool"]][p["template_id"]].append(p)
    w = {n: (2.0 if tools[n].get("priority") == "core" else 1.0) * sum(map(len, t.values()))
         for n, t in by_tool.items()}
    tw = sum(w.values()) or 1
    held, evals = set(), []  # held: (tool, template_id, intent_idx); every variant of a held intent leaves train
    for n, by_tid in sorted(by_tool.items()):
        quota = max(2, round(EVAL_TARGET * w[n] / tw))
        rng = stable_rng("eval", n)
        tids = sorted(by_tid)
        rng.shuffle(tids)
        got = 0
        for tid in tids:
            if got >= quota:
                break
            tps = by_tid[tid]
            if len({p["intent_idx"] for p in tps}) < 3:
                continue
            hold = max(p["intent_idx"] for p in tps)
            held.add((n, tid, hold))
            group = [p for p in tps if p["intent_idx"] == hold]
            pick = [p for p in group if p["kind"] == "plain"][:1]
            other = [p for p in group if p["kind"] in ("context", "version")]
            if other and rng.random() < 0.4:
                pick += other[:1]
            evals += pick
            got += len(pick)
    train = [p for p in final if (p["tool"], p["template_id"], p["intent_idx"]) not in held]
    random.Random(SEED).shuffle(train)

    def rec(p):
        return {"messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "user", "content": user_text(p)},
                             {"role": "assistant", "content": p["cmd"]}],
                "meta": {"tool": p["tool"], "template_id": p["template_id"], "group": p["group"],
                         "destructive": p["destructive"], "variant": p["kind"],
                         "valid_versions": p.get("valid_versions"), "checked_versions": p.get("checked_versions"),
                         "verified": p["verified"], "validator": p["validator"], "validation": p["validation"]}}
    for fname, rows in (("train.jsonl", train), ("eval.jsonl", evals)):
        with (DATA / fname).open("w") as f:
            for p in rows:
                f.write(json.dumps(rec(p), ensure_ascii=False) + "\n")
    per_tool = {}
    for n in sorted(tools):
        sp = PARTS / f"{n}.summary.json"
        s = json.loads(sp.read_text()) if sp.exists() else {}
        per_tool[n] = {"train": sum(1 for p in train if p["tool"] == n),
                       "eval": sum(1 for p in evals if p["tool"] == n),
                       "destructive_train": sum(1 for p in train if p["tool"] == n and p["destructive"]),
                       **{k: s.get(k) for k in ("templates", "versions_pinned", "versions_checked",
                                                 "per_version_status", "dropped_templates")}}
    report = {"built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "train": len(train),
              "eval": len(evals), "system_prompt": SYSTEM_PROMPT, "per_tool": per_tool}
    (DATA / "build_report.json").write_text(json.dumps(report, indent=1))
    return report


def build(args) -> int:
    only = set(args.only.split(",")) if args.only else None
    tools = load_tools()
    errs = lint({k: v for k, v in tools.items() if not only or k in only})
    if errs:
        print("\n".join(errs) + f"\nlint: {len(errs)} problem(s); fix them first.")
        return 1
    val = Validators(args.bin_dir, not args.no_exec_validate)
    print("validators on PATH:", ", ".join(k for k, v in val.available().items() if v) or "none")
    for name in sorted(tools):
        if only and name not in only:
            continue
        s = build_part(name, tools[name], val)
        print(f"{name:16s} pairs {s['pairs']:5d}  checked on {s['versions_checked'] or 'PATH'}  "
              f"dropped {len(s['dropped_templates'])}", flush=True)
        for tid, e in s["dropped_templates"].items():
            print(f"    dropped {tid}: {e}")
    rep = merge(tools)
    print(f"train {rep['train']}  eval {rep['eval']}")
    return 0


# ------------------------------------------------------------------------------------------------- envs / snapshots
def build_go(name: str, cfg: dict, spec: str, d: Path) -> bool:
    """Build a Go CLI from its official source tag. spec: go:<github.com/org/repo>@<tag> [(<subdir> <./pkg> CGO)]"""
    m = re.match(r"go:(github\.com/[^@\s]+)@(\S+)\s*(?:\((.*)\))?", spec)
    go, git = shutil.which("go"), shutil.which("git")
    if not m or not go or not git:
        print(f"{name}: cannot build '{spec}' (needs go and git on PATH)")
        return False
    repo, tag, extra = m.group(1), m.group(2), (m.group(3) or "").replace(",", " ").split()
    pkg = next((x for x in extra if x.startswith("./")), ".")
    sub = next((x for x in extra if not x.startswith("./") and x != "CGO"), "")
    cgo = "1" if "CGO" in extra else "0"
    src = CACHE / "src" / f"{name}-{tag}"
    if not src.is_dir():
        r = subprocess.run([git, "clone", "-q", "--depth", "1", "--branch", tag, f"https://{repo}", str(src)])
        if r.returncode:
            print(f"{name} {tag}: clone failed")
            return False
    work = src / sub
    if name == "argo":  # the CLI embeds the web UI build; an empty placeholder is enough for its parser
        (work / "ui" / "dist" / "app").mkdir(parents=True, exist_ok=True)
        (work / "ui" / "dist" / "app" / "index.html").touch()
    (d / "bin").mkdir(parents=True, exist_ok=True)
    out = d / "bin" / (cfg.get("binaries") or [name])[0]
    r = subprocess.run([go, "build", "-o", str(out), pkg], cwd=work, capture_output=True, text=True,
                       env={**os.environ, "CGO_ENABLED": cgo})
    if r.returncode:
        print(f"{name} {tag}: build failed: {r.stderr.strip()[-300:]}")
        return False
    print(f"{name} {tag}: built {out}")
    return True


def setup_envs(args) -> int:
    tools = load_tools(set(args.only.split(",")) if args.only else None)
    uv = shutil.which("uv")
    for name, cfg in tools.items():
        for v in cfg.get("versions") or []:
            spec, ver = v.get("install", ""), str(v["version"])
            d = env_dir(name, ver)
            # Done only when the env holds one of the tool's binaries, the same test build_part uses;
            # a bin/ left by a failed install or build is removed and set up again.
            if env_ready(cfg, d):
                print(f"{name} {ver}: exists")
                continue
            if not spec.startswith(("go:", "pip:")):
                print(f"{name} {ver}: install '{spec or 'manual'}' is not pip or go; put the binary in {d}/bin")
                continue
            if d.exists():
                print(f"{name} {ver}: removing an incomplete env")
                shutil.rmtree(d)
            try:
                ok = build_go(name, cfg, spec, d) if spec.startswith("go:") else install_pip(v, spec, d, uv)
            except BaseException:
                ok = False
                raise
            finally:
                if not ok and d.exists():
                    shutil.rmtree(d)
    return 0


def env_ready(cfg: dict, d: Path) -> bool:
    return any((d / "bin" / b).exists() for b in cfg.get("binaries") or [])


def install_pip(v: dict, spec: str, d: Path, uv: str | None) -> bool:
    d.parent.mkdir(parents=True, exist_ok=True)
    py = v.get("python") or "python3"
    pkgs = spec[4:].split()
    con = ["--constraint", v["constraints"]] if v.get("constraints") else []
    print(f"{d.name}: installing {' '.join(pkgs)}")
    if uv:
        subprocess.run([uv, "venv", "-q", "-p", py, str(d)], check=True)
        subprocess.run([uv, "pip", "install", "-q", *pkgs, *con],
                       env={**os.environ, "VIRTUAL_ENV": str(d)}, check=True)
    else:
        subprocess.run([shutil.which(py) or sys.executable, "-m", "venv", str(d)], check=True)
        subprocess.run([str(d / "bin" / "pip"), "install", "-q", *pkgs, *con], check=True)
    return True


SNAPSHOT_SCRIPTS = {
    # each prints JSON {"command path": ["--flag", ...]}
    "click": ("import json, click\n{imp}\nout={{}}\n"
              "def walk(cmd, path):\n"
              "    out[' '.join(path)] = sorted(o for p in cmd.params for o in list(p.opts)+list(p.secondary_opts))\n"
              "    if isinstance(cmd, click.Group):\n"
              "        for n, s in cmd.commands.items(): walk(s, path+[n])\n"
              "walk(c, [{root!r}])\nprint(json.dumps(out))\n"),
    "airflow": ("import json, argparse\nfrom airflow.cli.cli_parser import get_parser\nout={{}}\n"
                "def walk(p, path):\n"
                "    out[' '.join(path)] = sorted(s for a in p._actions for s in a.option_strings)\n"
                "    for a in p._actions:\n"
                "        if isinstance(a, argparse._SubParsersAction):\n"
                "            for n, s in a.choices.items(): walk(s, path+[n])\n"
                "walk(get_parser(), ['airflow'])\nprint(json.dumps(out))\n"),
}
PARSERS = {"dbt": ("click", "from dbt.cli.main import cli as c", "dbt"),
           "snowflake-cli": ("click", "from snowflake.cli._app.cli_app import CliAppFactory as F; import typer; "
                                      "c = typer.main.get_command(F().create_or_get_app())", "snow"),
           "airflow": ("airflow", "", "airflow"),
           "spark": ("spark", "", "spark-submit")}
COBRA_TOOLS = {"databricks": "databricks", "confluent": "confluent", "astro": "astro", "argo": "argo", "rpk": "rpk"}


def cobra_tree(exe: str, home: Path, max_depth: int = 3) -> dict:
    """Walk a cobra CLI's help: 'Available Commands' sections give subcommands, 'Flags' sections give flags."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "NO_COLOR": "1"}
    out = {}

    def walk(path: list[str]):
        r = subprocess.run([exe] + path + ["--help"], env=env, capture_output=True, text=True, timeout=30)
        txt = r.stdout + r.stderr
        out[" ".join([Path(exe).name] + path)] = sorted(set(re.findall(r"(?m)^\s+(?:-\w, )?(--[a-z0-9][\w-]*)", txt)))
        if len(path) >= max_depth:
            return
        m = re.search(r"(?ms)^(?:Available Commands|[A-Z][\w ]* Commands):\n(.*?)(?:\n\n|\Z)", txt)
        subs = []
        for sec in re.finditer(r"(?ms)^(?:Available Commands|[A-Z][\w ]*Commands):\n(.*?)(?:\n\n|\Z)", txt):
            subs += re.findall(r"(?m)^\s{2}([a-z][\w-]*)\s", sec.group(1))
        for s in sorted(set(subs) - {"help", "completion"}):
            walk(path + [s])
    walk([])
    return out


def snapshot_tool(name: str, cfg: dict) -> list[str]:
    done = []
    kind = PARSERS.get(name)
    for v in cfg.get("versions") or []:
        ver = str(v["version"])
        b = env_bin(name, ver)
        if not b:
            continue
        if name in COBRA_TOOLS:
            home = Path(tempfile.mkdtemp(prefix="nl2sh-home-"))
            tree = cobra_tree(str(b / COBRA_TOOLS[name]), home)
        elif kind and kind[0] == "spark":
            r = subprocess.run([str(b / "spark-submit"), "--help"], capture_output=True, text=True, timeout=180)
            tree = {"spark-submit": sorted(set(re.findall(r"^\s+(--[a-z-]+)", r.stdout + r.stderr, re.M)))}
        elif kind:
            script = SNAPSHOT_SCRIPTS[kind[0]].format(imp=kind[1], root=kind[2])
            r = subprocess.run([str(b / "python"), "-c", script], capture_output=True, text=True, timeout=300,
                               env={**os.environ, "AIRFLOW_HOME": str(CACHE / f"af-home-{ver}")}, cwd=CACHE)
            try:
                tree = json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                print(f"{name} {ver}: snapshot failed: {r.stderr[-300:]}")
                continue
        else:
            continue
        (SNAP / name).mkdir(parents=True, exist_ok=True)
        (SNAP / name / f"{ver}.json").write_text(json.dumps(tree, indent=0, sort_keys=True))
        done.append(ver)
    return done


def snapshot(args) -> int:
    for name, cfg in load_tools(set(args.only.split(",")) if args.only else None).items():
        vs = snapshot_tool(name, cfg)
        if vs:
            print(f"{name}: snapshot {', '.join(vs)}")
    return 0


def diff_snapshots(a: dict, b: dict) -> dict:
    common = set(a) & set(b)
    return {"commands_added": sorted(set(b) - set(a)), "commands_removed": sorted(set(a) - set(b)),
            "flags_added": {k: sorted(set(b[k]) - set(a[k])) for k in common if set(b[k]) - set(a[k])},
            "flags_removed": {k: sorted(set(a[k]) - set(b[k])) for k in common if set(a[k]) - set(b[k])}}


def check_docs(name: str, cfg: dict) -> dict:
    """Fetch each source page, store a hash, report pages that changed since the last refresh."""
    path = SNAP / name / "docs.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    new, changed, failed = {}, [], []
    for s in cfg.get("sources") or []:
        url = s.get("url")
        if not url:
            continue
        # github.com/<org>/<repo>/blob/<ref>/<path> -> the raw file: stable bytes to hash, no page chrome
        fetch = re.sub(r"^https://github\.com/([^/]+)/([^/]+)/blob/", r"https://raw.githubusercontent.com/\1/\2/", url)
        try:
            with urllib.request.urlopen(urllib.request.Request(fetch, headers={"User-Agent": "nl2sh-refresh"}),
                                        timeout=30) as r:
                h = hashlib.sha256(r.read()).hexdigest()
            new[url] = {"sha256": h, "fetched": time.strftime("%Y-%m-%d")}
            if url in old and old[url]["sha256"] != h:
                changed.append(url)
        except Exception as e:
            failed.append(f"{url} ({type(e).__name__})")
            if url in old:
                new[url] = old[url]
    (SNAP / name).mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new, indent=1))
    return {"changed": changed, "unreachable": failed, "checked": len(new)}


def refresh(args) -> int:
    name, ver = args.tool, args.version
    p = TOOLS / f"{name}.yaml"
    cfg = load_tools({name}).get(name)
    if not cfg:
        print(f"no tools/{name}.yaml")
        return 1
    known = [str(v["version"]) for v in cfg.get("versions") or []]
    prev = known[0] if known else None
    if ver not in known:
        # Insert the new version at the top of `versions:` (first = current), leaving the rest of the file as is.
        entry = f'- {{version: "{ver}"' + (f', install: "{args.install}"' if args.install else "") + \
                (f', python: "{args.python}"' if args.python else "") + "}"
        text = p.read_text()
        m = re.search(r"(?m)^versions:[ \t]*(\[\])?[ \t]*$", text)
        if m:
            text = text[:m.start()] + "versions:\n" + entry + text[m.end():]
        else:
            text = re.sub(r"(?m)^sources:", "versions:\n" + entry + "\nsources:", text, count=1)
        p.write_text(text)
        cfg = load_tools({name})[name]
        print(f"added {name} {ver} to tools/{name}.yaml")
    setup_envs(argparse.Namespace(only=name))
    snapshot_tool(name, cfg)
    if prev and (SNAP / name / f"{prev}.json").exists() and (SNAP / name / f"{ver}.json").exists():
        d = diff_snapshots(json.loads((SNAP / name / f"{prev}.json").read_text()),
                           json.loads((SNAP / name / f"{ver}.json").read_text()))
        (SNAP / name / f"diff-{prev}-to-{ver}.json").write_text(json.dumps(d, indent=1))
        print(f"flag diff {prev} -> {ver}: +{len(d['commands_added'])} / -{len(d['commands_removed'])} commands, "
              f"{sum(map(len, d['flags_added'].values()))} flags added, "
              f"{sum(map(len, d['flags_removed'].values()))} removed")
        removed = {f for fl in d["flags_removed"].values() for f in fl}
        hit = [t["id"] for t in cfg["templates"] if any(re.search(re.escape(f) + r"\b", t["body"]) for f in removed)]
        if hit:
            print("templates using a removed flag (re-checked now; tagged to older versions or dropped):",
                  ", ".join(hit))
    docs = check_docs(name, cfg)
    print(f"docs: {docs['checked']} pages hashed, {len(docs['changed'])} changed, "
          f"{len(docs['unreachable'])} unreachable")
    for u in docs["changed"]:
        print("  changed:", u)
    return build(argparse.Namespace(only=name, bin_dir=None, no_exec_validate=False))


# ---------------------------------------------------------------------------------------------- predict / score
def normalize(cmd: str) -> str:
    """Collapse whitespace between words but keep each word's quotes, so '$FILE' and "$FILE" differ."""
    cmd = cmd.strip().strip("`").strip()
    try:
        return " ".join(shlex.split(cmd, posix=False))
    except ValueError:
        return re.sub(r"\s+", " ", cmd)


# An unquoted value can't start with a quote, so each value matches one way only (no ReDoS backtracking).
_VALUE = r"""(?:'[^']*'|"(?:[^"\\]|\\.)*"|[^\s'"]\S*)"""


def parse_line(cmd: str) -> tuple[dict, str]:
    """Split 'A=1 B=2 && body' into ({A:1,B:2}, body)."""
    m = re.match(rf"^((?:[A-Z][A-Z0-9_]*={_VALUE}\s+)+)&&\s*(.*)$", cmd.strip())
    if not m:
        return {}, cmd.strip()
    return ({k: v.strip("'\"") for k, v in re.findall(rf"([A-Z][A-Z0-9_]*)=({_VALUE})",
                                                       m.group(1))}, m.group(2).strip())


def flags(body: str) -> set[str]:
    return set(re.findall(r"(?<![\w$])(--?[A-Za-z][\w-]*)", body))


def head(body: str, n: int = 3) -> list[str]:
    out = []
    for w in body.split():
        if w.startswith("-") or "$" in w or len(out) >= n:
            break
        out.append(w)
    return out


def score(args) -> int:
    gold = {}
    for l in (DATA / "eval.jsonl").open():
        r = json.loads(l)
        gold[r["messages"][1]["content"]] = r
    n = em = nm = hd = varok = 0
    f1s, per_tool = [], defaultdict(lambda: [0, 0])
    for line in Path(args.predictions).open():
        r = json.loads(line)
        g = gold.get(r["user"])
        if not g:
            continue
        n += 1
        gc, pc = g["messages"][2]["content"], r["prediction"]
        ga, gb = parse_line(gc)
        pa, pb = parse_line(pc)
        em += pc.strip() == gc.strip()
        ok = normalize(pc) == normalize(gc)
        nm += ok
        hd += head(pb) == head(gb)
        varok += ga == pa
        gf, pf = flags(gb), flags(pb)
        f1s.append(1.0 if not gf and not pf else 2 * len(gf & pf) / (len(gf) + len(pf)))
        per_tool[g["meta"]["tool"]][0] += 1
        per_tool[g["meta"]["tool"]][1] += ok
    if not n:
        print("no predictions matched eval.jsonl")
        return 1
    print(f"n={n}  exact={em/n:.3f}  normalized={nm/n:.3f}  command_head={hd/n:.3f}  "
          f"flag_f1={sum(f1s)/n:.3f}  values_match={varok/n:.3f}")
    for t, (c, ok) in sorted(per_tool.items()):
        print(f"  {t:18s} {ok}/{c}")
    return 0


def predict(args) -> int:
    url = args.endpoint.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    with Path(args.out).open("w") as out:
        for line in (DATA / "eval.jsonl").open():
            r = json.loads(line)
            body = {"model": args.model, "messages": r["messages"][:2], "temperature": 0, "max_tokens": 200,
                    "stop": ["\n"]}
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                text = json.loads(resp.read())["choices"][0]["message"]["content"].strip()
            out.write(json.dumps({"user": r["messages"][1]["content"], "prediction": text}) + "\n")
    print("wrote", args.out)
    return 0


# ---------------------------------------------------------------------------------------------------------- scaffold
SCAFFOLD = """tool: {name}
binaries: [{binary}]
category: {category}
priority: standard
target_pairs: 100
version_note: ""
version_cmd: {binary} --version
versions: []
sources:
  - url: https://example.com/docs/{binary}
    license: "vendor docs; facts only"
destructive_patterns: []
templates:
  - id: {name}.version
    group: setup
    intents:
      - what version of {binary} do i have
      - check {binary} version
      - {binary} version
      - is {binary} installed
    vars: []
    body: {binary} --version
    destructive: false
    source: https://example.com/docs/{binary}
    verified: docs
"""


def add_tool(args) -> int:
    p = TOOLS / f"{args.name}.yaml"
    if p.exists():
        print(f"{p} already exists")
        return 1
    TOOLS.mkdir(exist_ok=True)
    p.write_text(SCAFFOLD.format(name=args.name, binary=args.binary or args.name, category=args.category))
    print(f"created {p}\nnext: add templates (TOOL_SPEC.md), then: python nl2sh.py build --only {args.name}")
    return 0


def stats(_args) -> int:
    rep = json.loads((DATA / "build_report.json").read_text())
    print(f"train {rep['train']}  eval {rep['eval']}")
    for n, s in rep["per_tool"].items():
        print(f"  {n:16s} train {s['train']:5d}  eval {s['eval']:3d}  templates {s['templates'] or 0:3d}  "
              f"destructive {s['destructive_train']:4d}  versions {s.get('versions_checked') or '-'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-tool"); a.add_argument("name"); a.add_argument("--binary")
    a.add_argument("--category", default="files")
    sub.add_parser("lint")
    sub.add_parser("installs")
    e = sub.add_parser("setup-envs"); e.add_argument("--only")
    sn = sub.add_parser("snapshot"); sn.add_argument("--only")
    b = sub.add_parser("build"); b.add_argument("--no-exec-validate", action="store_true")
    b.add_argument("--only"); b.add_argument("--bin-dir")
    r = sub.add_parser("refresh"); r.add_argument("tool"); r.add_argument("--version", required=True)
    r.add_argument("--install"); r.add_argument("--python")
    sub.add_parser("stats")
    p = sub.add_parser("predict"); p.add_argument("--endpoint", required=True)
    p.add_argument("--model", default="local"); p.add_argument("--out", default="predictions.jsonl")
    s = sub.add_parser("score"); s.add_argument("predictions")
    args = ap.parse_args()
    if args.cmd == "lint":
        errs = lint(load_tools())
        print("\n".join(errs) or "ok")
        return 1 if errs else 0
    return {"add-tool": add_tool, "installs": write_installs, "setup-envs": setup_envs, "snapshot": snapshot, "build": build,
            "refresh": refresh, "stats": stats, "predict": predict, "score": score}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
