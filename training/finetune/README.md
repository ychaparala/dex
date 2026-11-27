# finetune

LoRA fine-tuning, adapter merge and GGUF export. Not written yet. The plan:

1. LoRA on Qwen2.5-Coder-0.5B-Instruct with `../data/train.jsonl` (chat template, loss on the answer only, 512-token
   context).
2. Merge the adapter, convert with llama.cpp's `convert_hf_to_gguf.py`, quantize with `llama-quantize` (Q4_K_M or
   Q8_0).
3. Score the GGUF on `../data/eval.jsonl` with `python ../nl2sh.py predict` against a llama.cpp server, then
   `python ../nl2sh.py score`.
4. Ship the GGUF as `dex.gguf` in the `dex-model` package (see `docs/designs/design_draft.md`, section 11).
