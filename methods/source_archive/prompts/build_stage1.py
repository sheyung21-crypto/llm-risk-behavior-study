"""Build the Stage 1 Persona Conditioning Check prompt for one participant.

The trusted offline orchestrator supplies one already-isolated answer envelope.
This module never reads participant datasets and never invokes a model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .validate_prompt import (
    PROJECT_ROOT,
    PromptValidationError,
    build_profile_records,
    build_question_records,
    load_condition_specs,
    load_heldout_specs,
    validate_answer_envelope,
    validate_rendered_prompt,
)


def _load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise PromptValidationError(f"JSON Schema root must be an object: {path}")
    return payload


def build_stage1_prompt(
    job_participant_id: str,
    participant_envelope: Mapping[str, Any],
    *,
    project_root: Path | str | None = None,
) -> str:
    """Render Stage 1 from C1 conditioning answers while withholding all H8 answers.

    ``job_participant_id`` must equal the explicit ID in ``participant_envelope``.
    This is a trusted offline-orchestrator boundary: the ID is used for binding and
    is intentionally never interpolated into the returned prompt.
    """

    root = Path(project_root).resolve() if project_root is not None else PROJECT_ROOT
    conditioning_specs = load_condition_specs(
        "C1_broad_condition",
        include_heldout=False,
        project_root=root,
    )
    heldout_specs = load_heldout_specs(root)
    prohibited = {"R1", "R2", "R49"}
    present = {item.r_item for item in conditioning_specs}
    if present & prohibited:
        raise PromptValidationError("Stage 1 C1 metadata contains a prohibited item")

    questionnaire_answers = validate_answer_envelope(
        job_participant_id,
        participant_envelope,
        [item.variable_name for item in conditioning_specs],
    )
    profile = build_profile_records(
        job_participant_id,
        conditioning_specs,
        questionnaire_answers,
    )
    heldout_questions = build_question_records(heldout_specs)
    if any("previous_response" in item for item in heldout_questions):
        raise PromptValidationError("Stage 1 held-out answers must remain withheld")

    template = (root / "prompts/stage1/conditioning_check.txt").read_text(
        encoding="utf-8"
    )
    schema = _load_json(root / "prompts/schemas/heldout_response.schema.json")
    prompt = (
        template.replace(
            "[[PROFILE_JSON]]",
            json.dumps(profile, ensure_ascii=False, indent=2, allow_nan=False),
        )
        .replace(
            "[[HELDOUT_QUESTIONS_JSON]]",
            json.dumps(heldout_questions, ensure_ascii=False, indent=2, allow_nan=False),
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
            *(item.r_item for item in conditioning_specs),
            *(item.r_item for item in heldout_specs),
        ],
    )
    return prompt


__all__ = ["build_stage1_prompt"]
