from eval.seq_ab.run_seq_ab import CASES, FIXTURES


def test_has_added_harder_compound_cases():
    ids = {c["id"] for c in CASES}
    assert "c6_multiedit_fix" in ids
    assert sum(1 for c in CASES if c["kind"] == "compound") >= 4


def test_every_case_fixture_path_exists_in_fixtures():
    # Any /workspace/ab_*.py a case names must be defined in FIXTURES.
    import re

    for c in CASES:
        for path in re.findall(r"/workspace/ab_\w+\.py", c["task"]):
            assert path in FIXTURES, f"{c['id']} references undefined fixture {path}"
