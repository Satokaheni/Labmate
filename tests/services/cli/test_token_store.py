# tests/services/cli/test_token_store.py
from __future__ import annotations
import json
import time
from pathlib import Path
import pytest
from services.cli.token_store import load_token, save_token, clear_token, _decode_exp


def _make_token(exp_offset: int) -> str:
    """Build a minimal JWT with a real exp claim (no signature needed for local decode)."""
    import base64
    payload = json.dumps({"sub": "u-1", "exp": int(time.time()) + exp_offset}).encode()
    b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    return f"header.{b64}.sig"


def test_save_and_load_token(tmp_path: Path):
    token_path = tmp_path / "token.json"
    tok = _make_token(3600)
    save_token(tok, path=token_path)
    assert load_token(path=token_path) == tok


def test_load_returns_none_when_file_missing(tmp_path: Path):
    assert load_token(path=tmp_path / "missing.json") is None


def test_load_returns_none_when_expired(tmp_path: Path):
    token_path = tmp_path / "token.json"
    tok = _make_token(-10)   # expired 10 s ago
    save_token(tok, path=token_path)
    assert load_token(path=token_path) is None


def test_clear_token_removes_file(tmp_path: Path):
    token_path = tmp_path / "token.json"
    save_token(_make_token(3600), path=token_path)
    clear_token(path=token_path)
    assert not token_path.exists()


def test_clear_token_is_idempotent(tmp_path: Path):
    clear_token(path=tmp_path / "missing.json")  # must not raise


def test_decode_exp_returns_none_on_garbage():
    assert _decode_exp("not.a.jwt") is None


def test_saved_token_has_correct_permissions(tmp_path: Path):
    token_path = tmp_path / "sub" / "token.json"
    save_token(_make_token(3600), path=token_path)
    assert (token_path.stat().st_mode & 0o777) == 0o600


def test_decode_exp_returns_none_for_two_parts():
    assert _decode_exp("a.b") is None


def test_decode_exp_returns_none_for_non_base64_middle():
    assert _decode_exp("header.!!!.sig") is None


def test_decode_exp_returns_none_for_non_json_payload():
    import base64
    b64 = base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode()
    assert _decode_exp(f"header.{b64}.sig") is None
