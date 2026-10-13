"""Prompt-boundary and structured-response validation.

This module deliberately reads only approved prompt metadata.  It never opens raw
participant files, locked results, or experimental-result artifacts.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[3]
HELDOUT_R_ITEMS = ("R3", "R6", "R8", "R12", "R13", "R17", "R24", "R41")
HUMAN_OBSERVED_PROVENANCE = "human_observed"
R48_AMOUNT_BY_CODE = {
    1: 100_000,
    2: 80_000,
    3: 60_000,
    4: 40_000,
    5: 20_000,
    6: 0,
}
# User-approved high-confidence cross-source mapping.  The PSM appendix supplies
# the wording; the official Federal Reserve Q84 analogue supplies 1=Yes/2=No.
# The released PSM DTA does not expose an exact v_108 value label, so the mapping
# must never be described as a direct released-value-label confirmation.
R50_CODE_TO_LABEL: dict[int, str] = {1: "Yes", 2: "No"}
R50_RENDERED_RESPONSE_BY_CODE: dict[int, str] = {
    1: "Yes, you would be willing to take the new job.",
    2: "No, you would not be willing to take the new job.",
}

CONDITION_ALIASES = {
    "c1": "C1_broad_condition",
    "c1_broad_condition": "C1_broad_condition",
    "broad": "C1_broad_condition",
    "broad_condition": "C1_broad_condition",
    "c2": "C2_strict_condition",
    "c2_strict_condition": "C2_strict_condition",
    "strict": "C2_strict_condition",
    "strict_condition": "C2_strict_condition",
    "c3": "C3_task_proximal_condition",
    "c3_task_proximal_condition": "C3_task_proximal_condition",
    "task_proximal": "C3_task_proximal_condition",
    "task_proximal_condition": "C3_task_proximal_condition",
}

_R_ITEM_PATTERN = re.compile(r"(?<![A-Za-z0-9_])R(?:[1-9]|[1-4][0-9]|50)(?![0-9])")
_FORBIDDEN_TEXT_PATTERNS = (
    (re.compile(r"x[\s_-]*risk[\s_-]*av", re.IGNORECASE), "protected target field"),
    (re.compile(r"\boutcomes?\b", re.IGNORECASE), "human result field"),
    (
        re.compile(r"\b(?:experiment|experimental)\s+results?\b", re.IGNORECASE),
        "experiment result",
    ),
    (re.compile(r"實驗結果"), "experiment result"),
    (re.compile(r"participant[\s_-]*id", re.IGNORECASE), "participant identifier field"),
    (re.compile(r'"delta"\s*:', re.IGNORECASE), "internal list delta"),
    (re.compile(r'"reverse_coding"\s*:', re.IGNORECASE), "coding-direction metadata"),
)


class PromptValidationError(ValueError):
    """Raised when data would cross the predeclared prompt boundary."""


class ResponseValidationError(ValueError):
    """Raised when model text is not the exact required JSON response shape."""


@dataclass(frozen=True)
class ItemSpec:
    """The approved, prompt-safe fields for one questionnaire item."""

    variable_name: str
    r_item: str
    question_text_en: str
    response_scale_min: str
    response_scale_max: str
    response_options: str
    response_type: str
    reverse_coding: str


def _root(project_root: Path | str | None) -> Path:
    return Path(project_root).resolve() if project_root is not None else PROJECT_ROOT


def _read_csv(project_root: Path, relative_path: str) -> list[dict[str, str]]:
    path = project_root / relative_path
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise PromptValidationError(f"Metadata has no header: {relative_path}")
        return list(reader)


def _to_item_spec(row: Mapping[str, str]) -> ItemSpec:
    required = (
        "variable_name",
        "R_item",
        "question_text_en",
        "response_scale_min",
        "response_scale_max",
        "response_options",
        "response_type",
        "reverse_coding",
    )
    missing = [field for field in required if field not in row]
    if missing:
        raise PromptValidationError(f"Item metadata is missing fields: {missing}")
    return ItemSpec(
        variable_name=row["variable_name"],
        r_item=row["R_item"],
        question_text_en=row["question_text_en"],
        response_scale_min=row["response_scale_min"],
        response_scale_max=row["response_scale_max"],
        response_options=row["response_options"],
        response_type=row["response_type"],
        reverse_coding=row["reverse_coding"],
    )


def normalize_condition(condition: str) -> str:
    """Return the canonical approved condition name."""

    try:
        return CONDITION_ALIASES[condition.strip().lower()]
    except (AttributeError, KeyError) as exc:
        allowed = ", ".join(sorted({name for name in CONDITION_ALIASES.values()}))
        raise PromptValidationError(
            f"Unknown condition {condition!r}; expected one of: {allowed}"
        ) from exc


def load_heldout_specs(
    project_root: Path | str | None = None,
) -> tuple[ItemSpec, ...]:
    """Load H8 in its approved order, checking status and exact membership."""

    rows = _read_csv(_root(project_root), "metadata/heldout_items.csv")
    try:
        rows.sort(key=lambda row: int(row["heldout_order"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise PromptValidationError("Held-out metadata has an invalid order field") from exc
    if tuple(row.get("R_item") for row in rows) != HELDOUT_R_ITEMS:
        raise PromptValidationError("Held-out metadata does not match approved H8")
    if any(row.get("approval_status") != "approved" for row in rows):
        raise PromptValidationError("Every H8 item must have approval_status=approved")
    specs = tuple(_to_item_spec(row) for row in rows)
    if len({item.variable_name for item in specs}) != len(specs):
        raise PromptValidationError("H8 variable names are not unique")
    return specs


def load_condition_specs(
    condition: str,
    *,
    include_heldout: bool = True,
    project_root: Path | str | None = None,
) -> tuple[ItemSpec, ...]:
    """Load one approved structural condition from the exclusion manifest."""

    root = _root(project_root)
    canonical = normalize_condition(condition)
    mapping_rows = [
        row
        for row in _read_csv(root, "metadata/item_mapping.csv")
        if row.get("variable_name")
    ]
    exclusion_rows = [
        row
        for row in _read_csv(root, "metadata/exclusion_list.csv")
        if row.get("condition") == canonical
    ]
    if len(exclusion_rows) != len(mapping_rows):
        raise PromptValidationError(
            f"{canonical} has {len(exclusion_rows)} exclusion rows for "
            f"{len(mapping_rows)} mapped items"
        )
    decisions: dict[str, bool] = {}
    for row in exclusion_rows:
        variable = row.get("variable_name", "")
        if variable in decisions:
            raise PromptValidationError(f"Duplicate exclusion decision for {variable}")
        raw = row.get("included", "").lower()
        if raw not in {"true", "false"}:
            raise PromptValidationError(f"Invalid included value for {variable}: {raw!r}")
        decisions[variable] = raw == "true"
    mapping_variables = [row.get("variable_name", "") for row in mapping_rows]
    if set(mapping_variables) != set(decisions):
        raise PromptValidationError("Condition decisions do not cover item_mapping exactly")
    heldout_variables = {item.variable_name for item in load_heldout_specs(root)}
    result = []
    for row in mapping_rows:
        variable = row["variable_name"]
        if not decisions[variable]:
            continue
        if not include_heldout and variable in heldout_variables:
            continue
        result.append(_to_item_spec(row))
    return tuple(result)


def condition_variable_names(
    condition: str,
    *,
    include_heldout: bool = True,
    project_root: Path | str | None = None,
) -> tuple[str, ...]:
    """Expose deterministic condition membership for builders and tests."""

    return tuple(
        item.variable_name
        for item in load_condition_specs(
            condition,
            include_heldout=include_heldout,
            project_root=project_root,
        )
    )


def _json_scalar(value: Any, *, field_name: str) -> str | int | float | bool:
    if hasattr(value, "item") and callable(value.item):
        value = value.item()
    if value is None or not isinstance(value, (str, int, float, bool)):
        raise PromptValidationError(f"{field_name} must be one non-null JSON scalar")
    if isinstance(value, float) and not math.isfinite(value):
        raise PromptValidationError(f"{field_name} must be finite")
    if isinstance(value, str) and not value.strip():
        raise PromptValidationError(f"{field_name} must not be blank")
    return value


def validate_single_participant_payload(
    participant_id: str,
    payload: Any,
) -> None:
    """Reject participant collections or identifiers for anyone else.

    Builders accept only a flat answer mapping, but this recursive guard is kept
    public so callers can validate an envelope before extracting its answer map.
    """

    if not isinstance(participant_id, str) or not participant_id.strip():
        raise PromptValidationError("participant_id must be one non-empty string")
    discovered_ids: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = str(raw_key).strip().lower().replace("-", "_").replace(" ", "_")
                if key in {"participants", "participant_ids", "respondents", "respondent_ids"}:
                    raise PromptValidationError("Cross-participant collections are forbidden")
                if key in {"participant_id", "respondent_id"}:
                    if not isinstance(child, str):
                        raise PromptValidationError("Embedded participant identifiers must be strings")
                    discovered_ids.add(child)
                walk(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                walk(child)

    walk(payload)
    if discovered_ids - {participant_id}:
        raise PromptValidationError("Payload contains another participant identifier")
    if len(discovered_ids) > 1:
        raise PromptValidationError("Payload contains multiple participant identifiers")


def validate_answer_mapping(
    participant_id: str,
    answers: Mapping[str, Any],
    expected_variables: Sequence[str],
) -> dict[str, str | int | float | bool]:
    """Require exactly one scalar answer for every allowed variable and no others."""

    if not isinstance(answers, Mapping):
        raise PromptValidationError("answers must be a mapping for one participant")
    validate_single_participant_payload(participant_id, answers)
    expected = tuple(expected_variables)
    expected_set = set(expected)
    if any(not isinstance(key, str) for key in answers):
        raise PromptValidationError("Every answer key must be a variable-name string")
    actual_set = set(answers)
    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        raise PromptValidationError(
            f"Answer variables do not match the approved set; missing={missing}, extra={extra}"
        )
    return {
        variable: _json_scalar(answers[variable], field_name=variable)
        for variable in expected
    }


def validate_answer_envelope(
    job_participant_id: str,
    envelope: Mapping[str, Any],
    expected_variables: Sequence[str],
    *,
    require_human_observed_provenance: bool = False,
) -> dict[str, str | int | float | bool]:
    """Bind one offline job to exactly one participant answer envelope.

    The provenance marker is a contract with the trusted offline orchestrator; it
    cannot be inferred or defaulted by the prompt builder.  The orchestrator is
    responsible for attaching ``human_observed`` only to the original H8 answers,
    never to Stage 1 predictions.
    """

    if not isinstance(job_participant_id, str) or not job_participant_id.strip():
        raise PromptValidationError("job_participant_id must be one non-empty string")
    if not isinstance(envelope, Mapping):
        raise PromptValidationError("Participant envelope must be a mapping")
    if any(not isinstance(key, str) for key in envelope):
        raise PromptValidationError("Participant envelope keys must be strings")
    required_keys = {"participant_id", "answers"}
    if require_human_observed_provenance:
        required_keys.add("provenance")
    if set(envelope) != required_keys:
        raise PromptValidationError(
            f"Participant envelope must contain exactly {sorted(required_keys)}"
        )
    if not isinstance(envelope["participant_id"], str):
        raise PromptValidationError("Participant envelope ID must be a string")
    if envelope["participant_id"] != job_participant_id:
        raise PromptValidationError(
            "Participant envelope ID does not match the current job participant ID"
        )
    validate_single_participant_payload(job_participant_id, envelope)
    if require_human_observed_provenance:
        if envelope["provenance"] != HUMAN_OBSERVED_PROVENANCE:
            raise PromptValidationError(
                "H8 provenance must be explicitly set to human_observed by the "
                "trusted offline orchestrator"
            )
    answers = envelope["answers"]
    if not isinstance(answers, Mapping):
        raise PromptValidationError("Participant envelope answers must be a mapping")
    return validate_answer_mapping(job_participant_id, answers, expected_variables)


def _scale_value(value: str) -> str | int | float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _validate_item_response(item: ItemSpec, value: str | int | float | bool) -> None:
    numeric_types = {
        "ordinal_0_10",
        "coded_amount_category",
        "binary_choice",
        "open_numeric_certainty_equivalent",
        "adaptive_binary_task_summary",
        "multiple_price_list_summary",
    }
    if item.response_type not in numeric_types:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PromptValidationError(
            f"{item.variable_name} requires a numeric response, not a label or container"
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise PromptValidationError(f"{item.variable_name} must be finite")
    if item.response_type in {"ordinal_0_10", "coded_amount_category", "binary_choice"}:
        if isinstance(value, float) and not value.is_integer():
            raise PromptValidationError(f"{item.variable_name} requires an integer code")
    minimum = _scale_value(item.response_scale_min)
    maximum = _scale_value(item.response_scale_max)
    if isinstance(minimum, (int, float)) and value < minimum:
        raise PromptValidationError(
            f"{item.variable_name} is below its documented response scale"
        )
    if isinstance(maximum, (int, float)) and value > maximum:
        raise PromptValidationError(
            f"{item.variable_name} is above its documented response scale"
        )


def _model_visible_response(
    item: ItemSpec,
    value: str | int | float | bool,
) -> Any:
    """Translate source-resolved special responses into model-meaningful values."""

    if item.variable_name == "v_106":
        code = int(value)
        try:
            amount = R48_AMOUNT_BY_CODE[code]
        except KeyError as exc:
            raise PromptValidationError("v_106 has an unsupported source code") from exc
        if code == 6:
            return "You would invest nothing and decline the offer."
        return f"You would invest {amount:,} euros."
    if item.variable_name == "v_107":
        return {"amount": value, "unit": "euros"}
    if item.variable_name == "v_108":
        mapping = R50_CODE_TO_LABEL
        if (
            mapping is None
            or set(mapping) != {1, 2}
            or set(mapping.values()) != {"Yes", "No"}
        ):
            raise PromptValidationError(
                "v_108 requires the approved high-confidence cross-source 1/2 "
                "to Yes/No mapping before "
                "it can enter a model-visible profile"
            )
        code = int(value)
        try:
            label = mapping[code]
        except KeyError as exc:
            raise PromptValidationError("v_108 has an unsupported source code") from exc
        try:
            rendered = R50_RENDERED_RESPONSE_BY_CODE[code]
        except KeyError as exc:
            raise PromptValidationError("v_108 has an unsupported rendered response") from exc
        if not rendered.startswith(label):
            raise PromptValidationError("v_108 semantic renderer is internally inconsistent")
        return rendered
    return value


def build_profile_records(
    participant_id: str,
    specs: Sequence[ItemSpec],
    answers: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build source-faithful prompt records without emitting an identifier."""

    clean_answers = validate_answer_mapping(
        participant_id,
        answers,
        [item.variable_name for item in specs],
    )
    records: list[dict[str, Any]] = []
    for item in specs:
        response = clean_answers[item.variable_name]
        _validate_item_response(item, response)
        visible_response = _model_visible_response(item, response)
        records.append(
            {
            "variable_name": item.variable_name,
            "R_item": item.r_item,
            "question_text_en": item.question_text_en,
            "response_scale": {
                "minimum": _scale_value(item.response_scale_min),
                "maximum": _scale_value(item.response_scale_max),
                "options": item.response_options,
                "type": item.response_type,
            },
            "previous_response": visible_response,
        }
        )
    return records


def build_question_records(specs: Sequence[ItemSpec]) -> list[dict[str, Any]]:
    """Build question-and-scale records with no response field."""

    return [
        {
            "variable_name": item.variable_name,
            "R_item": item.r_item,
            "question_text_en": item.question_text_en,
            "response_scale": {
                "minimum": _scale_value(item.response_scale_min),
                "maximum": _scale_value(item.response_scale_max),
                "options": item.response_options,
                "type": item.response_type,
            },
        }
        for item in specs
    ]


def validate_rendered_prompt(
    prompt: str,
    *,
    participant_id: str,
    allowed_r_items: Sequence[str],
    other_participant_ids: Sequence[str] = (),
) -> None:
    """Check the final text immediately before it can cross an LLM boundary."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptValidationError("Rendered prompt is empty")
    if "[[" in prompt or "]]" in prompt:
        raise PromptValidationError("Rendered prompt still contains a template placeholder")
    for pattern, label in _FORBIDDEN_TEXT_PATTERNS:
        if pattern.search(prompt):
            raise PromptValidationError(f"Rendered prompt contains forbidden {label}")
    allowed = set(allowed_r_items)
    mentioned = set(_R_ITEM_PATTERN.findall(prompt))
    unauthorized = sorted(mentioned - allowed)
    if unauthorized:
        raise PromptValidationError(
            f"Rendered prompt contains unauthorized questionnaire items: {unauthorized}"
        )
    identifiers = [participant_id, *other_participant_ids]
    for identifier in identifiers:
        if isinstance(identifier, str) and len(identifier) >= 4 and identifier in prompt:
            raise PromptValidationError("Rendered prompt contains a participant identifier")


def _strict_object(value: Any, required_keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResponseValidationError(f"{label} must be a JSON object")
    keys = set(value)
    if keys != required_keys:
        raise ResponseValidationError(
            f"{label} must contain exactly {sorted(required_keys)}; got {sorted(keys)}"
        )
    return value


def validate_heldout_response(payload: Any) -> dict[str, Any]:
    """Enforce the Stage 1 schema plus unique, complete H8 membership."""

    root = _strict_object(payload, {"responses"}, "Stage 1 response")
    responses = root["responses"]
    if not isinstance(responses, list) or len(responses) != 8:
        raise ResponseValidationError("responses must contain exactly eight objects")
    for index, (raw, expected_r_item) in enumerate(
        zip(responses, HELDOUT_R_ITEMS, strict=True), start=1
    ):
        item = _strict_object(raw, {"R_item", "response"}, f"responses[{index}]")
        r_item = item["R_item"]
        response = item["response"]
        if r_item != expected_r_item:
            raise ResponseValidationError(
                f"responses[{index}].R_item must be {expected_r_item}"
            )
        if isinstance(response, bool) or not isinstance(response, int) or not 0 <= response <= 10:
            raise ResponseValidationError(
                f"responses[{index}].response must be an integer from 0 to 10"
            )
    return {"responses": [dict(item) for item in responses]}


def validate_risk_choices(payload: Any) -> dict[str, Any]:
    """Enforce the Stage 2 schema plus unique, complete row membership."""

    root = _strict_object(payload, {"choices"}, "Stage 2 response")
    choices = root["choices"]
    if not isinstance(choices, list) or len(choices) != 21:
        raise ResponseValidationError("choices must contain exactly 21 objects")
    for index, raw in enumerate(choices, start=1):
        item = _strict_object(raw, {"row", "choice"}, f"choices[{index}]")
        row = item["row"]
        choice = item["choice"]
        if isinstance(row, bool) or not isinstance(row, int) or row != index:
            raise ResponseValidationError(f"choices[{index}].row must be {index}")
        if choice not in {"LOTTERY", "SAFE"}:
            raise ResponseValidationError(
                f"choices[{index}].choice must be LOTTERY or SAFE"
            )
    return {"choices": [dict(item) for item in choices]}


def parse_and_validate_json_response(text: str, *, stage: str) -> dict[str, Any]:
    """Reject prose/code fences and validate one complete JSON response."""

    if not isinstance(text, str):
        raise ResponseValidationError("Model response must be text containing one JSON object")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResponseValidationError("Model response is not pure JSON") from exc
    normalized = stage.strip().lower()
    if normalized in {"stage1", "stage_1", "heldout"}:
        return validate_heldout_response(payload)
    if normalized in {"stage2", "stage_2", "risk_choices"}:
        return validate_risk_choices(payload)
    raise ResponseValidationError(f"Unknown response stage: {stage!r}")
