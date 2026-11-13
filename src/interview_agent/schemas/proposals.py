"""What the LLM is allowed to return during extraction (step 5).

Proposals carry a `quote` copied from the document but NO offsets, ids or pages —
those are computed by code in grounding.py. A proposal is untrusted until
grounding turns it into a Fact / JobRequirement.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from interview_agent.schemas.models import FactCategory, NonEmptyStr, RequirementKind


class _Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricProposal(_Proposal):
    raw: NonEmptyStr  # exactly as written in the quote
    value: float | None = None
    unit: str | None = None
    measures: NonEmptyStr


class FactProposal(_Proposal):
    quote: NonEmptyStr  # verbatim text from the candidate document
    claim: NonEmptyStr
    category: FactCategory
    metric: MetricProposal | None = None
    skills: list[NonEmptyStr] = Field(default_factory=list)
    time_period: str | None = None


class RequirementProposal(_Proposal):
    quote: NonEmptyStr  # verbatim text from the job description
    requirement: NonEmptyStr
    kind: RequirementKind
    skills: list[NonEmptyStr] = Field(default_factory=list)
