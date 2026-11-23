#!/usr/bin/env python3
"""Recover every verified 2026 WNBA PDF missing from the cumulative archive.

This does not invent report URLs, discard prior records, or declare missing files
unpublished. Unresolved games are recorded as NEEDS_REVIEW, never as verified.
"""
import json
import logging
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests
import wnba_archive
import wnba_monitor

ARCHIVE = Path("wnba/archive.json")
REVIEW = Path("wnba/report-review.json")
LOG = logging.getLogger("wnba-recover")


def recover(archive_path=ARCHIVE, review_path=REVIEW, session=None,
            today=None, start=date(2026, 1, 1)):
    today = today or date.today()
    existing = wnba_archive.load_payload(Path(archive_path), "WNBA archived history")
    records = wnba_archive.validate_games(existing, "WNBA before report recovery")
    missing = [game for game in records.values()
               if (start <= date.fromisoformat(game["date"]) < today
                   and str(game.get("status", "")).lower().startswith("final")
                   and not game.get("pdf_url"))]
    by_date = defaultdict(list)
    for game in missing:
        by_date[game["date"]].append(game)
    session = session or requests.Session()
    session.headers.update(wnba_monitor.HEADERS)
    discovered, issues = 0, []
    for day in sorted(by_date):
        try:
            official = {
                str(g.get("id")): g for g in wnba_monitor.games_on(
                    session, date.fromisoformat(day)
                )
            }
            for archived in by_date[day]:
                found = official.get(str(archived.get("id")), {})
                if found.get("pdf_url") and not archived.get("pdf_url"):
                    archived["pdf_url"] = found["pdf_url"]
                    discovered += 1
                if not archived.get("pdf_url"):
                    issues.append({
                        "id": str(archived.get("id")),
                        "date": day,
                        "label": archived.get("label", ""),
                        "classification": (
                            "exhibition_needs_review"
                            if "JAPAN" in str(archived.get("label", "")).upper()
                            else "official_pdf_not_verified"
                        ),
                    })
        except (requests.RequestException, ValueError, KeyError) as exc:
            LOG.warning("Schedule unverified on %s: %s", day, exc)
            for archived in by_date[day]:
                issues.append({
                    "id": str(archived.get("id")),
                    "date": day,
                    "label": archived.get("label", ""),
                    "classification": "source_unavailable_needs_review",
                })
    if discovered:
        merged = wnba_archive.build_archive({"games": existing["games"]},
                                            wnba_archive.load_payload(
                                                Path(archive_path),
                                                "WNBA archive prepublication"))
        wnba_archive.atomic_write_json(Path(archive_path), merged)
    issues.sort(key=lambda g: (g["date"], g["id"]), reverse=True)
    summary = {
        "league": "WNBA",
        "scope": "2026",
        "verified_recovered_in_scan": discovered,
        "unresolved_count": len(issues),
        "unresolved": issues,
    }
    previous = {}
    if Path(review_path).exists():
        previous = json.loads(Path(review_path).read_text(encoding="utf-8"))
    # Avoid needless commits caused only by the count of reports newly recovered.
    stable = {k: v for k, v in summary.items() if k != "verified_recovered_in_scan"}
    old_stable = {k: v for k, v in previous.items() if k != "verified_recovered_in_scan"}
    if stable != old_stable:
        Path(review_path).parent.mkdir(parents=True, exist_ok=True)
        Path(review_path).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    print(f"WNBA 2026: checked {len(missing)} missing PDFs over "
          f"{len(by_date)} dates; verified {discovered} new PDF(s); "
          f"{len(issues)} remain under review")
    return summary


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    recover()


if __name__ == "__main__":
    main()
