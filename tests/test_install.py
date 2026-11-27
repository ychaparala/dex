from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dex import install


@pytest.mark.parametrize(
    ("line", "programs"),
    [
        ("FILE=a.csv && duckdb -c \"from '$FILE'\" | less -S", ["duckdb", "less"]),
        ("aws s3 ls | jq -r '.[]' | sort | uniq -c", ["aws", "jq", "sort", "uniq"]),
        ("X=1 && echo $(uname -m) > out.txt; cd x && ./run.sh", ["uname"]),
        ('for f in *.csv; do csvlook "$f"; done', ["csvlook"]),
        ("sudo -u postgres psql -c 'select 1'", ["sudo", "psql"]),
        ("ls | xargs -n 1 -I {} aws s3 cp {} s3://b/", ["ls", "xargs", "aws"]),
        ("env AWS_PROFILE=x time aws s3 ls || true", ["env", "aws"]),
        ("if [ -f a ]; then duckdb a; fi", ["duckdb"]),
        ('"$CMD" --flag && /usr/bin/env', []),
        ("bash -c 'duckdb x' && duckdb y", ["bash", "duckdb"]),
        ("printf '%s' '|' jq", []),
        ("echo $(date) jq && X=$(uname -m) duckdb a", ["date", "uname", "duckdb"]),
        ('eval "$(aws configure export-credentials --format env)"', ["aws"]),
        ('curl "https://x/$(uname -m).zip" -o "a b.zip" | jq', ["curl", "jq", "uname"]),
        ('echo "$(date" | jq', ["jq"]),
        ("echo \\| jq && echo $(echo ')' | sed s/x/y/) yq", ["sed"]),
        ('echo "a; b" \\| jq && grep "x|y" f', ["grep"]),
        ('(cd "$(mktemp -d)" && curl -o a x && sudo sh a)', ["curl", "sudo", "sh", "mktemp"]),
    ],
)
def test_commands(line: str, programs: list[str]) -> None:
    assert install.commands(line) == programs


@pytest.fixture
def path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty PATH directory; ``put`` adds programs to it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("PIPX_BIN_DIR", str(bin_dir))
    return bin_dir


def put(bin_dir: Path, *names: str) -> None:
    for name in names:
        (bin_dir / name).write_text("#!/bin/sh\n")
        (bin_dir / name).chmod(0o755)


@pytest.fixture
def table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "installs.json"
    file.write_text(
        json.dumps(
            {
                "duckdb": {"macos": ["brew install duckdb"], "linux": ["brew install duckdb"]},
                "csvlook": {"any": ["uv tool install csvkit", "pipx install csvkit"]},
                "csvcut": {"any": ["uv tool install csvkit", "pipx install csvkit"]},
                "aws": {"linux": ["curl -o a.zip https://x && unzip a.zip && sudo ./aws/install"]},
                "jq": {"linux": ["sudo apt-get install jq"]},
                "kcat": {"macos": ["brew install kcat"]},
                "helm": {"macos": ["brew install helm"], "docs": ["https://helm.sh/install"]},
                "uv": {"macos": ["brew install uv"], "docs": ["https://uv.example/install"]},
                "brew": {"docs": ["https://brew.example"]},
            }
        )
    )
    monkeypatch.setattr(install, "_FILE", str(file))


def test_which(path: Path) -> None:
    put(path, "duckdb")
    (path / "notes").write_text("not executable")
    assert install.which("duckdb")
    assert not install.which("notes")
    assert not install.which("jq")


def test_empty_path_entry_is_the_current_directory(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(path, "jq")
    monkeypatch.chdir(path)
    monkeypatch.setenv("PATH", ":")
    assert install.which("jq")
    assert install._on_path(str(path))


@pytest.mark.usefixtures("table")
def test_everything_installed(path: Path) -> None:
    put(path, "duckdb", "less")
    assert install.hints("duckdb x.db | less") == []


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_missing_tool_gets_an_install_hint(
    path: Path, platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", platform)
    put(path, "brew")
    assert install.hints("FILE=a && duckdb -c x") == [
        "duckdb not found, install with: brew install duckdb"
    ]


@pytest.mark.usefixtures("table")
def test_first_usable_installer_wins_and_tools_share_a_hint(path: Path) -> None:
    put(path, "pipx", "brew")
    assert install.hints("csvcut -c a x.csv | csvlook && duckdb x") == [
        "csvcut, csvlook not found, install with: pipx install csvkit",
        "duckdb not found, install with: brew install duckdb",
    ]


@pytest.mark.usefixtures("table")
def test_hint_adds_the_tool_folder_when_it_is_not_on_path(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(path, "uv")
    for name in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "XDG_DATA_HOME", "PIPX_BIN_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    local_bin = tmp_path / ".local" / "bin"
    assert install.hints("csvlook a.csv") == [
        f"csvlook not found, install with: uv tool install csvkit, then add {local_bin} to PATH"
    ]
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg" / "data"))
    assert install.hints("csvlook a.csv")[0].endswith(
        f"then add {tmp_path / 'xdg' / 'bin'} to PATH"
    )
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(path))
    assert install.hints("csvlook a.csv") == [
        "csvlook not found, install with: uv tool install csvkit"
    ]


@pytest.mark.usefixtures("table")
def test_installer_missing_too(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    put(path, "curl")
    assert install.hints("aws s3 ls") == [
        "aws not found, install with: "
        "curl -o a.zip https://x && unzip a.zip && sudo ./aws/install (needs unzip, sudo)"
    ]


@pytest.mark.usefixtures("table")
def test_unknown_os(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "freebsd14")
    put(path, "brew")
    assert install.hints("duckdb x | kcat -C") == [
        "duckdb, kcat not found, and dex doesn't know how to install them on freebsd14"
    ]


@pytest.mark.usefixtures("table")
def test_only_tools_dex_knows_get_a_hint(path: Path) -> None:
    """A shell function such as conda isn't on PATH, so it would read as missing."""
    assert install.hints("conda activate x && mlr --icsv cat a.csv") == []


@pytest.mark.usefixtures("table")
def test_apt_hint_names_the_distribution(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    put(path, "sudo", "apt-get")
    assert install.hints("jq .a f.json") == [
        "jq not found, on Debian/Ubuntu install with: sudo apt-get install jq"
    ]


@pytest.mark.usefixtures("table")
def test_install_page_when_no_command_fits(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    assert install.hints("helm list") == [
        "helm not found, see https://helm.sh/install to install it"
    ]
    monkeypatch.setattr("sys.platform", "darwin")
    assert install.hints("helm list") == [
        "helm not found, install with: brew install helm (needs brew)",
        "brew not found, see https://brew.example to install it",
    ]


@pytest.mark.usefixtures("table")
@pytest.mark.parametrize(
    ("platform", "second"),
    [
        ("linux", "uv not found, see https://uv.example/install to install it"),
        ("darwin", "brew not found, see https://brew.example to install it"),
    ],
)
def test_missing_installer_gets_its_own_hint(
    path: Path, platform: str, second: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two CLIs that need the same missing installer give it one line, after theirs."""
    monkeypatch.setattr("sys.platform", platform)
    hints = install.hints("csvlook a.csv | csvcut -c x")
    assert hints[0].startswith("csvlook, csvcut not found, install with: uv tool install csvkit")
    if platform == "darwin":  # brew installs uv, so uv's line points to brew's
        assert hints[1:] == ["uv not found, install with: brew install uv (needs brew)", second]
    else:
        assert hints[1:] == [second]


def test_shipped_table_tokenizes() -> None:
    """Every shipped install command is one line dex can read the programs of."""
    table = json.loads(Path(install._FILE).read_text(encoding="utf-8"))
    assert table
    for name, entry in table.items():
        assert set(entry) <= {"macos", "linux", "any", "docs"}, name
        for os_name, options in entry.items():
            for option in options:
                if os_name == "docs":
                    assert option.startswith("https://"), option
                    continue
                assert install.commands(option), option
                # Package managers only: no downloaded script or installer is run.
                assert "curl " not in option, option
                assert " -y" not in option, option


@pytest.mark.parametrize(
    ("command", "ok"),
    [
        ("uv tool install csvkit", True),
        ("pipx install dagster && pipx inject dagster dagster-webserver", True),
        ("uv tool install dbt-core --with dbt-<adapter>", False),
        ("sudo apt-get install jq", False),
        ("sudo snap install yq", False),
        ("brew install duckdb", True),
        ("brew install --cask docker-desktop", False),
        ("brew install databricks/tap/databricks", False),
        ("brew tap hashicorp/tap && brew install hashicorp/tap/terraform", False),
        ("curl x | sh", False),
    ],
)
def test_user_level(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, ok: bool
) -> None:
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    assert install._user_level(command) is ok


def test_user_level_needs_the_tool_folder_on_path_and_a_writable_brew(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "elsewhere"))
    assert not install._user_level("uv tool install csvkit")
    monkeypatch.delenv("HOMEBREW_PREFIX", raising=False)
    put(path, "brew")  # brew in <tmp>/bin, so the prefix is <tmp>
    (tmp_path / "Cellar").mkdir()
    assert install._user_level("brew install duckdb")
    if os.geteuid() == 0:  # root can write anywhere
        return
    (tmp_path / "Cellar").chmod(0o500)
    try:
        assert not install._user_level("brew install duckdb")
    finally:
        (tmp_path / "Cellar").chmod(0o700)


@pytest.mark.usefixtures("table")
def test_offer_is_the_first_user_level_hint(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    put(path, "sudo", "apt-get", "uv")
    assert install.offer("jq .a f | csvlook | csvcut -c 1") == (
        ["csvlook", "csvcut"],
        "uv tool install csvkit",
    )
    assert install.offer("jq .a f") is None  # needs sudo
    assert install.offer("aws s3 ls") is None  # its installer is missing


@pytest.mark.usefixtures("table")
def test_offer_never_the_installer_a_hint_needs(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    put(path, "brew")
    assert any("brew install uv" in hint for hint in install.hints("csvlook f.csv"))
    assert install.offer("csvlook f.csv") is None  # uv would leave csvlook missing


def test_run_runs_each_step_and_stops_at_a_failure(
    path: Path, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log"
    for name, code in (("uv", 0), ("pipx", 3)):
        (path / name).write_text(f'#!/bin/sh\necho "{name} $*" >> {log}\necho out\nexit {code}\n')
        (path / name).chmod(0o755)
    assert install.run("uv tool install x && uv tool list") == 0
    assert install.run("pipx install y && uv tool install z") == 3
    assert log.read_text().splitlines() == [
        "uv tool install x",
        "uv tool list",
        "pipx install y",
    ]
    out, err = capfd.readouterr()
    assert out == "" and err == "out\n" * 3  # the installer's output stays off stdout
