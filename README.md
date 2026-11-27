# dex

Type what you want in plain English and get back one ready-to-edit shell command for the data engineering CLIs you
use every day. A tiny fine-tuned model ships with dex and runs on your machine: no network, no API key, no server
to set up.

```console
$ dex show the first 20 rows of sales.parquet
FILE=sales.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" | less -S
```

The values you're likely to change come first, so you edit them and press Enter. Values dex can't know stay as
`<placeholders>`, and an unedited line fails instead of running with a blank. dex never runs the command itself.

If the command needs a CLI you don't have, dex says how to install it on your OS and leaves the command as is:

```console
$ dex show the first 20 rows of sales.parquet
FILE=sales.parquet LIMIT=20 && duckdb -c "select * from '$FILE' limit $LIMIT" | less -S
dex: duckdb not found, install with: brew install duckdb
```

With the zsh integration, when the install needs no sudo (uv, pipx or Homebrew), dex first asks whether to install
it. On y it runs that install, then the command lands on your prompt. Enter, n or Ctrl-C skips it:

```console
$ dex show the first 20 rows of sales.parquet
dex: duckdb isn't installed. Install it with: brew install duckdb (Homebrew may update other packages)? [y/N]
```

## In scope

Airflow and Astronomer, Databricks, Snowflake (`snow` and SnowSQL), Confluent, Kafka and Redpanda, AWS (S3, EMR,
Glue, Athena and more), Spark, DuckDB, dbt, curl, and about 25 more everyday data tools: kubectl, helm, gcloud, jq,
yq, psql, Trino and others. The full list, with counts, is in [training/README.md](training/README.md).

## Install

dex isn't on PyPI yet. Until it is, install it from this repo:

```sh
uv tool install git+https://github.com/ychaparala/dex   # or: pipx install git+https://github.com/ychaparala/dex
```

The fine-tuned model isn't published yet. Until it is, point dex at any GGUF file:

```sh
export DEX_MODEL=~/models/dex.gguf
```

Then add the zsh integration to `~/.zshrc`:

```sh
eval "$(dex --init zsh)"
```

With oh-my-zsh, link `shell/zsh` from this repo as a plugin instead:

```sh
ln -s "$PWD/shell/zsh" "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/dex"   # then add dex to plugins=(...)
```

## Use

- `dex <request>` puts the command on your next prompt, ready to edit.
- Or type the request on the command line and press **Ctrl-X Ctrl-D**: it is replaced by the command, with the
  cursor on the first `<placeholder>`. Set `DEX_KEY` before loading the integration to change the key, or to `''` for
  none.
- Requests with `> | ; ' " #` need quotes on the command line, or the key binding. `*` and `?` are fine.
- If a command looks destructive (deletes, drops, terminates, overwrites, resets offsets), dex prints it anyway,
  warns on stderr and exits with code 3. Read it before you press Enter.

```
dex [OPTIONS] REQUEST...

  --no-context    send no facts about the local environment (profiles, files)
  --show-prompt   print the prompt instead of generating
  -q, --quiet     no warnings on stderr
  --stop          stop the background model process
  --init zsh      print the shell integration
  -h, --help, --version
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEX_MODEL` | the model from the `dex-model` package | a GGUF file to use instead |
| `DEX_IDLE` | `15` | minutes the model stays loaded after the last request; `0` loads it on every call |
| `DEX_THREADS` | performance or physical cores | inference threads |
| `DEX_KEY` | `^X^D` | the zsh key binding |
| `DEX_ASK` | set to `1` by the zsh `dex` function | lets dex ask to install a missing CLI |

## How it works

```
dex (standard library only, about 30 ms)              warm process (keeps the model loaded)
  gather local context: AWS profile, Snowflake
  connection, kubectl context, files here ...
  build the prompt ─────────── Unix socket ─────────►  generate with llama.cpp (xllamacpp)
  check the answer, run the denylist ◄────────────────
  print one line
```

dex reads a few facts about your environment (only for the tools your request mentions, from env vars and config
files, never by running programs) so the model fills in real values. The model runs in a background process that
starts on the first request and exits after 15 idle minutes, so later requests skip loading it. Details are in
[docs/architecture.md](docs/architecture.md).

## Develop

```sh
uv sync                   # Python 3.11+, the dev tools and an editable dex
uv run pre-commit install
uv run pytest             # tests need no model
```

[CONTRIBUTING.md](CONTRIBUTING.md) has the checks and conventions, [docs/adding-a-tool.md](docs/adding-a-tool.md)
how to teach dex a new CLI, and [docs/training.md](docs/training.md) how the model is built. The design and its
reviews are in [docs/designs/](docs/designs/).

## License

MIT, see [LICENSE](LICENSE). The training templates follow each CLI's own docs or source; the licenses of those
sources are listed in [training/README.md](training/README.md).
