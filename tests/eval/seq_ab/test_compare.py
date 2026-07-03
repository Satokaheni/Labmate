import json

from eval.seq_ab.compare import compare_case, compare_runs


def _rec(cid, kind, passes, n):
    return {
        "id": cid,
        "kind": kind,
        "pass_count": passes,
        "trials_run": n,
        "pass_rate": round(passes / n, 2) if n else 0.0,
    }


def test_clear_win_has_disjoint_cis():
    base = _rec("c2", "compound", 0, 5)
    var = _rec("c2", "compound", 5, 5)
    out = compare_case(base, var)
    assert out["delta"] > 0
    assert out["cis_disjoint"] is True
    assert out["win"] is True
    assert out["noop_floor"] == 0.0
    assert out["above_noop"] is True


def test_overlapping_cis_is_not_a_win():
    base = _rec("c1", "compound", 2, 3)
    var = _rec("c1", "compound", 3, 3)
    out = compare_case(base, var)
    assert out["win"] is False  # +1/3 with n=3 — CIs overlap


def test_too_few_trials_never_a_win():
    base = _rec("c2", "compound", 0, 2)
    var = _rec("c2", "compound", 2, 2)
    out = compare_case(base, var, min_trials=3)
    assert out["win"] is False


def test_three_trials_below_default_floor_even_for_perfect_split():
    # n=3 is below the default floor (5) AND its CIs overlap even at 3/3 vs 0/3,
    # so a perfect split is still not a win. Documents why decisive A/Bs need n>=5.
    base = _rec("c2", "compound", 0, 3)
    var = _rec("c2", "compound", 3, 3)
    out = compare_case(base, var)  # default min_trials=5
    assert out["enough_trials"] is False
    assert out["cis_disjoint"] is False
    assert out["win"] is False


def test_compare_runs_matches_by_id_and_rolls_up():
    baseline = {"cases": [_rec("c2", "compound", 0, 5), _rec("c5", "control_trivial", 5, 5)]}
    variant = {"cases": [_rec("c2", "compound", 5, 5), _rec("c5", "control_trivial", 5, 5)]}
    out = compare_runs(baseline, variant)
    assert out["any_win"] is True
    ids = {c["id"] for c in out["cases"]}
    assert ids == {"c2", "c5"}


def test_compare_runs_from_json_files(tmp_path):
    base = {
        "cases": [
            {"id": "c2", "kind": "compound", "pass_count": 0, "trials_run": 5, "pass_rate": 0.0}
        ]
    }
    var = {
        "cases": [
            {"id": "c2", "kind": "compound", "pass_count": 5, "trials_run": 5, "pass_rate": 1.0}
        ]
    }
    (tmp_path / "b.json").write_text(json.dumps(base))
    (tmp_path / "v.json").write_text(json.dumps(var))
    loaded_b = json.loads((tmp_path / "b.json").read_text())
    loaded_v = json.loads((tmp_path / "v.json").read_text())
    out = compare_runs(loaded_b, loaded_v)
    assert out["any_win"] is True
