"""Load the model and generate text with llama.cpp, through xllamacpp.

This is the only module that imports the inference library, and it does so only when
a model is loaded, so finding the model path stays cheap for the dex command itself.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

TYPE_CHECKING = False  # typing.TYPE_CHECKING without importing typing on every request
if TYPE_CHECKING:
    from typing import Any, Protocol

MODEL_PACKAGE = "dex_model"
MODEL_FILE = "dex.gguf"
CONTEXT_TOKENS = 1024


class ModelError(Exception):
    """The model is missing or couldn't be loaded or run."""


if TYPE_CHECKING:

    class Engine(Protocol):
        """Anything that can complete a prompt."""

        def complete(self, prompt: str, max_tokens: int, stop: Sequence[str]) -> tuple[str, bool]:
            """Return the generated text and whether it stopped at ``max_tokens``."""
            ...


def model_path() -> str:
    """Return the GGUF to load: ``DEX_MODEL``, else the one in the ``dex-model`` package.

    Raises:
        ModelError: Neither exists.
    """
    if override := os.environ.get("DEX_MODEL", "").strip():
        path = os.path.expanduser(override)
        if not os.path.isfile(path):
            raise ModelError(f"DEX_MODEL points at {path}, which isn't a file")
        return path
    from importlib.util import find_spec

    spec = find_spec(MODEL_PACKAGE)
    for location in (spec.submodule_search_locations or []) if spec else []:
        path = os.path.join(location, MODEL_FILE)
        if os.path.isfile(path):
            return path
    raise ModelError("no model is installed: install dex-model, or set DEX_MODEL to a GGUF file")


def default_threads() -> int:
    """Return the number of threads to run inference on.

    ``DEX_THREADS`` if set, else the performance cores on macOS and the physical cores
    on Linux, because efficiency cores and hyperthreads usually slow llama.cpp down.
    """
    if (value := os.environ.get("DEX_THREADS", "").strip()).isdigit() and int(value) > 0:
        return int(value)
    logical = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    logical = logical or 1
    if sys.platform == "darwin":
        import subprocess  # nosec B404  # only in the warm process, once

        for key in ("hw.perflevel0.physicalcpu", "hw.physicalcpu"):
            out = subprocess.run(  # nosec B603 B607  # fixed command, no user input
                ["sysctl", "-n", key], capture_output=True, text=True, check=False
            ).stdout.strip()
            if out.isdigit() and int(out) > 0:
                return min(int(out), logical)
        return logical
    else:
        cores: set[tuple[str, str]] = set()
        physical = core = ""
        try:
            with open("/proc/cpuinfo", encoding="ascii", errors="replace") as f:
                for line in f:
                    key, _, val = line.partition(":")
                    key = key.strip()
                    if key == "physical id":
                        physical = val.strip()
                    elif key == "core id":
                        core = val.strip()
                        cores.add((physical, core))
        except OSError:
            pass
        return min(len(cores), logical) if cores else logical


class LlamaEngine:
    """A GGUF model loaded with xllamacpp, generating greedily."""

    def __init__(self, path: str, threads: int, http_socket: str) -> None:
        """Load the model.

        Args:
            path: The GGUF file, mapped with mmap.
            threads: Inference threads.
            http_socket: Where xllamacpp's built-in HTTP listener goes. It is a Unix
                socket inside dex's private runtime directory, so the process opens no
                TCP port. dex never talks to it.

        Raises:
            ModelError: The library or the model failed to load.
        """
        try:
            import xllamacpp  # type: ignore[import-untyped]
        except ImportError as e:
            raise ModelError(f"the inference library isn't installed ({e})") from None
        params = xllamacpp.CommonParams()
        params.model.path = path
        params.n_ctx = CONTEXT_TOKENS
        params.n_batch = CONTEXT_TOKENS
        params.n_parallel = 1
        params.cpuparams.n_threads = threads
        params.cpuparams_batch.n_threads = threads
        params.hostnames = [http_socket]
        params.verbosity = -1
        try:
            self._server: Any = xllamacpp.Server(params)
        except Exception as e:
            raise ModelError(f"couldn't load {path}: {e}") from None

    def complete(self, prompt: str, max_tokens: int, stop: Sequence[str]) -> tuple[str, bool]:
        """Generate greedily from a raw prompt.

        Returns:
            The text and whether generation stopped at ``max_tokens``.

        Raises:
            ModelError: The library reported an error.
        """
        reply = self._server.handle_completions(
            {
                "prompt": prompt,
                "n_predict": max_tokens,
                "temperature": 0.0,
                "stop": list(stop),
                "cache_prompt": True,
            }
        )
        if isinstance(reply, dict) and isinstance(reply.get("error"), dict):
            raise ModelError(str(reply["error"].get("message", "unknown error"))[:200])
        try:
            choice = reply["choices"][0]
            return str(choice["text"]), choice.get("finish_reason") == "length"
        except (KeyError, IndexError, TypeError):
            raise ModelError(f"unexpected reply from the model: {str(reply)[:200]}") from None
