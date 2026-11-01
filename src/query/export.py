"""Self-contained audit export of the currently available investigation records."""

from typing import Literal

from query.answer_loop import LoopState, ReviewedAnswer
from query.evidence import EvidenceInput, EvidenceSnapshot, SourceVersion
from query.investigation import Contract, Investigation
from query.recovery import ReviewAttempt


class InvestigationExport(Contract):
    format_version: Literal[1] = 1
    investigation: Investigation
    evidence_inputs: tuple[EvidenceInput, ...]
    source_versions: tuple[SourceVersion, ...]
    snapshots: tuple[EvidenceSnapshot, ...]

    answer_loop: LoopState | None = None
    last_reviewed_answer: ReviewedAnswer | None = None
    review_attempts: tuple[ReviewAttempt, ...] = ()
