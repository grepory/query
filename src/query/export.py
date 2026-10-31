"""Self-contained audit export of the currently available investigation records."""

from typing import Literal

from query.evidence import EvidenceInput, EvidenceSnapshot, SourceVersion
from query.investigation import Contract, Investigation


class InvestigationExport(Contract):
    format_version: Literal[1] = 1
    investigation: Investigation
    evidence_inputs: tuple[EvidenceInput, ...]
    source_versions: tuple[SourceVersion, ...]
    snapshots: tuple[EvidenceSnapshot, ...]
