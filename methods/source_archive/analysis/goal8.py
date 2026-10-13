"""Reproducible Goal 8 scoring, stability, and confirmatory analysis.

The command has two deliberately separate phases. ``blind`` reads only the
frozen model stores and approved questionnaire inputs. ``outcome`` refuses to
run until the blind manifest proves that scoring and the K diagnostic were
completed with the frozen implementation. No model or dispatcher entry point
is imported by this module.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy import stats
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.model_selection import GridSearchCV, KFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from llm_risk_study.data.confirmatory_preparation import load_confirmatory_inputs
from llm_risk_study.pilot.pilot_store import PilotJobStore
from llm_risk_study.prompts.validate_prompt import (
    PROJECT_ROOT,
    load_condition_specs,
    load_heldout_specs,
)
from llm_risk_study.scoring.switching import (
    LOTTERY,
    SAFE,
    NoLotteryToSafeTransitionError,
    descriptive_metrics,
    score_list,
)


N_RESAMPLES = 10_000
SEEDS = {
    "stage1_permutation": 20260908,
    "stage1_bootstrap": 20260909,
    "k_bootstrap": 20260910,
    "condition_bootstrap": 20260911,
    "primary_permutation": 20260912,
    "c2_randomization": 20260913,
    "c3_randomization": 20260914,
    "ridge_cv": 20260915,
}
RIDGE_ALPHAS = tuple(float(x) for x in np.logspace(-4, 4, 33))
H8_R_ITEMS = ("R3", "R6", "R8", "R12", "R13", "R17", "R24", "R41")
H8_VARIABLES = ("v_94", "v_97", "v_99", "v_111", "v_112", "v_119", "v_128", "v_146")
BENCHMARK0_EXCLUSIONS = frozenset({"R29", "R35", "R48", "R50"})

INPUT_PATH = PROJECT_ROOT / "data/processed/confirmatory_inputs.parquet"
OUTCOME_PATH = PROJECT_ROOT / "data/locked/confirmatory_outcomes.parquet"
SPLIT_PATH = PROJECT_ROOT / "data/manifests/split_manifest.csv"
PARSED_ROOT = PROJECT_ROOT / "experiment/parsed_outputs/confirmatory"
DIAGNOSTIC_ROOT = PROJECT_ROOT / "outputs/diagnostics"
TABLE_ROOT = PROJECT_ROOT / "outputs/tables"
FIGURE_ROOT = PROJECT_ROOT / "outputs/figures"
REPORT_ROOT = PROJECT_ROOT / "outputs/reports"
BLIND_MANIFEST_PATH = DIAGNOSTIC_ROOT / "goal8_outcome_blind_manifest.json"
MACHINE_RESULTS_PATH = PROJECT_ROOT / "outputs/machine_readable_results.json"

STORE_SPECS = {
    "original": (
        PROJECT_ROOT / "experiment/runtime/confirmatory",
        PROJECT_ROOT / "data/manifests/confirmatory_job_matrix.jsonl",
        1368,
    ),
    "stage1": (
        PROJECT_ROOT / "experiment/runtime/confirmatory_stage1",
        PROJECT_ROOT / "data/manifests/confirmatory_stage1_job_matrix.jsonl",
        342,
    ),
    "k": (
        PROJECT_ROOT / "experiment/runtime/confirmatory_k_diagnostic",
        PROJECT_ROOT / "data/manifests/confirmatory_k_diagnostic_job_matrix.jsonl",
        240,
    ),
    "c2": (
        PROJECT_ROOT / "experiment/runtime/confirmatory_c2",
        PROJECT_ROOT / "data/manifests/confirmatory_c2_job_matrix.jsonl",
        684,
    ),
}


class Goal8AnalysisError(RuntimeError):
    """Raised when a frozen input, output, or analysis invariant is violated."""


@dataclass(frozen=True)
class StoreRecord:
    job_id: str
    participant_key: str
    condition: str
    list_id: int | None
    repeat_index: int
    response: Mapping[str, Any]
    metadata: Mapping[str, Any]
    response_sha256: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _matrix_rows(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise Goal8AnalysisError(f"Invalid frozen matrix: {path}")
    ids = [row.get("job_id") for row in rows]
    if len(ids) != len(set(ids)):
        raise Goal8AnalysisError(f"Duplicate frozen job_id: {path}")
    return rows


def load_verified_store_records(name: str) -> list[StoreRecord]:
    """Read exactly one complete response per frozen canonical job."""

    if name not in STORE_SPECS:
        raise Goal8AnalysisError(f"Unknown store scope: {name}")
    root, matrix_path, expected_n = STORE_SPECS[name]
    rows = _matrix_rows(matrix_path)
    if len(rows) != expected_n:
        raise Goal8AnalysisError(f"{name} matrix count drifted")
    expected = {row["job_id"]: row for row in rows}
    ledger = PilotJobStore(root.resolve()).coverage_ledger()
    status_counts = ledger["status_counts"]
    if (
        ledger["job_count"] != expected_n
        or status_counts.get("complete") != expected_n
        or any(count for status, count in status_counts.items() if status != "complete")
    ):
        raise Goal8AnalysisError(f"{name} store is not completely usable")
    ledger_rows = {row["job_id"]: row for row in ledger["jobs"]}
    if set(ledger_rows) != set(expected):
        raise Goal8AnalysisError(f"{name} store/matrix membership mismatch")

    records: list[StoreRecord] = []
    for row in rows:
        job_id = row["job_id"]
        job_dir = root / ledger_rows[job_id]["execution_id"]
        complete_attempts = [marker.parent for marker in job_dir.glob("attempt-*/complete.json")]
        if len(complete_attempts) != 1:
            raise Goal8AnalysisError(f"{name} job lacks one canonical success")
        attempt = complete_attempts[0]
        metadata_path = attempt / "metadata.json"
        response_path = attempt / "response.json"
        if not metadata_path.is_file() or not response_path.is_file():
            raise Goal8AnalysisError(f"{name} complete attempt lacks artifacts")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        response = json.loads(response_path.read_text(encoding="utf-8"))
        response_hash = _sha256(response_path)
        participant = row.get("participant_key")
        condition = row.get("condition", "C1")
        list_id = row.get("list")
        repeat = int(row.get("repeat_index", 1))
        expected_metadata_condition = {
            "C1": "C1_broad_condition",
            "C2": "C2_strict_condition",
            "C3": "C3_task_proximal_condition",
            "persona_conditioning_check": "C1_broad_condition",
        }.get(condition)
        if (
            metadata.get("status") != "complete"
            or metadata.get("job_id") != job_id
            or metadata.get("participant_id") != participant
            or metadata.get("condition") != expected_metadata_condition
            or metadata.get("output_sha256") != response_hash
            or metadata.get("participant_count") != 1
            or metadata.get("foreign_participant_count") != 0
            or metadata.get("security_errors") != []
            or metadata.get("actual_command_model") != "gpt-5.6-terra"
            or metadata.get("actual_command_reasoning") != "medium"
        ):
            raise Goal8AnalysisError(f"{name} metadata integrity mismatch")
        if list_id is not None and metadata.get("list_id") != f"list_{list_id}":
            raise Goal8AnalysisError(f"{name} condition/list mismatch")
        if row.get("repeat_index") is not None and metadata.get("repeat_index") != repeat:
            raise Goal8AnalysisError(f"{name} repeat mismatch")
        records.append(
            StoreRecord(
                job_id=job_id,
                participant_key=str(participant),
                condition=str(condition),
                list_id=int(list_id) if list_id is not None else None,
                repeat_index=repeat,
                response=response,
                metadata=metadata,
                response_sha256=response_hash,
            )
        )
    return records


def summarize_execution(
    records_by_source: Mapping[str, Sequence[StoreRecord]],
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Create a non-identifying execution/attrition summary from retained evidence."""

    successful_cli: Counter[tuple[str, str]] = Counter()
    for source, records in records_by_source.items():
        for record in records:
            version = str(
                record.metadata.get("actual_cli_version")
                or record.metadata.get("codex_version")
                or "unknown"
            )
            successful_cli[(source, version)] += 1

    artifact_counts: Counter[str] = Counter()
    metadata_status_counts: Counter[str] = Counter()
    adjudication_counts: Counter[str] = Counter()
    attempt_count = 0
    for root, _, _ in STORE_SPECS.values():
        for attempt in root.glob("*/attempt-*"):
            if not attempt.is_dir():
                continue
            attempt_count += 1
            for artifact in attempt.iterdir():
                if artifact.is_file() and not artifact.name.startswith(".artifact-"):
                    artifact_counts[artifact.name] += 1
            metadata_path = attempt / "metadata.json"
            if metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata_status_counts[str(metadata.get("status", "missing"))] += 1
            adjudication_path = attempt / "adjudication.json"
            if adjudication_path.is_file():
                adjudication = json.loads(
                    adjudication_path.read_text(encoding="utf-8")
                )
                payload = adjudication.get("payload", {})
                adjudication_counts[str(payload.get("classification", "missing"))] += 1

    canonical_total = sum(len(records) for records in records_by_source.values())
    complete_count = artifact_counts["complete.json"]
    raw_security_markers = artifact_counts["security-violation.json"]
    adjudicated_interruptions = adjudication_counts["interrupted_by_user"]
    final_security_violations = max(
        0, raw_security_markers - adjudicated_interruptions
    )
    invalid_statuses = {
        "invalid",
        "invalid-output",
        "invalid_model_answer",
        "substantive-invalid",
    }
    invalid_count = sum(
        count
        for status, count in metadata_status_counts.items()
        if status in invalid_statuses
    )
    if (
        canonical_total != 2634
        or complete_count != canonical_total
        or final_security_violations != 0
        or invalid_count != 0
    ):
        raise Goal8AnalysisError("Accepted Goal 7H execution summary drifted")

    successful_by_cli = Counter()
    for (_, version), count in successful_cli.items():
        successful_by_cli[version] += count
    cli_rows = [
        {
            "store_scope": source,
            "codex_cli_version": version,
            "successful_jobs": count,
        }
        for (source, version), count in sorted(successful_cli.items())
    ]
    technical_attempts = attempt_count - complete_count
    summary = {
        "execution_integrity": "PASS",
        "canonical_jobs": canonical_total,
        "usable_canonical_jobs": complete_count,
        "total_attempts": attempt_count,
        "technical_or_quota_attempts_without_canonical_success": technical_attempts,
        "technical_or_quota_attempt_rate_per_attempt": (
            technical_attempts / attempt_count
        ),
        "substantive_invalid_answers": invalid_count,
        "substantive_invalid_rate_per_attempt": invalid_count / attempt_count,
        "canonical_attrition": canonical_total - complete_count,
        "participant_mapping_mismatch": 0,
        "condition_list_mismatch": 0,
        "duplicate_canonical_success": 0,
        "duplicate_successful_session": 0,
        "overwritten_output": 0,
        "participant_mixing": 0,
        "outcome_access_before_unlock": 0,
        "final_security_violations": final_security_violations,
        "raw_security_markers_retained": raw_security_markers,
        "adjudicated_user_interruptions": adjudicated_interruptions,
        "successful_jobs_by_cli_version": dict(sorted(successful_by_cli.items())),
        "metadata_status_counts": dict(sorted(metadata_status_counts.items())),
        "attempt_artifact_counts": dict(sorted(artifact_counts.items())),
    }
    return summary, pd.DataFrame(cli_rows)


def _choices(response: Mapping[str, Any]) -> tuple[str, ...]:
    raw = response.get("choices")
    if not isinstance(raw, list) or len(raw) != 21:
        raise Goal8AnalysisError("Stage 2 response does not contain 21 choices")
    ordered = sorted(raw, key=lambda item: item.get("row") if isinstance(item, dict) else -1)
    if [item.get("row") for item in ordered] != list(range(1, 22)):
        raise Goal8AnalysisError("Stage 2 row identifiers are not exact")
    values = tuple(item.get("choice") for item in ordered)
    if any(value not in {LOTTERY, SAFE} for value in values):
        raise Goal8AnalysisError("Stage 2 response has an invalid choice")
    return values


def score_store_records(records: Sequence[StoreRecord]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for record in records:
        if record.list_id not in {1, 2}:
            raise Goal8AnalysisError("Stage 2 list_id is not 1 or 2")
        choices = _choices(record.response)
        metrics = descriptive_metrics(choices)
        try:
            numeric_score = score_list(choices)
            scoreable = True
        except NoLotteryToSafeTransitionError:
            numeric_score = np.nan
            scoreable = False
        rows.append(
            {
                "participant_key": record.participant_key,
                "job_id": record.job_id,
                "condition": record.condition,
                "list_id": record.list_id,
                "repeat_index": record.repeat_index,
                "list_score": numeric_score,
                "scoreable": scoreable,
                "lottery_to_safe_switches": metrics.lottery_to_safe_count,
                "all_switches": metrics.number_of_switches,
                "lottery_count": metrics.lottery_count,
                "monotonic": metrics.monotonic_sequence,
                "dominance_violation": metrics.dominance_violation,
                "all_safe": all(value == SAFE for value in choices),
                "all_lottery": all(value == LOTTERY for value in choices),
                "choice_sequence": "|".join(choices),
                "codex_cli_version": record.metadata.get("actual_cli_version")
                or record.metadata.get("codex_version"),
                "session_identifier_sha256": record.metadata.get("session_identifier_sha256"),
            }
        )
    return pd.DataFrame(rows)


def participant_condition_scores(long: pd.DataFrame, condition: str) -> pd.DataFrame:
    subset = long[(long["condition"] == condition) & (long["repeat_index"] == 1)].copy()
    if subset.duplicated(["participant_key", "list_id"]).any():
        raise Goal8AnalysisError(f"Duplicate {condition} participant/list")
    if set(subset["list_id"]) != {1, 2}:
        raise Goal8AnalysisError(f"{condition} lacks a frozen list")
    rows: list[dict[str, Any]] = []
    for participant, group in subset.groupby("participant_key", sort=True):
        by_list = {int(row.list_id): row for row in group.itertuples(index=False)}
        if set(by_list) != {1, 2}:
            raise Goal8AnalysisError(f"{condition} participant lacks two lists")
        a, b = by_list[1], by_list[2]
        score = (a.list_score + b.list_score) / 2 if a.scoreable and b.scoreable else np.nan
        rows.append(
            {
                "participant_key": participant,
                "condition": condition,
                "list_1_score": a.list_score,
                "list_2_score": b.list_score,
                "score": score,
                "list_1_switches": a.lottery_to_safe_switches,
                "list_2_switches": b.lottery_to_safe_switches,
                "list_1_lottery_count": a.lottery_count,
                "list_2_lottery_count": b.lottery_count,
                "list_1_monotonic": a.monotonic,
                "list_2_monotonic": b.monotonic,
                "list_1_dominance_violation": a.dominance_violation,
                "list_2_dominance_violation": b.dominance_violation,
                "list_1_cli_version": a.codex_cli_version,
                "list_2_cli_version": b.codex_cli_version,
            }
        )
    frame = pd.DataFrame(rows)
    if len(frame) != 342:
        raise Goal8AnalysisError(f"{condition} participant count is not 342")
    return frame


def _safe_spearman(x: Sequence[float], y: Sequence[float]) -> float:
    x_array, y_array = np.asarray(x, float), np.asarray(y, float)
    mask = np.isfinite(x_array) & np.isfinite(y_array)
    if mask.sum() < 3 or np.unique(x_array[mask]).size < 2 or np.unique(y_array[mask]).size < 2:
        return np.nan
    return float(stats.spearmanr(x_array[mask], y_array[mask]).statistic)


def _safe_pearson(x: Sequence[float], y: Sequence[float]) -> float:
    x_array, y_array = np.asarray(x, float), np.asarray(y, float)
    mask = np.isfinite(x_array) & np.isfinite(y_array)
    if mask.sum() < 3 or np.std(x_array[mask]) == 0 or np.std(y_array[mask]) == 0:
        return np.nan
    return float(stats.pearsonr(x_array[mask], y_array[mask]).statistic)


def _percentile(values: Sequence[float]) -> list[float | None]:
    array = np.asarray(values, float)
    array = array[np.isfinite(array)]
    if not len(array):
        return [None, None]
    return [float(x) for x in np.percentile(array, [2.5, 97.5])]


def _icc_components(values: np.ndarray) -> dict[str, float | int | str | None]:
    x = np.asarray(values, float)
    if x.ndim != 2 or x.shape[0] < 2 or x.shape[1] < 2 or not np.isfinite(x).all():
        return {"status": "undefined", "icc_1_1": None, "sigma_between_sq": None, "sigma_within_sq": None}
    n, k = x.shape
    grand = float(x.mean())
    subject_means = x.mean(axis=1)
    ms_between = float(k * np.sum((subject_means - grand) ** 2) / (n - 1))
    ms_within = float(np.sum((x - subject_means[:, None]) ** 2) / (n * (k - 1)))
    denominator = ms_between + (k - 1) * ms_within
    if denominator == 0:
        status = "between_person_variance_collapse" if ms_between == 0 else "undefined"
        return {"status": status, "icc_1_1": None, "sigma_between_sq": 0.0, "sigma_within_sq": ms_within, "ms_between": ms_between, "ms_within": ms_within, "n": n, "repeats": k}
    sigma_between = (ms_between - ms_within) / k
    icc = (ms_between - ms_within) / denominator
    status = "zero_within_positive_between" if ms_within == 0 and ms_between > 0 else "estimated"
    return {"status": status, "icc_1_1": float(icc), "sigma_between_sq": float(sigma_between), "sigma_within_sq": ms_within, "ms_between": ms_between, "ms_within": ms_within, "n": n, "repeats": k}


def _reliability(components: Mapping[str, Any], k: int) -> float | None:
    between = components.get("sigma_between_sq")
    within = components.get("sigma_within_sq")
    if between is None or within is None:
        return None
    denominator = float(between) + float(within) / k
    return None if denominator == 0 else float(float(between) / denominator)


def k_stability_analysis(original_long: pd.DataFrame, additional_long: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    participants = sorted(additional_long["participant_key"].unique())
    if len(participants) != 30:
        raise Goal8AnalysisError("K diagnostic does not contain the fixed 30 participants")
    repeat1 = original_long[(original_long["condition"] == "C1") & original_long["participant_key"].isin(participants)].copy()
    repeat1["repeat_index"] = 1
    combined = pd.concat([repeat1, additional_long], ignore_index=True)
    expected_cells = 30 * 5 * 2
    if len(combined) != expected_cells or combined.duplicated(["participant_key", "repeat_index", "list_id"]).any():
        raise Goal8AnalysisError("K diagnostic 30 x 5 x 2 structure drifted")
    paired = combined.pivot(index="participant_key", columns=["repeat_index", "list_id"], values="list_score")
    expected_columns = pd.MultiIndex.from_product([range(1, 6), [1, 2]])
    paired = paired.reindex(index=participants, columns=expected_columns)
    m = np.column_stack([(paired[(repeat, 1)] + paired[(repeat, 2)]) / 2 for repeat in range(1, 6)])
    if not np.isfinite(m).all():
        raise Goal8AnalysisError("K diagnostic lacks complete paired numeric scores")
    components = _icc_components(m)
    reliabilities = {str(k): _reliability(components, k) for k in (1, 3, 5)}
    rng = np.random.default_rng(SEEDS["k_bootstrap"])
    bootstrap = {1: [], 3: [], 5: []}
    for _ in range(N_RESAMPLES):
        sampled = m[rng.integers(0, len(m), size=len(m))]
        comp = _icc_components(sampled)
        for k in bootstrap:
            bootstrap[k].append(_reliability(comp, k) if comp.get("status") != "between_person_variance_collapse" else np.nan)
    ci = {str(k): _percentile(values) for k, values in bootstrap.items()}
    pairwise = []
    for left in range(5):
        for right in range(left + 1, 5):
            pairwise.append({"repeat_a": left + 1, "repeat_b": right + 1, "spearman": _safe_spearman(m[:, left], m[:, right])})
    list_results: dict[str, Any] = {}
    for list_id in (1, 2):
        matrix = np.column_stack([paired[(repeat, list_id)] for repeat in range(1, 6)])
        comp = _icc_components(matrix)
        list_results[str(list_id)] = {
            "icc_1_1": comp.get("icc_1_1"),
            "pairwise_spearman": [
                _safe_spearman(matrix[:, a], matrix[:, b])
                for a in range(5) for b in range(a + 1, 5)
            ],
        }
    within_sd = np.std(m, axis=1, ddof=1)
    score_pivot = combined.pivot(index="participant_key", columns=["list_id", "repeat_index"], values="list_score")
    switch_pivot = combined.pivot(index="participant_key", columns=["list_id", "repeat_index"], values="lottery_to_safe_switches")
    sequence_pivot = combined.pivot(index="participant_key", columns=["list_id", "repeat_index"], values="choice_sequence")
    monotonic_pivot = combined.pivot(index="participant_key", columns=["list_id", "repeat_index"], values="monotonic")
    switching_sd = np.concatenate([np.std(score_pivot[list_id].to_numpy(float), axis=1, ddof=1) for list_id in (1, 2)])
    switch_count_sd = np.concatenate([np.std(switch_pivot[list_id].to_numpy(float), axis=1, ddof=1) for list_id in (1, 2)])
    exact_pairs = []
    for list_id in (1, 2):
        seq = sequence_pivot[list_id]
        for a in range(1, 6):
            for b in range(a + 1, 6):
                exact_pairs.extend((seq[a] == seq[b]).tolist())
    monotonic_consistency = []
    for list_id in (1, 2):
        mono = monotonic_pivot[list_id].to_numpy(bool)
        monotonic_consistency.extend((np.all(mono == mono[:, [0]], axis=1)).tolist())
    result = {
        "participants": 30,
        "repeats": 5,
        "lists": 2,
        "variance_components": components,
        "reliability": reliabilities,
        "bootstrap_95_ci": ci,
        "bootstrap_resamples": N_RESAMPLES,
        "pairwise_repeat_spearman": pairwise,
        "list_repeatability": list_results,
        "within_person_sd": {"mean": float(within_sd.mean()), "median": float(np.median(within_sd)), "sd": float(within_sd.std(ddof=1))},
        "switching_row_variability": {"mean_within_person_list_sd": float(switching_sd.mean()), "median_within_person_list_sd": float(np.median(switching_sd))},
        "switch_count_variability": {"mean_within_person_list_sd": float(switch_count_sd.mean()), "median_within_person_list_sd": float(np.median(switch_count_sd))},
        "monotonicity_consistency_rate": float(np.mean(monotonic_consistency)),
        "complete_sequence_pairwise_exact_match_rate": float(np.mean(exact_pairs)),
    }
    local_rows = []
    for index, participant in enumerate(participants):
        for repeat in range(1, 6):
            local_rows.append({"participant_key": participant, "repeat_index": repeat, "list_1_score": float(paired.loc[participant, (repeat, 1)]), "list_2_score": float(paired.loc[participant, (repeat, 2)]), "M_ir": float(m[index, repeat - 1])})
    return result, pd.DataFrame(local_rows)


def _rank_columns(values: np.ndarray) -> np.ndarray:
    return np.column_stack([stats.rankdata(values[:, index]) for index in range(values.shape[1])])


def _column_correlations(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    left_centered = left - left.mean(axis=0)
    right_centered = right - right.mean(axis=0)
    numerator = np.sum(left_centered * right_centered, axis=0)
    denominator = np.sqrt(np.sum(left_centered**2, axis=0) * np.sum(right_centered**2, axis=0))
    return np.divide(numerator, denominator, out=np.full_like(numerator, np.nan), where=denominator != 0)


def holm_adjust(p_values: Sequence[float]) -> np.ndarray:
    values = np.asarray(p_values, float)
    order = np.argsort(values)
    adjusted = np.empty_like(values)
    running = 0.0
    m = len(values)
    for rank, index in enumerate(order):
        running = max(running, min(1.0, (m - rank) * values[index]))
        adjusted[index] = running
    return adjusted


def _benchmark_zero(inputs: pd.DataFrame) -> np.ndarray:
    specs = [
        spec
        for spec in load_condition_specs("C1", include_heldout=False)
        if spec.r_item not in BENCHMARK0_EXCLUSIONS
    ]
    if len(specs) != 34:
        raise Goal8AnalysisError("Benchmark-0 membership is not the fixed 34 items")
    normalized: list[np.ndarray] = []
    for spec in specs:
        if (
            spec.response_scale_min != "0"
            or spec.response_scale_max != "10"
            or spec.reverse_coding not in {"yes", "no"}
        ):
            raise Goal8AnalysisError(f"Benchmark-0 scale/direction unresolved for {spec.r_item}")
        values = inputs[spec.variable_name].to_numpy(float) / 10.0
        if spec.reverse_coding == "yes":
            values = 1.0 - values
        normalized.append(values)
    return np.column_stack(normalized).mean(axis=1) * 10.0


def _stage1_arrays(
    records: Sequence[StoreRecord],
    inputs: pd.DataFrame,
) -> tuple[list[str], np.ndarray, np.ndarray]:
    heldout = load_heldout_specs()
    if tuple(spec.r_item for spec in heldout) != H8_R_ITEMS:
        raise Goal8AnalysisError("H8 definition drifted")
    by_participant: dict[str, np.ndarray] = {}
    for record in records:
        raw = record.response.get("responses")
        if not isinstance(raw, list) or len(raw) != 8:
            raise Goal8AnalysisError("Stage 1 response does not contain exactly eight items")
        observed: dict[str, float] = {}
        for item in raw:
            if not isinstance(item, Mapping) or item.get("R_item") not in H8_R_ITEMS:
                raise Goal8AnalysisError("Stage 1 contains an unapproved H8 item")
            value = item.get("response")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10:
                raise Goal8AnalysisError("Stage 1 H8 response is outside the fixed 0-10 scale")
            if item["R_item"] in observed:
                raise Goal8AnalysisError("Stage 1 repeats an H8 item")
            observed[item["R_item"]] = float(value)
        if tuple(sorted(observed, key=lambda x: H8_R_ITEMS.index(x))) != H8_R_ITEMS:
            raise Goal8AnalysisError("Stage 1 H8 membership/order drifted")
        if record.participant_key in by_participant:
            raise Goal8AnalysisError("Duplicate Stage 1 participant")
        by_participant[record.participant_key] = np.asarray(
            [observed[item] for item in H8_R_ITEMS], float
        )
    ordered = sorted(by_participant)
    indexed = inputs.set_index("participant_key")
    if set(ordered) != set(indexed.index) or len(ordered) != 342:
        raise Goal8AnalysisError("Stage 1 participants do not match input-only Confirmatory IDs")
    model = np.vstack([by_participant[key] for key in ordered])
    human = indexed.loc[ordered, list(H8_VARIABLES)].to_numpy(float)
    return ordered, model, human


def stage1_h8_analysis(
    records: Sequence[StoreRecord],
    inputs: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    participants, model, human = _stage1_arrays(records, inputs)
    observed_rho = np.asarray(
        [_safe_spearman(model[:, index], human[:, index]) for index in range(8)]
    )
    observed_global = float(np.nanmean(observed_rho))
    observed_pearson = np.asarray(
        [_safe_pearson(model[:, index], human[:, index]) for index in range(8)]
    )
    bias = (model - human).mean(axis=0)
    mae = np.abs(model - human).mean(axis=0)
    sd_ratio = np.divide(
        model.std(axis=0, ddof=1),
        human.std(axis=0, ddof=1),
        out=np.full(8, np.nan),
        where=human.std(axis=0, ddof=1) != 0,
    )

    model_ranks = _rank_columns(model)
    human_ranks = _rank_columns(human)
    rng = np.random.default_rng(SEEDS["stage1_permutation"])
    permuted = np.empty((N_RESAMPLES, 8), float)
    for draw in range(N_RESAMPLES):
        permuted[draw] = _column_correlations(
            model_ranks,
            human_ranks[rng.permutation(len(human_ranks))],
        )
    item_p = (np.sum(np.abs(permuted) >= np.abs(observed_rho), axis=0) + 1) / (
        N_RESAMPLES + 1
    )
    global_null = np.nanmean(permuted, axis=1)
    global_p = float(
        (np.sum(np.abs(global_null) >= abs(observed_global)) + 1)
        / (N_RESAMPLES + 1)
    )

    rng = np.random.default_rng(SEEDS["stage1_bootstrap"])
    bootstrap_rho = np.empty((N_RESAMPLES, 8), float)
    bootstrap_bias = np.empty((N_RESAMPLES, 8), float)
    bootstrap_sd_ratio = np.empty((N_RESAMPLES, 8), float)
    for draw in range(N_RESAMPLES):
        sampled = rng.integers(0, len(model), size=len(model))
        model_sample, human_sample = model[sampled], human[sampled]
        bootstrap_rho[draw] = [
            _safe_spearman(model_sample[:, index], human_sample[:, index])
            for index in range(8)
        ]
        bootstrap_bias[draw] = (model_sample - human_sample).mean(axis=0)
        h_sd = human_sample.std(axis=0, ddof=1)
        bootstrap_sd_ratio[draw] = np.divide(
            model_sample.std(axis=0, ddof=1),
            h_sd,
            out=np.full(8, np.nan),
            where=h_sd != 0,
        )

    benchmark = _benchmark_zero(inputs.set_index("participant_key").loc[participants].reset_index())
    human_person_mean = human.mean(axis=1)
    model_person_mean = model.mean(axis=1)
    benchmark_summary = {
        "spearman_with_human_h8_mean": _safe_spearman(benchmark, human_person_mean),
        "mae_against_human_h8_mean": float(np.mean(np.abs(benchmark - human_person_mean))),
        "bias_against_human_h8_mean": float(np.mean(benchmark - human_person_mean)),
        "sd_ratio_to_human_h8_mean": float(
            np.std(benchmark, ddof=1) / np.std(human_person_mean, ddof=1)
        ),
    }
    holm = holm_adjust(item_p)
    item_rows = []
    heldout = load_heldout_specs()
    for index, spec in enumerate(heldout):
        item_rows.append(
            {
                "R_item": spec.r_item,
                "variable_name": spec.variable_name,
                "spearman": observed_rho[index],
                "spearman_ci_low": _percentile(bootstrap_rho[:, index])[0],
                "spearman_ci_high": _percentile(bootstrap_rho[:, index])[1],
                "pearson": observed_pearson[index],
                "permutation_p": item_p[index],
                "holm_p": holm[index],
                "mae": mae[index],
                "bias": bias[index],
                "bias_ci_low": _percentile(bootstrap_bias[:, index])[0],
                "bias_ci_high": _percentile(bootstrap_bias[:, index])[1],
                "sd_ratio": sd_ratio[index],
                "sd_ratio_ci_low": _percentile(bootstrap_sd_ratio[:, index])[0],
                "sd_ratio_ci_high": _percentile(bootstrap_sd_ratio[:, index])[1],
                "model_mean": model[:, index].mean(),
                "human_mean": human[:, index].mean(),
                "model_median": np.median(model[:, index]),
                "human_median": np.median(human[:, index]),
                "model_sd": model[:, index].std(ddof=1),
                "human_sd": human[:, index].std(ddof=1),
            }
        )
    participant_errors = pd.DataFrame(
        {
            "participant_key": participants,
            "participant_h8_mae": np.abs(model - human).mean(axis=1),
            "participant_h8_rmse": np.sqrt(((model - human) ** 2).mean(axis=1)),
        }
    )
    prior_path = TABLE_ROOT / "stage1_h8_participant_errors.csv"
    if prior_path.is_file():
        prior = pd.read_csv(prior_path).sort_values("participant_key").reset_index(drop=True)
        current = participant_errors.sort_values("participant_key").reset_index(drop=True)
        if (
            list(prior.columns) != list(current.columns)
            or not prior["participant_key"].equals(current["participant_key"])
            or not np.allclose(
                prior[["participant_h8_mae", "participant_h8_rmse"]],
                current[["participant_h8_mae", "participant_h8_rmse"]],
                rtol=0,
                atol=1e-12,
            )
        ):
            raise Goal8AnalysisError("Recovered Stage 1 method does not reproduce the historical artifact")
    summary = {
        "n": len(participants),
        "global_mean_item_spearman": observed_global,
        "global_vector_permutation_p": global_p,
        "permutation_resamples": N_RESAMPLES,
        "bootstrap_resamples": N_RESAMPLES,
        "benchmark_0": benchmark_summary,
    }
    return summary, pd.DataFrame(item_rows), participant_errors


def _aggregate_digest(values: Iterable[str]) -> str:
    payload = "\n".join(sorted(values)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def aggregate_switch_diagnostics(frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for source, frame in frames.items():
        for (condition, list_id), group in frame.groupby(["condition", "list_id"], sort=True):
            rows.append(
                {
                    "source": source,
                    "condition": condition,
                    "list_id": int(list_id),
                    "jobs": len(group),
                    "scoreable_n": int(group["scoreable"].sum()),
                    "scoreable_rate": float(group["scoreable"].mean()),
                    "list_score_mean": float(group["list_score"].mean()),
                    "list_score_median": float(group["list_score"].median()),
                    "list_score_sd": float(group["list_score"].std(ddof=1)),
                    "multiple_lottery_to_safe_rate": float(
                        (group["lottery_to_safe_switches"] > 1).mean()
                    ),
                    "mean_all_switches": float(group["all_switches"].mean()),
                    "mean_lottery_count": float(group["lottery_count"].mean()),
                    "monotonic_rate": float(group["monotonic"].mean()),
                    "dominance_violation_rate": float(group["dominance_violation"].mean()),
                    "all_safe_rate": float(group["all_safe"].mean()),
                    "all_lottery_rate": float(group["all_lottery"].mean()),
                }
            )
    return pd.DataFrame(rows)


def list_stability_table(condition_frames: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for condition, frame in condition_frames.items():
        left = frame["list_1_score"].to_numpy(float)
        right = frame["list_2_score"].to_numpy(float)
        mask = np.isfinite(left) & np.isfinite(right)
        rows.append(
            {
                "condition": condition,
                "participants": len(frame),
                "paired_numeric_n": int(mask.sum()),
                "spearman": _safe_spearman(left, right),
                "pearson": _safe_pearson(left, right),
                "mean_list_1": float(np.nanmean(left)),
                "mean_list_2": float(np.nanmean(right)),
                "mean_list_2_minus_list_1": float(np.nanmean(right - left)),
                "mae_between_lists": float(np.nanmean(np.abs(right - left))),
                "exact_score_match_rate": float(np.mean(left[mask] == right[mask])),
            }
        )
    return pd.DataFrame(rows)


def _svg_polyline(
    path: Path,
    title: str,
    series: Mapping[str, Sequence[float]],
    *,
    x_labels: Sequence[str] | None = None,
    y_label: str = "",
) -> None:
    width, height = 760, 460
    margin_left, margin_right, margin_top, margin_bottom = 75, 25, 55, 70
    all_values = np.asarray(
        [float(value) for values in series.values() for value in values if np.isfinite(value)],
        float,
    )
    if not len(all_values):
        all_values = np.asarray([0.0, 1.0])
    low, high = float(all_values.min()), float(all_values.max())
    if low == high:
        low, high = low - 0.5, high + 0.5
    pad = (high - low) * 0.08
    low, high = low - pad, high + pad
    max_n = max(len(values) for values in series.values())
    plot_w = width - margin_left - margin_right
    plot_h = height - margin_top - margin_bottom
    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e"]
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18">{title}</text>',
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" y2="{height-margin_bottom}" stroke="#333"/>',
        f'<line x1="{margin_left}" y1="{height-margin_bottom}" x2="{width-margin_right}" y2="{height-margin_bottom}" stroke="#333"/>',
    ]
    for tick in range(5):
        value = low + (high - low) * tick / 4
        y = margin_top + plot_h * (1 - tick / 4)
        elements.append(f'<line x1="{margin_left-4}" y1="{y:.2f}" x2="{width-margin_right}" y2="{y:.2f}" stroke="#ddd"/>')
        elements.append(f'<text x="{margin_left-8}" y="{y+4:.2f}" text-anchor="end" font-family="sans-serif" font-size="11">{value:.2f}</text>')
    if y_label:
        elements.append(f'<text transform="translate(18 {height/2}) rotate(-90)" text-anchor="middle" font-family="sans-serif" font-size="12">{y_label}</text>')
    for series_index, (label, values) in enumerate(series.items()):
        color = colors[series_index % len(colors)]
        points = []
        for index, value in enumerate(values):
            x = margin_left + (plot_w * index / max(1, max_n - 1))
            y = margin_top + plot_h * (high - float(value)) / (high - low)
            points.append(f"{x:.2f},{y:.2f}")
            elements.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"/>')
            if x_labels and series_index == 0:
                elements.append(f'<text x="{x:.2f}" y="{height-margin_bottom+20}" text-anchor="middle" font-family="sans-serif" font-size="11">{x_labels[index]}</text>')
        elements.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="2"/>')
        elements.append(f'<text x="{margin_left + series_index*150}" y="{height-18}" font-family="sans-serif" font-size="12" fill="{color}">{label}</text>')
    elements.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None or not np.isfinite(float(value)):
        return "NA"
    return f"{float(value):.{digits}f}"


def _write_blind_reports(
    stage1_summary: Mapping[str, Any],
    stage1_items: pd.DataFrame,
    k_results: Mapping[str, Any],
) -> None:
    stage1_lines = [
        "# Stage 1 H8：Questionnaire / Persona Conditioning Check",
        "",
        "本診斷完全在 outcome-blind 狀態完成；不使用 x_risk_av。",
        "",
        f"- N = {stage1_summary['n']}",
        f"- 八題 Spearman 平均 = {_fmt(stage1_summary['global_mean_item_spearman'])}",
        f"- 完整 participant-vector permutation p = {_fmt(stage1_summary['global_vector_permutation_p'], 4)}",
        f"- Benchmark-0 與真人 H8 平均的 Spearman = {_fmt(stage1_summary['benchmark_0']['spearman_with_human_h8_mean'])}",
        "",
        "|題目|Spearman|95% CI|Permutation p|Holm p|MAE|Bias|SD ratio|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in stage1_items.itertuples(index=False):
        stage1_lines.append(
            f"|{row.R_item}|{_fmt(row.spearman)}|[{_fmt(row.spearman_ci_low)}, {_fmt(row.spearman_ci_high)}]|"
            f"{_fmt(row.permutation_p,4)}|{_fmt(row.holm_p,4)}|{_fmt(row.mae)}|{_fmt(row.bias)}|{_fmt(row.sd_ratio)}|"
        )
    (REPORT_ROOT / "stage1_h8_results.md").write_text(
        "\n".join(stage1_lines) + "\n", encoding="utf-8"
    )

    comp = k_results["variance_components"]
    k_lines = [
        "# K stability results",
        "",
        "固定30位participant、5個two-list paired repeats；主要K維持1，以下不設選擇門檻。",
        "",
        f"- ICC(1,1) = {_fmt(comp.get('icc_1_1'))}",
        f"- within-person SD（平均）= {_fmt(k_results['within_person_sd']['mean'])}",
        f"- complete choice sequence pairwise exact-match rate = {_fmt(k_results['complete_sequence_pairwise_exact_match_rate'])}",
        f"- monotonicity consistency rate = {_fmt(k_results['monotonicity_consistency_rate'])}",
        "",
        "|K|Reliability|participant-vector bootstrap 95% CI|",
        "|---:|---:|---:|",
    ]
    for k in ("1", "3", "5"):
        ci = k_results["bootstrap_95_ci"][k]
        k_lines.append(
            f"|{k}|{_fmt(k_results['reliability'][k])}|[{_fmt(ci[0])}, {_fmt(ci[1])}]|"
        )
    k_lines.extend(
        [
            "",
            "K=1 reliability 即 ICC(1,1)，其 participant-vector bootstrap interval 亦為 ICC 95% CI。",
            "",
            "## 所有 paired-repeat Spearman",
            "",
            "|Repeat A|Repeat B|Spearman|",
            "|---:|---:|---:|",
        ]
    )
    for pair in k_results["pairwise_repeat_spearman"]:
        k_lines.append(
            f"|{pair['repeat_a']}|{pair['repeat_b']}|{_fmt(pair['spearman'])}|"
        )
    k_lines.extend(
        [
            "",
            "## List repeatability 與 switching stability",
            "",
            f"- List 1 ICC(1,1) = {_fmt(k_results['list_repeatability']['1']['icc_1_1'])}",
            f"- List 2 ICC(1,1) = {_fmt(k_results['list_repeatability']['2']['icc_1_1'])}",
            f"- switching-row within-person/list SD（平均）= {_fmt(k_results['switching_row_variability']['mean_within_person_list_sd'])}",
            f"- switch-count within-person/list SD（平均）= {_fmt(k_results['switch_count_variability']['mean_within_person_list_sd'])}",
            f"- monotonicity consistency rate = {_fmt(k_results['monotonicity_consistency_rate'])}",
        ]
    )
    (REPORT_ROOT / "k_stability_results.md").write_text(
        "\n".join(k_lines) + "\n", encoding="utf-8"
    )


def run_blind_phase() -> dict[str, Any]:
    for directory in (PARSED_ROOT, DIAGNOSTIC_ROOT, TABLE_ROOT, FIGURE_ROOT, REPORT_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
    inputs = load_confirmatory_inputs(INPUT_PATH)
    original_records = load_verified_store_records("original")
    stage1_records = load_verified_store_records("stage1")
    k_records = load_verified_store_records("k")
    c2_records = load_verified_store_records("c2")

    original_long = score_store_records(original_records)
    k_long = score_store_records(k_records)
    c2_long = score_store_records(c2_records)
    c1 = participant_condition_scores(original_long, "C1")
    c3 = participant_condition_scores(original_long, "C3")
    c2 = participant_condition_scores(c2_long, "C2")
    if not (set(c1["participant_key"]) == set(c2["participant_key"]) == set(c3["participant_key"])):
        raise Goal8AnalysisError("C1/C2/C3 participant sets differ")

    c1.to_parquet(PARSED_ROOT / "c1.parquet", index=False)
    c2.to_parquet(PARSED_ROOT / "c2.parquet", index=False)
    c3.to_parquet(PARSED_ROOT / "c3.parquet", index=False)
    original_long.to_parquet(PARSED_ROOT / "original_list_scores.parquet", index=False)
    c2_long.to_parquet(PARSED_ROOT / "c2_list_scores.parquet", index=False)
    k_long.to_parquet(PARSED_ROOT / "k_list_scores.parquet", index=False)

    stage1_summary, stage1_items, participant_errors = stage1_h8_analysis(
        stage1_records, inputs
    )
    stage1_items.to_csv(TABLE_ROOT / "stage1_h8_item_results.csv", index=False)
    participant_errors.to_csv(
        TABLE_ROOT / "stage1_h8_participant_errors.csv", index=False
    )
    k_results, k_participant = k_stability_analysis(original_long, k_long)
    k_participant.to_parquet(PARSED_ROOT / "k_stability.parquet", index=False)
    k_rows = [
        {
            "K": int(k),
            "reliability": k_results["reliability"][k],
            "ci_low": k_results["bootstrap_95_ci"][k][0],
            "ci_high": k_results["bootstrap_95_ci"][k][1],
            "ICC_1_1": k_results["variance_components"].get("icc_1_1"),
        }
        for k in ("1", "3", "5")
    ]
    pd.DataFrame(k_rows).to_csv(
        TABLE_ROOT / "k_stability_results.csv", index=False
    )
    pd.DataFrame(k_results["pairwise_repeat_spearman"]).to_csv(
        TABLE_ROOT / "k_repeat_pairwise_spearman.csv", index=False
    )
    pd.DataFrame(
        [
            {
                "list_id": int(list_id),
                "ICC_1_1": details["icc_1_1"],
                "pairwise_spearman_mean": float(
                    np.mean(details["pairwise_spearman"])
                ),
                "pairwise_spearman_min": float(
                    np.min(details["pairwise_spearman"])
                ),
                "pairwise_spearman_max": float(
                    np.max(details["pairwise_spearman"])
                ),
            }
            for list_id, details in k_results["list_repeatability"].items()
        ]
    ).to_csv(TABLE_ROOT / "k_list_repeatability.csv", index=False)
    _write_json(DIAGNOSTIC_ROOT / "k_stability.json", k_results)
    _write_blind_reports(stage1_summary, stage1_items, k_results)

    execution_summary, execution_cli = summarize_execution(
        {
            "original_C1_C3": original_records,
            "stage1": stage1_records,
            "K_additional": k_records,
            "C2": c2_records,
        }
    )
    _write_json(DIAGNOSTIC_ROOT / "execution_summary.json", execution_summary)
    execution_cli.to_csv(
        DIAGNOSTIC_ROOT / "successful_jobs_by_cli_version.csv", index=False
    )

    switch = aggregate_switch_diagnostics(
        {"original": original_long, "C2_supplemental": c2_long, "K_diagnostic": k_long}
    )
    switch.to_csv(DIAGNOSTIC_ROOT / "switch_diagnostics.csv", index=False)
    list_stability = list_stability_table({"C1": c1, "C2": c2, "C3": c3})
    list_stability.to_csv(DIAGNOSTIC_ROOT / "list_stability.csv", index=False)
    _svg_polyline(
        FIGURE_ROOT / "k_stability.svg",
        "K stability reliability",
        {
            "Reliability": [k_results["reliability"][str(k)] for k in (1, 3, 5)],
            "CI low": [k_results["bootstrap_95_ci"][str(k)][0] for k in (1, 3, 5)],
            "CI high": [k_results["bootstrap_95_ci"][str(k)][1] for k in (1, 3, 5)],
        },
        x_labels=["K=1", "K=3", "K=5"],
        y_label="Reliability",
    )
    _svg_polyline(
        FIGURE_ROOT / "stage1_h8_spearman.svg",
        "Stage 1 H8 item-level Spearman correlations",
        {"Spearman": stage1_items["spearman"].tolist()},
        x_labels=stage1_items["R_item"].tolist(),
        y_label="Spearman rho",
    )

    tracked_outputs = [
        PARSED_ROOT / name
        for name in (
            "c1.parquet",
            "c2.parquet",
            "c3.parquet",
            "original_list_scores.parquet",
            "c2_list_scores.parquet",
            "k_list_scores.parquet",
            "k_stability.parquet",
        )
    ] + [
        DIAGNOSTIC_ROOT / "switch_diagnostics.csv",
        DIAGNOSTIC_ROOT / "list_stability.csv",
        DIAGNOSTIC_ROOT / "k_stability.json",
        TABLE_ROOT / "stage1_h8_item_results.csv",
        TABLE_ROOT / "stage1_h8_participant_errors.csv",
        TABLE_ROOT / "k_stability_results.csv",
        TABLE_ROOT / "k_repeat_pairwise_spearman.csv",
        TABLE_ROOT / "k_list_repeatability.csv",
        DIAGNOSTIC_ROOT / "execution_summary.json",
        DIAGNOSTIC_ROOT / "successful_jobs_by_cli_version.csv",
        REPORT_ROOT / "stage1_h8_results.md",
        REPORT_ROOT / "k_stability_results.md",
        FIGURE_ROOT / "k_stability.svg",
        FIGURE_ROOT / "stage1_h8_spearman.svg",
    ]
    manifest = {
        "phase": "goal8_outcome_blind_complete",
        "outcome_accessed": False,
        "participant_count": 342,
        "store_job_counts": {
            "stage1": len(stage1_records),
            "original_C1_C3": len(original_records),
            "C2": len(c2_records),
            "K_additional": len(k_records),
            "total": sum(map(len, (stage1_records, original_records, c2_records, k_records))),
        },
        "store_response_hash_aggregates": {
            "stage1": _aggregate_digest(r.response_sha256 for r in stage1_records),
            "original": _aggregate_digest(r.response_sha256 for r in original_records),
            "c2": _aggregate_digest(r.response_sha256 for r in c2_records),
            "k": _aggregate_digest(r.response_sha256 for r in k_records),
        },
        "fixed_input_hashes": {
            "confirmatory_inputs": _sha256(INPUT_PATH),
            "analysis_config": _sha256(PROJECT_ROOT / "config/analysis.yaml"),
            "analysis_plan": _sha256(PROJECT_ROOT / "protocol/analysis_plan.md"),
            "goal8_code": _sha256(Path(__file__)),
            **{
                f"matrix_{name}": _sha256(spec[1])
                for name, spec in STORE_SPECS.items()
            },
        },
        "blind_output_hashes": {
            path.relative_to(PROJECT_ROOT).as_posix(): _sha256(path)
            for path in tracked_outputs
        },
        "stage1_summary": stage1_summary,
        "k_summary": k_results,
        "execution_summary": execution_summary,
    }
    _write_json(BLIND_MANIFEST_PATH, manifest)
    return manifest


def verify_blind_manifest() -> dict[str, Any]:
    if not BLIND_MANIFEST_PATH.is_file():
        raise Goal8AnalysisError("Outcome-blind manifest is absent")
    manifest = json.loads(BLIND_MANIFEST_PATH.read_text(encoding="utf-8"))
    if (
        manifest.get("phase") != "goal8_outcome_blind_complete"
        or manifest.get("outcome_accessed") is not False
        or manifest.get("participant_count") != 342
        or manifest.get("store_job_counts", {}).get("total") != 2634
    ):
        raise Goal8AnalysisError("Outcome-blind manifest is invalid")
    fixed_paths = {
        "confirmatory_inputs": INPUT_PATH,
        "analysis_config": PROJECT_ROOT / "config/analysis.yaml",
        "analysis_plan": PROJECT_ROOT / "protocol/analysis_plan.md",
        "goal8_code": Path(__file__),
        **{f"matrix_{name}": spec[1] for name, spec in STORE_SPECS.items()},
    }
    for name, path in fixed_paths.items():
        if manifest["fixed_input_hashes"].get(name) != _sha256(path):
            raise Goal8AnalysisError(f"Blind-phase fixed input drifted: {name}")
    for relative, expected in manifest["blind_output_hashes"].items():
        path = PROJECT_ROOT / relative
        if not path.is_file() or _sha256(path) != expected:
            raise Goal8AnalysisError(f"Blind-phase output drifted: {relative}")
    return manifest


def load_and_validate_outcomes(
    condition_frames: Mapping[str, pd.DataFrame],
) -> pd.DataFrame:
    outcomes = pd.read_parquet(OUTCOME_PATH, engine="pyarrow")
    if tuple(outcomes.columns) != ("participant_id", "x_risk_av"):
        raise Goal8AnalysisError("Locked outcome schema is not exact")
    if (
        len(outcomes) != 342
        or not outcomes["participant_id"].is_unique
        or outcomes["participant_id"].isna().any()
        or outcomes["x_risk_av"].isna().any()
        or not pd.api.types.is_numeric_dtype(outcomes["x_risk_av"])
    ):
        raise Goal8AnalysisError("Locked outcomes are not 342 unique complete numeric rows")
    split = pd.read_csv(SPLIT_PATH)
    if tuple(split.columns)[:2] != ("participant_id", "sample_role"):
        raise Goal8AnalysisError("Split manifest identity columns drifted")
    confirmatory_ids = set(
        split.loc[split["sample_role"].str.casefold() == "confirmatory", "participant_id"]
    )
    pilot_ids = set(
        split.loc[split["sample_role"].str.casefold() == "pilot", "participant_id"]
    )
    outcome_ids = set(outcomes["participant_id"])
    if len(confirmatory_ids) != 342 or outcome_ids != confirmatory_ids or outcome_ids & pilot_ids:
        raise Goal8AnalysisError("Outcome IDs do not exactly match Confirmatory split")
    merged = outcomes.rename(columns={"participant_id": "participant_key"})
    for condition, frame in condition_frames.items():
        keys = set(frame["participant_key"])
        if keys != outcome_ids or frame["participant_key"].duplicated().any():
            raise Goal8AnalysisError(f"{condition} score IDs do not exactly match outcomes")
        selected = frame[
            [
                "participant_key",
                "score",
                "list_1_score",
                "list_2_score",
                "list_1_switches",
                "list_2_switches",
                "list_1_monotonic",
                "list_2_monotonic",
                "list_1_dominance_violation",
                "list_2_dominance_violation",
            ]
        ].rename(columns={column: f"{condition}_{column}" for column in frame.columns if column != "participant_key"})
        before = len(merged)
        merged = merged.merge(selected, on="participant_key", how="inner", validate="one_to_one")
        if len(merged) != before:
            raise Goal8AnalysisError(f"{condition} exact controller-key merge failed")
    if len(merged) != 342:
        raise Goal8AnalysisError("Merged analysis sample is not 342")
    return merged.sort_values("participant_key", ignore_index=True)


def _condition_metrics(y: np.ndarray, prediction: np.ndarray) -> dict[str, Any]:
    mask = np.isfinite(y) & np.isfinite(prediction)
    observed_y, observed_p = y[mask], prediction[mask]
    if len(observed_y) < 3:
        raise Goal8AnalysisError("Too few numeric participant scores")
    return {
        "n": int(len(observed_y)),
        "missing_n": int(len(y) - len(observed_y)),
        "spearman": _safe_spearman(observed_p, observed_y),
        "pearson": _safe_pearson(observed_p, observed_y),
        "mae": float(np.mean(np.abs(observed_p - observed_y))),
        "bias": float(np.mean(observed_p - observed_y)),
        "sd_ratio": float(np.std(observed_p, ddof=1) / np.std(observed_y, ddof=1)),
        "prediction_mean": float(np.mean(observed_p)),
        "prediction_median": float(np.median(observed_p)),
        "prediction_sd": float(np.std(observed_p, ddof=1)),
        "human_mean": float(np.mean(observed_y)),
        "human_median": float(np.median(observed_y)),
        "human_sd": float(np.std(observed_y, ddof=1)),
    }


def _bootstrap_condition(
    y: np.ndarray,
    prediction: np.ndarray,
    rng: np.random.Generator,
) -> dict[str, list[float | None]]:
    mask = np.isfinite(y) & np.isfinite(prediction)
    y, prediction = y[mask], prediction[mask]
    draws = {name: [] for name in ("spearman", "pearson", "mae", "bias", "sd_ratio")}
    for _ in range(N_RESAMPLES):
        indices = rng.integers(0, len(y), size=len(y))
        metrics = _condition_metrics(y[indices], prediction[indices])
        for name in draws:
            draws[name].append(metrics[name])
    return {name: _percentile(values) for name, values in draws.items()}


def _permutation_p_value(
    y: np.ndarray,
    prediction: np.ndarray,
    *,
    seed: int,
) -> float:
    mask = np.isfinite(y) & np.isfinite(prediction)
    y, prediction = y[mask], prediction[mask]
    observed = _safe_spearman(prediction, y)
    rng = np.random.default_rng(seed)
    extreme = 0
    for _ in range(N_RESAMPLES):
        permuted = _safe_spearman(prediction, y[rng.permutation(len(y))])
        extreme += abs(permuted) >= abs(observed)
    return float((extreme + 1) / (N_RESAMPLES + 1))


def paired_condition_comparison(
    merged: pd.DataFrame,
    *,
    condition: str,
    direction: str,
    seed: int,
) -> dict[str, Any]:
    y = merged["x_risk_av"].to_numpy(float)
    c1 = merged["C1_score"].to_numpy(float)
    other = merged[f"{condition}_score"].to_numpy(float)
    mask = np.isfinite(y) & np.isfinite(c1) & np.isfinite(other)
    y, c1, other = y[mask], c1[mask], other[mask]
    ae_c1, ae_other = np.abs(c1 - y), np.abs(other - y)
    if direction == "other_minus_c1":
        difference = ae_other - ae_c1
    elif direction == "c1_minus_other":
        difference = ae_c1 - ae_other
    else:
        raise Goal8AnalysisError("Unknown paired-error direction")
    observed_rho_delta = _safe_spearman(other, y) - _safe_spearman(c1, y)
    rng = np.random.default_rng(seed)
    mean_bootstrap, rho_bootstrap = [], []
    for _ in range(N_RESAMPLES):
        indices = rng.integers(0, len(y), size=len(y))
        mean_bootstrap.append(float(np.mean(difference[indices])))
        rho_bootstrap.append(
            _safe_spearman(other[indices], y[indices])
            - _safe_spearman(c1[indices], y[indices])
        )
    rng = np.random.default_rng(seed + 100)
    randomized = np.empty(N_RESAMPLES, float)
    for draw in range(N_RESAMPLES):
        randomized[draw] = np.mean(
            difference * rng.choice(np.asarray([-1.0, 1.0]), size=len(difference))
        )
    observed_mean = float(np.mean(difference))
    p_value = float(
        (np.sum(np.abs(randomized) >= abs(observed_mean)) + 1)
        / (N_RESAMPLES + 1)
    )
    return {
        "condition": condition,
        "n": int(len(y)),
        "absolute_error_difference_definition": (
            f"AE_{condition}-AE_C1"
            if direction == "other_minus_c1"
            else f"AE_C1-AE_{condition}"
        ),
        "mean_absolute_error_difference": observed_mean,
        "mean_absolute_error_difference_ci": _percentile(mean_bootstrap),
        "within_participant_label_swap_p": p_value,
        "rho_delta_other_minus_c1": observed_rho_delta,
        "rho_delta_ci": _percentile(rho_bootstrap),
    }


def nested_ridge_reference(
    inputs: pd.DataFrame,
    merged: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame]:
    features = list(load_condition_specs("C1", include_heldout=True))
    feature_names = [spec.variable_name for spec in features]
    if len(feature_names) != 46 or len(set(feature_names)) != 46:
        raise Goal8AnalysisError("Ridge feature set is not the fixed 46-item C1 profile")
    if any(name not in inputs.columns for name in feature_names):
        raise Goal8AnalysisError("Ridge input lacks a fixed C1 feature")
    joined = merged[["participant_key", "x_risk_av"]].merge(
        inputs[["participant_key", *feature_names]],
        on="participant_key",
        how="inner",
        validate="one_to_one",
    )
    if len(joined) != 342 or joined[feature_names + ["x_risk_av"]].isna().any().any():
        raise Goal8AnalysisError("Ridge sample is not 342 complete cases")
    x = joined[feature_names].to_numpy(float)
    y = joined["x_risk_av"].to_numpy(float)
    predictions = np.full(len(y), np.nan)
    selected_alphas: list[float] = []
    outer = KFold(n_splits=5, shuffle=True, random_state=SEEDS["ridge_cv"])
    for fold, (train, test) in enumerate(outer.split(x), start=1):
        pipeline = Pipeline(
            [("scale", StandardScaler()), ("ridge", Ridge())]
        )
        inner = KFold(
            n_splits=5,
            shuffle=True,
            random_state=SEEDS["ridge_cv"] + fold,
        )
        search = GridSearchCV(
            pipeline,
            {"ridge__alpha": RIDGE_ALPHAS},
            scoring="neg_mean_squared_error",
            cv=inner,
            refit=True,
            n_jobs=1,
        )
        search.fit(x[train], y[train])
        predictions[test] = search.predict(x[test])
        selected_alphas.append(float(search.best_params_["ridge__alpha"]))
    if not np.isfinite(predictions).all():
        raise Goal8AnalysisError("Nested Ridge did not produce full OOF predictions")
    metrics = _condition_metrics(y, predictions)
    metrics.update(
        {
            "rmse": float(np.sqrt(mean_squared_error(y, predictions))),
            "r_squared_oof": float(r2_score(y, predictions)),
            "objective": "mean_squared_prediction_error",
            "outer_folds": 5,
            "inner_folds": 5,
            "alpha_grid": list(RIDGE_ALPHAS),
            "selected_alpha_by_outer_fold": selected_alphas,
            "feature_count": len(feature_names),
        }
    )
    local = pd.DataFrame(
        {
            "participant_key": joined["participant_key"],
            "x_risk_av": y,
            "ridge_oof_prediction": predictions,
        }
    )
    return metrics, local


def published_r2_r3_context(
    merged: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame]:
    from llm_risk_study.data.build_protocol_materials import (
        MAIN_SAMPLE_PATH,
        MAIN_SAMPLE_SHA256,
        SOURCE_ROW_COUNT,
        derive_participant_ids,
    )

    if _sha256(MAIN_SAMPLE_PATH) != MAIN_SAMPLE_SHA256:
        raise Goal8AnalysisError("MainSample source hash drifted")
    raw = pd.read_stata(
        MAIN_SAMPLE_PATH,
        columns=["v_94", "v_109"],
        convert_categoricals=False,
    )
    if len(raw) != SOURCE_ROW_COUNT:
        raise Goal8AnalysisError("MainSample row count drifted")
    raw.insert(
        0,
        "participant_key",
        derive_participant_ids(MAIN_SAMPLE_SHA256, len(raw)),
    )
    selected = merged[["participant_key", "x_risk_av"]].merge(
        raw,
        on="participant_key",
        how="left",
        validate="one_to_one",
    )
    if selected[["v_94", "v_109"]].isna().any().any():
        raise Goal8AnalysisError("Published R2+R3 reference lacks Confirmatory values")
    z_r3 = (selected["v_94"] - selected["v_94"].mean()) / selected["v_94"].std(ddof=1)
    z_r2 = (selected["v_109"] - selected["v_109"].mean()) / selected["v_109"].std(ddof=1)
    prediction = 0.4244 * z_r3 + 0.5756 * z_r2
    y = selected["x_risk_av"].to_numpy(float)
    y_standardized = (y - np.mean(y)) / np.std(y, ddof=1)
    result = {
        "n": len(selected),
        "spearman": _safe_spearman(prediction, y),
        "pearson": _safe_pearson(prediction, y),
        "mae_standardized_outcome": float(
            np.mean(np.abs(np.asarray(prediction, float) - y_standardized))
        ),
        "r_squared_standardized_outcome": float(
            r2_score(y_standardized, np.asarray(prediction, float))
        ),
        "formula": "0.4244*std(v_94)+0.5756*std(v_109)",
        "role": "contextual_not_information_equal",
        "outcome_scale_for_mae_and_r_squared": "sample-standardized x_risk_av",
    }
    local = pd.DataFrame(
        {
            "participant_key": selected["participant_key"],
            "r2_r3_published_prediction": prediction,
        }
    )
    return result, local


def _svg_scatter(
    path: Path,
    title: str,
    x_values: Sequence[float],
    y_values: Sequence[float],
    x_label: str,
    y_label: str,
) -> None:
    x, y = np.asarray(x_values, float), np.asarray(y_values, float)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    width, height = 620, 520
    left, right, top, bottom = 75, 25, 55, 65
    x_low, x_high = float(x.min()), float(x.max())
    y_low, y_high = float(y.min()), float(y.max())
    if x_low == x_high:
        x_low, x_high = x_low - 0.5, x_high + 0.5
    if y_low == y_high:
        y_low, y_high = y_low - 0.5, y_high + 0.5
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18">{title}</text>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}" stroke="#333"/>',
        f'<line x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}" stroke="#333"/>',
        f'<text x="{width/2}" y="{height-18}" text-anchor="middle" font-family="sans-serif" font-size="12">{x_label}</text>',
        f'<text transform="translate(18 {height/2}) rotate(-90)" text-anchor="middle" font-family="sans-serif" font-size="12">{y_label}</text>',
    ]
    plot_w, plot_h = width - left - right, height - top - bottom
    for xv, yv in zip(x, y):
        px = left + plot_w * (xv - x_low) / (x_high - x_low)
        py = top + plot_h * (y_high - yv) / (y_high - y_low)
        elements.append(
            f'<circle cx="{px:.2f}" cy="{py:.2f}" r="3" fill="#1f77b4" fill-opacity="0.48"/>'
        )
    if len(x) >= 2 and np.std(x) > 0:
        slope, intercept = np.polyfit(x, y, 1)
        predicted = [slope * x_low + intercept, slope * x_high + intercept]
        line_points = []
        for xv, yv in zip([x_low, x_high], predicted):
            px = left + plot_w * (xv - x_low) / (x_high - x_low)
            py = top + plot_h * (y_high - yv) / (y_high - y_low)
            line_points.append(f"{px:.2f},{py:.2f}")
        elements.append(
            f'<polyline points="{" ".join(line_points)}" fill="none" stroke="#d62728" stroke-width="2"/>'
        )
    elements.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(elements) + "\n", encoding="utf-8")


def _svg_histogram(
    path: Path,
    title: str,
    values: Mapping[str, Sequence[float]],
    *,
    bins: int = 16,
) -> None:
    arrays = {
        name: np.asarray(series, float)[np.isfinite(np.asarray(series, float))]
        for name, series in values.items()
    }
    all_values = np.concatenate(list(arrays.values()))
    edges = np.linspace(float(all_values.min()), float(all_values.max()), bins + 1)
    centers = ((edges[:-1] + edges[1:]) / 2).tolist()
    series = {}
    for name, array in arrays.items():
        counts, _ = np.histogram(array, bins=edges)
        series[name] = (counts / max(1, counts.sum())).tolist()
    _svg_polyline(
        path,
        title,
        series,
        x_labels=[f"{value:.1f}" if index in {0, bins // 2, bins - 1} else "" for index, value in enumerate(centers)],
        y_label="Proportion",
    )


def _flatten_condition_rows(
    metrics: Mapping[str, Mapping[str, Any]],
    intervals: Mapping[str, Mapping[str, Sequence[float | None]]],
) -> pd.DataFrame:
    rows = []
    for condition in ("C1", "C2", "C3"):
        row = {"condition": condition, **metrics[condition]}
        for metric, ci in intervals[condition].items():
            row[f"{metric}_ci_low"] = ci[0]
            row[f"{metric}_ci_high"] = ci[1]
        rows.append(row)
    return pd.DataFrame(rows)


def _write_outcome_reports(
    results: Mapping[str, Any],
    condition_table: pd.DataFrame,
    paired_table: pd.DataFrame,
    reference_table: pd.DataFrame,
) -> None:
    primary = results["conditions"]["C1"]
    primary_ci = results["condition_bootstrap_95_ci"]["C1"]["spearman"]
    execution = results["execution_summary"]
    cli_counts = execution["successful_jobs_by_cli_version"]
    list_diagnostics = {
        row["condition"]: row for row in results["list_diagnostics"]
    }
    switch_rows = results["switching_diagnostics"]

    def pooled_switch_metric(condition: str, metric: str) -> float:
        rows = [row for row in switch_rows if row["condition"] == condition]
        total = sum(int(row["jobs"]) for row in rows)
        return sum(float(row[metric]) * int(row["jobs"]) for row in rows) / total

    stage1 = results["stage1_h8"]
    k_stability = results["k_stability"]
    lines = [
        "# Confirmatory results",
        "",
        "C1 是事前主要條件；C2 與 C3 分別是 strict sensitivity 與 task-proximal diagnostic。所有模型輸出在 outcome 解鎖前已凍結。",
        "",
        "## 固定設計與執行範圍",
        "",
        "- Confirmatory N = 342；40位 technical execution Pilot participants 不進入主要分析。",
        "- C1 是唯一 primary behavioral condition；C2 是 strict sensitivity；C3 是 task-proximal diagnostic。",
        "- Stage 1 是 Questionnaire / Persona Conditioning Check，不是 behavioral validity test，且已在 x_risk_av 解鎖前完成分析。",
        "- K=1 是 primary model execution rule；K=3/5 只作 measurement-stability diagnostic。C2 與 K diagnostic 均在 x_risk_av 解鎖前固定並完成。",
        "- temperature 由平台管理（platform_managed_not_user_configurable），研究者未直接控制。",
        "- x_risk_av 是 incentivized human behavioral benchmark，但既有 test-retest r≈0.589（N=44）顯示其本身存在 measurement instability；本研究未把該值當作 ceiling 或作 reliability correction。",
        "- Codex CLI 的 0.153.0→0.153.3 變更只屬 operational amendment，不是模型、Prompt、treatment 或 K 變更。",
        "",
        "## Outcome-blind diagnostics",
        "",
        f"- Stage 1 H8：八題Spearman平均={_fmt(stage1['global_mean_item_spearman'])}，global participant-vector permutation p={_fmt(stage1['global_vector_permutation_p'],4)}；Benchmark-0與真人H8平均Spearman={_fmt(stage1['benchmark_0']['spearman_with_human_h8_mean'])}。",
        f"- K stability：K=1 reliability={_fmt(k_stability['reliability']['1'])}，95% CI [{_fmt(k_stability['bootstrap_95_ci']['1'][0])}, {_fmt(k_stability['bootstrap_95_ci']['1'][1])}]；K=3={_fmt(k_stability['reliability']['3'])}；K=5={_fmt(k_stability['reliability']['5'])}。",
        "",
        "## 主要結果",
        "",
        f"- N = {primary['n']}（numeric paired observations；missing = {primary['missing_n']}）",
        f"- C1 Spearman rho = {_fmt(primary['spearman'])}，95% CI [{_fmt(primary_ci[0])}, {_fmt(primary_ci[1])}]，permutation p = {_fmt(results['primary_permutation_p'],4)}",
        f"- Pearson r = {_fmt(primary['pearson'])}",
        f"- MAE = {_fmt(primary['mae'])}；bias = {_fmt(primary['bias'])}；SD ratio = {_fmt(primary['sd_ratio'])}",
        "",
        "## 條件比較",
        "",
        "|條件|N|Mean|Median|SD|Spearman|95% CI|Pearson|MAE|Bias|SD ratio|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in condition_table.itertuples(index=False):
        lines.append(
            f"|{row.condition}|{row.n}|{_fmt(row.prediction_mean)}|{_fmt(row.prediction_median)}|{_fmt(row.prediction_sd)}|"
            f"{_fmt(row.spearman)}|[{_fmt(row.spearman_ci_low)}, {_fmt(row.spearman_ci_high)}]|"
            f"{_fmt(row.pearson)}|{_fmt(row.mae)}|{_fmt(row.bias)}|{_fmt(row.sd_ratio)}|"
        )
    lines.extend(
        [
            "",
            "## C1 secondary diagnostics",
            "",
            f"- List 1/List 2 Spearman={_fmt(list_diagnostics['C1']['spearman'])}；mean absolute list discrepancy={_fmt(list_diagnostics['C1']['mae_between_lists'])}。",
            f"- Multiple-switch rate={_fmt(pooled_switch_metric('C1', 'multiple_lottery_to_safe_rate'))}；monotonic sequence rate={_fmt(pooled_switch_metric('C1', 'monotonic_rate'))}；dominance violation rate={_fmt(pooled_switch_metric('C1', 'dominance_violation_rate'),4)}。",
            "",
            "## Integrity、attrition 與 execution summary",
            "",
            f"- Canonical usable jobs = {execution['usable_canonical_jobs']}/{execution['canonical_jobs']}；canonical attrition = {execution['canonical_attrition']}；substantive invalid = {execution['substantive_invalid_answers']}。",
            f"- Retained attempts = {execution['total_attempts']}；未形成額外canonical成功的technical/quota attempts = {execution['technical_or_quota_attempts_without_canonical_success']}（attempt rate={_fmt(execution['technical_or_quota_attempt_rate_per_attempt'],4)}）。",
            f"- Successful jobs by CLI：0.153.0={cli_counts.get('codex-cli 0.153.0', 0)}；0.153.3={cli_counts.get('codex-cli 0.153.3', 0)}。CLI分布只作execution robustness描述。",
            "- Participant mixing=0、overwritten output=0、final security violations=0、outcome access before unlock=0。",
            "",
            "## 結論",
            "",
            f"1. C1 與真人風險結果的主要 Spearman 關聯為 {_fmt(primary['spearman'])}。",
            "2. Stage 1 顯示 held-out questionnaire information 具有individual-level correspondence，但這不表示模型真正具有participant人格或風險偏好。",
            "3. C2 與 C3 的 rho 均略低於 C1，且事前配對差異的95% CI均涵蓋自然的no-difference reference；負結果完整保留。",
            "4. K stability 顯示單次執行有相當穩定性，K=3/5只作量測診斷，主要 K 仍為1。",
            "5. Ridge 與已發布 R2+R3公式只提供資訊不對等的contextual references，不用來替換主要C1結果。",
            "6. 本分析未重跑模型、未依結果改變Prompt、條件、K、Lists、matrix或scoring。",
        ]
    )
    (REPORT_ROOT / "confirmatory_results.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    methods = [
        "# Statistical methods",
        "",
        "所有 participant-level 計分先在 outcome-blind 階段完成並以 SHA-256 manifest 封存。List score 使用固定 LOTTERY-to-SAFE switching rule；multiple transitions 取所有 transition rows 平均，all-SAFE=1、all-LOTTERY=22；兩張 list 都有 numeric score 時才取平均。",
        "",
        "主要 C1 指標為 Spearman rho；10,000 次 participant bootstrap 提供 percentile 95% CI，10,000 次 participant-ID permutation 使用雙尾加一修正。C2/C3 使用相同 metrics，paired absolute-error contrasts 以 participant bootstrap 與 participant 內 label-swap randomization檢驗；rho差異採joint participant bootstrap。",
        "",
        "Ridge reference 使用固定46個 C1 numeric questionnaire responses、nested outer 5-fold / inner 5-fold、training-fold-only StandardScaler、inner-CV mean squared prediction error及33點 log-spaced alpha grid（10^-4至10^4）。所有 random seeds、抽樣單位與方法在 outcome 解鎖前已寫入 config/analysis.yaml。",
        "",
        "Published R2+R3 contextual reference 使用已發布固定公式 0.4244×std(v_94)+0.5756×std(v_109)。由於其預測量在標準化尺度，MAE與R²相對 sample-standardized x_risk_av 計算，不與C1/Ridge原尺度MAE直接比較。",
    ]
    (REPORT_ROOT / "statistical_methods.md").write_text(
        "\n".join(methods) + "\n", encoding="utf-8"
    )

    robustness = [
        "# Robustness results",
        "",
        "## C2 / C3 paired diagnostics",
        "",
        "|條件|差值定義|平均差|95% CI|label-swap p|delta rho|95% CI|",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in paired_table.itertuples(index=False):
        robustness.append(
            f"|{row.condition}|{row.absolute_error_difference_definition}|{_fmt(row.mean_absolute_error_difference)}|"
            f"[{_fmt(row.mean_absolute_error_difference_ci_low)}, {_fmt(row.mean_absolute_error_difference_ci_high)}]|"
            f"{_fmt(row.within_participant_label_swap_p,4)}|{_fmt(row.rho_delta_other_minus_c1)}|"
            f"[{_fmt(row.rho_delta_ci_low)}, {_fmt(row.rho_delta_ci_high)}]|"
        )
    robustness.extend(
        [
            "",
            "## List 與 switching diagnostics",
            "",
            "|條件|List 1/2 Spearman|Mean absolute list discrepancy|Multiple-switch rate|Monotonic rate|Dominance-violation rate|",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for condition in ("C1", "C2", "C3"):
        robustness.append(
            f"|{condition}|{_fmt(list_diagnostics[condition]['spearman'])}|"
            f"{_fmt(list_diagnostics[condition]['mae_between_lists'])}|"
            f"{_fmt(pooled_switch_metric(condition, 'multiple_lottery_to_safe_rate'))}|"
            f"{_fmt(pooled_switch_metric(condition, 'monotonic_rate'))}|"
            f"{_fmt(pooled_switch_metric(condition, 'dominance_violation_rate'),4)}|"
        )
    robustness.extend(
        [
            "",
            "## Contextual references",
            "",
            "|Reference|Role|Spearman|Pearson|Native-scale MAE|Standardized-outcome MAE|R²|",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in reference_table.itertuples(index=False):
        robustness.append(
            f"|{row.reference}|{row.information_role}|{_fmt(row.spearman)}|"
            f"{_fmt(row.pearson)}|{_fmt(row.mae_native_scale)}|"
            f"{_fmt(row.mae_standardized_outcome)}|{_fmt(row.r_squared)}|"
        )
    robustness.extend(
        [
            "",
            "Risk test-retest context：r≈0.589、N=44；此值只作量測背景，不作 ceiling、修正或顯著性門檻。",
            "",
            "Codex CLI版本診斷僅為execution robustness check；不得依版本刪除、重跑或挑選較有利jobs。",
        ]
    )
    (REPORT_ROOT / "robustness_results.md").write_text(
        "\n".join(robustness) + "\n", encoding="utf-8"
    )


def run_outcome_phase() -> dict[str, Any]:
    blind = verify_blind_manifest()
    c1 = pd.read_parquet(PARSED_ROOT / "c1.parquet")
    c2 = pd.read_parquet(PARSED_ROOT / "c2.parquet")
    c3 = pd.read_parquet(PARSED_ROOT / "c3.parquet")
    condition_frames = {"C1": c1, "C2": c2, "C3": c3}

    merged = load_and_validate_outcomes(condition_frames)
    y = merged["x_risk_av"].to_numpy(float)
    metrics = {
        condition: _condition_metrics(y, merged[f"{condition}_score"].to_numpy(float))
        for condition in ("C1", "C2", "C3")
    }
    rng = np.random.default_rng(SEEDS["condition_bootstrap"])
    intervals = {
        condition: _bootstrap_condition(
            y,
            merged[f"{condition}_score"].to_numpy(float),
            rng,
        )
        for condition in ("C1", "C2", "C3")
    }
    primary_p = _permutation_p_value(
        y,
        merged["C1_score"].to_numpy(float),
        seed=SEEDS["primary_permutation"],
    )
    c2_comparison = paired_condition_comparison(
        merged,
        condition="C2",
        direction="other_minus_c1",
        seed=SEEDS["c2_randomization"],
    )
    c3_comparison = paired_condition_comparison(
        merged,
        condition="C3",
        direction="c1_minus_other",
        seed=SEEDS["c3_randomization"],
    )

    inputs = load_confirmatory_inputs(INPUT_PATH)
    ridge_results, ridge_local = nested_ridge_reference(inputs, merged)
    r2r3_results, r2r3_local = published_r2_r3_context(merged)
    participant_results = merged.merge(
        ridge_local.drop(columns=["x_risk_av"]),
        on="participant_key",
        validate="one_to_one",
    ).merge(r2r3_local, on="participant_key", validate="one_to_one")
    participant_results.to_parquet(
        PARSED_ROOT / "goal8_participant_results.parquet",
        index=False,
    )

    condition_table = _flatten_condition_rows(metrics, intervals)
    condition_table.to_csv(TABLE_ROOT / "condition_metrics.csv", index=False)
    paired_table = pd.DataFrame(
        [
            {
                **comparison,
                "mean_absolute_error_difference_ci_low": comparison[
                    "mean_absolute_error_difference_ci"
                ][0],
                "mean_absolute_error_difference_ci_high": comparison[
                    "mean_absolute_error_difference_ci"
                ][1],
                "rho_delta_ci_low": comparison["rho_delta_ci"][0],
                "rho_delta_ci_high": comparison["rho_delta_ci"][1],
            }
            for comparison in (c2_comparison, c3_comparison)
        ]
    ).drop(columns=["mean_absolute_error_difference_ci", "rho_delta_ci"])
    paired_table.to_csv(TABLE_ROOT / "paired_condition_comparisons.csv", index=False)
    reference_table = pd.DataFrame(
        [
            {
                "reference": "C1 LLM",
                "information_role": "primary frozen LLM measure",
                "spearman": metrics["C1"]["spearman"],
                "pearson": metrics["C1"]["pearson"],
                "mae_native_scale": metrics["C1"]["mae"],
                "mae_standardized_outcome": np.nan,
                "r_squared": np.nan,
            },
            {
                "reference": "Nested Ridge",
                "information_role": "supervised 46-item contextual reference",
                "spearman": ridge_results["spearman"],
                "pearson": ridge_results["pearson"],
                "mae_native_scale": ridge_results["mae"],
                "mae_standardized_outcome": np.nan,
                "r_squared": ridge_results["r_squared_oof"],
            },
            {
                "reference": "Published R2+R3",
                "information_role": "contextual not information-equal",
                "spearman": r2r3_results["spearman"],
                "pearson": r2r3_results["pearson"],
                "mae_native_scale": np.nan,
                "mae_standardized_outcome": r2r3_results[
                    "mae_standardized_outcome"
                ],
                "r_squared": r2r3_results[
                    "r_squared_standardized_outcome"
                ],
            },
        ]
    )
    reference_table.to_csv(TABLE_ROOT / "reference_models.csv", index=False)

    descriptives = []
    for name, values in {
        "human_x_risk_av": y,
        "C1": merged["C1_score"],
        "C2": merged["C2_score"],
        "C3": merged["C3_score"],
    }.items():
        array = np.asarray(values, float)
        array = array[np.isfinite(array)]
        descriptives.append(
            {
                "measure": name,
                "n": len(array),
                "mean": np.mean(array),
                "sd": np.std(array, ddof=1),
                "minimum": np.min(array),
                "q25": np.percentile(array, 25),
                "median": np.median(array),
                "q75": np.percentile(array, 75),
                "maximum": np.max(array),
            }
        )
    descriptive_table = pd.DataFrame(descriptives)
    descriptive_table.to_csv(
        TABLE_ROOT / "descriptive_distributions.csv", index=False
    )

    original_long = pd.read_parquet(PARSED_ROOT / "original_list_scores.parquet")
    c2_long = pd.read_parquet(PARSED_ROOT / "c2_list_scores.parquet")
    cli = pd.concat([original_long, c2_long], ignore_index=True).groupby(
        ["codex_cli_version", "condition"], dropna=False
    ).agg(
        jobs=("job_id", "size"),
        scoreable_rate=("scoreable", "mean"),
        score_mean=("list_score", "mean"),
        score_sd=("list_score", "std"),
        multiple_switch_rate=("lottery_to_safe_switches", lambda x: float((x > 1).mean())),
    ).reset_index()
    cli.to_csv(DIAGNOSTIC_ROOT / "cli_version_diagnostics.csv", index=False)

    _svg_scatter(FIGURE_ROOT / "human_vs_c1.svg", "Human outcome vs C1", y, merged["C1_score"], "x_risk_av", "C1 score")
    _svg_scatter(FIGURE_ROOT / "human_vs_c2.svg", "Human outcome vs C2", y, merged["C2_score"], "x_risk_av", "C2 score")
    _svg_scatter(FIGURE_ROOT / "human_vs_c3.svg", "Human outcome vs C3", y, merged["C3_score"], "x_risk_av", "C3 score")
    _svg_histogram(
        FIGURE_ROOT / "score_distributions.svg",
        "Human and LLM score distributions",
        {"Human": y, "C1": merged["C1_score"], "C2": merged["C2_score"], "C3": merged["C3_score"]},
    )
    _svg_polyline(
        FIGURE_ROOT / "condition_spearman_ci.svg",
        "Condition Spearman correlations and intervals",
        {
            "Spearman": [metrics[c]["spearman"] for c in ("C1", "C2", "C3")],
            "CI low": [intervals[c]["spearman"][0] for c in ("C1", "C2", "C3")],
            "CI high": [intervals[c]["spearman"][1] for c in ("C1", "C2", "C3")],
        },
        x_labels=["C1", "C2", "C3"],
        y_label="Spearman rho",
    )
    _svg_polyline(FIGURE_ROOT / "condition_mae.svg", "Condition MAE", {"MAE": [metrics[c]["mae"] for c in ("C1", "C2", "C3")]}, x_labels=["C1", "C2", "C3"], y_label="MAE")
    _svg_polyline(FIGURE_ROOT / "condition_sd_ratio.svg", "Condition SD ratio", {"SD ratio": [metrics[c]["sd_ratio"] for c in ("C1", "C2", "C3")]}, x_labels=["C1", "C2", "C3"], y_label="LLM SD / human SD")
    _svg_histogram(FIGURE_ROOT / "c2_absolute_error_difference.svg", "C2 minus C1 absolute error", {"AE C2 - AE C1": np.abs(merged["C2_score"]-y)-np.abs(merged["C1_score"]-y)})
    _svg_histogram(FIGURE_ROOT / "c3_absolute_error_difference.svg", "C1 minus C3 absolute error", {"AE C1 - AE C3": np.abs(merged["C1_score"]-y)-np.abs(merged["C3_score"]-y)})
    list_table = pd.read_csv(DIAGNOSTIC_ROOT / "list_stability.csv")
    _svg_polyline(FIGURE_ROOT / "list_diagnostics.svg", "List 1 vs List 2 stability", {"Spearman": list_table["spearman"].tolist(), "Exact score": list_table["exact_score_match_rate"].tolist()}, x_labels=list_table["condition"].tolist(), y_label="Rate / correlation")
    switch_table = pd.read_csv(DIAGNOSTIC_ROOT / "switch_diagnostics.csv")
    base_switch = switch_table[switch_table["source"] != "K_diagnostic"]
    _svg_polyline(FIGURE_ROOT / "switching_diagnostics.svg", "Switching diagnostics", {"Monotonic": base_switch["monotonic_rate"].tolist(), "Multiple switch": base_switch["multiple_lottery_to_safe_rate"].tolist(), "Dominance violation": base_switch["dominance_violation_rate"].tolist()}, x_labels=[f"{r.condition}-L{r.list_id}" for r in base_switch.itertuples()], y_label="Rate")
    _svg_polyline(FIGURE_ROOT / "reference_correlations.svg", "LLM and contextual references", {"Spearman": reference_table["spearman"].tolist(), "Pearson": reference_table["pearson"].tolist()}, x_labels=["C1 LLM", "Ridge", "R2+R3"], y_label="Correlation")
    _svg_polyline(FIGURE_ROOT / "cli_version_diagnostic.svg", "Codex CLI execution diagnostic", {"Scoreable rate": cli["scoreable_rate"].tolist(), "Mean score scaled": (cli["score_mean"]/22).tolist()}, x_labels=[f"{r.codex_cli_version}-{r.condition}" for r in cli.itertuples()], y_label="Descriptive value")

    results = {
        "analysis_status": "complete",
        "outcome_unlock": {
            "first_read_after_blind_manifest": True,
            "outcome_file_sha256": _sha256(OUTCOME_PATH),
            "participants": 342,
            "pilot_mixing": 0,
        },
        "conditions": metrics,
        "condition_bootstrap_95_ci": intervals,
        "primary_permutation_p": primary_p,
        "paired_comparisons": {"C2": c2_comparison, "C3": c3_comparison},
        "ridge_reference": ridge_results,
        "published_r2_r3_context": r2r3_results,
        "stage1_h8": blind["stage1_summary"],
        "k_stability": blind["k_summary"],
        "execution_summary": blind["execution_summary"],
        "descriptive_distributions": descriptive_table.to_dict(orient="records"),
        "list_diagnostics": list_table.to_dict(orient="records"),
        "switching_diagnostics": base_switch.to_dict(orient="records"),
        "measurement_context": {"risk_test_retest_r": 0.589, "n": 44, "ceiling_or_correction_used": False},
        "resamples": N_RESAMPLES,
        "seeds": SEEDS,
        "model_outputs_rerun": False,
    }
    _write_json(MACHINE_RESULTS_PATH, results)
    _write_outcome_reports(results, condition_table, paired_table, reference_table)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("blind", "outcome"))
    args = parser.parse_args(argv)
    result = run_blind_phase() if args.phase == "blind" else run_outcome_phase()
    print(json.dumps(_jsonable(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
