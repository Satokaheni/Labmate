import pytest
from unittest.mock import patch, MagicMock


def _make_mock_tokenizer(chars_per_token: int = 4):
    tok = MagicMock()
    tok.encode.side_effect = lambda text, **kw: list(range(max(1, len(text) // chars_per_token)))
    return tok


def test_token_count_empty_string():
    mock_tok = _make_mock_tokenizer()
    with patch("services.memory.tokenizer._TOKENIZER", mock_tok):
        from services.memory.tokenizer import token_count
        assert token_count("") == 0


def test_token_count_nonempty():
    mock_tok = _make_mock_tokenizer(chars_per_token=4)
    with patch("services.memory.tokenizer._TOKENIZER", mock_tok):
        from services.memory.tokenizer import token_count
        result = token_count("hello world!")  # 12 chars → 3 tokens
        assert result == 3


def test_token_count_delegates_to_tokenizer():
    mock_tok = _make_mock_tokenizer()
    with patch("services.memory.tokenizer._TOKENIZER", mock_tok):
        from services.memory.tokenizer import token_count
        token_count("some text")
        mock_tok.encode.assert_called_once_with(
            "some text", add_special_tokens=False
        )
