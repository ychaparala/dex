# Training

The model learns one job: turn a request (plus optional context lines) into one values-first command line.
Everything for it lives in [training/](../training/); its [README](../training/README.md) is the full reference.

## The data

`training/tools/*.yaml` holds 1,123 templates for 39 CLIs, written from each CLI's docs or source. `nl2sh.py build`
expands them into 6,199 training and 245 eval pairs in `training/data/`, checking every command with `bash -n` and,
where the CLI can run offline, with the CLI's own parser on each pinned version. `data/` is committed, so
fine-tuning needs neither the validators nor the per-version CLI installs.

Eval holds out whole request phrasings, so it measures how well the model generalizes, not what it memorized.

## The contract with dex

- System prompt: `Turn the request into one bash command. Put editable values first as VAR=value, then && and the command.`
- User turn: context lines, a blank line, then the request between `<request>` and `</request>` lines.
- Answer: one line, values first, joined with `&&`.

`src/dex/prompt.py` and `src/dex/context.py` produce exactly this. `tests/test_prompt.py` rebuilds every eval
user turn with dex's own code and fails on any byte of difference, and checks that each context line dex can write
also appears in the training data.

## Fine-tuning (to do)

LoRA on Qwen2.5-Coder-0.5B-Instruct, loss on the answer only, at a 512-token context; merge, convert with llama.cpp's
`convert_hf_to_gguf.py` and quantize to Q4_K_M or Q8_0. Score with `nl2sh.py predict` and `nl2sh.py score` on
`data/eval.jsonl`. Install the training dependencies with `uv sync --extra train`.

Before the size is final, the 0.5B model gets an accuracy gate (the pass rate on eval) and a latency measurement on
an Apple Silicon Mac, next to a smaller and a larger candidate.
