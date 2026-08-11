from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_controller.protocols.agent import AgentResult, DecisionResult


SCHEMA_ADAPTER_VERSION = "codex-structured-schema/v0.1"


class CodexWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodexProducedArtifactWire(CodexWireModel):
    logical_name: str
    path: str
    artifact_kind: str
    evidence_candidate: bool
    metadata_json: str | None


class CodexRequestedTaskWire(CodexWireModel):
    action: str
    role: str
    objective: str
    reason: str
    inputs: list[str]


class CodexTransitionRequestWire(CodexWireModel):
    schema_version: Literal["transition-request/v0.1"]
    project_id: str
    from_stage: str
    to_stage: str
    reason: str
    evidence_artifact_ids: list[str]
    asserted_preconditions: list[str]


class CodexProtocolAmendmentRequestWire(CodexWireModel):
    reason: str
    proposed_changes_json: str | None


class CodexAgentResultWire(CodexWireModel):
    schema_version: Literal["agent-result/v0.1"]
    task_id: str
    outcome: Literal["completed", "partial", "blocked", "failed"]
    summary: str
    artifacts: list[CodexProducedArtifactWire]
    warnings: list[str]
    requested_tasks: list[CodexRequestedTaskWire]
    transition_request: CodexTransitionRequestWire | None
    protocol_amendment_request: CodexProtocolAmendmentRequestWire | None
    needs_escalation: bool
    escalation: str | None


class CodexDecisionResultWire(CodexWireModel):
    schema_version: Literal["decision-result/v0.1"]
    decision: str
    confidence: float = Field(ge=0, le=1)
    rationale: str
    evidence_used: list[str]
    missing_information: list[str]
    requested_tasks: list[CodexRequestedTaskWire]
    transition_request: CodexTransitionRequestWire | None


@dataclass(frozen=True)
class SchemaNode:
    pointer: str
    value: dict[str, Any]


@dataclass(frozen=True)
class SchemaCompatibilityIssue:
    pointer: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "pointer": self.pointer,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class SchemaCompatibilityReport:
    compatible: bool
    issues: tuple[SchemaCompatibilityIssue, ...]
    object_count: int
    definition_count: int
    top_level_properties: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "issues": [item.as_dict() for item in self.issues],
            "object_count": self.object_count,
            "definition_count": self.definition_count,
            "top_level_properties": list(self.top_level_properties),
        }


class CodexSchemaCompatibilityError(ValueError):
    def __init__(self, issues: list[SchemaCompatibilityIssue]) -> None:
        self.issues = tuple(issues)
        details = "; ".join(
            f"{item.pointer}: {item.code}: {item.message}" for item in issues
        )
        super().__init__(details or "Codex output schema is incompatible")


class CodexStructuredResultError(ValueError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class StructuredContract:
    schema_adapter_version: str
    domain_model: str
    wire_model: str
    domain_schema: dict[str, Any]
    wire_schema: dict[str, Any]
    codex_schema: dict[str, Any]
    domain_schema_hash: str
    wire_schema_hash: str
    codex_schema_hash: str
    compatibility_report: SchemaCompatibilityReport

    def introspection(self, *, include_schema: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_adapter_version": self.schema_adapter_version,
            "domain_model": self.domain_model,
            "wire_model": self.wire_model,
            "domain_schema_hash": self.domain_schema_hash,
            "wire_schema_hash": self.wire_schema_hash,
            "codex_schema_hash": self.codex_schema_hash,
            "compatibility_report": self.compatibility_report.as_dict(),
        }
        if include_schema:
            value["codex_schema"] = self.codex_schema
        return value


ANNOTATION_ONLY_KEYWORDS = frozenset(
    {
        "title",
        "examples",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)
PROHIBITED_WIRE_KEYWORDS = ANNOTATION_ONLY_KEYWORDS | {"format"}


def schema_sha256(schema: dict[str, Any]) -> str:
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _escape_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def walk_schema(schema: dict[str, Any]) -> Iterator[SchemaNode]:
    """Yield schema dictionaries with stable JSON pointers."""

    def visit(value: Any, pointer: str, *, schema_position: bool) -> Iterator[SchemaNode]:
        if isinstance(value, dict):
            if schema_position:
                yield SchemaNode(pointer, value)
            for key, child in value.items():
                child_pointer = f"{pointer}.{_escape_pointer(str(key))}"
                if key in {"properties", "$defs", "definitions"} and isinstance(child, dict):
                    for name, nested in child.items():
                        yield from visit(
                            nested,
                            f"{child_pointer}.{_escape_pointer(str(name))}",
                            schema_position=True,
                        )
                elif key in {"items", "additionalProperties", "not", "if", "then", "else"}:
                    yield from visit(child, child_pointer, schema_position=True)
                elif key in {"anyOf", "oneOf", "allOf", "prefixItems"} and isinstance(child, list):
                    for index, nested in enumerate(child):
                        yield from visit(nested, f"{child_pointer}[{index}]", schema_position=True)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from visit(child, f"{pointer}[{index}]", schema_position=schema_position)

    yield from visit(schema, "$", schema_position=True)


def _strip_annotation_only(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_annotation_only(child)
            for key, child in value.items()
            if key not in ANNOTATION_ONLY_KEYWORDS
        }
    if isinstance(value, list):
        return [_strip_annotation_only(item) for item in value]
    return value


def project_codex_wire_schema(wire_model: type[BaseModel]) -> dict[str, Any]:
    """Project an explicit conservative wire model into the Codex schema subset.

    This intentionally does not accept a domain model. Semantic constraints are
    preserved; only annotation-only Pydantic keywords are removed.
    """

    raw = wire_model.model_json_schema(mode="validation")
    projected = _strip_annotation_only(raw)
    if not isinstance(projected, dict):  # pragma: no cover - Pydantic invariant
        raise TypeError("wire schema root must be an object")
    return projected


def validate_codex_output_schema(schema: dict[str, Any]) -> SchemaCompatibilityReport:
    issues: list[SchemaCompatibilityIssue] = []
    object_count = 0
    for node in walk_schema(schema):
        value = node.value
        for keyword in sorted(PROHIBITED_WIRE_KEYWORDS & value.keys()):
            issues.append(
                SchemaCompatibilityIssue(
                    pointer=f"{node.pointer}.{keyword}",
                    code="UNSUPPORTED_ANNOTATION",
                    message=f"{keyword} is not permitted in the Codex wire schema",
                )
            )
        is_object = value.get("type") == "object" or isinstance(
            value.get("properties"), dict
        )
        if is_object:
            object_count += 1
            properties = value.get("properties")
            if not isinstance(properties, dict):
                issues.append(
                    SchemaCompatibilityIssue(
                        pointer=node.pointer,
                        code="OPEN_OR_UNTYPED_OBJECT",
                        message="wire objects must declare a fixed properties mapping",
                    )
                )
            if value.get("additionalProperties") is not False:
                issues.append(
                    SchemaCompatibilityIssue(
                        pointer=f"{node.pointer}.additionalProperties",
                        code="OBJECT_NOT_CLOSED",
                        message="every wire object must set additionalProperties=false",
                    )
                )
            if isinstance(properties, dict):
                required = value.get("required")
                if not isinstance(required, list) or set(required) != set(properties):
                    issues.append(
                        SchemaCompatibilityIssue(
                            pointer=f"{node.pointer}.required",
                            code="NONDETERMINISTIC_REQUIRED_FIELDS",
                            message="every declared wire property must be required; use null for optional semantics",
                        )
                    )
        if "additionalProperties" in value and value["additionalProperties"] is not False:
            issues.append(
                SchemaCompatibilityIssue(
                    pointer=f"{node.pointer}.additionalProperties",
                    code="OPEN_MAPPING",
                    message="arbitrary mappings are forbidden in strict wire output",
                )
            )
    # Stable de-duplication keeps reports useful when an object violates both
    # the closure and open-mapping checks at the same pointer.
    deduped = list(
        {
            (item.pointer, item.code, item.message): item for item in issues
        }.values()
    )
    report = SchemaCompatibilityReport(
        compatible=not deduped,
        issues=tuple(deduped),
        object_count=object_count,
        definition_count=len(schema.get("$defs", {})),
        top_level_properties=tuple(schema.get("properties", {}).keys()),
    )
    if deduped:
        raise CodexSchemaCompatibilityError(deduped)
    return report


def _decode_json_object(value: str | None, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CodexStructuredResultError(
            "INVALID_CODEX_WIRE_METADATA",
            f"{field_name} is not valid JSON: {exc}",
        ) from exc
    if not isinstance(decoded, dict):
        raise CodexStructuredResultError(
            "INVALID_CODEX_WIRE_METADATA",
            f"{field_name} must decode to a JSON object",
        )
    return decoded


def _requested_task_domain(value: CodexRequestedTaskWire) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _transition_domain(value: CodexTransitionRequestWire | None) -> dict[str, Any] | None:
    return value.model_dump(mode="json") if value is not None else None


class CodexStructuredOutputAdapter:
    schema_version = SCHEMA_ADAPTER_VERSION

    def _contract(
        self, domain_model: type[BaseModel], wire_model: type[BaseModel]
    ) -> StructuredContract:
        domain_schema = domain_model.model_json_schema(mode="validation")
        wire_schema = wire_model.model_json_schema(mode="validation")
        codex_schema = project_codex_wire_schema(wire_model)
        report = validate_codex_output_schema(codex_schema)
        return StructuredContract(
            schema_adapter_version=self.schema_version,
            domain_model=domain_model.__name__,
            wire_model=wire_model.__name__,
            domain_schema=domain_schema,
            wire_schema=wire_schema,
            codex_schema=codex_schema,
            domain_schema_hash=schema_sha256(domain_schema),
            wire_schema_hash=schema_sha256(wire_schema),
            codex_schema_hash=schema_sha256(codex_schema),
            compatibility_report=report,
        )

    def for_agent_result(self) -> StructuredContract:
        return self._contract(AgentResult, CodexAgentResultWire)

    def for_decision_result(self) -> StructuredContract:
        return self._contract(DecisionResult, CodexDecisionResultWire)

    def parse_agent_result(
        self, payload: object, *, expected_task_id: str | None = None
    ) -> AgentResult:
        try:
            wire = CodexAgentResultWire.model_validate(payload)
        except ValidationError as exc:
            raise CodexStructuredResultError(
                "INVALID_CODEX_WIRE_RESULT", str(exc)
            ) from exc
        artifacts = [
            {
                "logical_name": item.logical_name,
                "path": item.path,
                "artifact_kind": item.artifact_kind,
                "evidence_candidate": item.evidence_candidate,
                "metadata": _decode_json_object(
                    item.metadata_json,
                    field_name=f"artifacts[{index}].metadata_json",
                ),
            }
            for index, item in enumerate(wire.artifacts)
        ]
        amendment = None
        if wire.protocol_amendment_request is not None:
            amendment = {
                "reason": wire.protocol_amendment_request.reason,
                "proposed_changes": _decode_json_object(
                    wire.protocol_amendment_request.proposed_changes_json,
                    field_name="protocol_amendment_request.proposed_changes_json",
                ),
            }
        domain_value = {
            "schema_version": wire.schema_version,
            "task_id": wire.task_id,
            "outcome": wire.outcome,
            "summary": wire.summary,
            "artifacts": artifacts,
            "warnings": wire.warnings,
            "requested_tasks": [
                _requested_task_domain(item) for item in wire.requested_tasks
            ],
            "transition_request": _transition_domain(wire.transition_request),
            "protocol_amendment_request": amendment,
            "needs_escalation": wire.needs_escalation,
            "escalation": wire.escalation,
        }
        try:
            result = AgentResult.model_validate(domain_value)
        except ValidationError as exc:
            raise CodexStructuredResultError(
                "INVALID_CODEX_DOMAIN_RESULT", str(exc)
            ) from exc
        if expected_task_id is not None and result.task_id != expected_task_id:
            raise CodexStructuredResultError(
                "CODEX_RESULT_TASK_MISMATCH",
                f"wire task_id {result.task_id!r} does not match {expected_task_id!r}",
            )
        return result

    def parse_decision_result(self, payload: object) -> DecisionResult:
        try:
            wire = CodexDecisionResultWire.model_validate(payload)
            return DecisionResult.model_validate(
                {
                    "schema_version": wire.schema_version,
                    "decision": wire.decision,
                    "confidence": wire.confidence,
                    "rationale": wire.rationale,
                    "evidence_used": wire.evidence_used,
                    "missing_information": wire.missing_information,
                    "requested_tasks": [
                        _requested_task_domain(item) for item in wire.requested_tasks
                    ],
                    "transition_request": _transition_domain(wire.transition_request),
                }
            )
        except ValidationError as exc:
            raise CodexStructuredResultError(
                "INVALID_CODEX_DECISION_RESULT", str(exc)
            ) from exc


def native_output_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Compatibility entry point restricted to the two supported contracts."""
    adapter = CodexStructuredOutputAdapter()
    if model is AgentResult:
        return adapter.for_agent_result().codex_schema
    if model is DecisionResult:
        return adapter.for_decision_result().codex_schema
    raise TypeError(f"Codex structured output does not support {model.__name__}")
