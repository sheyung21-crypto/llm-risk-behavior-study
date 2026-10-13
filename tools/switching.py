"""Switching-score calculations for the two 21-row choice lists.

The source-defined score is the row of each LOTTERY-to-SAFE transition.  If a
list contains more than one such transition, all LOTTERY-to-SAFE transition
rows are averaged.  The score across two lists is the mean of the two list
scores.

The source materials do not define scores for all-SAFE or all-LOTTERY
sequences.  Before Pilot execution, the user approved a researcher-authored
ex-ante boundary rule: all-SAFE is virtual row 1 and all-LOTTERY is virtual
row 22.  The implementation keeps that provenance explicit and applies the
approved rule by default; it must not be described as an author-defined rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from statistics import fmean
from typing import Sequence


ROW_COUNT = 21
LOTTERY = "LOTTERY"
SAFE = "SAFE"
VALID_CHOICES = frozenset({LOTTERY, SAFE})


class SwitchingScoreError(ValueError):
    """Base class for switching-score validation and scoring errors."""


class InvalidChoiceSequenceError(SwitchingScoreError):
    """Raised when a list does not contain exactly 21 valid choices."""


class NoLotteryToSafeTransitionError(SwitchingScoreError):
    """Raised for a non-extreme sequence with no LOTTERY-to-SAFE transition."""


class BoundaryPolicy(str, Enum):
    """Approved researcher policy for source-undefined boundary sequences."""

    RESEARCHER_APPROVED_VIRTUAL_ROWS = "researcher_approved_virtual_rows"


@dataclass(frozen=True)
class SwitchingMetrics:
    """Descriptive indicators for one validated 21-row choice sequence.

    ``number_of_switches`` counts every adjacent state change in either
    direction.  ``lottery_to_safe_count`` is reported separately because only
    LOTTERY-to-SAFE transitions enter the primary switching score.
    """

    number_of_switches: int
    lottery_to_safe_count: int
    safe_to_lottery_count: int
    monotonic_sequence: bool
    lottery_count: int
    dominance_violation: bool


def validate_choices(choices: Sequence[str]) -> tuple[str, ...]:
    """Return an immutable validated sequence of exactly 21 strict choices."""

    values = tuple(choices)
    if len(values) != ROW_COUNT:
        raise InvalidChoiceSequenceError(
            f"Expected exactly {ROW_COUNT} choices; received {len(values)}."
        )

    invalid = [
        (row, value)
        for row, value in enumerate(values, start=1)
        if not isinstance(value, str) or value not in VALID_CHOICES
    ]
    if invalid:
        rendered = ", ".join(f"row {row}={value!r}" for row, value in invalid)
        raise InvalidChoiceSequenceError(
            "Every choice must be exactly 'LOTTERY' or 'SAFE'; invalid "
            f"choice(s): {rendered}."
        )
    return values


def lottery_to_safe_transition_rows(choices: Sequence[str]) -> tuple[int, ...]:
    """Return one-based rows where the choice changes LOTTERY -> SAFE."""

    values = validate_choices(choices)
    return tuple(
        row
        for row in range(2, ROW_COUNT + 1)
        if values[row - 2] == LOTTERY and values[row - 1] == SAFE
    )


def descriptive_metrics(choices: Sequence[str]) -> SwitchingMetrics:
    """Calculate prespecified descriptive indicators for one choice list."""

    values = validate_choices(choices)
    lottery_to_safe_count = 0
    safe_to_lottery_count = 0
    for previous, current in zip(values, values[1:]):
        if previous == LOTTERY and current == SAFE:
            lottery_to_safe_count += 1
        elif previous == SAFE and current == LOTTERY:
            safe_to_lottery_count += 1

    return SwitchingMetrics(
        number_of_switches=lottery_to_safe_count + safe_to_lottery_count,
        lottery_to_safe_count=lottery_to_safe_count,
        safe_to_lottery_count=safe_to_lottery_count,
        monotonic_sequence=safe_to_lottery_count == 0,
        lottery_count=values.count(LOTTERY),
        dominance_violation=values[0] == SAFE or values[-1] == LOTTERY,
    )


def score_list(
    choices: Sequence[str],
    *,
    boundary_policy: BoundaryPolicy | str = (
        BoundaryPolicy.RESEARCHER_APPROVED_VIRTUAL_ROWS
    ),
) -> float:
    """Score one list from its LOTTERY-to-SAFE transition rows.

    Normal sequences do not depend on the boundary policy.  Exact all-SAFE and
    all-LOTTERY sequences use the user-approved researcher rule assigning
    virtual row 1 to all-SAFE and virtual row 22 to all-LOTTERY.
    """

    values = validate_choices(choices)
    transition_rows = lottery_to_safe_transition_rows(values)
    if transition_rows:
        return float(fmean(transition_rows))

    policy = _coerce_boundary_policy(boundary_policy)
    if len(set(values)) == 1:
        if policy is BoundaryPolicy.RESEARCHER_APPROVED_VIRTUAL_ROWS:
            return 1.0 if values[0] == SAFE else float(ROW_COUNT + 1)

    raise NoLotteryToSafeTransitionError(
        "The sequence is neither an all-SAFE/all-LOTTERY boundary case nor "
        "does it contain a LOTTERY-to-SAFE transition, so its switching "
        "score is undefined."
    )


def score_two_lists(
    list_1_choices: Sequence[str],
    list_2_choices: Sequence[str],
    *,
    boundary_policy: BoundaryPolicy | str = (
        BoundaryPolicy.RESEARCHER_APPROVED_VIRTUAL_ROWS
    ),
) -> float:
    """Return the mean of the separately calculated List 1 and List 2 scores."""

    list_scores = (
        score_list(list_1_choices, boundary_policy=boundary_policy),
        score_list(list_2_choices, boundary_policy=boundary_policy),
    )
    return float(fmean(list_scores))


def _coerce_boundary_policy(
    boundary_policy: BoundaryPolicy | str,
) -> BoundaryPolicy:
    if isinstance(boundary_policy, BoundaryPolicy):
        return boundary_policy
    try:
        return BoundaryPolicy(boundary_policy)
    except (TypeError, ValueError) as error:
        allowed = BoundaryPolicy.RESEARCHER_APPROVED_VIRTUAL_ROWS.value
        raise SwitchingScoreError(
            f"Unknown boundary policy {boundary_policy!r}; approved policy: "
            f"{allowed!r}."
        ) from error
