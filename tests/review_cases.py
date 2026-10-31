"""Prepared review histories for future controller tests, not a reference controller."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from query.investigation import Brief, Budget

Text = Annotated[str, Field(min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Origin(Record):
    path: Text
    symbol: Text


class Citation(Record):
    version: Text
    excerpt: Text


class Claim(Record):
    id: Text
    text: Text
    question: Text


class Draft(Record):
    id: Text
    snapshot: Text
    claims: tuple[Claim, ...]
    after_rulings: tuple[Text, ...] = ()


class Issue(Record):
    id: Text
    claim: Text
    kind: Literal["factual", "inference", "editorial", "coverage"]
    problem: Text
    evidence: tuple[Citation, ...] = ()


class Review(Record):
    id: Text
    draft: Text
    publishable: bool
    issues: tuple[Issue, ...]


class Response(Record):
    draft: Text
    issue: Text
    disposition: Literal["defend", "concede", "applied_ruling", "unstructured"]
    explanation: Text
    evidence: tuple[Citation, ...] = ()
    applied_edit: Text | None = None


class Ruling(Record):
    id: Text
    issue: Text
    snapshot: Text
    claim: Text
    claim_text: Text
    decision: Literal["supported", "narrow", "remove"]
    rationale: Text
    replacement: Text | None = None


class Operation(Record):
    key: Text
    role: Literal["author", "reviewer", "adjudicator"]
    state: Literal["committed", "planned", "remote_outcome_unknown"]
    result: Text | None = None
    used_tokens: Annotated[int, Field(ge=0)] = 0


class Expected(Record):
    actions: tuple[Text, ...] = Field(min_length=1)
    forbidden: tuple[Text, ...] = Field(min_length=1)
    visible_draft: Text | None
    rationale: Text


class ReviewCase(Record):
    id: Text
    area: Literal[
        "final_review",
        "responses",
        "disputes",
        "rulings",
        "editorial",
        "evidence",
        "closeout",
        "resume",
        "coverage",
    ]
    owners: tuple[Text, ...] = Field(min_length=1)
    origins: tuple[Origin, ...] = Field(min_length=1)
    changed_assumptions: Text
    snapshot: Text
    drafts: tuple[Draft, ...]
    reviews: tuple[Review, ...]
    responses: tuple[Response, ...] = ()
    rulings: tuple[Ruling, ...] = ()
    operations: tuple[Operation, ...] = ()
    budget: Budget
    closeout: bool = False
    trigger: Text
    expected: Expected


class Corpus(Record):
    source_repository: Literal["https://github.com/grepory/aibyai"]
    source_revision: Literal["ce28ff41013e30e5b4a47ba8d9f15a50f48ea8c9"]
    brief: Brief
    versions: dict[str, Text]
    snapshots: dict[str, tuple[Text, ...]]
    cases: tuple[ReviewCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_references(self):
        def unique(rows, label):
            keys = [row.id for row in rows]
            if len(keys) != len(set(keys)):
                raise ValueError(f"Duplicate {label}")

        unique(self.cases, "case")
        questions = {q.key for q in self.brief.questions}
        for versions in self.snapshots.values():
            if not versions or len(set(versions)) != len(versions):
                raise ValueError("Snapshot membership must be nonempty and unique")
            if set(versions) - self.versions.keys():
                raise ValueError("Unknown snapshot version")
        for case in self.cases:
            unique(case.drafts, "draft")
            unique(case.reviews, "review")
            unique(case.rulings, "ruling")
            if case.snapshot not in self.snapshots:
                raise ValueError("Unknown current snapshot")
            drafts = {d.id: d for d in case.drafts}
            issues = {}
            issue_drafts = {}
            reviewed = set()
            for draft in case.drafts:
                unique(draft.claims, "claim")
                if draft.snapshot not in self.snapshots:
                    raise ValueError("Unknown draft snapshot")
                if {c.question for c in draft.claims} - questions:
                    raise ValueError("Unknown required question")
            for review in case.reviews:
                if review.draft not in drafts:
                    raise ValueError("Review must name a saved draft")
                reviewed.add(review.draft)
                claims = {c.id for c in drafts[review.draft].claims}
                for issue in review.issues:
                    if issue.id in issues:
                        raise ValueError("Duplicate issue occurrence")
                    if issue.claim not in claims:
                        raise ValueError("Issue must name a claim in its reviewed draft")
                    issues[issue.id] = issue
                    issue_drafts[issue.id] = drafts[review.draft]
            for response in case.responses:
                if response.draft not in drafts or response.issue not in issues:
                    raise ValueError("Response must name a saved draft and issue")
                if response.disposition == "unstructured" and response.evidence:
                    raise ValueError("Free-form response is not issue-specific evidence")
            for ruling in case.rulings:
                if ruling.issue not in issues or ruling.snapshot not in self.snapshots:
                    raise ValueError("Ruling must name an issue and snapshot")
                if ruling.claim != issues[ruling.issue].claim:
                    raise ValueError("Ruling must name the disputed claim")
                disputed_draft = issue_drafts[ruling.issue]
                disputed_claim = next(c for c in disputed_draft.claims if c.id == ruling.claim)
                if (
                    ruling.snapshot != disputed_draft.snapshot
                    or ruling.claim_text != disputed_claim.text
                ):
                    raise ValueError("Ruling scope must match the reviewed claim and snapshot")
                if (ruling.decision == "narrow") != (ruling.replacement is not None):
                    raise ValueError("Only narrowing rulings require replacement text")
            rulings = {r.id: r for r in case.rulings}
            draft_order = {draft.id: i for i, draft in enumerate(case.drafts)}
            for draft in case.drafts:
                for ruling_id in draft.after_rulings:
                    if ruling_id not in rulings:
                        raise ValueError("Draft must name an existing prior ruling")
                    disputed_draft = issue_drafts[rulings[ruling_id].issue]
                    if draft_order[draft.id] <= draft_order[disputed_draft.id]:
                        raise ValueError("Post-ruling draft must follow the disputed draft")
            citations = [e for i in issues.values() for e in i.evidence]
            citations += [e for r in case.responses for e in r.evidence]
            if any(e.version not in self.versions for e in citations):
                raise ValueError("Citation must name a known version (possibly excluded or wrong)")
            keys = [op.key for op in case.operations]
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate operation key")
            for op in case.operations:
                if (op.state == "committed") != (op.result is not None):
                    raise ValueError("Only committed operations have a saved result")
                result_ids = {
                    "author": set(drafts),
                    "reviewer": {r.id for r in case.reviews},
                    "adjudicator": set(rulings),
                }
                if op.result is not None and op.result not in result_ids[op.role]:
                    raise ValueError("Operation result must name a saved record for its role")
                if op.state != "committed" and op.used_tokens:
                    raise ValueError("Uncommitted operation usage is unknown, not a saved charge")
            visible = case.expected.visible_draft
            if visible is not None and visible not in reviewed:
                raise ValueError("Visible answer must have a saved review")
            if set(case.expected.actions) & set(case.expected.forbidden):
                raise ValueError("Expected and forbidden actions overlap")
        return self


def load_corpus() -> Corpus:
    path = Path(__file__).parent / "fixtures" / "review_invariants.json"
    return Corpus.model_validate(json.loads(path.read_text()))
