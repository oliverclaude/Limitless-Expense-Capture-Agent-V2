"""Rules that do not need a mailbox or a model."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("CLAIM_SUBJECT_PREFIX", "claimv2:")

from extract import apply_decision, apply_vat_policy, comment_from_subject  # noqa: E402
from mail import is_claim_subject  # noqa: E402
from ntfy_out import claim_reply_text  # noqa: E402
from replies import (  # noqa: E402
    coerce_duplicate_action,
    coerce_reply_action,
    decision_prompt,
    interpret_reply,
    parse_decision,
    parse_reply_command,
    parse_setting,
)
from settings import (  # noqa: E402
    append_csv_value,
    apply_assignments,
    display_value,
    format_settings,
    job_id_from_store,
    normalize_value,
    parse_created_job_id,
    parse_settings_command,
    read_env,
    update_key,
)


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
    def test_duplicate_action_is_the_agents_choice(self) -> None:
        self.assertEqual(coerce_duplicate_action("keep"), "keep")
        self.assertEqual(coerce_duplicate_action("remove_old"), "remove_old")
        self.assertEqual(coerce_duplicate_action("drop_new"), "drop_new")
        self.assertEqual(coerce_duplicate_action("ask"), "ask")

    def test_yes_is_not_a_duplicate_decision(self) -> None:
        self.assertEqual(coerce_duplicate_action("yes"), "ask")
        self.assertEqual(coerce_duplicate_action("ok"), "ask")
        self.assertEqual(coerce_duplicate_action("confirm"), "ask")
        self.assertEqual(coerce_duplicate_action(""), "ask")

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

    def test_list_hides_secrets_and_updates_only_known_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text(
                "IMAP_PASSWORD=secret\nHIGH_VALUE_ZAR=500\nNTFY_TOPIC=Fin-Limitless-V2\n",
                encoding="utf-8",
            )
            shown = format_settings(read_env(path))
            self.assertIn("IMAP_PASSWORD=(set)", shown)
            self.assertNotIn("secret", shown)
            self.assertIn("HIGH_VALUE_ZAR=500", shown)
            self.assertEqual(display_value("OLLAMA_API_KEY", ""), "(empty)")
            text = apply_assignments(path, [("HIGH_VALUE_ZAR", "800")])
            self.assertIn("HIGH_VALUE_ZAR is now 800.", text)
            self.assertIn("HIGH_VALUE_ZAR=800", path.read_text(encoding="utf-8"))
            self.assertIn("IMAP_PASSWORD=secret", path.read_text(encoding="utf-8"))
            with self.assertRaises(RuntimeError):
                apply_assignments(path, [("NOT_A_SETTING", "1")])
            self.assertNotIn("NOT_A_SETTING", path.read_text(encoding="utf-8"))
            added = apply_assignments(path, [], ["Parking"])
            self.assertIn("Added journal Parking.", added)
            self.assertIn("JOURNALS=Parking", path.read_text(encoding="utf-8"))

    def test_settings_command_parse_and_number_checks(self) -> None:
        pairs, journals = parse_settings_command(
            ["--set", "HIGH_VALUE_ZAR=800", "--add-journal", "Parking"]
        )
        self.assertEqual(pairs, [("HIGH_VALUE_ZAR", "800")])
        self.assertEqual(journals, ["Parking"])
        self.assertEqual(normalize_value("POLL_INTERVAL_MINUTES", "15"), "15")
        with self.assertRaises(RuntimeError):
            normalize_value("POLL_INTERVAL_MINUTES", "0")
        with self.assertRaises(RuntimeError):
            normalize_value("DELETE_COMPLETED_STATE", "yes")
        with self.assertRaises(RuntimeError):
            normalize_value("IMAP_PORT", "abc")


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

    def test_using_the_new_line_removes_the_old_row_and_proofs(self) -> None:
        from agent import remove_old
        from sheet import find_duplicate, write_claim_row

        with tempfile.TemporaryDirectory() as tmp:
            month = Path(tmp) / "2026-10"
            proofs = month / "proofs"
            proofs.mkdir(parents=True)
            old_orig = proofs / "2026-10-02_Cafe_orig.jpg"
            old_scan = proofs / "2026-10-02_Cafe_scan.jpg"
            new_orig = proofs / "2026-10-02_Cafe_orig-2.jpg"
            new_scan = proofs / "2026-10-02_Cafe_scan-2.jpg"
            for path in (old_orig, old_scan, new_orig, new_scan):
                path.write_bytes(b"x")
            old = {
                "claim_date": "2026-10-02",
                "personal_amount": 120.0,
                "company_amount": 0,
                "vat": 0,
                "comment": "Lunch",
                "journal": "Entertainment",
                "proof_file": old_scan.name,
                "merchant": "Cafe",
            }
            write_claim_row(month / "2026-10-Expenses.xlsx", old)
            fields = {**old, "proof_file": new_scan.name}
            note = remove_old(fields, new_orig, {"proof_file": old_scan.name})
            self.assertIn("Removed the old line", note)
            self.assertFalse(old_orig.exists())
            self.assertFalse(old_scan.exists())
            self.assertTrue(new_orig.is_file())
            self.assertTrue(new_scan.is_file())
            self.assertIsNone(
                find_duplicate(month / "2026-10-Expenses.xlsx", {**fields, "proof_file": "other.jpg"})
            )

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


class ListenTests(unittest.TestCase):
    def test_own_messages_and_empty_bodies_are_not_replies(self) -> None:
        self.assertIsNone(claim_reply_text({"event": "message", "message": "Logged.", "tags": ["hermes-agent"]}))
        self.assertIsNone(claim_reply_text({"event": "message", "message": "   ", "tags": []}))
        self.assertEqual(
            claim_reply_text({"event": "message", "message": "Please continue", "tags": []}),
            "Please continue",
        )

    def test_unreadable_model_output_does_not_log(self) -> None:
        self.assertEqual(parse_decision("I think it is confirmed.", duplicate=False)["action"], "ask")
        self.assertEqual(parse_decision("I think it is confirmed.", duplicate=False)["overrides"], {})
        decided = parse_decision('{"action": "confirm"}', duplicate=True)
        self.assertEqual(decided["action"], "ask")
        self.assertEqual(decided["overrides"], {})

    def test_model_json_keeps_only_named_fields(self) -> None:
        raw = '<think>long</think>\n```json\n{"action": "update", "merchant": "Woolworths", "comment": null}\n```'
        decided = parse_decision(raw, duplicate=False)
        self.assertEqual(decided["action"], "update")
        self.assertEqual(decided["overrides"], {"merchant": "Woolworths"})

    def test_the_program_asks_the_model_instead_of_matching_words(self) -> None:
        claim = {
            "hold_kind": "high_value",
            "reasons": ["amount R20 is over the high-value mark R10"],
            "fields": {"merchant": "Cafe", "personal_amount": 20, "company_amount": 0},
        }
        seen: list[str] = []

        def complete(prompt: str) -> str:
            seen.append(prompt)
            return '{"action": "confirm"}'

        decided = interpret_reply("Please continue", claim, complete)
        self.assertEqual(decided["action"], "confirm")
        self.assertIn("Please continue", seen[0])
        self.assertIn("Cafe", seen[0])
        self.assertIn("confirm", decision_prompt("Please continue", claim))


class VatTests(unittest.TestCase):
    def test_entertainment_vat_is_forced_to_zero(self) -> None:
        fields = {"journal": "Entertainment", "vat": 15.0}
        apply_vat_policy(fields, ["Telephone & Internet"])
        self.assertEqual(fields["vat"], 0.0)


if __name__ == "__main__":
    unittest.main()
