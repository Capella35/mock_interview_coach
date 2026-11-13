"""Pydantic contracts. Model output that does not validate against these is rejected."""

from interview_agent.schemas.models import (
    CANDIDATE_DOC_KINDS,
    Discrepancy,
    DocKind,
    DocumentRef,
    Fact,
    FactCategory,
    FactSheet,
    Grounded,
    JobRequirement,
    MatchEntry,
    MatchGapMap,
    MatchStatus,
    Metric,
    QuestionAngle,
    RequirementKind,
    SourceDocument,
)
from interview_agent.schemas.proposals import FactProposal, MetricProposal, RequirementProposal

__all__ = [
    "CANDIDATE_DOC_KINDS",
    "Discrepancy",
    "DocKind",
    "DocumentRef",
    "Fact",
    "FactCategory",
    "FactProposal",
    "FactSheet",
    "Grounded",
    "JobRequirement",
    "MatchEntry",
    "MatchGapMap",
    "MatchStatus",
    "Metric",
    "MetricProposal",
    "QuestionAngle",
    "RequirementKind",
    "RequirementProposal",
    "SourceDocument",
]
