"""The ruled production embedding model, against the real library.

Skipped unless ``sentence-transformers`` is installed and the weights are present —
CI installs neither, because the API image must not carry torch. The run that
produced the WP-06 measurements executed this for real; the assertions encode
what a working BGE-M3 must satisfy regardless of machine.
"""

import os

import pytest

pytest.importorskip("sentence_transformers", reason="torch/BGE-M3 not installed here")

from app.retrieval.embedding import BgeM3Provider, cosine_similarity


@pytest.fixture(scope="module")
def provider() -> BgeM3Provider:
    if os.environ.get("EVA_RUN_MODEL_TESTS") != "1":
        pytest.skip("set EVA_RUN_MODEL_TESTS=1 to load the 2.3 GB BGE-M3 weights")
    return BgeM3Provider()


def test_dimension_matches_the_ruled_configuration(provider) -> None:
    assert provider.dimension == 1024
    assert len(provider.embed(["hello"])[0]) == 1024


def test_vectors_are_l2_normalised(provider) -> None:
    """Cosine distance assumes unit vectors; storage relies on it."""
    vector = provider.embed(["refunds are processed within five days"])[0]
    norm = sum(x * x for x in vector) ** 0.5
    assert norm == pytest.approx(1.0, abs=1e-3)


def test_paraphrases_score_higher_than_unrelated_text(provider) -> None:
    anchor, paraphrase, unrelated = provider.embed(
        [
            "How long do refunds take to process?",
            "Refunds are processed within five business days.",
            "Our office is closed on public holidays.",
        ]
    )
    assert cosine_similarity(anchor, paraphrase) > cosine_similarity(anchor, unrelated)


def test_embedding_is_deterministic(provider) -> None:
    first = provider.embed(["consistent input text"])[0]
    second = provider.embed(["consistent input text"])[0]
    assert cosine_similarity(first, second) == pytest.approx(1.0, abs=1e-5)
