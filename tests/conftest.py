"""Shared, offline prepared histories for review-controller implementation."""

import pytest

from tests.review_cases import load_corpus

REVIEW_CORPUS = load_corpus()


@pytest.fixture
def review_corpus():
    return REVIEW_CORPUS


@pytest.fixture(params=REVIEW_CORPUS.cases, ids=lambda case: case.id)
def review_case(request):
    return request.param
