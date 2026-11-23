"""Regression cases: PDX PDFs, recovery integrity, unverified exceptions."""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import repair_wnba_2026 as repair
import wnba_monitor


class HeaderOnlySession:
    def __init__(self):
        self.headers = {}


class WnbaPdfRecoveryTests(unittest.TestCase):
    def test_portland_filename_uses_pdx_not_nba_por(self):
        self.assertEqual(wnba_monitor.wnba_code("POR"), "PDX")
        self.assertEqual(wnba_monitor.wnba_code("PDX"), "PDX")
        self.assertEqual(wnba_monitor.wnba_code("SEA"), "SEA")
        self.assertEqual(wnba_monitor.wnba_code("TOR"), "TOR")

    def test_original_verified_report_preserved_and_only_valid_new_link_added(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "archive.json"
            review = Path(root) / "review.json"
            archive.write_text(json.dumps({"games": [
                {"id": "1", "date": "2026-06-17", "label": "Seattle Storm @ Portland Fire", "status": "Final"},
                {"id": "2", "date": "2026-06-17", "label": "Atlanta Dream @ Chicago Sky",
                 "status": "Final", "pdf_url": "https://statsdmz.nba.com/pdfs/known.pdf"},
                {"id": "3", "date": "2026-04-29", "label": "JAPAN @ Phoenix Mercury", "status": "Final"}
            ]}))
            pdf = "https://statsdmz.nba.com/pdfs/20260617/20260617_SEAPDX_book.pdf"
            def games(_, day):
                if day == date(2026, 6, 17):
                    return [{"id": "1", "pdf_url": pdf}]
                return [{"id": "3"}]
            with patch.object(wnba_monitor, "games_on", side_effect=games):
                result = repair.recover(archive, review, HeaderOnlySession(), today=date(2026, 11, 23))
            self.assertEqual(result["verified_recovered_in_scan"], 1)
            records = {g["id"]: g for g in json.loads(archive.read_text())["games"]}
            self.assertEqual(records["1"]["pdf_url"], pdf)
            self.assertEqual(records["2"]["pdf_url"], "https://statsdmz.nba.com/pdfs/known.pdf")
            self.assertNotIn("pdf_url", records["3"])
            self.assertEqual(result["unresolved_count"], 1)
            self.assertEqual(result["unresolved"][0]["classification"], "exhibition_needs_review")

    def test_unavailable_pdf_does_not_create_fake_url(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "archive.json"
            review = Path(root) / "review.json"
            archive.write_text(json.dumps({"games": [
                {"id": "1", "date": "2026-08-10", "status": "Final", "label": "Fire @ Team"}
            ]}))
            before = archive.read_text()
            with patch.object(wnba_monitor, "games_on", return_value=[{"id": "1"}]):
                result = repair.recover(archive, review, HeaderOnlySession(), today=date(2026, 11, 23))
            self.assertEqual(result["verified_recovered_in_scan"], 0)
            self.assertEqual(archive.read_text(), before)
            self.assertEqual(result["unresolved_count"], 1)

    def test_already_saved_pdf_not_rechecked(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "archive.json"
            review = Path(root) / "review.json"
            archive.write_text(json.dumps({"games": [
                {"id": "1", "date": "2026-06-17", "status": "Final", "label": "Fire @ Team",
                 "pdf_url": "https://statsdmz.nba.com/pdfs/known.pdf"}
            ]}))
            with patch.object(wnba_monitor, "games_on") as scan:
                result = repair.recover(archive, review, HeaderOnlySession(), today=date(2026, 11, 23))
            scan.assert_not_called()
            self.assertEqual(result["unresolved_count"], 0)


if __name__ == "__main__":
    unittest.main()
