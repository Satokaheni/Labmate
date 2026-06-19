from __future__ import annotations
import json
import pytest
from pathlib import Path
from services.cli.identity import Identity, load_or_create_identity


def test_identity_serializes():
    ident = Identity(user_id="u-1", display_name="Alice")
    d = ident.to_dict()
    assert d["user_id"] == "u-1"
    assert d["display_name"] == "Alice"


def test_identity_round_trips(tmp_path):
    ident = Identity(user_id="u-2", display_name="Bob")
    path = tmp_path / "identity.json"
    ident.save(path)
    loaded = Identity.load(path)
    assert loaded.user_id == "u-2"
    assert loaded.display_name == "Bob"


def test_load_or_create_new(tmp_path, monkeypatch):
    monkeypatch.setattr("services.cli.identity.IDENTITY_PATH", tmp_path / "identity.json")
    monkeypatch.setattr("builtins.input", lambda _: "Charlie")
    ident = load_or_create_identity()
    assert ident.display_name == "Charlie"
    assert (tmp_path / "identity.json").exists()


def test_load_or_create_existing(tmp_path, monkeypatch):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"user_id": "u-3", "display_name": "Dave"}))
    monkeypatch.setattr("services.cli.identity.IDENTITY_PATH", path)
    ident = load_or_create_identity()
    assert ident.user_id == "u-3"
    assert ident.display_name == "Dave"
