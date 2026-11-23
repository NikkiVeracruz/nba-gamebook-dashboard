#!/usr/bin/env python3
"""Publish WNBA gamebook PDF availability for today and yesterday (America/New_York).

ESPN's public scoreboard supplies the schedule. Every displayed PDF is checked
against candidate WNBA PDF URLs using exact dates and team abbreviations.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
OUTPUT = Path("wnba/gamebooks.json")
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
PDF_BASE = "https://statsdmz.nba.com/pdfs"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; WNBAGamebookDashboard/1.0)", "Accept": "application/json,application/pdf,*/*"}
LOG = logging.getLogger("wnba-gamebooks")

# ESPN's abbreviations can differ from the WNBA's official PDF naming scheme.
WNBA_CODES = {"NY":"NYL","NYL":"NYL","LV":"LVA","LVA":"LVA","LA":"LAS","LAS":"LAS","PHO":"PHX","PHX":"PHX","WSH":"WAS","WAS":"WAS","CON":"CON","CT":"CON","GS":"GSV","GSV":"GSV","SEA":"SEA","ATL":"ATL","DAL":"DAL","MIN":"MIN","IND":"IND","CHI":"CHI","POR":"POR","TOR":"TOR"}

def wnba_code(value: str) -> str:
    code = value.upper().strip()
    return WNBA_CODES.get(code, code)

def valid_pdf(session: requests.Session, url: str) -> bool:
    """Check actual PDF bytes, not merely the URL or HTTP status."""
    try:
        with session.get(url, stream=True, timeout=18, headers={"Range": "bytes=0-31"}) as response:
            response.raise_for_status()
            first = next(response.iter_content(chunk_size=32), b"")
            return first.startswith(b"%PDF-")
    except (requests.RequestException, StopIteration) as exc:
        LOG.info("No verified PDF at %s (%s)", url, exc)
        return False

def games_on(session: requests.Session, day) -> list[dict]:
    response = session.get(SCOREBOARD, params={"dates": day.strftime("%Y%m%d"), "limit": "100"}, timeout=25)
    response.raise_for_status()
    results = []
    for event in response.json().get("events", []):
        competitions = event.get("competitions", [])
        if not competitions:
            continue
        comp = competitions[0]
        competitors = comp.get("competitors", [])
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        if not away or not home:
            continue
        away_team, home_team = away.get("team", {}), home.get("team", {})
        away_code = wnba_code(away_team.get("abbreviation", ""))
        home_code = wnba_code(home_team.get("abbreviation", ""))
        if not (away_code.isalpha() and home_code.isalpha() and len(away_code) == 3 and len(home_code) == 3):
            LOG.warning("Unknown WNBA team codes for %s", event.get("id"))
            continue
        status = event.get("status", {}).get("type", {})
        # Do not assume a gamebook is published merely because the game is final.
        is_final = bool(status.get("completed"))
        pdf_url = f"{PDF_BASE}/{day:%Y%m%d}/{day:%Y%m%d}_{away_code}{home_code}_book.pdf"
        game = {
            "id": str(event.get("id", "")),
            "date": day.isoformat(),
            "label": f"{away_team.get('displayName', away_code)} @ {home_team.get('displayName', home_code)}",
            "status": status.get("description", "Scheduled"),
        }
        # Only display a PDF when its bytes are verified; URL patterns are candidates, not proof.
        if is_final and valid_pdf(session, pdf_url):
            game["pdf_url"] = pdf_url
            LOG.info("Verified official PDF: %s", pdf_url)
        results.append(game)
    return results

def write_if_changed(path, payload):
    """Publish only genuine feed changes; save successful checks separately."""
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prior = {}
    visible = lambda x: {k: v for k, v in x.items() if k != "updated_at"}
    if visible(prior) == visible(payload):
        LOG.info("Feed unchanged; skipped timestamp-only commit")
        return False
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Seasonal guard: keep the website live but avoid report scraping in the offseason.
    # Broad calendar boundaries allow preseason and late postseason games; the
    # schedule source itself determines which games actually appear.
    if datetime.now(ET).month not in [5,6,7,8,9,10] and not __import__("adaptive_monitor").should_poll("WNBA"):
        now = datetime.now(timezone.utc).isoformat()
        write_if_changed(OUTPUT, {"window_label": "Offseason", "updated_at": now, "offseason": True, "games": []})
        LOG.info("Offseason: skipping gamebook checks")
        return

    # Reporting day changes at 3:00 AM Eastern, including DST.
    today = (datetime.now(ET) - timedelta(hours=3)).date()
    days = [today - timedelta(days=1), today]
    session = requests.Session()
    session.headers.update(HEADERS)
    games = []
    failures = []
    for day in days:
        try:
            day_games = games_on(session, day)
            LOG.info("%s: %d games", day, len(day_games))
            games.extend(day_games)
        except (requests.RequestException, ValueError, KeyError) as exc:
            LOG.exception("Could not retrieve WNBA schedule for %s", day)
            failures.append(str(day))
    if failures:
        # Preserve existing data if the source is temporarily unavailable,
        # but never preserve games outside the current two-day window.
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            existing = {g["id"]: g for g in games}
            for g in previous.get("games", []):
                if g.get("date") in {str(x) for x in days} and g.get("id") not in existing:
                    games.append(g)
        except (OSError, ValueError):
            pass
    games.sort(key=lambda g: (g["date"], g["label"]))
    payload = {
        "window_label": f"{days[0]:%b %d, %Y} – {days[1]:%b %d, %Y} (ET)",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "games": games,
    }
    if failures:
        payload["warning"] = "Schedule data is temporarily unavailable for " + ", ".join(failures) + ". Some results may be delayed."
    write_if_changed(OUTPUT, payload)
    LOG.info("Published %d games (%d verified PDFs)", len(games), sum("pdf_url" in g for g in games))
    if len(failures) == len(days):
        raise RuntimeError("Both WNBA scoreboard requests failed; check source availability")

if __name__ == "__main__":
    main()
