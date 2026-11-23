"""Special basketball events are verified by ID, not guessed PDF filenames."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import reconcile_basketball_special_2026 as s


class Response:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def json(self):return self.data


class Session:
    def __init__(self,malformed=False):
        self.malformed=malformed
        self.calls=[]
    def get(self,url,params,timeout):
        league="WNBA" if "/wnba/" in url else "NBA"
        day=params["dates"]
        expected=s.SPECIAL[league][f"{day[:4]}-{day[4:6]}-{day[6:8]}"]
        self.calls.append((league,day))
        games=[]
        for gid in expected:
            games.append({
                "id":gid,"date":day[:4]+"-"+day[4:6]+"-"+day[6:8]+"T20:00:00-05:00",
                "status":{"type":{"completed":True,"description":"Final"}},
                "competitions":[{"competitors":[
                    {"homeAway":"away","team":{"displayName":"Visitors"}},
                    {"homeAway":"home","team":{"displayName":"Hosts"}},
                ]}],
            })
        if self.malformed:games.pop()
        return Response({"events":games})


class BasketballSpecialTests(unittest.TestCase):
    def setUp(self):
        a=patch.object(s.wnba_monitor,"valid_pdf",return_value=False)
        b=patch.object(s.nba_monitor,"valid_pdf",return_value=False)
        a.start();b.start()
        self.addCleanup(a.stop);self.addCleanup(b.stop)

    def test_all_expected_special_ids_require_verified_schedule(self):
        self.assertEqual(len(s.fetch_special("NBA",Session())),4)
        self.assertEqual(len(s.fetch_special("WNBA",Session())),4)
        with self.assertRaises(ValueError):
            s.fetch_special("NBA",Session(malformed=True))

    def test_archive_reconciliation_preserves_old_docs_and_no_fake_pdfs(self):
        for league in ("NBA","WNBA"):
            with self.subTest(league=league),tempfile.TemporaryDirectory() as root:
                path=Path(root)/"archive.json"
                review=Path(root)/"special.json"
                path.write_text(json.dumps({"games":[{"id":"old","date":"2026-05-01",
                          "label":"Old @ Game","status":"Final",
                          "pdf_url":"https://statsdmz.nba.com/pdfs/old.pdf"}]}))
                result=s.reconcile(league,Session(),path,review)
                self.assertEqual(result["matched_special_events"],4)
                self.assertEqual(result["new_records_added"],4)
                data=json.loads(path.read_text())
                self.assertEqual(len(data["games"]),5)
                self.assertEqual(sum(bool(g.get("pdf_url")) for g in data["games"]),1)
                before=path.read_text()
                again=s.reconcile(league,Session(),path,review)
                self.assertEqual(again["new_records_added"],0)
                self.assertEqual(path.read_text(),before)

    def test_official_pdf_candidate_requires_verified_bytes(self):
        url=s.try_official_pdf("WNBA","2026-04-25",
                               {"abbreviation":"NGR"},
                               {"abbreviation":"LAS"},Session())
        self.assertIsNone(url)
        with patch.object(s.wnba_monitor,"valid_pdf",return_value=True):
            url=s.try_official_pdf("WNBA","2026-04-25",
                                   {"abbreviation":"NGR"},
                                   {"abbreviation":"LAS"},Session())
        self.assertEqual(url,"https://statsdmz.nba.com/pdfs/20260425/20260425_NGRLAS_book.pdf")
        with patch.object(s.nba_monitor,"valid_pdf",return_value=True):
            url=s.try_official_pdf("NBA","2026-02-15",
                                   {"displayName":"World"},
                                   {"displayName":"Stars"},Session())
        self.assertEqual(url,"https://statsdmz.nba.com/pdfs/20260215/20260215_WLDSTR_book.pdf")

    def test_allstar_and_exhibition_classification(self):
        self.assertEqual(s.type_of("NBA","2026-02-15"),"nba_all_star")
        self.assertEqual(s.type_of("WNBA","2026-07-25"),"wnba_all_star")
        self.assertEqual(s.type_of("WNBA","2026-04-27"),"wnba_exhibition")


if __name__=="__main__":
    unittest.main()
