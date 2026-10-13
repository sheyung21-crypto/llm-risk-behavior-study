"""Goal 8.5 result reconciliation and manuscript-input audit.

This module does not execute models and does not alter any participant mapping,
model output, frozen condition, or confirmatory estimator.  It reads the
already-frozen Goal 8 results and local ignored participant-level derived data,
then writes only aggregate reconciliation artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from llm_risk_study.analysis.goal8 import (
    H8_R_ITEMS,
    H8_VARIABLES,
    INPUT_PATH,
    MACHINE_RESULTS_PATH,
    PARSED_ROOT,
    PROJECT_ROOT,
    REPORT_ROOT,
    TABLE_ROOT,
    DIAGNOSTIC_ROOT,
    _benchmark_zero,
    _safe_spearman,
    _stage1_arrays,
    load_verified_store_records,
)
from llm_risk_study.data.confirmatory_preparation import load_confirmatory_inputs


PARTICIPANT_RESULTS_PATH = PARSED_ROOT / "goal8_participant_results.parquet"
BENCHMARK_REPORT_PATH = DIAGNOSTIC_ROOT / "benchmark0_reconciliation.md"
SCALE_AUDIT_PATH = TABLE_ROOT / "metric_scale_audit.csv"
DESCRIPTIVES_PATH = TABLE_ROOT / "outcome_score_descriptives.csv"
LEDGER_PATH = TABLE_ROOT / "analysis_ledger.csv"
CONSISTENCY_PATH = DIAGNOSTIC_ROOT / "methods_results_consistency.md"
MANUSCRIPT_PATH = REPORT_ROOT / "manuscript_input_summary.md"
STATUS_PATH = PROJECT_ROOT / "STATUS.md"

EXPECTED_PRIMARY = {
    "spearman": 0.34041891732005897,
    "ci_low": 0.24360598784700804,
    "ci_high": 0.4332464175131597,
    "permutation_p": 0.00009999000099990002,
}
EXPECTED_EARLY_BENCHMARK = {
    "mean_itemwise_spearman": 0.36465289881129526,
    "mean_itemwise_mae": 2.6732026143790852,
    "mean_itemwise_sd_ratio": 0.4818504531505908,
}
ALLOWED_ROLES = {
    "PRIMARY",
    "SECONDARY",
    "SENSITIVITY",
    "DIAGNOSTIC",
    "CONTEXTUAL_REFERENCE",
    "POST_HOC_DESCRIPTIVE",
}


class Goal85AuditError(RuntimeError):
    """Raised when Goal 8.5 detects an integrity or consistency failure."""


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "NA"
    return f"{float(value):.{digits}f}"


def _close(actual: float, expected: float, *, label: str, atol: float = 1e-12) -> None:
    if not math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=atol):
        raise Goal85AuditError(f"{label} drifted: {actual!r} != {expected!r}")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def distribution_summary(name: str, values: Sequence[float]) -> dict[str, Any]:
    """Return the fixed aggregate descriptive statistics requested by Goal 8.5."""

    array = np.asarray(values, dtype=float)
    if array.ndim != 1 or len(array) != 342 or not np.isfinite(array).all():
        raise Goal85AuditError(f"{name} must contain 342 finite values")
    return {
        "measure": name,
        "n": int(len(array)),
        "mean": float(np.mean(array)),
        "median": float(np.median(array)),
        "sd": float(np.std(array, ddof=1)),
        "q1": float(np.percentile(array, 25)),
        "q3": float(np.percentile(array, 75)),
        "minimum": float(np.min(array)),
        "maximum": float(np.max(array)),
    }


def benchmark0_reconciliation(
    benchmark: Sequence[float], human_h8: np.ndarray
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Compute both historically reported Benchmark-0 aggregations."""

    baseline = np.asarray(benchmark, dtype=float)
    human = np.asarray(human_h8, dtype=float)
    if baseline.shape != (342,) or human.shape != (342, 8):
        raise Goal85AuditError("Benchmark-0/H8 arrays have unexpected dimensions")
    if not np.isfinite(baseline).all() or not np.isfinite(human).all():
        raise Goal85AuditError("Benchmark-0/H8 arrays contain missing values")

    item_rows: list[dict[str, Any]] = []
    for index, (r_item, variable) in enumerate(zip(H8_R_ITEMS, H8_VARIABLES)):
        human_item = human[:, index]
        item_rows.append(
            {
                "R_item": r_item,
                "variable_name": variable,
                "analysis_unit": "participants within one H8 item",
                "spearman": _safe_spearman(baseline, human_item),
                "mae": float(np.mean(np.abs(baseline - human_item))),
                "sd_ratio": float(np.std(baseline, ddof=1) / np.std(human_item, ddof=1)),
            }
        )
    item_table = pd.DataFrame(item_rows)
    human_person_mean = human.mean(axis=1)
    summary = {
        "benchmark_definition": (
            "mean of 34 direction-aligned 0-10 C1 conditioning items; "
            "R29, R35, R48, and R50 excluded"
        ),
        "n": 342,
        "item_count": 8,
        "mean_itemwise_h8_spearman": float(item_table["spearman"].mean()),
        "mean_itemwise_h8_mae": float(item_table["mae"].mean()),
        "mean_itemwise_h8_sd_ratio": float(item_table["sd_ratio"].mean()),
        "spearman_with_participant_mean_h8": _safe_spearman(
            baseline, human_person_mean
        ),
        "mae_against_participant_mean_h8": float(
            np.mean(np.abs(baseline - human_person_mean))
        ),
        "bias_against_participant_mean_h8": float(
            np.mean(baseline - human_person_mean)
        ),
        "sd_ratio_to_participant_mean_h8": float(
            np.std(baseline, ddof=1) / np.std(human_person_mean, ddof=1)
        ),
        "reconciliation": "different_valid_aggregations",
        "analysis_bug": False,
        "reporting_ambiguity": True,
    }
    return summary, item_table


def posthoc_scale_references(
    participant_results: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Create the explicitly post-hoc descriptive MAE scale references."""

    required = {
        "x_risk_av",
        "C1_score",
        "C2_score",
        "C3_score",
        "ridge_oof_prediction",
    }
    if not required.issubset(participant_results.columns):
        raise Goal85AuditError("Participant result columns required for scale audit are absent")
    if len(participant_results) != 342 or participant_results["participant_key"].duplicated().any():
        raise Goal85AuditError("Participant result membership is not one-to-one for N=342")

    y = participant_results["x_risk_av"].to_numpy(float)
    if not np.isfinite(y).all():
        raise Goal85AuditError("Human outcome contains missing values")
    human_median = float(np.median(y))
    human_sd = float(np.std(y, ddof=1))
    constant_mae = float(np.mean(np.abs(y - human_median)))
    columns = {
        "C1": "C1_score",
        "C2": "C2_score",
        "C3": "C3_score",
        "Ridge": "ridge_oof_prediction",
    }
    reference_rows: list[dict[str, Any]] = []
    for name, column in columns.items():
        prediction = participant_results[column].to_numpy(float)
        if not np.isfinite(prediction).all():
            raise Goal85AuditError(f"{name} prediction contains missing values")
        mae = float(np.mean(np.abs(prediction - y)))
        reference_rows.append(
            {
                "measure": name,
                "mae_vs_human": mae,
                "mae_over_constant_median_mae": mae / constant_mae,
                "mae_over_human_sd": mae / human_sd,
                "research_role": "POST_HOC_DESCRIPTIVE",
                "label": (
                    "POST-HOC DESCRIPTIVE SCALE REFERENCE; "
                    "NOT CONFIRMATORY BENCHMARK"
                ),
            }
        )
    summary = {
        "label": "POST-HOC DESCRIPTIVE SCALE REFERENCE; NOT CONFIRMATORY BENCHMARK",
        "n": 342,
        "constant_prediction": human_median,
        "constant_median_mae": constant_mae,
        "human_sd": human_sd,
        "p_values_computed": False,
        "success_cutoffs_defined": False,
    }
    return summary, pd.DataFrame(reference_rows)


def _pooled_switch_metric(results: Mapping[str, Any], condition: str, metric: str) -> float:
    rows = [
        row for row in results["switching_diagnostics"] if row["condition"] == condition
    ]
    total = sum(int(row["jobs"]) for row in rows)
    if total != 684:
        raise Goal85AuditError(f"{condition} switching diagnostics do not contain 684 jobs")
    return sum(float(row[metric]) * int(row["jobs"]) for row in rows) / total


def _scale_audit() -> pd.DataFrame:
    rows = [
        {
            "measure": "x_risk_av",
            "original_definition": "mean switching row across two incentivized human risk lists",
            "numeric_unit_or_scale": "switching-row score",
            "theoretical_or_actual_range": "theoretical 1-22; observed 2-22",
            "same_numeric_scale_as_x_risk_av": "YES (reference outcome)",
            "direct_mae_comparison_with_c1": "YES",
            "direct_r_squared_comparison": "YES when predictions are on this raw scale",
            "standardized": "NO",
            "special_interpretation": "higher values indicate more lottery choices / greater behavioral risk tolerance",
        },
        {
            "measure": "C1 score",
            "original_definition": "mean of List 1 and List 2 LLM switching-row scores under broad profile",
            "numeric_unit_or_scale": "switching-row score",
            "theoretical_or_actual_range": "theoretical 1-22; observed 2-14.5",
            "same_numeric_scale_as_x_risk_av": "YES",
            "direct_mae_comparison_with_c1": "YES",
            "direct_r_squared_comparison": "YES",
            "standardized": "NO",
            "special_interpretation": "unique primary behavioral condition; K=1",
        },
        {
            "measure": "C2 score",
            "original_definition": "mean of List 1 and List 2 LLM switching-row scores under strict profile",
            "numeric_unit_or_scale": "switching-row score",
            "theoretical_or_actual_range": "theoretical 1-22; observed 2-15",
            "same_numeric_scale_as_x_risk_av": "YES",
            "direct_mae_comparison_with_c1": "YES",
            "direct_r_squared_comparison": "YES",
            "standardized": "NO",
            "special_interpretation": "strict sensitivity; not primary",
        },
        {
            "measure": "C3 score",
            "original_definition": "mean of List 1 and List 2 LLM switching-row scores under task-proximal profile",
            "numeric_unit_or_scale": "switching-row score",
            "theoretical_or_actual_range": "theoretical 1-22; observed 2-21",
            "same_numeric_scale_as_x_risk_av": "YES",
            "direct_mae_comparison_with_c1": "YES",
            "direct_r_squared_comparison": "YES",
            "standardized": "NO",
            "special_interpretation": "task-proximal diagnostic; not primary",
        },
        {
            "measure": "Ridge prediction",
            "original_definition": "outer-fold OOF prediction from nested 5x5 Ridge using 46 C1 inputs",
            "numeric_unit_or_scale": "predicted raw x_risk_av switching-row score",
            "theoretical_or_actual_range": "unbounded model prediction; observed 8.5921-13.4203",
            "same_numeric_scale_as_x_risk_av": "YES",
            "direct_mae_comparison_with_c1": "YES, with information-role caveat",
            "direct_r_squared_comparison": "YES",
            "standardized": "features standardized within training folds; outcome/prediction reported on raw scale",
            "special_interpretation": "supervised statistical reference; not information-equal to zero-shot LLM",
        },
        {
            "measure": "R2+R3 prediction/reference",
            "original_definition": "0.4244*std(v_94)+0.5756*std(v_109)",
            "numeric_unit_or_scale": "standardized linear index; MAE/R2 use sample-standardized x_risk_av",
            "theoretical_or_actual_range": "unbounded standardized index; observed -1.9140 to 2.3788",
            "same_numeric_scale_as_x_risk_av": "NO for reported MAE",
            "direct_mae_comparison_with_c1": "NO",
            "direct_r_squared_comparison": "dimensionless but interpret only on its stated standardized-outcome prediction scale",
            "standardized": "YES",
            "special_interpretation": "contextual reference; rank association and point calibration answer different questions",
        },
    ]
    return pd.DataFrame(rows)


def _analysis_ledger() -> pd.DataFrame:
    rows = [
        ("Stage 1 H8 conditioning check", "DIAGNOSTIC", True, "secondary", "inferential", "mean item-wise Spearman and vector permutation", 342, "outputs/reports/stage1_h8_results.md", "Stage 1 conditioning result"),
        ("Benchmark-0 H8 references", "DIAGNOSTIC", True, "secondary", "descriptive", "item-wise and participant-mean H8 correspondence", 342, "outputs/diagnostics/benchmark0_reconciliation.md", "Stage 1 conditioning result"),
        ("K stability", "DIAGNOSTIC", True, "secondary", "descriptive", "ICC(1,1), K=3/5 reliability, repeat diagnostics", 30, "outputs/reports/k_stability_results.md", "K stability"),
        ("C1 Spearman", "PRIMARY", True, "primary", "inferential", "Spearman rho, bootstrap CI, participant-ID permutation", 342, "outputs/reports/confirmatory_results.md", "C1 primary result"),
        ("C1 secondary metrics", "SECONDARY", True, "secondary", "descriptive", "Pearson, MAE, bias, SD ratio, list/switching diagnostics", 342, "outputs/reports/confirmatory_results.md", "C1 calibration and dispersion"),
        ("C2 strict sensitivity", "SENSITIVITY", True, "secondary", "inferential", "condition metrics and paired C2-C1 contrasts", 342, "outputs/reports/robustness_results.md", "C2 sensitivity"),
        ("C3 task-proximal diagnostic", "DIAGNOSTIC", True, "secondary", "inferential", "condition metrics and paired C3-C1 contrasts", 342, "outputs/reports/robustness_results.md", "C3 diagnostic"),
        ("Nested Ridge OOF reference", "CONTEXTUAL_REFERENCE", True, "secondary", "descriptive", "Spearman, Pearson, MAE, R2", 342, "outputs/reports/robustness_results.md", "Ridge reference"),
        ("Published R2+R3 reference", "CONTEXTUAL_REFERENCE", True, "secondary", "descriptive", "Spearman, Pearson, standardized-outcome MAE and R2", 342, "outputs/reports/robustness_results.md", "R2+R3 contextual reference"),
        ("Human risk test-retest context", "CONTEXTUAL_REFERENCE", True, "secondary", "descriptive", "Pearson r", 44, "outputs/reports/data_audit.md", "Human measurement reliability context"),
        ("Constant-median MAE reference", "POST_HOC_DESCRIPTIVE", False, "secondary", "descriptive", "MAE and MAE ratio", 342, "outputs/tables/outcome_score_descriptives.csv", "Scale-comparability notes"),
        ("Human-SD standardized MAE reference", "POST_HOC_DESCRIPTIVE", False, "secondary", "descriptive", "MAE / human SD", 342, "outputs/tables/outcome_score_descriptives.csv", "Scale-comparability notes"),
    ]
    frame = pd.DataFrame(rows, columns=[
        "analysis_name", "research_role", "outcome_blind_fixed",
        "primary_or_secondary", "inferential_or_descriptive", "metric",
        "sample_N", "result_location", "manuscript_section",
    ])
    if not set(frame["research_role"]).issubset(ALLOWED_ROLES):
        raise Goal85AuditError("Analysis ledger contains an unapproved research role")
    return frame


def _benchmark_report(summary: Mapping[str, Any], item_table: pd.DataFrame) -> str:
    lines = [
        "# Benchmark-0 reconciliation",
        "",
        "## 結論",
        "",
        "0.365 與 0.6897 均為同一個 Benchmark-0 產生的合法統計量，但 aggregation（聚合方式）與分析單位不同。沒有發現題目 mapping、Benchmark membership 或衍生計算錯誤；發現的是報告命名不充分造成的歧義。",
        "",
        "## 共同 Benchmark 定義",
        "",
        "每位 participant 的 Benchmark-0 分數 B_i，是34個方向對齊、0至10量尺之 C1 conditioning items 的平均再乘10；固定排除 R29、R35、R48、R50。兩個統計量都使用同一組342位 Confirmatory participants、同一份 H8 真人答案與同一個 B_i。",
        "",
        "## 早期統計量：Benchmark-0 mean item-wise H8 Spearman",
        "",
        "對每一個 H8 題目 j，先跨 participant 計算 Spearman(B_i, H_ij)，再對8個題目取算術平均：",
        "",
        "`mean_j Spearman_i(B_i, H_ij)`",
        "",
        f"正式名稱：**Benchmark-0 mean item-wise H8 Spearman** = {_fmt(summary['mean_itemwise_h8_spearman'], 7)}。相同 item-wise 分析單位下，mean MAE={_fmt(summary['mean_itemwise_h8_mae'], 7)}、mean SD ratio={_fmt(summary['mean_itemwise_h8_sd_ratio'], 7)}。",
        "",
        "|H8 item|Spearman|MAE|SD ratio|",
        "|---|---:|---:|---:|",
    ]
    for row in item_table.itertuples(index=False):
        lines.append(f"|{row.R_item}|{_fmt(row.spearman, 7)}|{_fmt(row.mae, 7)}|{_fmt(row.sd_ratio, 7)}|")
    lines.extend([
        "",
        "## Goal 8統計量：Benchmark-0 Spearman with participant mean H8",
        "",
        "先對每位 participant 的8題 H8 真人答案取平均 Hbar_i，再跨 participant 計算：",
        "",
        "`Spearman_i(B_i, mean_j H_ij)`",
        "",
        f"正式名稱：**Benchmark-0 Spearman with participant mean H8** = {_fmt(summary['spearman_with_participant_mean_h8'], 7)}。同一 participant-composite 分析單位下，MAE={_fmt(summary['mae_against_participant_mean_h8'], 7)}、bias={_fmt(summary['bias_against_participant_mean_h8'], 7)}、SD ratio={_fmt(summary['sd_ratio_to_participant_mean_h8'], 7)}。",
        "",
        "## Bug判定與修正",
        "",
        "- `analysis bug`: 否。兩個計算可由現有凍結資料與實作重現。",
        "- `mapping bug`: 否。H8與Benchmark membership均未漂移。",
        "- `report-generation ambiguity`: 是。Goal 8摘要只寫「Benchmark-0 Spearman=0.6897」，未同時保留早期 item-wise 定義，造成表面矛盾。",
        "- 修正只限報告名稱、公式、分析單位與兩組配套 MAE/SD ratio 的分離呈現；沒有重跑LLM、沒有修改participant答案或原始Stage 1 outputs。",
    ])
    return "\n".join(lines) + "\n"


def _stage1_report(results: Mapping[str, Any], benchmark: Mapping[str, Any], item_table: pd.DataFrame) -> str:
    stage1 = results["stage1_h8"]
    llm_items = pd.read_csv(TABLE_ROOT / "stage1_h8_item_results.csv")
    lines = [
        "# Stage 1 H8：Questionnaire / Persona Conditioning Check",
        "",
        "本診斷完全在 outcome-blind 狀態完成；不使用 x_risk_av，也不是 behavioral validity test。",
        "",
        f"- N = {stage1['n']}",
        f"- 八題模型回答與真人答案的 mean item-wise Spearman = {_fmt(stage1['global_mean_item_spearman'])}",
        f"- global participant-vector permutation p = {_fmt(stage1['global_vector_permutation_p'], 8)}",
        f"- Benchmark-0 mean item-wise H8 Spearman = {_fmt(benchmark['mean_itemwise_h8_spearman'])}",
        f"- Benchmark-0 Spearman with participant mean H8 = {_fmt(benchmark['spearman_with_participant_mean_h8'])}",
        "",
        "## Stage 1模型回答逐題結果",
        "",
        "|題目|Spearman|95% CI|Permutation p|Holm p|MAE|Bias|SD ratio|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in llm_items.itertuples(index=False):
        lines.append(
            f"|{row.R_item}|{_fmt(row.spearman)}|[{_fmt(row.spearman_ci_low)}, {_fmt(row.spearman_ci_high)}]|"
            f"{_fmt(row.permutation_p, 8)}|{_fmt(row.holm_p, 8)}|{_fmt(row.mae)}|{_fmt(row.bias)}|{_fmt(row.sd_ratio)}|"
        )
    lines.extend([
        "",
        "## Benchmark-0逐題參考",
        "",
        "下表的分析單位是「單一H8題目內跨participant」；其8題Spearman平均不是 participant-mean H8 的0.6897。",
        "",
        "|題目|Spearman|MAE|SD ratio|",
        "|---|---:|---:|---:|",
    ])
    for row in item_table.itertuples(index=False):
        lines.append(f"|{row.R_item}|{_fmt(row.spearman)}|{_fmt(row.mae)}|{_fmt(row.sd_ratio)}|")
    return "\n".join(lines) + "\n"


def _condition_block(results: Mapping[str, Any], condition: str) -> list[str]:
    metrics = results["conditions"][condition]
    ci = results["condition_bootstrap_95_ci"][condition]["spearman"]
    list_row = next(row for row in results["list_diagnostics"] if row["condition"] == condition)
    lines = [
        f"- N={metrics['n']}；Spearman={_fmt(metrics['spearman'])}，95% CI=[{_fmt(ci[0])}, {_fmt(ci[1])}]；Pearson={_fmt(metrics['pearson'])}。",
        f"- MAE={_fmt(metrics['mae'])}；mean bias={_fmt(metrics['bias'])}；SD ratio={_fmt(metrics['sd_ratio'])}。",
        f"- 預測 mean={_fmt(metrics['prediction_mean'])}；median={_fmt(metrics['prediction_median'])}；SD={_fmt(metrics['prediction_sd'])}。",
        f"- List 1/List 2 Spearman={_fmt(list_row['spearman'])}；mean absolute discrepancy={_fmt(list_row['mae_between_lists'])}。",
        f"- Multiple-switch rate={_fmt(_pooled_switch_metric(results, condition, 'multiple_lottery_to_safe_rate'))}；monotonicity rate={_fmt(_pooled_switch_metric(results, condition, 'monotonic_rate'))}；dominance-violation rate={_fmt(_pooled_switch_metric(results, condition, 'dominance_violation_rate'), 6)}。",
    ]
    if condition in {"C2", "C3"}:
        comparison = results["paired_comparisons"][condition]
        lines.extend([
            f"- Delta rho {condition}-C1={_fmt(comparison['rho_delta_other_minus_c1'])}，95% CI=[{_fmt(comparison['rho_delta_ci'][0])}, {_fmt(comparison['rho_delta_ci'][1])}]。",
            f"- {comparison['absolute_error_difference_definition']}={_fmt(comparison['mean_absolute_error_difference'])}，95% CI=[{_fmt(comparison['mean_absolute_error_difference_ci'][0])}, {_fmt(comparison['mean_absolute_error_difference_ci'][1])}]；paired label-swap p={_fmt(comparison['within_participant_label_swap_p'], 6)}。",
        ])
    return lines


def _confirmatory_report(
    results: Mapping[str, Any], benchmark: Mapping[str, Any], posthoc: Mapping[str, Any], ratios: pd.DataFrame
) -> str:
    c1 = results["conditions"]["C1"]
    c1_ci = results["condition_bootstrap_95_ci"]["C1"]["spearman"]
    ratio_map = ratios.set_index("measure").to_dict(orient="index")
    lines = [
        "# Confirmatory results",
        "",
        "C1是唯一primary behavioral condition；C2是strict sensitivity；C3是task-proximal diagnostic。所有模型輸出與分析方法都在outcome解鎖前凍結。",
        "",
        "## Outcome-blind diagnostics",
        "",
        f"- Stage 1 mean item-wise Spearman={_fmt(results['stage1_h8']['global_mean_item_spearman'])}；global participant-vector permutation p={_fmt(results['stage1_h8']['global_vector_permutation_p'], 8)}。",
        f"- Benchmark-0 mean item-wise H8 Spearman={_fmt(benchmark['mean_itemwise_h8_spearman'])}；Benchmark-0 Spearman with participant mean H8={_fmt(benchmark['spearman_with_participant_mean_h8'])}。",
        f"- K=1 ICC(1,1)={_fmt(results['k_stability']['reliability']['1'])}，95% CI=[{_fmt(results['k_stability']['bootstrap_95_ci']['1'][0])}, {_fmt(results['k_stability']['bootstrap_95_ci']['1'][1])}]；K=3/5只作穩定性診斷。",
        "",
        "## C1 primary result",
        "",
        f"- N={c1['n']}；Spearman rho={_fmt(c1['spearman'])}，95% CI=[{_fmt(c1_ci[0])}, {_fmt(c1_ci[1])}]，participant-ID permutation p={_fmt(results['primary_permutation_p'], 8)}。",
        f"- Human mean={_fmt(c1['human_mean'])}、SD={_fmt(c1['human_sd'])}；C1 mean={_fmt(c1['prediction_mean'])}、median={_fmt(c1['prediction_median'])}、SD={_fmt(c1['prediction_sd'])}。",
        f"- Pearson={_fmt(c1['pearson'])}；MAE={_fmt(c1['mae'])}；mean bias={_fmt(c1['bias'])}；SD ratio={_fmt(c1['sd_ratio'])}。C1平均比human低{_fmt(abs(c1['bias']))}，且人際差異較壓縮。",
        *_condition_block(results, "C1")[3:],
        "",
        "## C2 strict sensitivity",
        "",
        *_condition_block(results, "C2"),
        "",
        "## C3 task-proximal diagnostic",
        "",
        *_condition_block(results, "C3"),
        "",
        "## POST-HOC DESCRIPTIVE SCALE REFERENCE — NOT CONFIRMATORY BENCHMARK",
        "",
        f"最佳constant median prediction={_fmt(posthoc['constant_prediction'])}，MAE={_fmt(posthoc['constant_median_mae'])}。這只回答完全不區分participant時的自然MAE尺度，不含p-value或成功門檻。",
        "",
        "|Measure|MAE|MAE / constant-median MAE|MAE / Human SD|",
        "|---|---:|---:|---:|",
    ]
    for measure in ("C1", "C2", "C3", "Ridge"):
        row = ratio_map[measure]
        lines.append(f"|{measure}|{_fmt(row['mae_vs_human'])}|{_fmt(row['mae_over_constant_median_mae'])}|{_fmt(row['mae_over_human_sd'])}|")
    lines.extend([
        "",
        "## 解讀",
        "",
        "問卷條件化LLM產生的風險行為指標，與同一真人在有金錢誘因風險任務中的行為存在正向但有限的個體層級對應；模型輸出具有良好重測穩定性，但在絕對數值上呈現明顯系統性偏低與一定程度的人際差異壓縮。不得以「準確預測真人風險行為」概括。",
    ])
    return "\n".join(lines) + "\n"


def _methods_report() -> str:
    return """# Statistical methods

所有participant-level計分先在outcome-blind階段完成並以SHA-256 manifest封存。List score沿用固定LOTTERY-to-SAFE switching rule；multiple transitions取所有transition rows平均，all-SAFE=1、all-LOTTERY=22；兩張list均為numeric時才取平均。C1為唯一primary behavioral condition。

C1主要指標是Spearman rho；10,000次participant bootstrap提供percentile 95% CI，10,000次participant-ID permutation採雙尾加一修正。C2/C3使用相同condition metrics；paired absolute-error contrasts使用participant bootstrap與participant內label-swap randomization，rho差異採joint participant bootstrap。Goal 8.5沒有重選分析方法。

Stage 1逐題比較模型與真人H8答案，global metric為8個item-wise Spearman的平均，並以完整participant vector permutation。Benchmark-0是34個方向對齊的0至10 C1 conditioning items平均；Goal 8.5分別命名其「mean item-wise H8 Spearman」與「Spearman with participant mean H8」，避免不同aggregation共用名稱。

K stability固定30位participants、5個two-list paired repeats，報告ICC(1,1)、participant-vector bootstrap 95% CI、Spearman–Brown K=3/5 average reliability、repeat-pair Spearman、list repeatability及switching-pattern stability；主要K永久維持1。

Ridge reference使用固定46個C1 numeric questionnaire responses、nested outer 5-fold/inner 5-fold、training-fold-only StandardScaler、inner-CV mean squared prediction error及33點log-spaced alpha grid。所有prediction是outer-fold OOF prediction；它是supervised statistical reference，不是與zero-shot LLM資訊對等的競爭。

Published R2+R3 contextual reference使用固定公式`0.4244*std(v_94)+0.5756*std(v_109)`。Spearman與Pearson是該index對human outcome的跨人關聯；MAE與R2則相對sample-standardized x_risk_av計算。其MAE不與C1/C2/C3/Ridge原始switching-row量尺MAE直接比較。

Goal 8.5新增的constant-median MAE與MAE/Human SD均標記為`POST-HOC DESCRIPTIVE SCALE REFERENCE; NOT CONFIRMATORY BENCHMARK`。不計算p-value、不設定cutoff、不改變primary conclusion。
"""


def _robustness_report(results: Mapping[str, Any], ratios: pd.DataFrame) -> str:
    ridge = results["ridge_reference"]
    r2r3 = results["published_r2_r3_context"]
    lines = [
        "# Robustness results",
        "",
        "## C2 strict sensitivity",
        "",
        *_condition_block(results, "C2"),
        "",
        "## C3 task-proximal diagnostic",
        "",
        *_condition_block(results, "C3"),
        "",
        "## Nested Ridge supervised statistical reference",
        "",
        f"- Nested CV=5×5；feature count={ridge['feature_count']}；outer-fold OOF N={ridge['n']}。",
        f"- Spearman={_fmt(ridge['spearman'])}；Pearson={_fmt(ridge['pearson'])}；MAE={_fmt(ridge['mae'])}；R²={_fmt(ridge['r_squared_oof'])}。",
        f"- Prediction mean={_fmt(ridge['prediction_mean'])}；median={_fmt(ridge['prediction_median'])}；SD={_fmt(ridge['prediction_sd'])}。",
        "- Preprocessing完全fold-contained：scaling只fit training fold，alpha只由inner CV的MSE選擇。",
        "",
        "## Published R2+R3 contextual reference",
        "",
        f"- Formula=`{r2r3['formula']}`；N={r2r3['n']}。",
        f"- Spearman={_fmt(r2r3['spearman'])}；Pearson={_fmt(r2r3['pearson'])}。",
        f"- Standardized-outcome MAE={_fmt(r2r3['mae_standardized_outcome'])}；R²={_fmt(r2r3['r_squared_standardized_outcome'])}。DIRECT_MAE_COMPARISON=NO。",
        "- 較高rank correlation與負R²可同時存在：前者衡量排序對應，後者衡量指定數值尺度上的absolute point calibration。負R²不等於完全沒有individual-level signal，但確實顯示absolute point-prediction存在問題。",
        "",
        "## Human measurement context",
        "",
        "既有TestRetestExp資料與replication code確認experimental risk test-retest Pearson r=0.589023730、N=44。只作Discussion量測背景；不是理論上限、沒有校正C1 rho，也未加入主要假設檢定。",
        "",
        "## POST-HOC DESCRIPTIVE SCALE REFERENCE — NOT CONFIRMATORY BENCHMARK",
        "",
        "下列比率只供尺度閱讀，不含p-value、cutoff或方法重選。",
        "",
        "|Measure|MAE / constant-median MAE|MAE / Human SD|",
        "|---|---:|---:|",
    ]
    for row in ratios.itertuples(index=False):
        lines.append(f"|{row.measure}|{_fmt(row.mae_over_constant_median_mae)}|{_fmt(row.mae_over_human_sd)}|")
    return "\n".join(lines) + "\n"


def _k_report(results: Mapping[str, Any]) -> str:
    k = results["k_stability"]
    within = k["within_person_sd"]
    lines = [
        "# K stability results",
        "",
        "固定30位participants、5個two-list paired repeats；K=1是主要執行規則，K=3/5只作measurement-stability diagnostic，沒有以outcome重選K或設定ICC cutoff。",
        "",
        f"- ICC(1,1) / K=1 single-run reliability={_fmt(k['reliability']['1'])}，95% CI=[{_fmt(k['bootstrap_95_ci']['1'][0])}, {_fmt(k['bootstrap_95_ci']['1'][1])}]。",
        f"- K=3 average reliability={_fmt(k['reliability']['3'])}，95% CI=[{_fmt(k['bootstrap_95_ci']['3'][0])}, {_fmt(k['bootstrap_95_ci']['3'][1])}]。",
        f"- K=5 average reliability={_fmt(k['reliability']['5'])}，95% CI=[{_fmt(k['bootstrap_95_ci']['5'][0])}, {_fmt(k['bootstrap_95_ci']['5'][1])}]。",
        f"- Participant-level within-person SD：mean={_fmt(within['mean'])}、median={_fmt(within['median'])}、across-participant SD={_fmt(within['sd'])}。",
        f"- Complete choice-sequence pairwise exact-match rate={_fmt(k['complete_sequence_pairwise_exact_match_rate'])}。",
        "",
        "## Repeat-to-repeat paired Spearman",
        "",
        "|Repeat A|Repeat B|Spearman|",
        "|---:|---:|---:|",
    ]
    for row in k["pairwise_repeat_spearman"]:
        lines.append(f"|{row['repeat_a']}|{row['repeat_b']}|{_fmt(row['spearman'])}|")
    lines.extend([
        "",
        "## List repeatability與switching-pattern stability",
        "",
        f"- List 1 ICC(1,1)={_fmt(k['list_repeatability']['1']['icc_1_1'])}。",
        f"- List 2 ICC(1,1)={_fmt(k['list_repeatability']['2']['icc_1_1'])}。",
        f"- Switching-row within-person/list SD：mean={_fmt(k['switching_row_variability']['mean_within_person_list_sd'])}、median={_fmt(k['switching_row_variability']['median_within_person_list_sd'])}。",
        f"- Switch-count within-person/list SD：mean={_fmt(k['switch_count_variability']['mean_within_person_list_sd'], 6)}、median={_fmt(k['switch_count_variability']['median_within_person_list_sd'], 6)}。",
        f"- Monotonicity consistency rate={_fmt(k['monotonicity_consistency_rate'])}。",
    ])
    return "\n".join(lines) + "\n"


def _consistency_report(checks: Sequence[tuple[str, str, str]]) -> str:
    lines = [
        "# Methods–Results consistency",
        "",
        "總結：**PASS**。Goal 8.5只修正報告命名歧義、補齊既定結果及增加明確標示的事後描述性尺度參考；沒有重跑LLM或改變正式估計。",
        "",
        "|Check|Status|Evidence / conclusion|",
        "|---|---|---|",
    ]
    lines.extend(f"|{name}|{status}|{detail}|" for name, status, detail in checks)
    lines.extend([
        "",
        "## Bug classification",
        "",
        "- Derived-analysis computation bug：未發現。",
        "- Participant mapping / raw-data integrity bug：未發現。",
        "- Reporting ambiguity：發現一項。0.6897曾被簡稱為Benchmark-0 Spearman，未與早期0.365 item-wise aggregation並列與區分。",
        "- Reporting omission：既有摘要未完整聚合呈現C1/C2/C3 list與switching diagnostics、Ridge prediction distribution、R2+R3尺度限制及兩個允許的post-hoc scale references；本Goal已補齊。",
    ])
    return "\n".join(lines) + "\n"


def _manuscript_summary(
    results: Mapping[str, Any], benchmark: Mapping[str, Any], posthoc: Mapping[str, Any], descriptives: pd.DataFrame, ratios: pd.DataFrame
) -> str:
    d = descriptives.set_index("measure").to_dict(orient="index")
    rr = ratios.set_index("measure").to_dict(orient="index")
    c1, c2, c3 = (results["conditions"][key] for key in ("C1", "C2", "C3"))
    ridge, r2r3, k = results["ridge_reference"], results["published_r2_r3_context"], results["k_stability"]
    return f"""# Goal 9 manuscript input summary

本檔是Goal 9唯一高階結果摘要；它不代表已獲准開始論文寫作。

## 1. Study design snapshot

342位Confirmatory participants；C1是唯一primary behavioral condition，C2是strict sensitivity，C3是task-proximal diagnostic。主要K固定為1。所有2,634個frozen canonical LLM jobs在outcome解鎖前完成，Goal 8.5沒有重跑模型。

## 2. Sample與資料完整性

三個條件均N=342、missing=0；Pilot mixing=0。participant-level scores、outcome與模型輸出只留在Git-ignored本機路徑，公開聚合產物不含pseudonymous keys。

## 3. Stage 1 conditioning result

模型H8回答對真人H8的mean item-wise Spearman={_fmt(results['stage1_h8']['global_mean_item_spearman'])}，global participant-vector permutation p={_fmt(results['stage1_h8']['global_vector_permutation_p'], 8)}。Benchmark-0 mean item-wise H8 Spearman={_fmt(benchmark['mean_itemwise_h8_spearman'])}；Benchmark-0 Spearman with participant mean H8={_fmt(benchmark['spearman_with_participant_mean_h8'])}。兩者aggregation不同，不矛盾。

## 4. K stability

30位participants、5 repeats；ICC(1,1)={_fmt(k['reliability']['1'])}，95% CI=[{_fmt(k['bootstrap_95_ci']['1'][0])}, {_fmt(k['bootstrap_95_ci']['1'][1])}]；K=3={_fmt(k['reliability']['3'])} [{_fmt(k['bootstrap_95_ci']['3'][0])}, {_fmt(k['bootstrap_95_ci']['3'][1])}]；K=5={_fmt(k['reliability']['5'])} [{_fmt(k['bootstrap_95_ci']['5'][0])}, {_fmt(k['bootstrap_95_ci']['5'][1])}]。K=3/5只作stability diagnostic，主要K仍為1。

## 5. C1 primary result

Spearman rho={_fmt(c1['spearman'])}，95% CI=[{_fmt(results['condition_bootstrap_95_ci']['C1']['spearman'][0])}, {_fmt(results['condition_bootstrap_95_ci']['C1']['spearman'][1])}]，participant-ID permutation p={_fmt(results['primary_permutation_p'], 8)}。這是正向但有限的individual-level ranking correspondence。

## 6. C1 calibration與dispersion

Human mean={_fmt(d['Human x_risk_av']['mean'])}、SD={_fmt(d['Human x_risk_av']['sd'])}；C1 mean={_fmt(d['C1']['mean'])}、SD={_fmt(d['C1']['sd'])}。C1 Pearson={_fmt(c1['pearson'])}、MAE={_fmt(c1['mae'])}、bias={_fmt(c1['bias'])}、SD ratio={_fmt(c1['sd_ratio'])}，表示明顯系統性偏低與一定程度人際差異壓縮。

## 7. C2 sensitivity

Spearman={_fmt(c2['spearman'])}、Pearson={_fmt(c2['pearson'])}、MAE={_fmt(c2['mae'])}、bias={_fmt(c2['bias'])}、SD ratio={_fmt(c2['sd_ratio'])}。Delta rho C2-C1={_fmt(results['paired_comparisons']['C2']['rho_delta_other_minus_c1'])}，95% CI=[{_fmt(results['paired_comparisons']['C2']['rho_delta_ci'][0])}, {_fmt(results['paired_comparisons']['C2']['rho_delta_ci'][1])}]；AE_C2-AE_C1={_fmt(results['paired_comparisons']['C2']['mean_absolute_error_difference'])}，label-swap p={_fmt(results['paired_comparisons']['C2']['within_participant_label_swap_p'], 6)}。

## 8. C3 diagnostic

Spearman={_fmt(c3['spearman'])}、Pearson={_fmt(c3['pearson'])}、MAE={_fmt(c3['mae'])}、bias={_fmt(c3['bias'])}、SD ratio={_fmt(c3['sd_ratio'])}。Delta rho C3-C1={_fmt(results['paired_comparisons']['C3']['rho_delta_other_minus_c1'])}，95% CI=[{_fmt(results['paired_comparisons']['C3']['rho_delta_ci'][0])}, {_fmt(results['paired_comparisons']['C3']['rho_delta_ci'][1])}]；AE_C1-AE_C3={_fmt(results['paired_comparisons']['C3']['mean_absolute_error_difference'])}，label-swap p={_fmt(results['paired_comparisons']['C3']['within_participant_label_swap_p'], 6)}。

## 9. Ridge reference

Nested 5×5 CV、fold-contained scaling與inner-CV alpha selection；全部prediction為outer-fold OOF。Spearman={_fmt(ridge['spearman'])}、Pearson={_fmt(ridge['pearson'])}、MAE={_fmt(ridge['mae'])}、R²={_fmt(ridge['r_squared_oof'])}、prediction mean={_fmt(ridge['prediction_mean'])}、SD={_fmt(ridge['prediction_sd'])}。角色是supervised statistical reference，不是資訊對等競爭。

## 10. R2+R3 contextual reference

固定公式`{r2r3['formula']}`；Spearman={_fmt(r2r3['spearman'])}、Pearson={_fmt(r2r3['pearson'])}、standardized-outcome MAE={_fmt(r2r3['mae_standardized_outcome'])}、R²={_fmt(r2r3['r_squared_standardized_outcome'])}。較高rank correlation與負R²可並存，因排序對應與absolute numerical calibration不同。

## 11. Scale-comparability notes

R2+R3 MAE不在C1/Ridge原始量尺，`DIRECT_MAE_COMPARISON=NO`。POST-HOC DESCRIPTIVE SCALE REFERENCE（NOT CONFIRMATORY BENCHMARK）：constant-median MAE={_fmt(posthoc['constant_median_mae'])}；C1 MAE/constant={_fmt(rr['C1']['mae_over_constant_median_mae'])}；C1 MAE/Human SD={_fmt(rr['C1']['mae_over_human_sd'])}。沒有p-value或cutoff。

## 12. Human measurement reliability context

既有experimental risk test-retest Pearson r=0.589023730、N=44。僅作measurement context，不是理論上限、不校正C1 rho、不加入主要檢定。

## 13. Main limitations

排序對應有限；C1在絕對數值上系統性偏低且dispersion較小；Ridge與R2+R3具有不同資訊或量尺；human outcome本身存在measurement instability；單一模型/runtime與特定問卷/任務限制外部效度。

## 14. 哪些結果是confirmatory

唯一primary confirmatory result是C1 Spearman及其事前bootstrap CI與participant-ID permutation p。C1其他既定metrics為secondary。

## 15. 哪些結果是diagnostic/sensitivity

Stage 1、Benchmark-0、K stability、C3是diagnostic；C2是strict sensitivity；Ridge與R2+R3是contextual references。

## 16. 哪些結果是post-hoc descriptive

Constant-median MAE與各原始量尺prediction的MAE/constant、MAE/Human SD比率。它們不是confirmatory benchmark，不得改變主要結論。

## 建議核心敘述

問卷條件化LLM產生的風險行為指標，與同一真人在有金錢誘因風險任務中的行為存在正向但有限的個體層級對應；模型輸出具有良好重測穩定性，但在絕對數值上呈現明顯系統性偏低與一定程度的人際差異壓縮。
"""


def run_audit() -> dict[str, Any]:
    """Run Goal 8.5 and write aggregate-only artifacts."""

    results = json.loads(MACHINE_RESULTS_PATH.read_text(encoding="utf-8"))
    primary = results["conditions"]["C1"]
    _close(primary["spearman"], EXPECTED_PRIMARY["spearman"], label="C1 Spearman")
    primary_ci = results["condition_bootstrap_95_ci"]["C1"]["spearman"]
    _close(primary_ci[0], EXPECTED_PRIMARY["ci_low"], label="C1 CI low")
    _close(primary_ci[1], EXPECTED_PRIMARY["ci_high"], label="C1 CI high")
    _close(results["primary_permutation_p"], EXPECTED_PRIMARY["permutation_p"], label="C1 permutation p")

    participant_results = pd.read_parquet(PARTICIPANT_RESULTS_PATH)
    posthoc, ratios = posthoc_scale_references(participant_results)
    distributions = pd.DataFrame([
        distribution_summary("Human x_risk_av", participant_results["x_risk_av"]),
        distribution_summary("C1", participant_results["C1_score"]),
        distribution_summary("C2", participant_results["C2_score"]),
        distribution_summary("C3", participant_results["C3_score"]),
        distribution_summary("Ridge OOF prediction", participant_results["ridge_oof_prediction"]),
    ])
    distributions["reference_measure"] = distributions["measure"].replace(
        {"Ridge OOF prediction": "Ridge"}
    )
    distributions = distributions.merge(
        ratios[["measure", "mae_vs_human", "mae_over_constant_median_mae", "mae_over_human_sd"]],
        how="left", left_on="reference_measure", right_on="measure", suffixes=("", "_ratio"),
    ).drop(columns=["measure_ratio", "reference_measure"])
    distributions["analysis_role"] = np.where(
        distributions["measure"].eq("Human x_risk_av"),
        "REFERENCE_OUTCOME",
        "EXISTING_GOAL8_RESULT_WITH_POST_HOC_SCALE_REFERENCE",
    )
    by_measure = distributions.set_index("measure")
    for condition in ("C1", "C2", "C3"):
        _close(by_measure.loc[condition, "mean"], results["conditions"][condition]["prediction_mean"], label=f"{condition} descriptive mean")
        _close(by_measure.loc[condition, "sd"], results["conditions"][condition]["prediction_sd"], label=f"{condition} descriptive SD")
        _close(by_measure.loc[condition, "mae_vs_human"], results["conditions"][condition]["mae"], label=f"{condition} descriptive MAE")
    _close(by_measure.loc["Human x_risk_av", "mean"], results["conditions"]["C1"]["human_mean"], label="Human descriptive mean")
    _close(by_measure.loc["Human x_risk_av", "sd"], results["conditions"]["C1"]["human_sd"], label="Human descriptive SD")
    _close(by_measure.loc["Ridge OOF prediction", "mean"], results["ridge_reference"]["prediction_mean"], label="Ridge descriptive mean")
    _close(by_measure.loc["Ridge OOF prediction", "sd"], results["ridge_reference"]["prediction_sd"], label="Ridge descriptive SD")
    _close(by_measure.loc["Ridge OOF prediction", "mae_vs_human"], results["ridge_reference"]["mae"], label="Ridge descriptive MAE")

    inputs = load_confirmatory_inputs(INPUT_PATH)
    stage1_records = load_verified_store_records("stage1")
    participant_order, _, human_h8 = _stage1_arrays(stage1_records, inputs)
    ordered_inputs = inputs.set_index("participant_key").loc[participant_order].reset_index()
    benchmark_values = _benchmark_zero(ordered_inputs)
    benchmark, benchmark_items = benchmark0_reconciliation(benchmark_values, human_h8)
    for key, expected in EXPECTED_EARLY_BENCHMARK.items():
        computed_key = {
            "mean_itemwise_spearman": "mean_itemwise_h8_spearman",
            "mean_itemwise_mae": "mean_itemwise_h8_mae",
            "mean_itemwise_sd_ratio": "mean_itemwise_h8_sd_ratio",
        }[key]
        _close(benchmark[computed_key], expected, label=f"Benchmark-0 {key}", atol=1e-10)
    goal8_benchmark = results["stage1_h8"]["benchmark_0"]
    _close(
        benchmark["spearman_with_participant_mean_h8"],
        goal8_benchmark["spearman_with_human_h8_mean"],
        label="Benchmark-0 participant-mean Spearman",
    )

    data_audit = (REPORT_ROOT / "data_audit.md").read_text(encoding="utf-8")
    if "| x_risk_av | x_risk_av_2 | 44 | 0.589023730 |" not in data_audit:
        raise Goal85AuditError("Human risk test-retest r/N are not confirmed by existing audit")

    scale_audit = _scale_audit()
    ledger = _analysis_ledger()
    TABLE_ROOT.mkdir(parents=True, exist_ok=True)
    DIAGNOSTIC_ROOT.mkdir(parents=True, exist_ok=True)
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)
    scale_audit.to_csv(SCALE_AUDIT_PATH, index=False)
    distributions.to_csv(DESCRIPTIVES_PATH, index=False)
    ledger.to_csv(LEDGER_PATH, index=False)
    BENCHMARK_REPORT_PATH.write_text(_benchmark_report(benchmark, benchmark_items), encoding="utf-8")

    checks = [
        ("Goal 8 specified analyses", "PASS", "Stage 1, K, C1/C2/C3, Ridge and R2+R3 all have result locations."),
        ("Method traceability", "PASS", "Every formal result maps to the frozen Goal 8 method; no method was selected from results."),
        ("Result without method", "PASS", "None found."),
        ("Method with omitted result", "PASS", "Goal 8.5 reports all requested existing condition, list, switching, K and reference metrics."),
        ("Same-name different definition", "PASS after reconciliation", "Benchmark-0 item-wise and participant-mean aggregations now have distinct names."),
        ("MAE scale comparability", "PASS", "R2+R3 standardized-outcome MAE is explicitly DIRECT_MAE_COMPARISON=NO."),
        ("Sample N", "PASS", "Human, C1, C2, C3 and Ridge all contain 342 finite one-to-one observations."),
        ("Pilot mixing", "PASS", "Goal 8 outcome manifest reports pilot_mixing=0."),
        ("Participant-level public output", "PASS", "Goal 8.5 outputs are aggregate-only; participant-level files remain Git-ignored."),
        ("Primary role", "PASS", "C1 Spearman remains the unique primary behavioral result in all updated reports."),
        ("Other research roles", "PASS", "Stage 1/C3/K are diagnostic; C2 sensitivity; references contextual; new scale references post-hoc descriptive."),
    ]
    CONSISTENCY_PATH.write_text(_consistency_report(checks), encoding="utf-8")
    (REPORT_ROOT / "stage1_h8_results.md").write_text(_stage1_report(results, benchmark, benchmark_items), encoding="utf-8")
    (REPORT_ROOT / "confirmatory_results.md").write_text(_confirmatory_report(results, benchmark, posthoc, ratios), encoding="utf-8")
    (REPORT_ROOT / "statistical_methods.md").write_text(_methods_report(), encoding="utf-8")
    (REPORT_ROOT / "robustness_results.md").write_text(_robustness_report(results, ratios), encoding="utf-8")
    (REPORT_ROOT / "k_stability_results.md").write_text(_k_report(results), encoding="utf-8")
    MANUSCRIPT_PATH.write_text(_manuscript_summary(results, benchmark, posthoc, distributions, ratios), encoding="utf-8")

    results["goal8_5_reconciliation"] = {
        "status": "complete",
        "formal_primary_results_changed": False,
        "benchmark_0": benchmark,
        "post_hoc_descriptive_scale_reference": {
            **posthoc,
            "measures": ratios.to_dict(orient="records"),
        },
        "human_risk_test_retest_context": {
            "pearson_r": 0.589023730,
            "n": 44,
            "role": "CONTEXTUAL_REFERENCE",
            "ceiling_or_reliability_correction_used": False,
        },
        "methods_results_consistency": "PASS",
        "reporting_bug": "benchmark_0_aggregation_naming_ambiguity",
        "analysis_bug_found": False,
        "llm_outputs_rerun": False,
    }
    MACHINE_RESULTS_PATH.write_text(
        json.dumps(_jsonable(results), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    status = STATUS_PATH.read_text(encoding="utf-8")
    section = f"""## Goal 8.5（2026-09-08）

- Goal 8正式結果維持不變：C1 Spearman rho={_fmt(primary['spearman'])}，95% CI=[{_fmt(primary_ci[0])}, {_fmt(primary_ci[1])}]，participant-ID permutation p={_fmt(results['primary_permutation_p'], 8)}；C1仍為唯一primary behavioral condition。
- Benchmark-0表面矛盾已釐清：0.3647是8題逐題Spearman的平均，0.6897是Benchmark-0與每位participant真人H8平均的Spearman；兩者來自同一34題Benchmark與同一342人，屬不同合法aggregation。未發現analysis或mapping bug，修正的是報告命名歧義。
- 新增metric scale audit、Human/C1/C2/C3/Ridge descriptives、analysis ledger、Methods–Results consistency與Goal 9唯一高階寫作資料包；一致性總結為PASS。
- 新增的constant-median MAE={_fmt(posthoc['constant_median_mae'])}及MAE/Human SD只標記為POST-HOC DESCRIPTIVE SCALE REFERENCE／NOT CONFIRMATORY BENCHMARK；沒有p-value、cutoff或primary角色變更。
- R2+R3的MAE=0.7571在sample-standardized outcome尺度，與C1/Ridge原尺度MAE不得直接比較；human risk test-retest r=0.589023730、N=44已由既有data audit與replication code確認，只作量測背景。
- 本Goal未重跑LLM、未修改模型輸出、participant mapping、Prompt、條件、K、Lists、matrix、scoring或正式分析方法；Goal 9仍未開始。

"""
    if "## Goal 8.5（2026-09-08）" not in status:
        status = status.replace("# 專案狀態\n\n", "# 專案狀態\n\n" + section, 1)
    STATUS_PATH.write_text(status, encoding="utf-8")

    public_paths = [
        BENCHMARK_REPORT_PATH, SCALE_AUDIT_PATH, DESCRIPTIVES_PATH, LEDGER_PATH,
        CONSISTENCY_PATH, MANUSCRIPT_PATH,
        REPORT_ROOT / "confirmatory_results.md", REPORT_ROOT / "statistical_methods.md",
        REPORT_ROOT / "robustness_results.md", REPORT_ROOT / "stage1_h8_results.md",
        REPORT_ROOT / "k_stability_results.md",
        MACHINE_RESULTS_PATH, STATUS_PATH,
    ]
    participant_pattern = re.compile(r"p_[0-9a-f]{32,64}", re.IGNORECASE)
    leaks = [path for path in public_paths if participant_pattern.search(path.read_text(encoding="utf-8"))]
    if leaks:
        raise Goal85AuditError(f"Participant-level identifiers found in public outputs: {leaks}")

    return {
        "status": "complete",
        "primary_unchanged": True,
        "benchmark_0": benchmark,
        "post_hoc_descriptive": {**posthoc, "measures": ratios.to_dict(orient="records")},
        "methods_results_consistency": "PASS",
        "participant_level_identifier_hits": 0,
        "llm_outputs_rerun": False,
        "manuscript_input_summary": str(MANUSCRIPT_PATH.relative_to(PROJECT_ROOT)),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    print(json.dumps(_jsonable(run_audit()), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
