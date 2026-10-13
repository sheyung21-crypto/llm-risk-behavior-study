"""Synthetic-only public export tests. No source data or model calls."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("model_answers", ROOT / "tools/model_answers.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def example():
    return {"collection": "original", "condition": "C1", "list_id": 1,
            "repeat_index": 1, "model": "gpt-5.6-terra", "reasoning": "medium",
            "codex_cli_version": "0.153.3",
            "answer": {"choices": [{"row": i, "choice": "SAFE"} for i in range(1, 22)]}}


class ModelAnswerTests(unittest.TestCase):
    def test_valid_choices(self):
        m.validate_record(example())

    def test_reject_identifiers_and_human_fields(self):
        for field in ("participant_id", "participant_key", "job_id", "session_id", "prompt",
                      "x_risk_av", "questionnaire_answers", "timestamp", "execution_order"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                m.validate_record({**example(), field: "synthetic"})

    def test_reject_answer_free_text(self):
        row = example()
        row["answer"]["explanation"] = "synthetic text"
        with self.assertRaises(ValueError):
            m.validate_record(row)

    def test_reject_nested_extra_field(self):
        row = example()
        row["answer"]["choices"][0]["prompt"] = "synthetic"
        with self.assertRaises(ValueError):
            m.validate_record(row)

    def test_choice_order_and_count(self):
        for modify in (lambda r: r["answer"]["choices"].pop(),
                       lambda r: r["answer"]["choices"].reverse()):
            row = example()
            modify(row)
            with self.assertRaises(ValueError):
                m.validate_record(row)

    def test_h8_is_model_predictions_only(self):
        row = example()
        row.update(collection="stage1", list_id=None,
                   answer={"responses": [{"R_item": item, "response": 5} for item in m.H8]})
        m.validate_record(row)
        row["answer"]["responses"][0]["response"] = True
        with self.assertRaises(ValueError):
            m.validate_record(row)

    def test_public_order_independent_of_source_order(self):
        first, second = example(), example()
        second["condition"] = "C3"
        self.assertEqual(m.sorted_records([first, second]), m.sorted_records([second, first]))

    def test_duplicates_retained(self):
        row = example()
        self.assertEqual(len(m.sorted_records([row, copy.deepcopy(row)])), 2)

    def test_reject_duplicate_keys_nonfinite(self):
        for text in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                m.strict_json(text)

    def test_collection_count(self):
        with self.assertRaises(ValueError):
            m.validate_collection([example()], "original")

    def test_model_repeat_cli_rejected(self):
        for field, value in (("model", "other"), ("reasoning", "low"),
                             ("repeat_index", 2), ("codex_cli_version", "free text")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                m.validate_record({**example(), field: value})


if __name__ == "__main__":
    unittest.main(verbosity=2)
