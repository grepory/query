from datetime import date

import pytest
from pydantic import ValidationError

from query.investigation import (
    Brief,
    Budget,
    Checkpoint,
    CompletionChecks,
    Limits,
    Outcome,
    Phase,
    Question,
    transition,
)


def test_brief_rejects_duplicate_keys_and_optional_only():
    q = Question(key="capacity", text="How much?", acceptance_criteria="Operational watts")
    data = dict(question="What is operational?", scope="Public sources", cutoff=date(2026, 10, 20))
    with pytest.raises(ValidationError, match="unique"):
        Brief(**data, questions=(q, q))
    with pytest.raises(ValidationError, match="required"):
        Brief(**data, questions=(q.model_copy(update={"required": False}),))


@pytest.mark.parametrize(
    "values", [dict(research=-1, review=10, total=10), dict(research=1.5, review=10, total=10)]
)
def test_invalid_token_limits(values):
    with pytest.raises(ValidationError):
        Limits(**values)


def test_completion_requires_both_checks_for_current_brief():
    current = Checkpoint(
        phase=Phase.REVIEWING, budget=Budget(limits=Limits(research=10, review=10, total=20))
    )
    for integrity, sufficiency, version in [(False, True, 1), (True, False, 1), (True, True, 2)]:
        with pytest.raises(ValidationError):
            transition(
                current,
                Phase.COMPLETE,
                outcome=Outcome.QUALIFIED,
                completion=CompletionChecks(
                    brief_version=version,
                    answer_ref="draft-1",
                    answer_integrity=integrity,
                    research_sufficiency=sufficiency,
                    rationale="Public evidence exhausted",
                ),
            )


def test_invalid_edge_and_limit_reason():
    current = Checkpoint(budget=Budget(limits=Limits(research=10, review=10, total=20)))
    with pytest.raises(ValueError, match="Cannot move"):
        transition(current, Phase.COMPLETE)
    with pytest.raises(ValueError, match="budget or deadline"):
        transition(current, Phase.STOPPED_AT_LIMIT, reason="The models agreed")
