"""Offline-only deterministic test and pinned real model providers."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path
from typing import Any

from deptslm_worker.model_store import (
    validate_generation_model_store,
    validate_model_store,
)

from app.rag_domain import (
    GENERATION_MODEL_CONTEXT_TOKENS,
    GENERATION_NEW_TOKEN_RESERVE,
    MAX_GENERATION_INPUT_TOKENS,
    MAX_QUERY_EMBEDDING_INPUT_TOKENS,
    build_generation_messages,
    validate_generation_response,
)
from app.vector_index_domain import EMBEDDING_DIMENSION, QUERY_EMBEDDING_INSTRUCTION


class RuntimeModelError(RuntimeError):
    def __init__(self, code: str = "model_operation_failed") -> None:
        self.code = code
        super().__init__(code)


class RuntimeModels:
    def __init__(self, data_dir: Path, provider: str) -> None:
        self._provider = provider
        if provider == "fake":
            self._embedding = None
            self._tokenizer = None
            self._generation = None
            return
        embedding = validate_model_store(data_dir)
        generation = validate_generation_model_store(data_dir)
        from sentence_transformers import SentenceTransformer
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._embedding = SentenceTransformer(
            str(embedding.path),
            trust_remote_code=False,
            local_files_only=True,
            model_kwargs={"local_files_only": True, "trust_remote_code": False},
            tokenizer_kwargs={"local_files_only": True, "padding_side": "left"},
        )
        self._tokenizer = AutoTokenizer.from_pretrained(
            str(generation.path), trust_remote_code=False, local_files_only=True
        )
        self._generation = AutoModelForCausalLM.from_pretrained(
            str(generation.path),
            trust_remote_code=False,
            local_files_only=True,
            use_safetensors=True,
        )
        embedding_limit = getattr(self._embedding.tokenizer, "model_max_length", None)
        generation_limit = getattr(self._tokenizer, "model_max_length", None)
        configured_context = getattr(self._generation.config, "max_position_embeddings", None)
        validate_context_contract(
            embedding_limit=embedding_limit,
            embedding_sequence_limit=self._embedding.max_seq_length,
            generation_tokenizer_limit=generation_limit,
            generation_model_context=configured_context,
        )

    def embed_question(self, question: str) -> list[float]:
        query = f"{QUERY_EMBEDDING_INSTRUCTION}\nQuestion: {question}"
        if self._provider == "fake":
            digest = hashlib.sha256(query.encode()).digest()
            vector = [0.0] * EMBEDDING_DIMENSION
            vector[int.from_bytes(digest[:2], "big") % EMBEDDING_DIMENSION] = (
                -1.0 if digest[2] & 1 else 1.0
            )
            return vector
        tokenize_query_input(self._embedding.tokenizer, query)
        values = self._embedding.encode(
            [query],
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return values[0].tolist()

    def generate(
        self,
        question: str,
        evidence: list[dict[str, str]],
        *,
        seed: int | None = None,
    ) -> dict:
        labels = tuple(item["source_id"] for item in evidence)
        messages = build_generation_messages(question, evidence)
        if self._provider == "fake":
            value = (
                {"status": "insufficient_information", "answer": "", "citations": []}
                if not labels
                else {
                    "status": "answered",
                    "answer": f"The authorized evidence supports this answer [{labels[0]}].",
                    "citations": [labels[0]],
                }
            )
            validate_generation_response(value, labels)
            return value
        if seed is not None:
            random.seed(seed)
            import numpy
            import torch

            numpy.random.seed(seed % (1 << 32))
            torch.manual_seed(seed)
            if torch.cuda.is_available():
                torch.cuda.manual_seed_all(seed)
        inputs = tokenize_generation_input(self._tokenizer, messages)
        inputs = inputs.to(self._generation.device)
        outputs = self._generation.generate(
            **inputs,
            max_new_tokens=GENERATION_NEW_TOKEN_RESERVE,
            do_sample=True,
            temperature=0.7,
            top_p=0.8,
            top_k=20,
            min_p=0.0,
            pad_token_id=self._tokenizer.eos_token_id,
        )
        generated = outputs[0][inputs["input_ids"].shape[-1] :]
        raw = self._tokenizer.decode(generated, skip_special_tokens=True).strip()
        if "<think" in raw.casefold() or "</think" in raw.casefold():
            raise RuntimeModelError()
        try:
            value = json.loads(raw)
            result = validate_generation_response(value, labels)
        except Exception as error:
            raise RuntimeModelError() from error
        return {
            "status": result.status,
            "answer": result.answer,
            "citations": list(result.citations),
        }


def _token_count(value: Any) -> int:
    input_ids = (
        value.get("input_ids") if isinstance(value, dict) else getattr(value, "input_ids", None)
    )
    if input_ids is None:
        raise RuntimeModelError("model_context_mismatch")
    shape = getattr(input_ids, "shape", None)
    if shape is not None and len(shape) >= 1:
        return int(shape[-1])
    if isinstance(input_ids, list):
        if input_ids and isinstance(input_ids[0], list):
            if len(input_ids) != 1:
                raise RuntimeModelError("model_context_mismatch")
            return len(input_ids[0])
        return len(input_ids)
    raise RuntimeModelError("model_context_mismatch")


def _usable_context_limit(value: object, required: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and required <= value < 1_000_000


def validate_context_contract(
    *,
    embedding_limit: object,
    embedding_sequence_limit: object,
    generation_tokenizer_limit: object,
    generation_model_context: object,
) -> None:
    if (
        not _usable_context_limit(embedding_limit, MAX_QUERY_EMBEDDING_INPUT_TOKENS)
        or not _usable_context_limit(embedding_sequence_limit, MAX_QUERY_EMBEDDING_INPUT_TOKENS)
        or not _usable_context_limit(
            generation_tokenizer_limit,
            MAX_GENERATION_INPUT_TOKENS + GENERATION_NEW_TOKEN_RESERVE,
        )
        or generation_model_context != GENERATION_MODEL_CONTEXT_TOKENS
    ):
        raise RuntimeModelError("model_context_mismatch")


def enforce_query_token_budget(input_tokens: int) -> None:
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or not 1 <= input_tokens <= MAX_QUERY_EMBEDDING_INPUT_TOKENS
    ):
        raise RuntimeModelError("model_input_too_large")


def enforce_generation_token_budget(input_tokens: int, model_context: int) -> None:
    if model_context != GENERATION_MODEL_CONTEXT_TOKENS:
        raise RuntimeModelError("model_context_mismatch")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 1
        or input_tokens > MAX_GENERATION_INPUT_TOKENS
        or input_tokens + GENERATION_NEW_TOKEN_RESERVE > model_context
    ):
        raise RuntimeModelError("model_input_too_large")


def tokenize_query_input(tokenizer: Any, query: str) -> Any:
    tokenized = tokenizer(
        query,
        add_special_tokens=True,
        truncation=False,
        return_attention_mask=False,
    )
    enforce_query_token_budget(_token_count(tokenized))
    return tokenized


def tokenize_generation_input(tokenizer: Any, messages: list[dict[str, str]]) -> Any:
    inputs = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
        enable_thinking=False,
        truncation=False,
    )
    enforce_generation_token_budget(_token_count(inputs), GENERATION_MODEL_CONTEXT_TOKENS)
    return inputs
