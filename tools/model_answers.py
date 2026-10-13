"""Strict public model-answer schema: no subject keys or free text."""
from collections import Counter
import json

H8 = ("R3", "R6", "R8", "R12", "R13", "R17", "R24", "R41")
COUNTS = {"original": 1368, "stage1": 342, "k": 240, "c2": 684}
FIELDS = {"collection", "condition", "list_id", "repeat_index", "model",
          "reasoning", "codex_cli_version", "answer"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def strict_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError("Nonfinite JSON value")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)


def validate_record(row):
    if not isinstance(row, dict) or set(row) != FIELDS:
        raise ValueError("Public fields must match the exact allowlist")
    source = row["collection"]
    if source not in COUNTS:
        raise ValueError("Unapproved collection")
    if row["model"] != "gpt-5.6-terra" or row["reasoning"] != "medium":
        raise ValueError("Unexpected model configuration")
    if row["codex_cli_version"] not in {"0.153.0", "0.153.3"}:
        raise ValueError("Unexpected CLI version")
    repeat = row["repeat_index"]
    if type(repeat) is not int or repeat not in (range(2, 6) if source == "k" else [1]):
        raise ValueError("Unexpected repeat")
    conditions = {"original": {"C1", "C3"}, "stage1": {"C1"},
                  "k": {"C1"}, "c2": {"C2"}}
    if row["condition"] not in conditions[source]:
        raise ValueError("Unexpected condition")
    answer = row["answer"]
    if not isinstance(answer, dict):
        raise ValueError("Answer must be an object")
    if source == "stage1":
        if row["list_id"] is not None or set(answer) != {"responses"}:
            raise ValueError("Unexpected Stage 1 fields")
        values = answer["responses"]
        if not isinstance(values, list) or len(values) != 8:
            raise ValueError("Expected eight model predictions")
        for item, name in zip(values, H8):
            if (not isinstance(item, dict) or set(item) != {"R_item", "response"}
                    or item["R_item"] != name or type(item["response"]) is not int
                    or not 0 <= item["response"] <= 10):
                raise ValueError("Invalid H8 model prediction")
    else:
        if type(row["list_id"]) is not int or row["list_id"] not in {1, 2}:
            raise ValueError("Unexpected list")
        if set(answer) != {"choices"}:
            raise ValueError("Unexpected Stage 2 fields")
        values = answer["choices"]
        if not isinstance(values, list) or len(values) != 21:
            raise ValueError("Expected 21 model choices")
        for number, item in enumerate(values, 1):
            if (not isinstance(item, dict) or set(item) != {"row", "choice"}
                    or type(item["row"]) is not int or item["row"] != number
                    or item["choice"] not in {"LOTTERY", "SAFE"}):
                raise ValueError("Invalid model choice")


def sorted_records(records):
    for row in records:
        validate_record(row)
    # Only public content determines order. No original order or subject mapping.
    return sorted(records, key=canonical)


def validate_collection(records, source):
    if source not in COUNTS or len(records) != COUNTS[source]:
        raise ValueError("Model-answer collection count mismatch")
    for row in records:
        validate_record(row)
        if row["collection"] != source:
            raise ValueError("Mixed collections")
    if records != sorted_records(records):
        raise ValueError("Source order must not be preserved")
    actual = Counter((r["condition"], r["list_id"], r["repeat_index"]) for r in records)
    if source == "stage1":
        expected = {("C1", None, 1): 342}
    elif source == "k":
        expected = {("C1", li, rep): 30 for li in (1, 2) for rep in range(2, 6)}
    else:
        expected = {(c, li, 1): 342 for c in (["C1", "C3"] if source == "original" else ["C2"]) for li in (1, 2)}
    if actual != expected:
        raise ValueError("Condition/list/repeat counts mismatch")
