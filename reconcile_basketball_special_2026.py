#!/usr/bin/env python3
"""Reconcile 2026 NBA/WNBA special fixtures omitted by standard PDF filters.

All records must match independent ESPN event IDs, dates, final status and
two named participants. Official PDF links are added ONLY when verified.
"""
import json
from datetime import date
from pathlib import Path

import requests
import archive
import wnba_archive
import wnba_monitor
import monitor as nba_monitor
import re

URL="https://site.api.espn.com/apis/site/v2/sports/basketball/"
SPECIAL={
    "NBA":{
        "2026-02-15":{
            "401838140","401838141","401838142","401838143",
        }
    },
    "WNBA":{
        "2026-04-25":{"401867790"},
        "2026-04-27":{"401867791"},
        "2026-05-02":{"401867794"},
        "2026-07-25":{"401857320"},
    },
}


def type_of(league, day):
    if league=="NBA":
        return "nba_all_star"
    if day=="2026-07-25":
        return "wnba_all_star"
    return "wnba_exhibition"


def nba_special_code(name):
    text=str(name or "").lower()
    for word,code in (("world","WLD"),("stripes","STP"),("stars","STR")):
        if re.search(r"\b"+word+r"\b",text):
            return code
    return None


def try_official_pdf(league, day, away, home, session):
    if league=="WNBA":
        away_code=wnba_monitor.wnba_code(away.get("abbreviation",""))
        home_code=wnba_monitor.wnba_code(home.get("abbreviation",""))
        checker=wnba_monitor.valid_pdf
    else:
        away_code=nba_special_code(away.get("displayName") or away.get("name"))
        home_code=nba_special_code(home.get("displayName") or home.get("name"))
        checker=nba_monitor.valid_pdf
    if not (away_code and home_code and len(away_code)==len(home_code)==3
            and away_code.isalpha() and home_code.isalpha()):
        return None
    d=day.replace("-","")
    url=f"https://statsdmz.nba.com/pdfs/{d}/{d}_{away_code}{home_code}_book.pdf"
    return url if checker(session,url) else None


def fetch_special(league, session=None):
    session=session or requests.Session()
    events=[]
    for day, ids in SPECIAL[league].items():
        response=session.get(URL+league.lower()+"/scoreboard",
                             params={"dates":day.replace("-",""),"limit":100},
                             timeout=25)
        response.raise_for_status()
        payload=response.json()
        listing=payload.get("events")
        if not isinstance(listing,list):
            raise ValueError(f"{league} ESPN schedule malformed: {day}")
        seen=set()
        for e in listing:
            gid=str(e.get("id",""))
            if gid not in ids:continue
            status=(e.get("status") or {}).get("type") or {}
            if not status.get("completed"):
                raise ValueError(f"Special event {gid} is not final; refusing archival assertion")
            comp=(e.get("competitions") or [{}])[0]
            sides={}
            for c in comp.get("competitors",[]):
                side=c.get("homeAway")
                team=c.get("team") or {}
                name=team.get("displayName") or team.get("name")
                if side in ("home","away") and name:
                    sides[side]=str(name)
            if not sides.get("home") or not sides.get("away"):
                raise ValueError(f"Special event {gid} missing named participants")
            if e.get("date"):
                from datetime import datetime
                from zoneinfo import ZoneInfo
                kickoff=datetime.fromisoformat(e["date"].replace("Z","+00:00"))
                if kickoff.astimezone(ZoneInfo("America/New_York")).date().isoformat()!=day:
                    raise ValueError(f"Special event {gid} ESPN date mismatch {day}")
            kind=type_of(league,day)
            away_team=next(c.get("team") or {} for c in comp.get("competitors",[])
                           if c.get("homeAway")=="away")
            home_team=next(c.get("team") or {} for c in comp.get("competitors",[])
                           if c.get("homeAway")=="home")
            verified=try_official_pdf(league,day,away_team,home_team,session)
            event_record={
                "id":gid,"date":day,
                "label":f"{sides['away']} @ {sides['home']}",
                "status":status.get("description") or "Final",
                "competition":kind,
                "report_requirement":"verified" if verified else "special_event_needs_review",
            }
            if verified:event_record["pdf_url"]=verified
            events.append(event_record)
            seen.add(gid)
        if seen!=ids:
            raise ValueError(f"{league} missing official IDs on {day}: {sorted(ids-seen)}")
    # The NBA tournament can include repeated games between identical teams.
    # A single date+pair PDF is not enough to identify which game it belongs to.
    if league=="NBA":
        import collections
        counts=collections.Counter(tuple(sorted(str(g["label"]).lower().split(" @ ")))
                                   for g in events)
        for g in events:
            pair=tuple(sorted(str(g["label"]).lower().split(" @ ")))
            if counts[pair]>1:
                g.pop("pdf_url",None)
                g["report_requirement"]="special_event_needs_review"
    return events


def reconcile(league, session=None, path=None, summary_path=None):
    league=league.upper()
    if league not in SPECIAL:raise ValueError("NBA or WNBA required")
    manager=archive if league=="NBA" else wnba_archive
    target=Path(path or manager.ARCHIVE)
    summary=Path(summary_path or
                 ("nba-special-review.json" if league=="NBA"
                  else "wnba/special-review.json"))
    previous=manager.load_payload(target,f"{league} historical archive")
    saved=manager.validate_games(previous,"Original basketball archive")
    events=fetch_special(league,session)
    merged=manager.build_archive({"games":events},previous)
    if merged["games"]!=previous["games"]:
        manager.atomic_write_json(target,merged)
    added=sum(e["id"] not in saved for e in events)
    report={
        "league":league,"year":2026,
        "matched_special_events":len(events),
        "new_records_added":added,
        "events":[
            {"id":e["id"],"date":e["date"],"label":e["label"],
             "competition":e["competition"],
             "report_requirement":e["report_requirement"],
             "pdf_url":e.get("pdf_url")}
            for e in sorted(events,key=lambda g:(g["date"],g["id"]),reverse=True)
        ],
    }
    old=None
    if summary.exists():
        old=json.loads(summary.read_text(encoding="utf-8"))
    if report!=old:
        summary.parent.mkdir(parents=True,exist_ok=True)
        summary.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n")
    print(f"{league}: independently matched {len(events)} special 2026 events; "
          f"added {added} records; standard PDFs not assumed available")
    return report


if __name__=="__main__":
    import argparse
    args=argparse.ArgumentParser()
    args.add_argument("league",choices=("NBA","WNBA"))
    cli=args.parse_args()
    reconcile(cli.league)
