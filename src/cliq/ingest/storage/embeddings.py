#!/usr/bin/env python3
"""
embeddings.py — Local, swappable text-embedding providers for CLIQ ingestion.

CLIQ runs fully on-prem: embeddings are computed by a LOCAL model, never a
cloud API. The preferred default provider is `SentenceTransformerEmbedder`
(`all-MiniLM-L6-v2`) for high-quality semantic retrieval. If the dependency or
model cache is unavailable, construction fails loudly so operators can fix the
deployment image (no silent degradation).

Swap providers via `get_embedder(name=...)` or by registering your own with
`register_embedder()`.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from typing import Callable, Protocol

log = logging.getLogger("cliq.ingest.embeddings")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def configure_offline_hf_env() -> None:
    """Force fully-offline HF/Transformers behavior for air-gapped deployments."""
    # Force values (not setdefault) so callers cannot accidentally override with online mode.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"


# Apply offline mode as early as possible so any subsequent HF imports remain offline.
configure_offline_hf_env()


class Embedder(Protocol):
    """Minimal embedding interface every provider must satisfy."""

    name: str
    dim: int

    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class LocalHashingEmbedder:
    """Deterministic feature-hashing embedder — fully local, zero dependencies.

    Maps unigrams and bigrams of the input into a fixed-dimensional vector via a
    stable (hashlib) hash, then L2-normalises. It is NOT a neural model, but it
    is reproducible across runs/machines and groups commands that share
    vocabulary — enough to exercise and validate the whole storage/retrieval
    pipeline locally. Replace with SentenceTransformerEmbedder for production
    semantic quality without changing any calling code.
    """

    name = "local-hashing"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _hash(self, token: str) -> tuple[int, float]:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % self.dim
        sign = 1.0 if (digest[4] & 1) else -1.0
        return bucket, sign

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = _tokenize(text)
        if not tokens:
            return vec
        # Unigrams + bigrams give a little word-order signal.
        features = list(tokens)
        features += [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for feat in features:
            bucket, sign = self._hash(feat)
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class SentenceTransformerEmbedder:
    """Local neural embedder backed by `sentence-transformers`.

    Only constructed on demand. Requires the library AND a locally available
    model (downloaded ahead of time / cached) so that NO network call happens
    at ingestion. Raises a clear error if unavailable.
    """

    name = "sentence-transformers"

    def __init__(self, model_name: str = "all-MiniLM-L6-v2", local_files_only: bool = False):
        # Re-assert offline env for long-lived processes that may mutate env vars.
        configure_offline_hf_env()
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "SentenceTransformerEmbedder requires 'sentence-transformers'. "
                "Install it and pre-cache the model locally."
            ) from exc

        # Always use local cache only for deterministic on-prem/offline behavior.
        # Keep parameter for API compatibility, but never permit online fetches.
        _ = local_files_only
        try:
            self._model = SentenceTransformer(
                model_name,
                local_files_only=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "SentenceTransformer model is not cached locally. "
                "Run the pre-cache step on a connected build machine, bake cache into the image, and retry."
            ) from exc

        self.model_name = model_name
        if hasattr(self._model, "get_embedding_dimension"):
            self.dim = int(self._model.get_embedding_dimension())
        else:
            self.dim = int(self._model.get_sentence_embedding_dimension())

    def embed(self, text: str) -> list[float]:
        return [float(x) for x in self._model.encode(text, normalize_embeddings=True)]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in row] for row in vectors]


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------
_FACTORIES: dict[str, Callable[..., Embedder]] = {
    LocalHashingEmbedder.name: LocalHashingEmbedder,
    SentenceTransformerEmbedder.name: SentenceTransformerEmbedder,
}


def register_embedder(name: str, factory: Callable[..., Embedder]) -> None:
    """Register a custom embedder factory so it can be selected by name."""
    _FACTORIES[name] = factory


def available_embedders() -> list[str]:
    return sorted(_FACTORIES.keys())


def get_embedder(
    name: str = SentenceTransformerEmbedder.name,
    *,
    allow_fallback_to_hashing: bool = False,
    **kwargs,
) -> Embedder:
    """Return an embedder by name.

    Defaults to sentence-transformers for semantic quality.
    By default this function fails loudly if construction fails.
    Optional fallback to local hashing exists only for explicit test/dev use.
    """
    factory = _FACTORIES.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown embedder '{name}'. Available embedders: {', '.join(available_embedders())}"
        )
    try:
        return factory(**kwargs)
    except Exception as exc:
        if allow_fallback_to_hashing:
            log.warning("Embedder '%s' unavailable (%s); using local hashing by explicit request.", name, exc)
            return LocalHashingEmbedder()
        raise RuntimeError(
            f"Failed to initialize embedder '{name}': {exc}"
        ) from exc
