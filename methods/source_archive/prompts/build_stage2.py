"""Build Stage 2 and no-persona prompts from one isolated participant payload.

No function in this module reads raw participant data or invokes an LLM.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .validate_prompt import (
    PROJECT_ROOT,
    PromptValidationError,
    build_profile_records,
    load_condition_specs,
    load_heldout_specs,
    normalize_condition,
    validate_answer_envelope,
    validate_rendered_prompt,
)


LIST_COLUMNS = (
    "row",
    "lottery_high_points",
    "lottery_high_probability",
    "lottery_low_points",
    "lottery_low_probability",
    "safe_points",
    "delta",
)
APPROVED_LIST_IDS = ("list_1", "list_2")

_TEMPLATE_BY_CONDITION = {
    "C1_broad_condition": "prompts/stage2/broad_condition.txt",
    "C2_strict_condition": "prompts/stage2/strict_condition.txt",
    "C3_task_proximal_condition": "prompts/stage2/task_proximal_condition.txt",
}


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise PromptValidationError(f"JSON Schema root must be an object: {path}")
    return payload


def _integer(value: Any, *, field: str, row_number: int) -> int:
    if isinstance(value, bool):
        raise PromptValidationError(f"Row {row_number} {field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError as exc:
            raise PromptValidationError(
                f"Row {row_number} {field} must be an integer"
            ) from exc
        if str(parsed) == value.strip() or value.strip() in {f"+{parsed}", f"-{abs(parsed)}"}:
            return parsed
    raise PromptValidationError(f"Row {row_number} {field} must be an integer")


def _probability(value: Any, *, field: str, row_number: int) -> float:
    if isinstance(value, bool):
        raise PromptValidationError(f"Row {row_number} {field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise PromptValidationError(f"Row {row_number} {field} must be numeric") from exc
    if not math.isfinite(parsed):
        raise PromptValidationError(f"Row {row_number} {field} must be finite")
    return parsed


def normalize_choice_rows(choice_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate the exact seven-column, 21-row experimental-list contract."""

    if isinstance(choice_rows, (str, bytes, bytearray)) or not isinstance(
        choice_rows, Sequence
    ):
        raise PromptValidationError("choice_rows must be a sequence of row mappings")
    if len(choice_rows) != 21:
        raise PromptValidationError("The current list must contain exactly 21 rows")
    normalized: list[dict[str, Any]] = []
    for position, raw in enumerate(choice_rows, start=1):
        if not isinstance(raw, Mapping):
            raise PromptValidationError(f"Choice row {position} must be a mapping")
        if set(raw) != set(LIST_COLUMNS):
            raise PromptValidationError(
                f"Choice row {position} must contain exactly {list(LIST_COLUMNS)}"
            )
        row = _integer(raw["row"], field="row", row_number=position)
        high = _integer(
            raw["lottery_high_points"],
            field="lottery_high_points",
            row_number=position,
        )
        low = _integer(
            raw["lottery_low_points"],
            field="lottery_low_points",
            row_number=position,
        )
        safe = _integer(raw["safe_points"], field="safe_points", row_number=position)
        delta = _integer(raw["delta"], field="delta", row_number=position)
        high_probability = _probability(
            raw["lottery_high_probability"],
            field="lottery_high_probability",
            row_number=position,
        )
        low_probability = _probability(
            raw["lottery_low_probability"],
            field="lottery_low_probability",
            row_number=position,
        )
        if row != position:
            raise PromptValidationError("Choice rows must appear once each in order 1 through 21")
        if (high, high_probability, low, low_probability) != (1000, 0.5, 0, 0.5):
            raise PromptValidationError(
                f"Row {row} must use the fixed 50/50 lottery paying 1000 or 0 points"
            )
        if not -5 <= delta <= 5:
            raise PromptValidationError(f"Row {row} delta must be between -5 and 5")
        if safe != 50 * (row - 1) + delta:
            raise PromptValidationError(
                f"Row {row} safe_points does not equal its baseline plus delta"
            )
        if row in {1, 21} and delta != 0:
            raise PromptValidationError("The first and last list deltas must be zero")
        normalized.append(
            {
                "row": row,
                "lottery_high_points": high,
                "lottery_high_probability": high_probability,
                "lottery_low_points": low,
                "lottery_low_probability": low_probability,
                "safe_points": safe,
                "delta": delta,
            }
        )
    safe_values = [row["safe_points"] for row in normalized]
    if safe_values[0] != 0 or safe_values[-1] != 1000:
        raise PromptValidationError("The list endpoints must be 0 and 1000 points")
    if any(left >= right for left, right in zip(safe_values, safe_values[1:])):
        raise PromptValidationError("safe_points must be strictly increasing")
    return normalized


def load_official_list_rows(
    list_id: str,
    *,
    project_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Load one approved list after verifying its manifest path, hash, and content."""

    if list_id not in APPROVED_LIST_IDS:
        raise PromptValidationError(
            f"list_id must be one of {list(APPROVED_LIST_IDS)}; got {list_id!r}"
        )
    root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
    manifest_path = root / "experiment/lists/generation_manifest.json"
    with manifest_path.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, Mapping):
        raise PromptValidationError("List generation manifest root must be an object")
    if manifest.get("schema_name") != "llm-risk-study-experiment-list-generation":
        raise PromptValidationError("List generation manifest schema_name is invalid")
    if manifest.get("schema_version") != 1:
        raise PromptValidationError("List generation manifest schema_version is invalid")
    if manifest.get("random_generator") != "numpy.random.Generator(PCG64)":
        raise PromptValidationError("List generation manifest random_generator is invalid")
    section = manifest.get(list_id)
    if not isinstance(section, Mapping):
        raise PromptValidationError(f"List generation manifest has no {list_id} object")
    relative_path = f"experiment/lists/{list_id}.csv"
    if section.get("file") != relative_path:
        raise PromptValidationError(f"Manifest file binding for {list_id} is invalid")
    list_path = root / relative_path
    list_bytes = list_path.read_bytes()
    actual_hash = hashlib.sha256(list_bytes).hexdigest()
    declared_hash = section.get("sha256")
    if not isinstance(declared_hash, str) or actual_hash != declared_hash:
        raise PromptValidationError(f"SHA-256 verification failed for {list_id}")
    with io.StringIO(list_bytes.decode("utf-8-sig"), newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != LIST_COLUMNS:
            raise PromptValidationError(f"Official {list_id} CSV columns are invalid")
        official = normalize_choice_rows(list(reader))

    safe_points = [row["safe_points"] for row in official]
    row_deltas = [row["delta"] for row in official]
    if section.get("safe_points") != safe_points:
        raise PromptValidationError(f"Manifest safe_points do not match {list_id}")
    if list_id == "list_1":
        if section.get("deltas") != row_deltas:
            raise PromptValidationError("Manifest deltas do not match list_1")
    else:
        if section.get("delta_min") != -5 or section.get("delta_max") != 5:
            raise PromptValidationError("Manifest delta bounds do not match list_2")
        if section.get("endpoint_rows_fixed") != [1, 21]:
            raise PromptValidationError("Manifest endpoint rows do not match list_2")
        if isinstance(section.get("random_seed"), bool) or not isinstance(
            section.get("random_seed"), int
        ):
            raise PromptValidationError("Manifest random_seed for list_2 is invalid")
        if section.get("row_deltas") != row_deltas:
            raise PromptValidationError("Manifest row_deltas do not match list_2")
        if section.get("deltas") != row_deltas[1:-1]:
            raise PromptValidationError("Manifest middle deltas do not match list_2")
    return official


def bind_official_list(
    list_id: str,
    choice_rows: Sequence[Mapping[str, Any]],
    *,
    project_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Reject mixed, modified, or third-list content at the execution boundary."""

    supplied = normalize_choice_rows(choice_rows)
    official = load_official_list_rows(list_id, project_root=project_root)
    if supplied != official:
        raise PromptValidationError(
            f"Supplied rows do not match the repository's approved {list_id}"
        )
    return official


def _model_visible_choice_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Remove the internal generation delta after the seven-field audit passes."""

    return [
        {
            "row": row["row"],
            "lottery_high_points": row["lottery_high_points"],
            "lottery_high_probability": row["lottery_high_probability"],
            "lottery_low_points": row["lottery_low_points"],
            "lottery_low_probability": row["lottery_low_probability"],
            "safe_points": row["safe_points"],
        }
        for row in rows
    ]


def _assert_condition_relations(project_root: Path) -> None:
    c1 = {
        item.variable_name
        for item in load_condition_specs("C1", include_heldout=True, project_root=project_root)
    }
    c2 = {
        item.variable_name
        for item in load_condition_specs("C2", include_heldout=True, project_root=project_root)
    }
    c3 = {
        item.variable_name
        for item in load_condition_specs("C3", include_heldout=True, project_root=project_root)
    }
    if c2 != c1 - {"v_106", "v_108"}:
        raise PromptValidationError("C2 must differ from C1 only by excluding R48 and R50")
    if c3 != c1 | {"v_107"}:
        raise PromptValidationError("C3 must differ from C1 only by adding R49")


def build_stage2_prompt(
    job_participant_id: str,
    condition: str,
    participant_envelope: Mapping[str, Any],
    heldout_envelope: Mapping[str, Any],
    list_id: str,
    choice_rows: Sequence[Mapping[str, Any]],
    *,
    project_root: Path | str | None = None,
) -> str:
    """Render one condition and one list using true H8 questionnaire answers.

    Both envelopes must match ``job_participant_id``.  ``heldout_envelope`` must
    explicitly carry provenance ``human_observed``; there is no default that can
    self-assert it.  This is a trusted offline-orchestrator contract: Stage 1
    predictions are not accepted as a substitute.  All identifiers are omitted.
    """

    root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
    canonical = normalize_condition(condition)
    _assert_condition_relations(root)
    profile_specs = load_condition_specs(
        canonical,
        include_heldout=False,
        project_root=root,
    )
    heldout_specs = load_heldout_specs(root)
    questionnaire_answers = validate_answer_envelope(
        job_participant_id,
        participant_envelope,
        [item.variable_name for item in profile_specs],
    )
    true_heldout_answers = validate_answer_envelope(
        job_participant_id,
        heldout_envelope,
        [item.variable_name for item in heldout_specs],
        require_human_observed_provenance=True,
    )
    profile = build_profile_records(
        job_participant_id,
        profile_specs,
        questionnaire_answers,
    )
    heldout_profile = build_profile_records(
        job_participant_id,
        heldout_specs,
        true_heldout_answers,
    )
    rows = bind_official_list(list_id, choice_rows, project_root=root)
    visible_rows = _model_visible_choice_rows(rows)
    schema = _load_json(root / "prompts/schemas/risk_choices.schema.json")
    template = (root / _TEMPLATE_BY_CONDITION[canonical]).read_text(encoding="utf-8")
    prompt = (
        template.replace(
            "[[PROFILE_JSON]]",
            json.dumps(
                [*profile, *heldout_profile],
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
        )
        .replace(
            "[[CHOICE_ROWS_JSON]]",
            json.dumps(visible_rows, ensure_ascii=False, indent=2, allow_nan=False),
        )
        .replace(
            "[[OUTPUT_SCHEMA_JSON]]",
            json.dumps(schema, ensure_ascii=False, indent=2, allow_nan=False),
        )
    )
    validate_rendered_prompt(
        prompt,
        participant_id=job_participant_id,
        allowed_r_items=[
            *(item.r_item for item in profile_specs),
            *(item.r_item for item in heldout_specs),
        ],
    )
    return prompt


def build_no_persona_prompt(
    job_participant_id: str,
    participant_envelope: Mapping[str, Any],
    list_id: str,
    choice_rows: Sequence[Mapping[str, Any]],
    *,
    project_root: Path | str | None = None,
) -> str:
    """Render the matched baseline using a job-bound empty participant envelope.

    The envelope is supplied by the trusted offline orchestrator and must contain
    the current job ID plus an empty ``answers`` object.  Its ID is never rendered.
    """

    root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
    validate_answer_envelope(job_participant_id, participant_envelope, ())
    rows = bind_official_list(list_id, choice_rows, project_root=root)
    visible_rows = _model_visible_choice_rows(rows)
    schema = _load_json(root / "prompts/schemas/risk_choices.schema.json")
    template = (root / "prompts/controls/no_persona.txt").read_text(encoding="utf-8")
    prompt = template.replace(
        "[[CHOICE_ROWS_JSON]]",
        json.dumps(visible_rows, ensure_ascii=False, indent=2, allow_nan=False),
    ).replace(
        "[[OUTPUT_SCHEMA_JSON]]",
        json.dumps(schema, ensure_ascii=False, indent=2, allow_nan=False),
    )
    validate_rendered_prompt(
        prompt,
        participant_id=job_participant_id,
        allowed_r_items=(),
    )
    return prompt


__all__ = [
    "LIST_COLUMNS",
    "APPROVED_LIST_IDS",
    "bind_official_list",
    "build_no_persona_prompt",
    "build_stage2_prompt",
    "load_official_list_rows",
    "normalize_choice_rows",
]
