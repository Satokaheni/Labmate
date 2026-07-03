import json
from pathlib import Path

HELDOUT = Path("eval/routing_eval.heldout.jsonl")
SEED = Path("eval/routing_eval.seed.jsonl")
WORKING = Path("eval/routing_eval.jsonl")


def _load(p):
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_heldout_exists_and_is_sized():
    cases = _load(HELDOUT)
    assert 30 <= len(cases) <= 40


def test_heldout_schema_and_source_tag():
    for c in _load(HELDOUT):
        assert c["source"] == "heldout"
        assert c["id"].startswith("ho_")
        assert c["expected"]
        assert c["task"].strip()


def test_heldout_has_negatives_and_is_disjoint_from_seed_and_working():
    cases = _load(HELDOUT)
    assert sum(1 for c in cases if c["expected"] == "none") >= 6
    tasks = {c["task"].strip().lower() for c in cases}
    for other in (SEED, WORKING):
        if other.exists():
            overlap = tasks & {c["task"].strip().lower() for c in _load(other)}
            assert not overlap, f"held-out overlaps {other}: {overlap}"
