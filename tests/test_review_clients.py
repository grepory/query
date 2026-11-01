"""Role calls use real Pydantic AI validation with offline models only."""

import json
from copy import deepcopy
from uuid import UUID

import pytest
from pydantic import ValidationError
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelAPIError,
    ModelConnectionError,
    ModelHTTPError,
    ModelQuotaExceededError,
    ModelRequestTimeout,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from evals.stopping_cases import load_corpus
from query.config import ModelSettings
from query.investigation import BriefVersion
from query.review import Answer, EvidenceCheck, IssueResponse, Review, ReviewContext, Ruling
from query.review_clients import CallLimits, ReviewClients, ReviewModels, RoleCallError


@pytest.fixture
def context():
    case = next(c for c in load_corpus().cases if c.id == "primary-read-and-settled")
    return ReviewContext(brief=BriefVersion(version=1, brief=case.brief), evidence=case.snapshot)


def config(author="anthropic", reviewer="openai"):
    def settings(backend):
        return ModelSettings(
            _env_file=None,
            model_backend=backend,
            model_name="fake-configured-model",
            anthropic_api_key="fake",
            openai_api_key="fake",
            local_base_url="http://127.0.0.1:1234/v1",
        )

    return ReviewModels(author=settings(author), reviewer=settings(reviewer))


def citation(context, index=0):
    passage = context.passages()[index]
    return {
        "version_id": passage["version_id"],
        "passage_id": passage["passage_id"],
        "excerpt": passage["text"],
    }


def scope(context):
    return {"snapshot_id": str(context.evidence.snapshot.id), "brief_version": 1}


def answer_data(context, id=1):
    return {
        **scope(context),
        "id": str(UUID(int=id)),
        "title": "Capacity",
        "body_markdown": "Aster reports 900 MW operational.",
        "claims": [
            {
                "id": "capacity",
                "text": "Aster reports 900 MW operational.",
                "question_ids": ["capacity"],
                "kind": "source_assertion",
                "citations": [citation(context)],
                "premises": [],
                "reasoning": None,
            }
        ],
        "coverage": [
            {
                "question_id": "capacity",
                "status": "qualified",
                "claim_ids": ["capacity"],
                "rationale": "Dated reported figure.",
            }
        ],
        "issue_responses": [],
        "ruling_applications": [],
    }


def review_data(context, answer=None):
    answer = answer or answer_data(context)
    return {
        **scope(context),
        "id": str(UUID(int=2)),
        "answer_id": answer["id"],
        "publishable": True,
        "assessment": "A factual issue remains despite advisory signoff.",
        "issues": [
            {
                "id": "capacity-issue",
                "kind": "factual",
                "claim_id": "capacity",
                "claim_text": answer["claims"][0]["text"],
                "question_id": "capacity",
                "problem": "The date qualification is missing.",
                "evidence_examined": [citation(context)],
                "evidence_gap": None,
                "suggestion": "Preserve the cutoff date.",
            }
        ],
        "coverage": answer["coverage"],
    }


def response_data(context):
    return {
        "review_id": str(UUID(int=2)),
        "issue_id": "capacity-issue",
        "claim_id": "capacity",
        "claim_text": answer_data(context)["claims"][0]["text"],
        "disposition": "defend",
        "explanation": "The dated passage supplies this qualification.",
        "evidence": [citation(context)],
        "applied_edit": None,
    }


def ruling_data(context):
    return {
        **scope(context),
        "id": str(UUID(int=3)),
        "answer_id": str(UUID(int=1)),
        "review_id": str(UUID(int=2)),
        "issue_id": "capacity-issue",
        "claim_id": "capacity",
        "claim_text": answer_data(context)["claims"][0]["text"],
        "decision": "supported",
        "rationale": "The saved dated passage supports the assertion.",
        "evidence": [citation(context)],
        "replacement": None,
        "missing_context": None,
    }


def function(output, captured):
    def respond(messages, info):
        assert not info.function_tools
        assert not info.model_request_parameters.native_tools
        assert info.model_settings["max_tokens"] == 2048
        parts = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        captured.append((json.loads(parts[-1].content), info.instructions))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    return FunctionModel(respond)


def invoke(client, role, context):
    if role == "author":
        return client.write(context, UUID(int=1))
    answer = Answer.model_validate(answer_data(context))
    if role == "reviewer":
        return client.review(context, UUID(int=2), answer)
    return client.adjudicate(
        context,
        UUID(int=3),
        answer,
        Review.model_validate(review_data(context)),
        "capacity-issue",
        IssueResponse.model_validate(response_data(context)),
    )


def test_author_and_reviewer_see_identical_frozen_context_without_tools(context):
    author_messages, reviewer_messages = [], []
    clients = ReviewClients(
        config(),
        author_model=function(answer_data(context), author_messages),
        reviewer_model=function(review_data(context), reviewer_messages),
    )
    authored = invoke(clients, "author", context)
    reviewed = clients.review(context, UUID(int=2), authored.output)
    assert author_messages[0][0]["context"] == reviewer_messages[0][0]["context"]
    assert authored.context_digest == reviewed.context_digest == context.digest()
    assert authored.simulated and reviewed.simulated
    assert authored.usage.requests == reviewed.usage.requests == 1
    assert reviewed.output.publishable and reviewed.output.issues[0].kind == "factual"
    assert "never browse" in author_messages[0][1]
    assert "Editorial preference never" in reviewer_messages[0][1]


def test_inference_has_individual_cited_premises(context):
    data = answer_data(context)
    data["body_markdown"] += " Operational capacity is 75% of announced capacity."
    data["claims"].append(
        {
            "id": "ratio",
            "text": "Operational capacity is 75% of announced capacity.",
            "question_ids": ["capacity"],
            "kind": "inference",
            "citations": [],
            "premises": [
                {
                    "id": "operational",
                    "text": "900 MW is operational",
                    "citations": [citation(context)],
                },
                {
                    "id": "announced",
                    "text": "1200 MW is announced",
                    "citations": [citation(context)],
                },
            ],
            "reasoning": "900 divided by 1200 is 0.75.",
        }
    )
    client = ReviewClients(config(), author_model=TestModel(custom_output_args=data))
    assert len(client.write(context, UUID(int=1)).output.claims[1].premises) == 2
    for field, value in [("premises", []), ("reasoning", None)]:
        broken = deepcopy(data)
        broken["claims"][1][field] = value
        client = ReviewClients(config(), author_model=TestModel(custom_output_args=broken))
        with pytest.raises(RoleCallError) as error:
            client.write(context, UUID(int=1))
        assert error.value.kind == "invalid_output" and not error.value.retryable
        assert error.value.usage is not None


def test_adjudicator_receives_both_arguments_checked_failures_and_independent_sources(context):
    captured = []
    client = ReviewClients(config(), adjudicator_model=function(ruling_data(context), captured))
    response = IssueResponse.model_validate(response_data(context))
    data = review_data(context)
    data["issues"][0]["evidence_examined"][0]["excerpt"] = (
        "Invented quote: Aster operates 1700 MW on Mars."
    )
    review = Review.model_validate(data)
    checks = (
        EvidenceCheck(
            citation=review.issues[0].evidence_examined[0],
            status="not_found",
            reason="The quote was not found verbatim.",
        ),
    )
    result = client.adjudicate(
        context,
        UUID(int=3),
        Answer.model_validate(answer_data(context)),
        review,
        "capacity-issue",
        response,
        reviewer_checks=checks,
    )
    packet, instructions = captured[0]
    assert packet["author_response"]["explanation"] == response.explanation
    assert packet["issue"]["problem"] == review.issues[0].problem
    assert packet["reviewer_evidence"][0]["status"] == "not_found"
    assert packet["author_evidence"][0]["status"] == "verified"
    assert (
        len(packet["source_packet"]) == 2
    )  # Includes the independent ledger neither party quoted.
    assert not packet["omitted_passage_ids"]
    assert "not_found quotations carry no" in instructions
    assert result.output.decision == "supported"
    assert result.prompt_version == "query-adjudicator-v3"


def test_partial_packet_exposes_omissions_and_cannot_cite_omitted_passage(context):
    captured = []
    client = ReviewClients(config(), adjudicator_model=function(ruling_data(context), captured))
    args = (
        context,
        UUID(int=3),
        Answer.model_validate(answer_data(context)),
        Review.model_validate(review_data(context)),
        "capacity-issue",
        None,
    )
    selected = (UUID(context.passages()[0]["passage_id"]),)
    with pytest.raises(ValueError, match="omission reason"):
        client.adjudicate(*args, passage_ids=selected)
    client.adjudicate(*args, passage_ids=selected, omission_reason="Explicit test packet selection")
    payload = captured[0][0]
    assert len(payload["source_packet"]) == len(payload["omitted_passage_ids"]) == 1
    assert payload["omission_reason"] == "Explicit test packet selection"
    bad = ruling_data(context)
    bad["evidence"] = [citation(context, 1)]
    client = ReviewClients(config(), adjudicator_model=TestModel(custom_output_args=bad))
    with pytest.raises(RoleCallError, match="invalid_output"):
        client.adjudicate(*args, passage_ids=selected, omission_reason="Explicit selection")


def test_revision_preserves_per_issue_defense_for_the_next_reviewer(context):
    revised = answer_data(context, 4)
    revised["issue_responses"] = [response_data(context)]
    next_review = review_data(context, revised)
    next_review["id"] = str(UUID(int=5))
    next_review["issues"] = []
    messages = []
    client = ReviewClients(
        config(),
        author_model=TestModel(custom_output_args=revised),
        reviewer_model=function(next_review, messages),
    )
    previous, review = (
        Answer.model_validate(answer_data(context)),
        Review.model_validate(review_data(context)),
    )
    result = client.revise(context, UUID(int=4), previous, review)
    with pytest.raises(ValueError, match="saved previous review"):
        client.review(context, UUID(int=5), result.output)
    reviewed = client.review(context, UUID(int=5), result.output, previous_review=review)
    assert not reviewed.output.issues
    assert messages[0][0]["answer"]["issue_responses"][0]["disposition"] == "defend"
    revised["issue_responses"] = []
    client = ReviewClients(config(), author_model=TestModel(custom_output_args=revised))
    with pytest.raises(RoleCallError, match="invalid_output"):
        client.revise(context, UUID(int=4), previous, review)


@pytest.mark.parametrize("role", ["author", "reviewer", "adjudicator"])
@pytest.mark.parametrize(
    "status,body,retryable",
    [
        (429, {"error": {"message": "Rate limit"}}, True),
        (503, {"error": {"message": "Unavailable"}}, True),
        (408, {}, True),
        (401, {}, False),
        (403, {}, False),
        (404, {}, False),
        (422, {}, False),
        (429, {"error": {"code": "insufficient_quota"}}, False),
        (429, {"error": {"details": {"error_code": "enforced_spend_limit_reached"}}}, False),
        (
            429,
            {"error": {"code": {"bad": "shape"}, "type": [], "details": {"error_code": []}}},
            True,
        ),
        (
            400,
            {"error": {"code": [], "type": {}, "details": {"error_code": {"bad": "shape"}}}},
            False,
        ),
        (
            400,
            {"error": {"message": "Your credit balance is too low to access the Anthropic API"}},
            False,
        ),
    ],
)
def test_provider_failures_are_classified_without_in_process_retries(
    context, role, status, body, retryable
):
    calls = []

    def fail(messages, info):
        calls.append(True)
        raise ModelHTTPError(status, "fake", body)

    clients = ReviewClients(config(), **{role + "_model": FunctionModel(fail)})
    with pytest.raises(RoleCallError) as error:
        invoke(clients, role, context)
    assert error.value.retryable is retryable and error.value.status_code == status
    assert error.value.usage is None and len(calls) == 1


@pytest.mark.parametrize(
    "exception,kind,retryable",
    [
        (ModelConnectionError("fake", "Disconnected"), "provider_unavailable", True),
        (ModelRequestTimeout("fake", 1), "provider_unavailable", True),
        (ModelQuotaExceededError("fake", "No quota"), "provider_rejected", False),
        (ModelAPIError("fake", "Other provider failure"), "provider_rejected", False),
        (UnexpectedModelBehavior("Malformed"), "invalid_output", False),
        (UsageLimitExceeded("Explicit test ceiling"), "usage_limit", False),
    ],
)
def test_typed_provider_and_validation_failures(context, exception, kind, retryable):
    def fail(messages, info):
        raise exception

    client = ReviewClients(config(), author_model=FunctionModel(fail))
    with pytest.raises(RoleCallError) as error:
        client.write(context, UUID(int=1))
    assert error.value.kind == kind and error.value.retryable is retryable


@pytest.mark.parametrize(
    "exception", [ValueError("programming bug"), AgentRunError("unknown run error")]
)
def test_unclassified_programming_errors_propagate(context, exception):
    def fail(messages, info):
        raise exception

    with pytest.raises(type(exception), match=str(exception)):
        ReviewClients(config(), author_model=FunctionModel(fail)).write(context, UUID(int=1))


@pytest.mark.parametrize(
    "author,reviewer",
    [("anthropic", "anthropic"), ("openai", "openai"), ("local", "openai"), ("anthropic", "local")],
)
def test_same_or_unprovable_vendor_separation_is_rejected(author, reviewer):
    with pytest.raises(ValidationError, match="different explicit hosted vendors"):
        config(author, reviewer)


def test_context_limit_refuses_call_without_truncation(context):
    captured = []
    client = ReviewClients(
        config(),
        limits=CallLimits(max_prompt_bytes=50),
        author_model=function(answer_data(context), captured),
    )
    with pytest.raises(RoleCallError, match="context_limit"):
        client.write(context, UUID(int=1))
    assert not captured


@pytest.mark.parametrize(
    "field,value", [("snapshot_id", str(UUID(int=999))), ("brief_version", 2), ("coverage", [])]
)
def test_wrong_output_scope_or_lost_coverage_is_rejected_with_usage(context, field, value):
    data = answer_data(context)
    data[field] = value
    client = ReviewClients(config(), author_model=TestModel(custom_output_args=data))
    with pytest.raises(RoleCallError) as error:
        client.write(context, UUID(int=1))
    assert error.value.kind == "invalid_output"
    assert error.value.usage.requests == 1


@pytest.mark.parametrize("mutation", ["claim_text", "claim_id", "duplicate_issue", "question"])
def test_review_rejects_incorrect_issue_identity(context, mutation):
    data = review_data(context)
    if mutation == "duplicate_issue":
        data["issues"].append(deepcopy(data["issues"][0]))
    elif mutation == "question":
        data["issues"][0]["question_id"] = "not-in-brief"
    else:
        data["issues"][0][mutation] = "changed"
    client = ReviewClients(config(), reviewer_model=TestModel(custom_output_args=data))
    with pytest.raises(RoleCallError, match="invalid_output"):
        client.review(context, UUID(int=2), Answer.model_validate(answer_data(context)))


def test_absent_evidence_is_a_valid_explicit_review_objection(context):
    data = review_data(context)
    data["issues"][0]["evidence_examined"] = []
    data["issues"][0]["evidence_gap"] = (
        "No supplied passage establishes the claimed measurement date."
    )
    client = ReviewClients(config(), reviewer_model=TestModel(custom_output_args=data))
    review = client.review(context, UUID(int=2), Answer.model_validate(answer_data(context))).output
    assert not review.issues[0].evidence_examined and review.issues[0].evidence_gap


@pytest.mark.parametrize(
    "decision,replacement,valid",
    [
        ("narrow", None, False),
        ("narrow", "As of the cutoff, 900 MW was reported operational.", True),
        ("supported", "Unexpected edit", False),
        ("remove", "Unexpected edit", False),
    ],
)
def test_narrowing_and_replacement_contract(context, decision, replacement, valid):
    data = ruling_data(context)
    data.update(decision=decision, replacement=replacement)
    if valid:
        assert Ruling.model_validate(data).replacement == replacement
    else:
        with pytest.raises(ValidationError, match="narrowing"):
            Ruling.model_validate(data)


def test_changed_claim_or_snapshot_cannot_inherit_an_old_ruling(context):
    previous = Answer.model_validate(answer_data(context))
    review = Review.model_validate(review_data(context))
    ruling = Ruling.model_validate(ruling_data(context))
    for changed in (
        ruling.model_copy(update={"claim_text": "Aster operates 1200 MW."}),
        ruling.model_copy(update={"snapshot_id": UUID(int=999)}),
    ):
        with pytest.raises(ValueError, match="exact claim|snapshot"):
            ReviewClients(config()).revise(
                context, UUID(int=4), previous, review, rulings=(changed,)
            )


def test_binding_ruling_survives_a_later_review_of_the_unchanged_claim(context):
    previous = Answer.model_validate(answer_data(context, 4))
    current_review = review_data(context, answer_data(context, 4))
    current_review["id"] = str(UUID(int=5))
    current_review["issues"][0]["id"] = "later-occurrence"
    reviewed = Review.model_validate(current_review)
    old_ruling = ruling_data(context)
    old_ruling.update(
        decision="narrow", replacement="As of 1 October 2026, Aster reports 900 MW operational."
    )
    revised = answer_data(context, 6)
    response = response_data(context)
    response.update(
        review_id=str(UUID(int=5)),
        issue_id="later-occurrence",
        disposition="applied_ruling",
        applied_edit="Added the measurement date from the binding ruling.",
    )
    revised["body_markdown"] = old_ruling["replacement"]
    revised["claims"][0]["text"] = old_ruling["replacement"]
    revised["issue_responses"] = [response]
    revised["ruling_applications"] = [
        {
            "ruling_id": old_ruling["id"],
            "claim_id": old_ruling["claim_id"],
            "claim_text": old_ruling["claim_text"],
            "applied_edit": "Added the exact binding date qualification.",
        }
    ]
    client = ReviewClients(config(), author_model=TestModel(custom_output_args=revised))
    result = client.revise(
        context, UUID(int=6), previous, reviewed, rulings=(Ruling.model_validate(old_ruling),)
    )
    assert result.output.issue_responses[0].disposition == "applied_ruling"


def test_context_exposes_exclusions_without_leaking_the_excluded_text(context):
    data = context.model_dump(mode="json")
    snapshot = data["evidence"]["snapshot"]
    excluded = snapshot["candidates"][1]["passages"][0]
    excluded.update(included=False, reason="Outside this selected corpus")
    snapshot["included_bytes"] -= data["evidence"]["versions"][1]["byte_count"]
    snapshot["requested_bytes"] = snapshot["included_bytes"]
    selected = ReviewContext.model_validate(data)
    assert len(selected.passages()) == 1
    assert data["evidence"]["versions"][1]["text"] not in json.dumps(selected.payload())
    answer = answer_data(selected)
    answer["claims"][0]["citations"][0] = citation(context, 1)
    with pytest.raises(RoleCallError, match="invalid_output"):
        ReviewClients(config(), author_model=TestModel(custom_output_args=answer)).write(
            selected, UUID(int=1)
        )


def test_limited_snapshot_can_only_supply_its_metadata_and_no_source_text(context):
    data = context.model_dump(mode="json")
    snapshot = data["evidence"]["snapshot"]
    snapshot.update(status="limited", reason="empty_selection", included_bytes=0, requested_bytes=0)
    for candidate in snapshot["candidates"]:
        for passage in candidate["passages"]:
            passage.update(included=False, reason="Explicitly unselected")
    limited = ReviewContext.model_validate(data)
    assert limited.passages() == []
    answer = answer_data(context)
    answer.update(
        claims=[],
        coverage=[
            {
                "question_id": "capacity",
                "status": "unresolved",
                "claim_ids": [],
                "rationale": "No usable source passages selected.",
            }
        ],
    )
    answer["body_markdown"] = "The supplied corpus cannot establish the requested quantity."
    result = ReviewClients(config(), author_model=TestModel(custom_output_args=answer)).write(
        limited, UUID(int=1)
    )
    assert result.output.coverage[0].status == "unresolved"


def test_real_models_are_not_accepted_as_offline_overrides():
    from query.models import build_model

    with pytest.raises(ValueError, match="offline TestModel"):
        ReviewClients(config(), author_model=build_model(config().author))


def test_uninventoried_title_claim_can_be_reviewed_adjudicated_and_removed(context):
    span = "All 1200 MW is operational."
    original = answer_data(context)
    original.update(title=span, body_markdown="The exact quantity remains unresolved.", claims=[])
    original["coverage"][0].update(
        status="unresolved", claim_ids=[], rationale="Unresolved quantity."
    )
    objection = review_data(context)
    objection["coverage"] = original["coverage"]
    objection["issues"][0].update(
        claim_id=None,
        claim_text=span,
        problem="Unsupported title assertion absent from the claim inventory.",
    )
    ruling = ruling_data(context)
    ruling.update(claim_id=None, claim_text=span, decision="remove")
    revised = deepcopy(original)
    revised.update(id=str(UUID(int=4)), title="Capacity remains unresolved")
    response = response_data(context)
    response.update(
        claim_id=None,
        claim_text=span,
        disposition="applied_ruling",
        applied_edit="Removed the unsupported 1200 MW title assertion.",
    )
    revised["issue_responses"] = [response]
    revised["ruling_applications"] = [
        {
            "ruling_id": ruling["id"],
            "claim_id": None,
            "claim_text": span,
            "applied_edit": response["applied_edit"],
        }
    ]
    client = ReviewClients(
        config(),
        reviewer_model=TestModel(custom_output_args=objection),
        adjudicator_model=TestModel(custom_output_args=ruling),
        author_model=TestModel(custom_output_args=revised),
    )
    original = Answer.model_validate(original)
    review = client.review(context, UUID(int=2), original).output
    result = client.adjudicate(context, UUID(int=3), original, review, "capacity-issue", None)
    applied = client.revise(context, UUID(int=4), original, review, rulings=(result.output,)).output
    assert applied.title != span
    assert applied.ruling_applications[0].claim_text == span


def test_original_inference_premises_reach_adjudicator_without_author_response(context):
    data = answer_data(context)
    claim = data["claims"][0]
    claim.update(
        kind="inference",
        citations=[],
        reasoning="900 / 1200 = 0.75.",
        text="Operational capacity is 75% of announced capacity.",
        premises=[
            {"id": "operational", "text": "900 MW operational", "citations": [citation(context)]},
            {"id": "announced", "text": "1200 MW planned total", "citations": [citation(context)]},
        ],
    )
    data["body_markdown"] = claim["text"]
    review = review_data(context, data)
    review["issues"][0]["kind"] = "inference"
    ruling = ruling_data(context)
    ruling["claim_text"] = claim["text"]
    captured = []
    client = ReviewClients(config(), adjudicator_model=function(ruling, captured))
    client.adjudicate(
        context,
        UUID(int=3),
        Answer.model_validate(data),
        Review.model_validate(review),
        "capacity-issue",
        None,
    )
    packet = captured[0][0]
    assert packet["author_response"] is None
    assert packet["targeted_claim"]["reasoning"] == claim["reasoning"]
    assert packet["targeted_claim"]["premises"] == claim["premises"]


@pytest.mark.parametrize("with_responses", [False, True])
def test_foreign_previous_review_is_rejected_even_without_issue_responses(context, with_responses):
    data = answer_data(context, 4)
    if with_responses:
        data["issue_responses"] = [response_data(context)]
    previous = review_data(context)
    previous["snapshot_id"] = str(UUID(int=999))
    captured = []
    client = ReviewClients(config(), reviewer_model=function(review_data(context, data), captured))
    with pytest.raises(ValueError, match="current brief and snapshot"):
        client.review(
            context,
            UUID(int=5),
            Answer.model_validate(data),
            previous_review=Review.model_validate(previous),
        )
    assert not captured


def test_historical_binding_edit_is_recorded_when_current_review_has_no_issue(context):
    previous = answer_data(context, 4)
    current_review = review_data(context, previous)
    current_review.update(id=str(UUID(int=5)), issues=[])
    ruling = ruling_data(context)
    ruling.update(
        decision="narrow", replacement="As of the cutoff, Aster reports 900 MW operational."
    )
    revised = answer_data(context, 6)
    revised["body_markdown"] = revised["claims"][0]["text"] = ruling["replacement"]
    revised["ruling_applications"] = [
        {
            "ruling_id": ruling["id"],
            "claim_id": "capacity",
            "claim_text": ruling["claim_text"],
            "applied_edit": "Applied the earlier date qualification.",
        }
    ]
    final_review = review_data(context, revised)
    final_review.update(id=str(UUID(int=7)), issues=[])
    captured = []
    clients = ReviewClients(
        config(),
        author_model=TestModel(custom_output_args=revised),
        reviewer_model=function(final_review, captured),
    )
    saved_ruling = Ruling.model_validate(ruling)
    result = clients.revise(
        context,
        UUID(int=6),
        Answer.model_validate(previous),
        Review.model_validate(current_review),
        rulings=(saved_ruling,),
    ).output
    assert not result.issue_responses and result.ruling_applications[0].ruling_id == saved_ruling.id
    clients.review(context, UUID(int=7), result, rulings=(saved_ruling,))
    assert captured[0][0]["rulings"][0]["review_id"] == str(UUID(int=2))


def test_role_outputs_reject_fabricated_quotes_with_known_usage(context):
    for role, data in (
        ("author", answer_data(context)),
        ("reviewer", review_data(context)),
        ("adjudicator", ruling_data(context)),
    ):
        if role == "author":
            data["claims"][0]["citations"][0]["excerpt"] = "A fabricated quotation."
        elif role == "reviewer":
            data["issues"][0]["evidence_examined"][0]["excerpt"] = "A fabricated quotation."
        else:
            data["evidence"][0]["excerpt"] = "A fabricated quotation."
        client = ReviewClients(config(), **{role + "_model": TestModel(custom_output_args=data)})
        with pytest.raises(RoleCallError) as error:
            invoke(client, role, context)
        assert error.value.kind == "invalid_output" and error.value.usage.requests == 1


def test_adjudication_marks_foreign_party_citations_without_accepting_them(context):
    data = answer_data(context)
    data["claims"][0]["citations"][0]["version_id"] = str(UUID(int=999))
    review = review_data(context)
    review["issues"][0]["evidence_examined"][0]["version_id"] = str(UUID(int=999))
    response = response_data(context)
    response["evidence"][0]["version_id"] = str(UUID(int=999))
    captured = []
    client = ReviewClients(config(), adjudicator_model=function(ruling_data(context), captured))
    answer = Answer.model_validate(data)
    with pytest.raises(ValueError, match="Citation verification failed"):
        client.review(context, UUID(int=2), answer)
    client.adjudicate(
        context,
        UUID(int=3),
        answer,
        Review.model_validate(review),
        "capacity-issue",
        IssueResponse.model_validate(response),
    )
    packet = captured[0][0]
    for key in ("claim_evidence", "reviewer_evidence", "author_evidence"):
        assert packet[key][0]["status"] == "not_found"
        assert packet[key][0]["reason"] == "unknown_source_version"
        assert not packet[key][0]["quote_available"]
    assert len(packet["source_packet"]) == 2


def test_forged_verified_status_is_rejected_before_provider_call(context):
    review = review_data(context)
    review["issues"][0]["evidence_examined"][0]["excerpt"] = "Invented support for 1700 MW."
    review = Review.model_validate(review)
    forged = EvidenceCheck(
        citation=review.issues[0].evidence_examined[0], status="verified", reason=None
    )
    captured = []
    client = ReviewClients(config(), adjudicator_model=function(ruling_data(context), captured))
    with pytest.raises(ValueError, match="disagree with mechanical verification"):
        client.adjudicate(
            context,
            UUID(int=3),
            Answer.model_validate(answer_data(context)),
            review,
            "capacity-issue",
            None,
            reviewer_checks=(forged,),
        )
    assert not captured


def test_snapshot_verified_quote_omitted_from_packet_is_explicitly_unavailable(context):
    captured = []
    ruling = ruling_data(context)
    ruling["evidence"] = [citation(context, 1)]
    client = ReviewClients(config(), adjudicator_model=function(ruling, captured))
    selected = (UUID(context.passages()[1]["passage_id"]),)
    client.adjudicate(
        context,
        UUID(int=3),
        Answer.model_validate(answer_data(context)),
        Review.model_validate(review_data(context)),
        "capacity-issue",
        IssueResponse.model_validate(response_data(context)),
        passage_ids=selected,
        omission_reason="Explicit packet truncation for this test",
    )
    packet, instructions = captured[0]
    for key in ("claim_evidence", "reviewer_evidence", "author_evidence"):
        assert packet[key][0]["status"] == "verified"
        assert not packet[key][0]["passage_in_packet"]
        assert not packet[key][0]["quote_available"]
    assert len(packet["omitted_passage_ids"]) == 1
    assert "quote_available=false" in instructions


def test_disputed_inference_premises_get_mechanical_checks_in_packet(context):
    data = answer_data(context)
    claim = data["claims"][0]
    bad_citation = citation(context)
    bad_citation["excerpt"] = "A fabricated premise quotation."
    claim.update(
        kind="inference",
        reasoning="Explicit disputed inference.",
        premises=[
            {"id": "real", "text": "Real premise", "citations": [citation(context)]},
            {"id": "fabricated", "text": "Unsupported premise", "citations": [bad_citation]},
        ],
    )
    captured = []
    client = ReviewClients(config(), adjudicator_model=function(ruling_data(context), captured))
    client.adjudicate(
        context,
        UUID(int=3),
        Answer.model_validate(data),
        Review.model_validate(review_data(context)),
        "capacity-issue",
        None,
    )
    evidence = captured[0][0]["premise_evidence"]
    assert evidence[0]["premise_id"] == "real" and evidence[0]["checks"][0]["status"] == "verified"
    assert (
        evidence[1]["premise_id"] == "fabricated"
        and evidence[1]["checks"][0]["status"] == "not_found"
    )
    assert not evidence[1]["checks"][0]["quote_available"]


def test_adjudication_retains_prior_defense_review_provenance(context):
    answer = Answer.model_validate(answer_data(context))
    old_review = Review.model_validate(review_data(context))
    current_data = review_data(context)
    current_data["id"] = str(UUID(int=20))
    current_data["issues"][0]["id"] = "reworded-objection"
    current_data["issues"][0]["problem"] = "Still disputes the same exact claim."
    current_review = Review.model_validate(current_data)
    result = ruling_data(context)
    result.update(review_id=str(current_review.id), issue_id="reworded-objection")
    captured = []
    clients = ReviewClients(config(), adjudicator_model=function(result, captured))
    response = IssueResponse.model_validate(response_data(context))
    clients.adjudicate(
        context,
        UUID(int=3),
        answer,
        current_review,
        "reworded-objection",
        response,
        response_review=old_review,
    )
    payload = captured[0][0]
    assert payload["author_response"]["review_id"] == str(old_review.id)
    assert payload["author_response"]["issue_id"] == "capacity-issue"
    assert payload["response_review"] == old_review.model_dump(mode="json")
    assert payload["review_id"] == str(current_review.id)
    wrong = response.model_copy(update={"claim_text": "Changed assertion"})
    with pytest.raises(ValueError, match="exact reviewed issue"):
        clients.adjudicate(
            context,
            UUID(int=3),
            answer,
            current_review,
            "reworded-objection",
            wrong,
            response_review=old_review,
        )
