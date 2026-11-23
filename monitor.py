#!/usr/bin/env python3
"""Publish NBA gamebook PDFs for yesterday, today, tomorrow (3 AM ET rollover).

ESPN's public scoreboard supplies the schedule. Every displayed PDF is checked
at the official NBA statsdmz host using its exact date and team abbreviations.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import archive as archive_module
from three_day_window import three_days, archive_outgoing_games

ET = ZoneInfo("America/New_York")
OUTPUT = Path("gamebooks.json")
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
PDF_BASE = "https://statsdmz.nba.com/pdfs"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NBAGamebookDashboard/1.0)", "Accept": "application/json,application/pdf,*/*"}
LOG = logging.getLogger("nba-gamebooks")

# ESPN's abbreviations can differ from the NBA's official PDF naming scheme.
NBA_CODES = {"GS": "GSW", "GSW": "GSW", "NY": "NYK", "NYK": "NYK",
             "SA": "SAS", "SAS": "SAS", "NO": "NOP", "NOP": "NOP",
             "UTAH": "UTA", "UT": "UTA", "WSH": "WAS", "WAS": "WAS",
             "BKN": "BKN", "BK": "BKN", "PHX": "PHX", "PHO": "PHX",
             "CHA": "CHA", "CH": "CHA", "LAC": "LAC", "LA": "LAL",
             "LAL": "LAL", "POR": "POR", "OKC": "OKC"}

def nba_code(value: str) -> str:
    code = value.upper().strip()
    return NBA_CODES.get(code, code)

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
        away_code = nba_code(away_team.get("abbreviation", ""))
        home_code = nba_code(home_team.get("abbreviation", ""))
        if not (away_code.isalpha() and home_code.isalpha() and len(away_code) == 3 and len(home_code) == 3):
            LOG.warning("Unknown NBA team codes for %s", event.get("id"))
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
    # Archive disappearing dates *before* publishing the new three-day feed.
    days = three_days()
    archive_outgoing_games(OUTPUT, archive_module.ARCHIVE, days, archive_module)
    # Seasonal guard: keep the website live but avoid report scraping in the offseason.
    # Broad calendar boundaries allow preseason and late postseason games; the
    # schedule source itself determines which games actually appear.
    if datetime.now(ET).month not in [10,11,12,1,2,3,4,5,6] and not __import__("adaptive_monitor").should_poll("NBA"):
        now = datetime.now(timezone.utc).isoformat()
        write_if_changed(OUTPUT, {"window_label": f"{days[0]:%b %d, %Y} – {days[-1]:%b %d, %Y} (ET; 3 AM rollover)", "window_dates": [d.isoformat() for d in days], "updated_at": now, "offseason": True, "games": []})
        LOG.info("Offseason: skipping gamebook checks")
        return

    # Reporting day changes at 3:00 AM Eastern, including DST.
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
            LOG.exception("Could not retrieve NBA schedule for %s", day)
            failures.append(str(day))
    if failures:
        # Preserve existing data if the source is temporarily unavailable,
        # but never preserve games outside the current three-day window.
        try:
            previous = json.loads(OUTPUT.read_text(encoding="utf-8"))
            existing = {g["id"]: g for g in games}
            for g in previous.get("games", []):
                if g.get("date") in {str(x) for x in days} and g.get("id") not in existing:
                    games.append(g)
        except (OSError, ValueError):
            pass
    # Completeness guard: a successful but partial schedule response must not erase games.
    try:
        prior_data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        known = {str(g["id"]): g for g in prior_data.get("games", []) if g.get("date") in {d.isoformat() for d in days} and g.get("id")}
        seen = {str(g["id"]) for g in games}
        missing = [g for gid, g in known.items() if gid not in seen]
        if missing:
            LOG.warning("SCHEDULE COMPLETENESS ALERT: %d previously known NBA games missing from source; retaining them", len(missing))
            games.extend(missing)
            failures.append("partial schedule: " + ", ".join(g["date"] for g in missing))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # Preserve links verified in a previous successful run if the official host
    # temporarily fails. Only restore the exact expected official URL for the
    # same game ID and date; never accept arbitrary URLs from old data.
    try:
        previous_data = json.loads(OUTPUT.read_text(encoding="utf-8"))
        prior_by_id = {str(g.get("id")): g for g in previous_data.get("games", [])
                       if isinstance(g, dict) and g.get("id")}
        for game in games:
            old = prior_by_id.get(str(game.get("id")))
            if not old or old.get("date") != game.get("date"):
                continue
            if not game.get("pdf_url") and old.get("pdf_url"):
                # Only trust a prior URL matching the official NBA path,
                # same date and the exact existing game's team pairing.
                from urllib.parse import urlparse
                prior_url = old["pdf_url"]
                expected_date = game["date"].replace("-", "")
                if (isinstance(prior_url, str)
                        and prior_url.startswith(PDF_BASE + "/" + expected_date + "/")
                        and prior_url.endswith("_book.pdf")
                        and old.get("label") == game.get("label")):
                    game["pdf_url"] = prior_url
                    LOG.warning("Retained previously verified NBA PDF for %s", game["id"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        LOG.warning("Could not restore previously verified report links: %s", exc)
    games.sort(key=lambda g: (g["date"], g["label"]))
    payload = {
        "window_label": f"{days[0]:%b %d, %Y} – {days[-1]:%b %d, %Y} (ET; 3 AM rollover)",
        "window_dates": [d.isoformat() for d in days],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "games": games,
    }
    if failures:
        payload["warning"] = "Schedule data is temporarily unavailable for " + ", ".join(failures) + ". Some results may be delayed."
    write_if_changed(OUTPUT, payload)
    LOG.info("Published %d games (%d verified PDFs)", len(games), sum("pdf_url" in g for g in games))
    if len(failures) == len(days):
        raise RuntimeError("All three NBA scoreboard requests failed; check source availability")

if __name__ == "__main__":
    main()
