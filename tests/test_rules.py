"""Rules that do not need a mailbox or a model."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CLAIM_SUBJECT_PREFIX", "claimv2:")

from extract import apply_decision, apply_vat_policy, comment_from_subject  # noqa: E402
from mail import is_claim_subject  # noqa: E402
from replies import coerce_reply_action, duplicate_action, parse_reply_command, parse_setting  # noqa: E402
from settings import append_csv_value, job_id_from_store, parse_created_job_id, update_key  # noqa: E402


class SubjectTests(unittest.TestCase):
    def test_prefix_is_case_insensitive(self) -> None:
        self.assertTrue(is_claim_subject("CLAIMV2: lunch"))
        self.assertTrue(is_claim_subject("claimv2: lunch"))

    def test_version_one_subject_does_not_match(self) -> None:
        self.assertFalse(is_claim_subject("CLAIM: lunch"))
        self.assertFalse(is_claim_subject("claim: lunch"))

    def test_comment_strips_only_the_prefix(self) -> None:
        self.assertEqual(comment_from_subject("claimv2: Slip for client drinks"), "Client Drinks")
        self.assertEqual(comment_from_subject("CLAIM: lunch"), "CLAIM: Lunch")


class ReplyTests(unittest.TestCase):
    def test_duplicate_phrases(self) -> None:
        self.assertEqual(duplicate_action("Keep both."), "keep")
        self.assertEqual(duplicate_action("Remove the old one"), "remove_old")
        self.assertEqual(duplicate_action("Don't capture the new one"), "drop_new")
        self.assertEqual(duplicate_action("do not capture the new one"), "drop_new")

    def test_yes_is_not_a_duplicate_decision(self) -> None:
        self.assertIsNone(duplicate_action("yes"))
        self.assertIsNone(duplicate_action("ok keep it"))

    def test_unknown_model_action_is_not_a_log(self) -> None:
        self.assertEqual(coerce_reply_action("confirm"), "confirm")
        self.assertEqual(coerce_reply_action("skip"), "skip")
        self.assertEqual(coerce_reply_action("update"), "update")
        self.assertEqual(coerce_reply_action("ask"), "ask")
        self.assertEqual(coerce_reply_action("yes"), "ask")
        self.assertEqual(coerce_reply_action(""), "ask")

    def test_reply_command_keeps_the_words_and_the_action(self) -> None:
        parsed = parse_reply_command(
            ["--action", "confirm", "--text", "The claim is good to continue please."]
        )
        self.assertEqual(parsed["action"], "confirm")
        self.assertEqual(parsed["text"], "The claim is good to continue please.")
        self.assertEqual(parsed["overrides"], {})

    def test_reply_command_takes_only_named_fields(self) -> None:
        parsed = parse_reply_command(
            ["--action", "update", "--merchant", "Woolworths", "--text", "The merchant is Woolworths."]
        )
        self.assertEqual(parsed["overrides"], {"merchant": "Woolworths"})


class DecisionTests(unittest.TestCase):
    def test_confirm_clears_the_hold_without_a_phrase_list(self) -> None:
        fields = {
            "claim_date": "2026-10-31",
            "personal_amount": 624.0,
            "company_amount": 0.0,
            "vat": 10.0,
            "merchant": "Bootlegger Irene Link",
            "comment": "Client Coffee",
            "journal": "Entertainment",
            "needs_review": True,
            "review_reasons": ["waiting"],
        }
        result = apply_decision(fields, ["Entertainment", "Staff Welfare"], "confirm", {})
        self.assertEqual(result["action"], "confirm")
        self.assertFalse(result["fields"]["needs_review"])
        self.assertEqual(result["fields"]["vat"], 0.0)
        self.assertEqual(result["fields"]["personal_amount"], 624.0)

    def test_unknown_action_does_not_change_the_claim(self) -> None:
        fields = {
            "claim_date": "2026-10-31",
            "personal_amount": 624.0,
            "company_amount": 0.0,
            "merchant": "Bootlegger Irene Link",
            "journal": "Entertainment",
        }
        result = apply_decision(fields, ["Entertainment"], "yes", {"merchant": "Other"})
        self.assertEqual(result["action"], "ask")
        self.assertEqual(result["fields"]["merchant"], "Bootlegger Irene Link")


class SettingTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_setting("set the high value to R800"), {"kind": "high_value", "value": "800"})
        self.assertEqual(parse_setting("add a journal named Parking"), {"kind": "journal", "value": "Parking"})
        self.assertEqual(parse_setting("check mail every 45 minutes"), {"kind": "interval", "value": "45"})
        self.assertIsNone(parse_setting("yes"))

    def test_env_edit_keeps_other_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("IMAP_PASSWORD=secret\nHIGH_VALUE_ZAR=500\n", encoding="utf-8")
            update_key(path, "HIGH_VALUE_ZAR", "800")
            text = path.read_text(encoding="utf-8")
            self.assertIn("IMAP_PASSWORD=secret", text)
            self.assertIn("HIGH_VALUE_ZAR=800", text)
            self.assertNotIn("HIGH_VALUE_ZAR=500", text)
            self.assertTrue(append_csv_value(path, "JOURNALS", "Parking"))
            self.assertFalse(append_csv_value(path, "JOURNALS", "parking"))
            self.assertIn("JOURNALS=Parking", path.read_text(encoding="utf-8"))


class SheetTests(unittest.TestCase):
    def test_duplicate_and_delete_row(self) -> None:
        from sheet import delete_claim_row, find_duplicate, write_claim_row

        with tempfile.TemporaryDirectory() as tmp:
            sheet = Path(tmp) / "2026-10" / "2026-10-Expenses.xlsx"
            fields = {
                "claim_date": "2026-10-02",
                "personal_amount": 120.0,
                "company_amount": 0,
                "vat": 0,
                "comment": "Lunch",
                "journal": "Entertainment",
                "proof_file": "2026-10-02_Cafe_scan.jpg",
                "merchant": "Cafe",
            }
            write_claim_row(sheet, fields)
            found = find_duplicate(sheet, {**fields, "proof_file": "2026-10-02_Cafe_scan-2.jpg"})
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found["comment"], "Lunch")
            self.assertTrue(delete_claim_row(sheet, "2026-10-02_Cafe_scan.jpg"))
            self.assertIsNone(find_duplicate(sheet, {**fields, "proof_file": "other.jpg"}))

    def test_proof_pair_finds_the_other_extension(self) -> None:
        from sheet import proof_pair_paths

        with tempfile.TemporaryDirectory() as tmp:
            proofs = Path(tmp)
            orig = proofs / "2026-10-02_Cafe_orig.png"
            scan = proofs / "2026-10-02_Cafe_scan.jpg"
            orig.write_bytes(b"png")
            scan.write_bytes(b"jpg")
            names = {path.name for path in proof_pair_paths(proofs, scan.name)}
            self.assertIn(orig.name, names)
            self.assertIn(scan.name, names)


class JobIdTests(unittest.TestCase):
    def test_created_line_ignores_colour(self) -> None:
        text = "\x1b[32mCreated job: abcdef123456\x1b[0m\n"
        self.assertEqual(parse_created_job_id(text), "abcdef123456")

    def test_store_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.json"
            path.write_text('{"jobs": [{"id": "abc123def456", "name": "expense-v2"}]}', encoding="utf-8")
            self.assertEqual(job_id_from_store("expense-v2", path), "abc123def456")
            self.assertEqual(job_id_from_store("other", path), "")


class CropTests(unittest.TestCase):
    def test_rectangle_uses_the_longer_edges(self) -> None:
        import numpy as np
        from crop import _four_point_transform

        image = np.full((500, 500, 3), 255, dtype=np.uint8)
        quad = np.array([[40, 60], [240, 80], [300, 320], [20, 220]], dtype=np.float32)
        warped = _four_point_transform(image, quad)
        self.assertIsNotNone(warped)
        assert warped is not None
        top = float(np.linalg.norm(quad[1] - quad[0]))
        bottom = float(np.linalg.norm(quad[2] - quad[3]))
        left = float(np.linalg.norm(quad[3] - quad[0]))
        right = float(np.linalg.norm(quad[2] - quad[1]))
        self.assertEqual(warped.shape[1], int(max(top, bottom)))
        self.assertEqual(warped.shape[0], int(max(left, right)))


class VatTests(unittest.TestCase):
    def test_entertainment_vat_is_forced_to_zero(self) -> None:
        fields = {"journal": "Entertainment", "vat": 15.0}
        apply_vat_policy(fields, ["Telephone & Internet"])
        self.assertEqual(fields["vat"], 0.0)


if __name__ == "__main__":
    unittest.main()
