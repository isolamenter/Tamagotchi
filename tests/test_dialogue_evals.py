from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

from evals.review import DEFAULT_CASES, main, make_review_sheet, validate_cases


class DialogueEvalFixtureTests(unittest.TestCase):
    """Dataset/worksheet tests only; these do not execute or evaluate an LLM."""

    def setUp(self):
        self.data = json.loads(DEFAULT_CASES.read_text(encoding="utf-8"))

    def test_thirty_cases_cover_six_categories_with_five_cases_each(self):
        self.assertEqual(validate_cases(self.data), [])
        self.assertEqual(len(self.data["cases"]), 30)
        self.assertEqual(set(Counter(c["category"] for c in self.data["cases"]).values()), {5})

    def test_all_cases_have_multiple_user_turns_and_end_in_user_input(self):
        for case in self.data["cases"]:
            with self.subTest(case=case["id"]):
                self.assertGreaterEqual(sum(t["role"] == "user" for t in case["turns"]), 2)
                self.assertEqual(case["turns"][-1]["role"], "user")

    def test_rejects_duplicate_ids(self):
        self.data["cases"][1]["id"] = self.data["cases"][0]["id"]
        self.assertTrue(any("duplicates" in e for e in validate_cases(self.data)))

    def test_rejects_unknown_speaker(self):
        self.data["cases"][0]["turns"][0]["speaker_id"] = "unknown"
        self.assertTrue(any("unknown speaker" in e for e in validate_cases(self.data)))

    def test_rejects_missing_rubric(self):
        self.data["cases"][0]["expected"]["must_not"] = []
        self.assertTrue(any("must_not" in e for e in validate_cases(self.data)))

    def test_rejects_invalid_reply_mode(self):
        self.data["cases"][0]["expected"]["allowed_reply_modes"] = ["cute"]
        self.assertTrue(any("reply_modes" in e for e in validate_cases(self.data)))

    def test_proactive_case_requires_boolean_and_no_direct_mentions(self):
        case = next(c for c in self.data["cases"] if c["stage"] == "proactive_eval")
        case["expected"]["should_speak"] = "false"
        case["turns"][0]["directed_to_bot"] = True
        errors = validate_cases(self.data)
        self.assertTrue(any("should_speak" in e for e in errors))
        self.assertTrue(any("must not mention" in e for e in errors))

    def test_rejects_non_object_and_wrong_types_without_crashing(self):
        for malformed in ([], None, "text", {"cases": [None]}):
            with self.subTest(data=malformed):
                self.assertTrue(validate_cases(malformed))
        self.data["cases"][0]["category"] = []
        self.data["cases"][0]["turns"][0]["speaker_id"] = []
        self.assertTrue(validate_cases(self.data))

    def test_dataset_cannot_be_marked_as_a_passed_model_evaluation(self):
        self.data["status"] = "passed"
        self.assertTrue(any("not_run" in e for e in validate_cases(self.data)))

    def test_blank_sheet_never_fabricates_scores_or_outputs(self):
        original = copy.deepcopy(self.data)
        sheet = make_review_sheet(self.data)
        self.assertEqual(self.data, original)
        self.assertEqual(sheet["status"], "not_run")
        for result in sheet["results"]:
            self.assertEqual(result["status"], "not_run")
            self.assertEqual(result["contract_verdict"], "not_run")
            self.assertIsNone(result["actual_output"])
            self.assertTrue(all(c["verdict"] == "not_run" for c in result["criteria"]))

    def test_invalid_dataset_cannot_produce_a_review_sheet(self):
        self.data["cases"] = []
        with self.assertRaises(ValueError):
            make_review_sheet(self.data)

    def test_cli_creates_template_but_never_overwrites_results(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            path = Path(temporary) / "review.json"
            self.assertEqual(main(["--template", str(path)]), 0)
            sheet = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(sheet["results"]), 30)
            path.write_text("existing reviewer work", encoding="utf-8")
            self.assertEqual(main(["--template", str(path)]), 1)
            self.assertEqual(path.read_text(encoding="utf-8"), "existing reviewer work")

    def test_cli_reports_bad_json_and_missing_path(self):
        with tempfile.TemporaryDirectory() as temporary, redirect_stdout(io.StringIO()):
            path = Path(temporary) / "cases.json"
            self.assertEqual(main(["--cases", str(path)]), 1)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(main(["--cases", str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
