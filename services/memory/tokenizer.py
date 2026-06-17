from __future__ import annotations
from functools import lru_cache

# Lazy singleton — the actual tokenizer object is loaded on first call, not at
# import time. This avoids failures when the HuggingFace model is gated and
# no token is configured. Tests can patch _TOKENIZER before any call is made.
_TOKENIZER = None


@lru_cache(maxsize=1)
def _load_tokenizer():
    # Gemma uses SentencePiece. NEVER use tiktoken — it miscounts Gemma tokens
    # by 30%+ on code and causes context window overflows.
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained("google/gemma-4-31B-it", use_fast=True)


def token_count(text: str) -> int:
    """Count tokens with the Gemma AutoTokenizer (SentencePiece)."""
    if not text:
        return 0
    tok = _TOKENIZER if _TOKENIZER is not None else _load_tokenizer()
    return len(tok.encode(text, add_special_tokens=False))
