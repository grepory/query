"""Snapshot-scoped answer/review contracts; no provider I/O or stopping policy."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from query.evidence import EvidenceContract, LoadedSnapshot, fingerprint
from query.investigation import BriefVersion

Text = Annotated[str, Field(min_length=1)]
Positive = Annotated[int, Field(gt=0, strict=True)]


class ReviewContract(EvidenceContract):
    @field_validator("*", mode="after")
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text must not be blank")
        return value


def unique(values, label):
    if len(values) != len(set(values)):
        raise ValueError(f"Duplicate {label}")


class Citation(ReviewContract):
    version_id: UUID
    passage_id: UUID
    excerpt: Text


class Premise(ReviewContract):
    id: Text
    text: Text
    citations: tuple[Citation, ...] = Field(min_length=1)


class Claim(ReviewContract):
    id: Text
    text: Text
    question_ids: tuple[Text, ...] = Field(min_length=1)
    kind: Literal["source_assertion", "inference"]
    citations: tuple[Citation, ...]
    premises: tuple[Premise, ...]
    reasoning: Text | None

    @model_validator(mode="after")
    def explicit_support(self):
        unique(self.question_ids, "claim question")
        unique([p.id for p in self.premises], "premise")
        if self.kind == "source_assertion":
            if not self.citations or self.premises or self.reasoning is not None:
                raise ValueError("Source assertions need citations, without inference fields")
        elif not self.premises or self.reasoning is None:
            raise ValueError("Inferences require individually cited premises and reasoning")
        return self


class Coverage(ReviewContract):
    question_id: Text
    status: Literal["answered", "qualified", "unresolved"]
    claim_ids: tuple[Text, ...]
    rationale: Text


class Scope(ReviewContract):
    brief_version: Positive
    snapshot_id: UUID


class IssueResponse(ReviewContract):
    review_id: UUID
    issue_id: Text
    claim_id: Text | None
    claim_text: Text | None
    disposition: Literal["defend", "concede", "applied_ruling"]
    explanation: Text
    evidence: tuple[Citation, ...]
    applied_edit: Text | None

    @model_validator(mode="after")
    def edited_response(self):
        if self.disposition in {"concede", "applied_ruling"} and self.applied_edit is None:
            raise ValueError("Concession or applied ruling must describe the exact edit")
        return self


class RulingApplication(ReviewContract):
    ruling_id: UUID
    claim_id: Text | None
    claim_text: Text
    applied_edit: Text


class Answer(Scope):
    id: UUID
    title: Text
    body_markdown: Text
    claims: tuple[Claim, ...]
    coverage: tuple[Coverage, ...]
    issue_responses: tuple[IssueResponse, ...]
    ruling_applications: tuple[RulingApplication, ...]


class Issue(ReviewContract):
    id: Text
    kind: Literal["factual", "inference", "coverage", "editorial"]
    claim_id: Text | None
    claim_text: Text | None
    question_id: Text | None
    problem: Text
    evidence_examined: tuple[Citation, ...]
    evidence_gap: Text | None
    suggestion: Text | None

    @model_validator(mode="after")
    def target_and_evidence(self):
        if self.claim_id is not None and self.claim_text is None:
            raise ValueError("Claim identity requires exact claim text")
        if self.kind in {"factual", "inference"} and self.claim_text is None:
            raise ValueError("Factual/inference issues must name an exact answer span")
        if self.kind == "coverage" and self.question_id is None:
            raise ValueError("Coverage issues must retain the affected question")
        if self.kind != "editorial" and not self.evidence_examined and self.evidence_gap is None:
            raise ValueError("An objection without evidence must explain the evidence gap")
        return self


class Review(Scope):
    id: UUID
    answer_id: UUID
    publishable: bool
    assessment: Text
    issues: tuple[Issue, ...]
    coverage: tuple[Coverage, ...]


class Ruling(Scope):
    id: UUID
    answer_id: UUID
    review_id: UUID
    issue_id: Text
    claim_id: Text | None
    claim_text: Text
    decision: Literal["supported", "narrow", "remove", "insufficient_evidence"]
    rationale: Text
    evidence: tuple[Citation, ...]
    replacement: Text | None
    missing_context: Text | None

    @model_validator(mode="after")
    def replacement_scope(self):
        if (self.decision == "narrow") != (self.replacement is not None):
            raise ValueError("Only a narrowing ruling must have replacement wording")
        if self.decision in {"supported", "narrow"} and not self.evidence:
            raise ValueError("A supporting ruling needs source evidence")
        if self.decision == "insufficient_evidence" and self.missing_context is None:
            raise ValueError("Insufficient evidence must identify missing context")
        return self


class EvidenceCheck(ReviewContract):
    citation: Citation
    status: Literal["unverified", "verified", "not_found"]
    reason: Text | None

    @model_validator(mode="after")
    def explain_unchecked_evidence(self):
        if self.status != "verified" and self.reason is None:
            raise ValueError("Unverified or missing quotes need an explicit reason")
        return self


class ReviewContext(ReviewContract):
    brief: BriefVersion
    evidence: LoadedSnapshot

    @model_validator(mode="after")
    def consistent_snapshot(self):
        snapshot = self.evidence.snapshot
        if self.brief.version != snapshot.brief_version:
            raise ValueError("Brief and snapshot must have the same version")
        versions = {v.id: v for v in self.evidence.versions}
        unique([v.id for v in self.evidence.versions], "source version")
        unique([c.version_id for c in snapshot.candidates], "snapshot candidate")
        if set(versions) != {c.version_id for c in snapshot.candidates}:
            raise ValueError("Snapshot candidates must match loaded versions")
        observations = {o.operation_key: o for o in self.evidence.observations}
        unique([o.operation_key for o in self.evidence.observations], "observation")
        size = 0
        for candidate in snapshot.candidates:
            version = versions[candidate.version_id]
            if not candidate.observation_keys:
                raise ValueError("Source candidate needs frozen observation keys")
            for key in candidate.observation_keys:
                obs = observations.get(key)
                if (
                    obs is None
                    or obs.version_id != version.id
                    or obs.investigation_id != snapshot.investigation_id
                    or obs.content_hash != version.content_hash
                ):
                    raise ValueError("Frozen observation must match its source and investigation")
            if len(candidate.passages) != len(version.passages):
                raise ValueError("Snapshot must account for all source passages")
            for selected, passage in zip(candidate.passages, version.passages, strict=True):
                if (selected.passage_id, selected.start, selected.end) != (
                    passage.id,
                    passage.start,
                    passage.end,
                ):
                    raise ValueError("Snapshot passage does not resolve to the saved version")
                if selected.included:
                    if selected.reason is not None:
                        raise ValueError("Included passage cannot have an exclusion reason")
                    size += len(version.passage_text(passage).encode())
                elif not selected.reason:
                    raise ValueError("Excluded passage needs a reason")
        if size != snapshot.included_bytes:
            raise ValueError("Snapshot included-byte accounting does not match saved passages")
        if snapshot.status == "ready" and (snapshot.reason is not None or size == 0):
            raise ValueError("Ready snapshot must contain usable evidence")
        if snapshot.status == "limited" and (snapshot.reason is None or size != 0):
            raise ValueError("Limited snapshot cannot supply usable passages")
        return self

    def passages(self) -> list[dict]:
        versions = {v.id: v for v in self.evidence.versions}
        return [
            {
                "version_id": str(version.id),
                "passage_id": str(p.passage_id),
                "title": version.title,
                "origin_url": version.origin_url,
                "text": version.text[p.start : p.end],
            }
            for candidate in self.evidence.snapshot.candidates
            for version in [versions[candidate.version_id]]
            for p in candidate.passages
            if p.included
        ]

    def payload(self) -> dict:
        # The manifest exposes omissions; excluded source text is never supplied.
        return {
            "brief": self.brief.model_dump(mode="json"),
            "snapshot": self.evidence.snapshot.model_dump(mode="json"),
            "observations": [o.model_dump(mode="json") for o in self.evidence.observations],
            "source_passages": self.passages(),
        }

    def digest(self) -> str:
        return fingerprint(self.payload())


def check_scope(record: Scope, context: ReviewContext):
    if (
        record.snapshot_id != context.evidence.snapshot.id
        or record.brief_version != context.brief.version
    ):
        raise ValueError("Record does not belong to the current brief and snapshot")


def check_citations(citations, context: ReviewContext):
    included = {(p["version_id"], p["passage_id"]) for p in context.passages()}
    for citation in citations:
        if (str(citation.version_id), str(citation.passage_id)) not in included:
            raise ValueError("Citation does not name an included snapshot passage")


def check_coverage(coverage, claims, context: ReviewContext):
    questions = {q.key for q in context.brief.brief.questions}
    unique([c.question_id for c in coverage], "question coverage")
    if {c.question_id for c in coverage} != questions:
        raise ValueError("Coverage must retain every brief question")
    for item in coverage:
        unique(item.claim_ids, "coverage claim")
        if not set(item.claim_ids) <= claims.keys():
            raise ValueError("Coverage names an unknown claim")
        if any(item.question_id not in claims[key].question_ids for key in item.claim_ids):
            raise ValueError("Coverage claim must address that question")
        if item.status == "answered" and not item.claim_ids:
            raise ValueError("Answered coverage must identify supporting claims")


def check_answer(answer: Answer, context: ReviewContext):
    check_scope(answer, context)
    unique([c.id for c in answer.claims], "claim")
    questions = {q.key for q in context.brief.brief.questions}
    for claim in answer.claims:
        if not set(claim.question_ids) <= questions:
            raise ValueError("Claim names an unknown brief question")
        if claim.text not in answer.title and claim.text not in answer.body_markdown:
            raise ValueError("Claim must quote an exact span of the answer title or body")
        check_citations(claim.citations, context)
        for premise in claim.premises:
            check_citations(premise.citations, context)
    check_coverage(answer.coverage, {c.id: c for c in answer.claims}, context)
    unique([r.issue_id for r in answer.issue_responses], "issue response")
    unique([r.ruling_id for r in answer.ruling_applications], "ruling application")
    for response in answer.issue_responses:
        check_citations(response.evidence, context)


def check_review(review: Review, answer: Answer, context: ReviewContext):
    check_scope(review, context)
    if review.answer_id != answer.id:
        raise ValueError("Review must name the exact answer")
    unique([i.id for i in review.issues], "issue")
    claims = {c.id: c for c in answer.claims}
    questions = {q.key for q in context.brief.brief.questions}
    for issue in review.issues:
        if issue.claim_id is not None and (
            issue.claim_id not in claims or issue.claim_text != claims[issue.claim_id].text
        ):
            raise ValueError("Issue must name the exact current claim text")
        if (
            issue.claim_id is None
            and issue.claim_text is not None
            and issue.claim_text not in answer.title
            and issue.claim_text not in answer.body_markdown
        ):
            raise ValueError("Uninventoried issue must quote an exact title/body span")
        if issue.question_id is not None and issue.question_id not in questions:
            raise ValueError("Issue names an unknown brief question")
        check_citations(issue.evidence_examined, context)
    check_coverage(review.coverage, claims, context)


def check_response(response: IssueResponse, review: Review):
    issue = next((i for i in review.issues if i.id == response.issue_id), None)
    if (
        issue is None
        or response.review_id != review.id
        or (response.claim_id, response.claim_text) != (issue.claim_id, issue.claim_text)
    ):
        raise ValueError("Response must name the exact reviewed issue and claim")


def check_ruling(ruling: Ruling, review: Review, context: ReviewContext):
    check_scope(ruling, context)
    issue = next((i for i in review.issues if i.id == ruling.issue_id), None)
    if (
        issue is None
        or issue.kind not in {"factual", "inference"}
        or ruling.answer_id != review.answer_id
        or ruling.review_id != review.id
        or (ruling.claim_id, ruling.claim_text) != (issue.claim_id, issue.claim_text)
    ):
        raise ValueError("Ruling must name the exact factual/inference dispute")
    check_citations(ruling.evidence, context)


def check_standing_ruling(ruling: Ruling, answer: Answer, context: ReviewContext):
    """A saved ruling may outlive its original review, but not its exact claim scope."""
    check_scope(ruling, context)
    claim = next((c for c in answer.claims if c.id == ruling.claim_id), None)
    if ruling.claim_id is None:
        matches = ruling.claim_text in answer.title or ruling.claim_text in answer.body_markdown
    else:
        matches = claim is not None and claim.text == ruling.claim_text
    if not matches:
        raise ValueError("Standing ruling must match the current exact claim text")
    check_citations(ruling.evidence, context)


def check_applications(answer: Answer, rulings: tuple[Ruling, ...]):
    by_id = {r.id: r for r in rulings}
    unique([r.id for r in rulings], "supplied ruling")
    for applied in answer.ruling_applications:
        original = by_id.get(applied.ruling_id)
        if original is None or (applied.claim_id, applied.claim_text) != (
            original.claim_id,
            original.claim_text,
        ):
            raise ValueError("Applied edit must name a supplied ruling and exact original claim")
