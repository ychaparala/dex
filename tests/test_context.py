from __future__ import annotations

import pytest

from dex import context
from tests.conftest import Dirs


def test_aws_default_from_config(dirs: Dirs) -> None:
    dirs.write(
        ".aws/config", "[profile etl]\nregion = us-west-2\n\n[default]\nregion = us-east-1\n"
    )
    assert context.aws() == ["AWS profile in use is default", "Default AWS region is us-east-1"]


def test_aws_default_from_credentials(dirs: Dirs) -> None:
    dirs.write(".aws/credentials", "[default]  # main\naws_access_key_id = x\n")
    assert context.aws() == ["AWS profile in use is default"]


def test_aws_env_profile(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "etl")
    dirs.write(".aws/config", "[default]\nregion = us-east-1\n[profile etl]\nregion = us-west-2\n")
    assert context.aws() == ["AWS profile in use is etl", "Default AWS region is us-west-2"]


def test_aws_env_region_and_custom_file(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = dirs.write("elsewhere/aws.ini", "[profile default]\nregion = eu-west-1\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(cfg))
    assert context.aws() == ["AWS profile in use is default", "Default AWS region is eu-west-1"]
    monkeypatch.setenv("AWS_REGION", "ap-south-1")
    assert context.aws()[1] == "Default AWS region is ap-south-1"


def test_aws_nothing(dirs: Dirs) -> None:
    dirs.write(".aws/config", "[profile etl]\nregion = us-west-2\n")
    assert context.aws() == []


def test_unsafe_values_are_dropped(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("two words", "a<b", "x" * 65, "line\nbreak", "esc\x1b", "`id`", "$(id)", "a;b"):
        monkeypatch.setenv("AWS_PROFILE", value)
        assert context.aws() == [], value


def test_databricks(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs.write(".databrickscfg", "[prod]\nhost = https://example.cloud.databricks.com\n")
    assert context.databricks() == []  # no DEFAULT section, so the CLI would ask
    dirs.write(".databrickscfg", "[DEFAULT]\nhost = h\n[prod]\nhost = h\n")
    assert context.databricks() == ["Databricks CLI profile is DEFAULT"]
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "prod")
    assert context.databricks() == ["Databricks CLI profile is prod"]


def test_snowflake(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    assert context.snowflake() == []
    dirs.write(".snowflake/config.toml", 'default_connection_name = "dev"\n[connections.prod]\n')
    assert context.snowflake() == ["Snowflake connection is dev"]
    dirs.write(".snowflake/config.toml", "[connections.default]\naccount = 'x'\n")
    assert context.snowflake() == ["Snowflake connection is default"]
    dirs.write(".snowflake/config.toml", "")
    dirs.write(".snowflake/connections.toml", "[default]\naccount = 'x'\n")
    assert context.snowflake() == ["Snowflake connection is default"]
    monkeypatch.setenv("SNOWFLAKE_DEFAULT_CONNECTION_NAME", "prod")
    assert context.snowflake() == ["Snowflake connection is prod"]


def test_snowflake_home(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = dirs.write("sf/config.toml", "default_connection_name = 'ci'\n")
    monkeypatch.setenv("SNOWFLAKE_HOME", str(cfg.parent))
    assert context.snowflake() == ["Snowflake connection is ci"]


def test_kafka(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    assert context.kafka() == []
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "b1:9092,b2:9092")
    assert context.kafka() == ["Kafka bootstrap server is b1:9092"]


KUBECONFIG = """\
apiVersion: v1
contexts:
- context:
    cluster: dev
    user: me
  name: dev
- context:
    cluster: prod
    namespace: data  # comment
    user: me
  name: prod-eks
current-context: prod-eks
kind: Config
"""


def test_kubernetes(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    dirs.write(".kube/config", KUBECONFIG)
    assert context.kubernetes() == ["kubectl context is prod-eks", "Kubernetes namespace is data"]
    dirs.write(
        ".kube/config", KUBECONFIG.replace("current-context: prod-eks", "current-context: dev")
    )
    assert context.kubernetes() == ["kubectl context is dev"]
    empty = dirs.write("k/empty", "kind: Config\n")
    other = dirs.write("k/other", 'current-context: "ci"\n')
    monkeypatch.setenv("KUBECONFIG", f"{empty}:{other}")
    assert context.kubernetes() == ["kubectl context is ci"]
    dirs.write("k/other", "current-context: bad name\n")
    assert context.kubernetes() == []


def test_kubernetes_namespace_shapes(dirs: Dirs) -> None:
    # minikube nests extensions with their own name: keys inside a context
    dirs.write(
        ".kube/config",
        "contexts:\n- context:\n    cluster: minikube\n    extensions:\n    - extension:\n"
        "        provider: minikube.sigs.k8s.io\n      name: context_info\n    namespace: data\n"
        "  name: minikube\ncurrent-context: minikube\n",
    )
    assert context.kubernetes() == ["kubectl context is minikube", "Kubernetes namespace is data"]
    # an indented list isn't parsed: no namespace hint rather than a wrong one
    dirs.write(
        ".kube/config",
        "contexts:\n  - name: a\n    context:\n      namespace: one\n"
        "  - name: b\n    context:\n      namespace: two\ncurrent-context: b\n",
    )
    assert context.kubernetes() == ["kubectl context is b"]


def test_gcp(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    assert context.gcp() == []
    dirs.write(".config/gcloud/configurations/config_default", "[core]\nproject = acme-data\n")
    assert context.gcp() == ["GCP project is acme-data"]
    dirs.write(".config/gcloud/active_config", "work\n")
    dirs.write(".config/gcloud/configurations/config_work", "[core]\nproject = acme-work\n")
    assert context.gcp() == ["GCP project is acme-work"]
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
    assert context.gcp() == ["GCP project is from-env"]


def test_dbt(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    assert context.dbt() == []
    monkeypatch.setenv("DBT_TARGET", "dev")
    assert context.dbt() == ["dbt target is dev"]


def test_airflow(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    assert context.airflow() == []
    for n in ("ingest_events.py", "daily_sales_etl.py", "__init__.py", "notes.md", ".x.py"):
        dirs.write(f"dags/{n}", root="cwd")
    assert context.airflow() == ["Airflow DAGs here: daily_sales_etl ingest_events"]
    dirs.write("airflow/dags/home_dag.py")
    monkeypatch.setenv("AIRFLOW_HOME", str(dirs.home / "airflow"))
    assert context.airflow() == ["Airflow DAGs here: home_dag"]


def test_files(dirs: Dirs) -> None:
    assert context.files() == []
    for n in ("README.md", "sales.parquet", "notebooks/a.ipynb", ".env", "bad name.csv"):
        dirs.write(n, root="cwd")
    assert context.files() == ["Working directory contains: sales.parquet README.md notebooks/"]


def test_wanted() -> None:
    names = [s.name for s in context.wanted("first rows of sales.parquet from my s3 bucket")]
    assert names == ["aws", "files"]
    assert [s.name for s in context.wanted("run the Step Functions machine")] == ["aws"]
    assert context.wanted("say hello") == []


def test_gather(dirs: Dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_PROFILE", "etl")
    monkeypatch.setenv("DBT_TARGET", "dev")
    dirs.write("sales.parquet", root="cwd")
    assert context.gather("list my s3 buckets") == ["AWS profile in use is etl"]
    assert context.gather("first rows of sales.parquet") == [
        "Working directory contains: sales.parquet"
    ]
    assert context.gather("hello") == []
