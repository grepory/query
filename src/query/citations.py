"""Pure quote occurrence checks against a named immutable selected passage."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from query.evidence import EvidenceContract
from query.review import Citation, EvidenceCheck, ReviewContext, ReviewContract

# Adapted from aibyai ce28ff41:src/services/evidence.py; no punctuation stripping.
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_ELISION = re.compile(r"\[(?:\.\.\.|…)\]|\.\.\.|…")
_NUMBER = re.compile(r"[+\-−±]?(?:\d+(?:[.,/⁄]\d+)*|[.,]\d+)(?:e[+\-−]?\d+)?[%‰‱]?")


def normalize(text: str) -> str:
    """NFKC, typographic quotes, case and whitespace; preserve other punctuation."""
    return " ".join(unicodedata.normalize("NFKC", text).translate(_QUOTES).lower().split())


def _word(char: str) -> bool:
    return bool(char) and (
        char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")
    )


def _boundary(text: str, start: int, end: int, before: str, after: str) -> bool:
    surrounding = before + text + after
    a, b = len(before) + start, len(before) + end
    left = surrounding[a - 1] if a else ""
    right = surrounding[b] if b < len(surrounding) else ""
    if _word(text[start]) and _word(left):
        return False
    if _word(text[end - 1]) and _word(right):
        return False
    # Neither boundary may bisect a numeric token, even at punctuation or a sign.
    for number in _NUMBER.finditer(surrounding):
        number_start = number.start()
        while number_start and (
            unicodedata.category(surrounding[number_start - 1]) in {"Sc", "Pd"}
            or surrounding[number_start - 1] in "+−±"
        ):
            number_start -= 1
        if number_start < a < number.end() or number_start < b < number.end():
            return False
    return True


def _find(text: str, excerpt: str, offset: int, before: str, after: str) -> int | None:
    start = text.find(excerpt, offset)
    while start != -1:
        if _boundary(text, start, start + len(excerpt), before, after):
            return start
        start = text.find(excerpt, start + 1)
    return None


class SavedPassage(EvidenceContract):
    version_id: UUID
    passage_id: UUID
    start: int
    end: int
    text: str


class CitationResolution(ReviewContract):
    snapshot_id: UUID
    brief_version: int
    check: EvidenceCheck
    match_kind: Literal["contiguous", "ordered_elision"] | None
    passage: SavedPassage | None


@dataclass(frozen=True)
class _Entry:
    saved: SavedPassage
    normalized: str
    before: str
    after: str
    included: bool


class CitationIndex:
    """One frozen context; never searches other sources or generated summaries."""

    def __init__(self, context: ReviewContext):
        context = ReviewContext.model_validate(context.model_dump())
        self.snapshot_id = context.evidence.snapshot.id
        self.brief_version = context.brief.version
        self._limited = context.evidence.snapshot.status == "limited"
        versions = {v.id: v for v in context.evidence.versions}
        self._entries: dict[tuple[UUID, UUID], _Entry] = {}
        self._versions = set(versions)
        for candidate in context.evidence.snapshot.candidates:
            version = versions[candidate.version_id]
            for disposition in candidate.passages:
                saved = SavedPassage(
                    version_id=version.id,
                    passage_id=disposition.passage_id,
                    start=disposition.start,
                    end=disposition.end,
                    text=version.text[disposition.start : disposition.end],
                )

                def edge(text):
                    return re.sub(
                        r"\s+", " ", unicodedata.normalize("NFKC", text).translate(_QUOTES).lower()
                    )

                before = edge(version.text[max(0, disposition.start - 4) : disposition.start])[-4:]
                after = edge(version.text[disposition.end : disposition.end + 4])[:4]
                canonical = edge(saved.text)
                if canonical[:1].isspace():
                    before = " "
                if canonical[-1:].isspace():
                    after = " "
                self._entries[(version.id, disposition.passage_id)] = _Entry(
                    saved,
                    normalize(saved.text),
                    before,
                    after,
                    disposition.included,
                )

    def resolve(self, citation: Citation) -> CitationResolution:
        citation = Citation.model_validate(citation.model_dump())
        entry = self._entries.get((citation.version_id, citation.passage_id))
        reason = None
        kind = None
        if self._limited:
            reason = "snapshot_limited"
        elif citation.version_id not in self._versions:
            reason = "unknown_source_version"
        elif entry is None:
            reason = "passage_not_in_named_version"
        elif not entry.included:
            reason = "passage_excluded"
        else:
            normalized = normalize(citation.excerpt)
            if _find(entry.normalized, normalized, 0, entry.before, entry.after) is not None:
                kind = "contiguous"
            else:
                # Elisions are explicit, ordered, nonoverlapping and confined to this passage.
                pieces = _ELISION.split(citation.excerpt)
                fragments = [normalize(piece) for piece in pieces]
                if len(fragments) > 1 and all(fragments):
                    offset = 0
                    for fragment in fragments:
                        start = _find(entry.normalized, fragment, offset, entry.before, entry.after)
                        if start is None:
                            break
                        offset = start + len(fragment)
                    else:
                        kind = "ordered_elision"
            if kind is None:
                reason = "quote_not_found_in_named_passage"
        check = EvidenceCheck(
            citation=citation, status="verified" if kind else "not_found", reason=reason
        )
        return CitationResolution(
            snapshot_id=self.snapshot_id,
            brief_version=self.brief_version,
            check=check,
            match_kind=kind,
            passage=entry.saved if kind and entry else None,
        )

    def check(self, citation: Citation) -> EvidenceCheck:
        return self.resolve(citation).check

    def check_all(self, citations) -> tuple[EvidenceCheck, ...]:
        return tuple(self.check(citation) for citation in citations)
