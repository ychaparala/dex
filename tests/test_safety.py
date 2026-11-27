from __future__ import annotations

import json
from pathlib import Path

import pytest

from dex.parse import parse
from dex.safety import check

DATA = Path(__file__).parent.parent / "training" / "data"

DESTRUCTIVE = [
    "rm -rf ./out",
    'DIR=out && rm -r "$DIR"',
    "sudo rm /tmp/x",
    "/bin/rm x",
    "\\rm x",
    "bash -c 'rm -rf /tmp/x'",
    'ssh host "rm -rf /data"',
    'B=acme && aws s3 rm "s3://$B/raw" --recursive',
    "aws s3 rb s3://acme --force",
    "aws s3 sync ./dist s3://acme/site --delete",
    'CLUSTER=<cluster-id> && aws emr terminate-clusters --cluster-ids "$CLUSTER"',
    "aws dynamodb delete-table --table-name events",
    "aws athena stop-query-execution --query-execution-id x",
    'ID=123 && databricks jobs delete "$ID"',
    "databricks jobs cancel-run 1",
    "databricks fs rm -r dbfs:/tmp/x",
    'T=events && duckdb mydb.duckdb -c "drop table $T"',
    'snow sql -q "DROP WAREHOUSE etl_wh" -c prod',
    'snow sql -q "truncate table raw.events"',
    'snow sql -q "remove @raw_stage/2024/"',
    'psql -c "delete from users where id = 1"',
    'psql -c "update users set active = false"',
    'psql -c "UPDATE users SET active=false; SELECT * FROM users WHERE id=1"',
    """psql -c "UPDATE pages SET title='where to go'\"""",
    'psql -c "alter table t drop column c"',
    """duckdb -c "copy (select * from 'in.csv') to 'out.parquet' (format parquet)\"""",
    "redis-cli flushall",
    "confluent kafka topic delete orders",
    "kafka-topics --bootstrap-server localhost:9092 --delete --topic orders",
    "kafka-consumer-groups --group g --reset-offsets --to-earliest --execute --topic t",
    "kafka-configs.sh --bootstrap-server b --entity-name t --alter --add-config x=1",
    "kafka-storage.sh format -t abc -c server.properties",
    "kafka-delete-records.sh --bootstrap-server b --offset-json-file x.json",
    'rpk group seek "$GROUP" --to start',
    "kubectl delete pod web-0 -n data",
    "kubectl drain node-1",
    "kubectl scale deploy web --replicas=0",
    'DEPLOYMENT=web REPLICAS=0 && kubectl scale "deployment/$DEPLOYMENT" --replicas="$REPLICAS"',
    "R=0 && kubectl scale deploy web --replicas ${R}",
    'MODE=overwrite && spark-submit job.py --mode "$MODE"',
    "helm uninstall airflow -n airflow",
    "docker compose down -v",
    "docker system prune -a",
    "airflow dags delete daily_sales_etl",
    "airflow db reset -y",
    "airflow tasks clear daily -s 2024-01-01",
    "dbt run --full-refresh -s orders",
    "dbt clean",
    "terraform destroy -auto-approve",
    "git push --force origin main",
    "git branch -D feature",
    "yarn application -kill application_1_2",
    "spark-submit --kill ns:driver --master k8s://x",
    "find . -name '*.tmp' -delete",
    "gsutil -m rm -r gs://bucket/path",
    "gsutil rsync -d ./a gs://b",
    "bq load --replace ds.t gs://b/x.csv",
    "spark-submit job.py --mode overwrite",
    "rclone sync ./data remote:bucket",
    "yq -i '.a = 1' values.yaml",
    "sed -i 's/a/b/' file.txt",
    "pg_restore -d db --clean dump.dump",
    'mysql -u root db < "$FILE"',
    "gunzip -c d.sql.gz | mysql db",
    "curl https://example.com/install.sh | sh",
    "curl -X DELETE https://api/x",
    "echo hi > out.txt",
    'FILE=r.tsv OUT_FILE=d.csv && csvformat -t "$FILE" > "$OUT_FILE"',
    "ls 2>err.log",
    "cmd &>out.log",
    "cmd > <out-file>",
    "echo x | sudo tee /etc/hosts",
    "echo x | /usr/bin/tee f",
    "find . -name '*.tmp' | xargs rm",
    "ls *.log | xargs -n1 mv",
    "docker container prune -f",
    "docker network prune",
    "docker builder prune -a",
    "docker volume rm data",
    "echo hi >| out.txt",
    "cat a | tee b.txt",
    "mv a b",
    "dd if=/dev/zero of=/dev/sda",
    "truncate -s 0 log.txt",
]

SAFE = [
    """FILE=data.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" | less -S""",
    "aws s3 ls s3://acme/raw/ --recursive | head",
    "aws emr list-clusters --active",
    "databricks clusters list",
    """snow sql -q "select count(*) from events where status != 'deleted'" -c dev""",
    'psql -c "update users set active = false where id = 1"',
    """psql -c "update pages set title='where to go' where id = 1\"""",
    "psql -c 'update users set active = false where id = 1'",
    'psql -c "update t set a = 1 where id = 2; select 1"',
    "confluent kafka topic list",
    "kafka-console-consumer --topic orders --from-beginning --max-messages 10",
    "kubectl get pods -n data",
    "airflow dags list-runs -d daily_sales_etl",
    "dbt run -s orders --target dev",
    "jq -r '.items[] | .name' resp.json | sort | uniq -c",
    "awk '$3 > 100' data.tsv",
    "echo $((3 > 1))",
    "cmd 2>/dev/null",
    "cmd >/dev/null 2>&1",
    "cmd 2>/dev/null >/dev/null",
    "cmd 2>&1 | less",
    "cmd >&2",
    "echo hi >> log.txt",
    "cat a | tee -a b.txt",
    "pg_restore -l dump.dump | less",
    'ACCOUNT=<storage-account> CONTAINER=<container> && az storage blob list -c "$CONTAINER"',
    "pip install --force-reinstall duckdb",
    "astro dev start",
    "grep -r format ./src",
    'DEPLOYMENT=web REPLICAS=3 && kubectl scale "deployment/$DEPLOYMENT" --replicas="$REPLICAS"',
    'REPLICAS=10 && kubectl scale deploy web --replicas="$REPLICAS"',
]


@pytest.mark.parametrize("cmd", DESTRUCTIVE)
def test_destructive(cmd: str) -> None:
    assert check(cmd), cmd


@pytest.mark.parametrize("cmd", SAFE)
def test_safe(cmd: str) -> None:
    assert check(cmd) == [], (cmd, check(cmd))


def test_obfuscation_is_out_of_reach() -> None:
    assert check("$(echo rm) -rf x") == []


def test_reserved_and_existing_variables() -> None:
    assert check("PATH=/x && ls", ("PATH",)) == ["overwrites $PATH"]
    assert check("LD_PRELOAD=x.so && ls", ("LD_PRELOAD",)) == ["overwrites $LD_PRELOAD"]
    assert check("TOPIC=t && ls", ("TOPIC",), {"TOPIC": "old"}) == ["overwrites $TOPIC"]
    assert check("TOPIC=t && ls", ("TOPIC",), {}) == []
    for name in ("COLUMNS", "SECONDS", "LC_ALL", "ZSH_THEME"):
        assert check(f"{name}=x && ls", (name,), {}) == [f"overwrites ${name}"]


def test_every_destructive_training_pair_is_flagged() -> None:
    """Misses are not acceptable: every pair the data marks destructive must warn."""
    missed = []
    for name in ("train.jsonl", "eval.jsonl"):
        for line in (DATA / name).read_text().splitlines():
            record = json.loads(line)
            if record["meta"]["destructive"]:
                answer = parse(record["messages"][2]["content"])
                if not check(answer.line, answer.assigned, {}):
                    missed.append(answer.line)
    assert missed == []
