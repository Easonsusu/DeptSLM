"""Secret-free persistent-per-job embedding subprocess."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

EMBEDDING_DIMENSION = 1024


class _FakeProvider:
    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSION
            sign = -1.0 if digest[2] & 1 else 1.0
            vector = [0.0] * EMBEDDING_DIMENSION
            vector[index] = sign
            vectors.append(vector)
        return vectors


class _RealProvider:
    def __init__(self, model_root: Path) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(
            str(model_root),
            trust_remote_code=False,
            local_files_only=True,
            model_kwargs={"local_files_only": True, "trust_remote_code": False},
            tokenizer_kwargs={"local_files_only": True, "padding_side": "left"},
        )

    def encode(self, texts: list[str]) -> list[list[float]]:
        values = self.model.encode(
            texts,
            batch_size=len(texts),
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return values.tolist()


def _provider(model_root: Path):
    provider = os.getenv("DEPTSLM_EMBEDDING_PROVIDER", "real")
    environment = os.getenv("ENVIRONMENT", "")
    if provider == "fake":
        if environment != "test":
            raise RuntimeError("fake provider denied")
        return _FakeProvider()
    if provider != "real":
        raise RuntimeError("unknown provider")
    return _RealProvider(model_root)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", required=True)
    args = parser.parse_args()
    try:
        provider = _provider(Path(args.model_root))
        for line in sys.stdin.buffer:
            if len(line) > 2 * 1024 * 1024:
                return 2
            request = json.loads(line)
            if set(request) != {"sequence", "texts"}:
                return 2
            sequence = request["sequence"]
            texts = request["texts"]
            if (
                not isinstance(sequence, int)
                or not isinstance(texts, list)
                or not texts
                or any(not isinstance(text, str) or not text for text in texts)
            ):
                return 2
            response = {"sequence": sequence, "vectors": provider.encode(texts)}
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        return 0
    except Exception:
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
