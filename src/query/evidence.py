"""Exact supplied text and immutable evidence selection; no I/O or model policy."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from query.investigation import Contract

Positive = Annotated[int, Field(gt=0, strict=True)]
Nonnegative = Annotated[int, Field(ge=0, strict=True)]


class EvidenceContract(Contract):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=False)

    @field_validator("*", mode="after")
    @classmethod
    def representable_text(cls, value):
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("NUL is not supported in saved text or metadata")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("Text must be valid UTF-8 without unpaired surrogates") from exc
        return value


class IngestText(EvidenceContract):
    operation_key: str = Field(min_length=1)
    document_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    origin_url: str | None = None
    observed_at: AwareDatetime
    published_at: AwareDatetime | None = None
    text: str = Field(strict=True)
    max_bytes: Positive = 1024 * 1024

    @field_validator("operation_key", "document_key", "title")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Identity and title must not be blank")
        return value


class TextIdentity(EvidenceContract):
    content_hash: str
    byte_count: Nonnegative
    codepoint_count: Nonnegative


def text_identity(text: str) -> TextIdentity:
    encoded = text.encode("utf-8")
    return TextIdentity(
        content_hash=hashlib.sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        codepoint_count=len(text),
    )


def ingest_identity(request: IngestText) -> dict:
    fields = request.model_dump(mode="json", exclude={"text"})
    return {**fields, **text_identity(request.text).model_dump()}


def fingerprint(fields: dict) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


class Passage(EvidenceContract):
    id: UUID
    version_id: UUID
    start: Nonnegative
    end: Positive

    @model_validator(mode="after")
    def valid_range(self):
        if self.end <= self.start:
            raise ValueError("Passage end must follow start")
        return self


def passage_ranges(text: str) -> tuple[tuple[int, int], ...]:
    return tuple((start, min(start + 2000, len(text))) for start in range(0, len(text), 2000))


class SourceVersion(EvidenceContract):
    id: UUID
    source_id: UUID
    document_key: str
    title: str
    origin_url: str | None
    content_hash: str
    text: str
    byte_count: Nonnegative
    codepoint_count: Nonnegative
    partition_version: Literal[1] = 1
    passages: tuple[Passage, ...]

    @model_validator(mode="after")
    def exact_partition(self):
        if text_identity(self.text).model_dump() != {
            "content_hash": self.content_hash,
            "byte_count": self.byte_count,
            "codepoint_count": self.codepoint_count,
        }:
            raise ValueError("Saved text does not match its identity")
        if tuple((p.start, p.end) for p in self.passages) != passage_ranges(self.text):
            raise ValueError("Saved passages do not partition the text")
        if any(p.version_id != self.id for p in self.passages):
            raise ValueError("Passage belongs to another version")
        return self

    def passage_text(self, passage: Passage) -> str:
        if passage not in self.passages:
            raise ValueError("Passage does not belong to this version")
        return self.text[passage.start : passage.end]


class EvidenceInput(EvidenceContract):
    investigation_id: UUID
    operation_key: str
    brief_version: Positive
    revision: Positive
    document_key: str
    title: str
    origin_url: str | None
    observed_at: AwareDatetime
    published_at: AwareDatetime | None
    content_hash: str
    byte_count: Nonnegative
    codepoint_count: Nonnegative
    max_bytes: Positive
    status: Literal["accepted", "rejected"]
    version_id: UUID | None
    reason: Literal["empty_document", "document_too_large"] | None

    @model_validator(mode="after")
    def valid_disposition(self):
        if (self.status == "accepted") != (self.version_id is not None):
            raise ValueError("Only accepted inputs reference a version")
        if (self.status == "rejected") != (self.reason is not None):
            raise ValueError("Only rejected inputs have a rejection reason")
        return self


class CandidateSelection(EvidenceContract):
    version_id: UUID
    passage_ids: tuple[UUID, ...] | None = None
    exclusion_reason: str | None = None

    @model_validator(mode="after")
    def valid_selection(self):
        if self.passage_ids is not None and len(set(self.passage_ids)) != len(self.passage_ids):
            raise ValueError("Duplicate passage selection")
        if self.exclusion_reason is not None and not self.exclusion_reason.strip():
            raise ValueError("Exclusion reason must not be blank")
        return self


class FreezeSnapshot(EvidenceContract):
    operation_key: str = Field(min_length=1)
    candidates: tuple[CandidateSelection, ...]
    max_bytes: Positive = 256 * 1024

    @model_validator(mode="after")
    def unique_candidates(self):
        if not self.operation_key.strip():
            raise ValueError("Operation key must not be blank")
        if len({c.version_id for c in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate candidate version")
        return self


class PassageDisposition(EvidenceContract):
    passage_id: UUID
    start: Nonnegative
    end: Positive
    included: bool
    reason: str | None


class SnapshotCandidate(EvidenceContract):
    version_id: UUID
    observation_keys: tuple[str, ...]
    passages: tuple[PassageDisposition, ...]


class EvidenceSnapshot(EvidenceContract):
    id: UUID
    investigation_id: UUID
    operation_key: str
    brief_version: Positive
    revision: Positive
    format_version: Literal[1] = 1
    status: Literal["ready", "limited"]
    reason: Literal["snapshot_too_large", "empty_selection"] | None
    candidates: tuple[SnapshotCandidate, ...]
    rejected_inputs: tuple[EvidenceInput, ...]
    requested_bytes: Nonnegative
    included_bytes: Nonnegative
    max_bytes: Positive


class LoadedSnapshot(EvidenceContract):
    snapshot: EvidenceSnapshot
    versions: tuple[SourceVersion, ...]
    observations: tuple[EvidenceInput, ...]


def select_passages(
    selection: CandidateSelection, version: SourceVersion
) -> tuple[PassageDisposition, ...]:
    if selection.version_id != version.id:
        raise ValueError("Selection names a different version")
    known = {p.id for p in version.passages}
    selected = known if selection.passage_ids is None else set(selection.passage_ids)
    if not selected <= known:
        raise ValueError("Unknown or foreign passage")
    if selected != known and not selection.exclusion_reason:
        raise ValueError("Partial or empty selection requires an exclusion reason")
    return tuple(
        PassageDisposition(
            passage_id=p.id,
            start=p.start,
            end=p.end,
            included=p.id in selected,
            reason=None if p.id in selected else selection.exclusion_reason,
        )
        for p in version.passages
    )
