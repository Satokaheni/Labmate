import pytest

import claim_verifier
from models import ClaimVerificationResult


@pytest.mark.mocked
def test_verify_claims_counts(patch_gemma):
    # 1st call = extraction; next 2 = classification of each triplet.
    patch_gemma["responses"] = [
        '[{"subject":"BERT","predicate":"introduced by","object":"Devlin 2018"},'
        ' {"subject":"BERT","predicate":"is a","object":"RNN"}]',
        '{"verdict":"entailed","evidence":"Devlin et al. 2018 introduced BERT"}',
        '{"verdict":"contradicted","evidence":"BERT is a Transformer, not an RNN"}',
    ]
    result = claim_verifier.verify_claims(
        "BERT was introduced by Devlin 2018 and is an RNN.",
        ["Devlin et al. 2018 introduced BERT, a Transformer encoder."],
    )
    assert isinstance(result, ClaimVerificationResult)
    assert len(result.triplets) == 2
    assert result.entailed_count == 1
    assert result.contradicted_count == 1
    assert result.unverifiable_count == 0


@pytest.mark.mocked
def test_contradicted_verdict_surfaces_evidence(patch_gemma):
    patch_gemma["responses"] = [
        '[{"subject":"X","predicate":"equals","object":"5"}]',
        '{"verdict":"contradicted","evidence":"X equals 7"}',
    ]
    result = claim_verifier.verify_claims("X equals 5.", ["The paper states X equals 7."])
    t = result.triplets[0]
    assert t.verdict == "contradicted"
    assert t.evidence == "X equals 7"


@pytest.mark.mocked
def test_no_references_yields_unverifiable(patch_gemma):
    patch_gemma["responses"] = ['[{"subject":"A","predicate":"is","object":"B"}]']
    result = claim_verifier.verify_claims("A is B.", [])
    assert result.unverifiable_count == 1
    assert result.entailed_count == 0
