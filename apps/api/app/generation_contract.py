"""Shared production generation token, context, parameter, and seed contracts."""

from __future__ import annotations

import random
from typing import Any

from app.rag_domain import (
    GENERATION_MODEL_CONTEXT_TOKENS,
    GENERATION_NEW_TOKEN_RESERVE,
    MAX_GENERATION_INPUT_TOKENS,
)

GENERATION_DO_SAMPLE = True
GENERATION_TEMPERATURE = 0.7
GENERATION_TOP_P = 0.8
GENERATION_TOP_K = 20
GENERATION_MIN_P = 0.0
GENERATION_ENABLE_THINKING = False


class GenerationContractError(ValueError):
    pass


def token_count(value: Any) -> int:
    input_ids = (
        value.get("input_ids") if isinstance(value, dict) else getattr(value, "input_ids", None)
    )
    if input_ids is None:
        raise GenerationContractError("model_context_mismatch")
    shape = getattr(input_ids, "shape", None)
    if shape is not None and len(shape) >= 1:
        return int(shape[-1])
    if isinstance(input_ids, list):
        if input_ids and isinstance(input_ids[0], list):
            if len(input_ids) != 1:
                raise GenerationContractError("model_context_mismatch")
            return len(input_ids[0])
        return len(input_ids)
    raise GenerationContractError("model_context_mismatch")


def enforce_generation_token_budget(input_tokens: int, model_context: int) -> None:
    if model_context != GENERATION_MODEL_CONTEXT_TOKENS:
        raise GenerationContractError("model_context_mismatch")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or input_tokens < 1
        or input_tokens > MAX_GENERATION_INPUT_TOKENS
        or input_tokens + GENERATION_NEW_TOKEN_RESERVE > model_context
    ):
        raise GenerationContractError("model_input_too_large")


def validate_generation_context_contract(
    tokenizer_limit: object,
    model_context: object,
) -> None:
    if (
        isinstance(tokenizer_limit, bool)
        or not isinstance(tokenizer_limit, int)
        or tokenizer_limit >= 1_000_000
        or tokenizer_limit < MAX_GENERATION_INPUT_TOKENS + GENERATION_NEW_TOKEN_RESERVE
        or model_context != GENERATION_MODEL_CONTEXT_TOKENS
    ):
        raise GenerationContractError("model_context_mismatch")


def tokenize_generation_input(tokenizer: Any, messages: list[dict[str, str]]) -> Any:
    inputs = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
        enable_thinking=GENERATION_ENABLE_THINKING,
        truncation=False,
    )
    enforce_generation_token_budget(token_count(inputs), GENERATION_MODEL_CONTEXT_TOKENS)
    return inputs


def initialize_generation_seed(seed: int) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= (1 << 63) - 1:
        raise GenerationContractError("invalid_request")
    random.seed(seed)
    import numpy
    import torch

    numpy.random.seed(seed % (1 << 32))
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
