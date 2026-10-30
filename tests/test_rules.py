"""Rules that do not need a mailbox or a model."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CLAIM_SUBJECT_PREFIX", "claimv2:")

from extract import apply_vat_policy, comment_from_subject  # noqa: E402
from mail import is_claim_subject  # noqa: E402
from replies import duplicate_action, parse_setting, simple_action  # noqa: E402
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

    def test_plain_confirm_and_skip(self) -> None:
        self.assertEqual(simple_action("Yes"), "confirm")
        self.assertEqual(simple_action("skip"), "skip")
        self.assertIsNone(simple_action("Keep both"))


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


class VatTests(unittest.TestCase):
    def test_entertainment_vat_is_forced_to_zero(self) -> None:
        fields = {"journal": "Entertainment", "vat": 15.0}
        apply_vat_policy(fields, ["Telephone & Internet"])
        self.assertEqual(fields["vat"], 0.0)


if __name__ == "__main__":
    unittest.main()
