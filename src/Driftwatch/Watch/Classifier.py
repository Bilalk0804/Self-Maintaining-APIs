"""Validate evidence-grounded change classifications at the LLM boundary."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from Driftwatch.Watch.Differ import Delta, Severity
from Driftwatch.Watch.Evidence import Evidence

ChangeType = Literal[
    "operation_added",
    "operation_removed",
    "operation_moved",
    "operation_deprecated",
    "param_added_optional",
    "param_added_required",
    "param_removed",
    "param_renamed",
    "param_type_changed",
    "param_deprecated",
    "response_field_removed",
    "response_field_renamed",
    "response_field_type_changed",
    "enum_value_added",
    "enum_value_removed",
    "default_changed",
    "model_deprecated",
    "auth_scheme_changed",
    "error_code_changed",
    "rate_limit_changed",
    "webhook_event_added",
    "webhook_event_removed",
    "sdk_signature_changed",
    "version_pin_required",
]
Enforcement = Literal["hard", "soft", "unknown"]
RecipeKind = Literal[
    "rename_param",
    "rename_response_field",
    "replace_model_id",
    "replace_enum_value",
    "operation_moved",
    "insert_required_param",
    "manual_only",
]


class MigrationRecipeOut(BaseModel):
    """A bounded migration proposal returned by the classifier."""

    model_config = ConfigDict(extra="forbid")

    kind: RecipeKind
    params: dict[str, Any] = Field(default_factory=dict)
    language: Literal["python", "typescript"] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str | None = None


class EvidenceCitation(BaseModel):
    """A model citation that must be checked against retrieved evidence."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["raw_delta", "provider_source"]
    ref: str
    quote: str = Field(min_length=8)


class ChangeRecordOut(BaseModel):
    """Structured classifier output before deterministic grounding checks."""

    model_config = ConfigDict(extra="forbid")

    change_type: ChangeType
    severity: Severity
    surface_refs: list[str] = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1)
    announced_on: date | None = None
    effective_on: date | None = None
    deadline_on: date | None = None
    enforcement: Enforcement = "unknown"
    code_probes: list[str] = Field(min_length=1)
    recipes: list[MigrationRecipeOut] = Field(default_factory=list)
    evidence: list[EvidenceCitation] = Field(min_length=1)


class ModelClient(Protocol):
    """Minimal structured-output model boundary."""

    def classify(self, delta: Delta, evidence: list[Evidence]) -> dict[str, Any]:
        """Return untrusted structured data for one delta."""


class ClassificationQuarantined(ValueError):
    """Raised when a model result cannot be trusted as grounded output."""


class EvidenceGroundedClassifier:
    """Classify deltas while mechanically rejecting unsupported model claims."""

    def __init__(self, client: ModelClient) -> None:
        self._client = client

    def classify(self, delta: Delta, evidence: list[Evidence]) -> ChangeRecordOut:
        """Return a validated record or quarantine the model response."""
        try:
            record = ChangeRecordOut.model_validate(self._client.classify(delta, evidence))
        except ValueError as exc:
            raise ClassificationQuarantined("classifier output failed schema validation") from exc

        self._validate_grounding(record, delta, evidence)
        return self._normalise_deadline(record)

    @staticmethod
    def _validate_grounding(
        record: ChangeRecordOut, delta: Delta, evidence: list[Evidence]
    ) -> None:
        """Reject fabricated quotes and surfaces outside the detected delta."""
        evidence_by_ref = {item.ref: item for item in evidence}
        valid_surfaces = {delta.surface_ref}
        if delta.renamed_to:
            valid_surfaces.add(delta.renamed_to)
        for citation in record.evidence:
            source = evidence_by_ref.get(citation.ref)
            if source is None or citation.quote not in source.quote:
                raise ClassificationQuarantined(
                    f"evidence citation is not grounded: {citation.ref}"
                )
        if not set(record.surface_refs).issubset(valid_surfaces):
            raise ClassificationQuarantined("classifier cited an undetected surface")
        if record.change_type != delta.change_type:
            raise ClassificationQuarantined(
                f"classifier changed deterministic type from {delta.change_type!r}"
            )
        if record.severity != delta.severity:
            raise ClassificationQuarantined(
                f"classifier changed deterministic severity from {delta.severity!r}"
            )

    @staticmethod
    def _normalise_deadline(record: ChangeRecordOut) -> ChangeRecordOut:
        """Drop stale or implausibly distant deadlines without trusting the model."""
        today = date.today()
        deadline = record.deadline_on
        if deadline is None or today < deadline <= today + timedelta(days=365 * 5):
            return record
        return record.model_copy(update={"deadline_on": None})
