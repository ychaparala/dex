"""Build the prompt sent to the model.

The format must match the one the training data is generated with (``training/nl2sh.py``):
a short fixed system prompt, then optional context lines, a blank line, and the request
wrapped in ``<request>`` tags. A tiny model learns the format from the data, so drift
between this module and the generator costs accuracy. ``tests/test_prompt.py`` checks
the user turn against records in ``training/data/eval.jsonl``.

dex renders the chat template itself (Qwen's ChatML) and sends the result as a raw
completion, so the bytes the model sees don't depend on the inference library.
"""

from __future__ import annotations

from collections.abc import Sequence

SYSTEM = (
    "Turn the request into one bash command. "
    "Put editable values first as VAR=value, then && and the command."
)
MAX_TOKENS = 200
STOP = ("\n\n", "</request>")


def user(request: str, context_lines: Sequence[str] = ()) -> str:
    """Render the user turn.

    Args:
        request: The request as the user typed it.
        context_lines: Plain-prose facts about the local environment.

    Returns:
        The context lines (if any), a blank line, then the request in ``<request>`` tags.
    """
    lines = [line.strip() for line in context_lines if line.strip()]
    head = "\n".join(lines) + "\n\n" if lines else ""
    return f"{head}<request>\n{request.strip()}\n</request>"


def render(user_turn: str) -> str:
    """Wrap the system prompt and a user turn in ChatML, ready for the assistant's answer.

    Args:
        user_turn: The output of :func:`user`.

    Returns:
        The complete prompt text.
    """
    return (
        f"<|im_start|>system\n{SYSTEM}<|im_end|>\n"
        f"<|im_start|>user\n{user_turn}<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
