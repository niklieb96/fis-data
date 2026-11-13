"""
Daily FIS update, meant to run on GitHub Actions twice a day.

    python daily.py              # current season
    python daily.py --days 4     # start lists for races in the next 4 days (default 3)

Writes
    data/fis_<season>_runs.csv        all results of the current season so far (model format)
    data/startlists/<event>.csv       start list (bib, skier) for each upcoming race
    data/status.json                  when it ran, what it found, what's coming up

Past seasons are kept outside this repository.
"""
import argparse, json, os, sys
from datetime import date, datetime, timedelta, timezone
import pandas as pd
import fis_scraper as fs

TECH = ("GS", "SL")


def current_season(today):
    return today.year + 1 if today.month >= 7 else today.year


def parse_startlist(html):
    """Start list from a race page that has no results yet: bib, FIS code + name, ski brand.
    Returns an empty table if the page has no readable start list (yet)."""
    try:
        cols, main, sections = fs.parse_race(html)
    except Exception as e:
        print(f"  start list not readable: {type(e).__name__}: {e}", file=sys.stderr)
        main, sections = [], {}
    rows = [dict(bib=bib, skier=f"{code} {name}", brand=brand) for code, name, vals, bib, brand in main]
    for entries in sections.values():
        rows += [dict(bib=bib, skier=f"{code} {name}", brand=brand) for code, name, vals, bib, brand in entries]
    df = pd.DataFrame(rows, columns=["bib", "skier", "brand"]).drop_duplicates("skier")
    df["bib_num"] = pd.to_numeric(df.bib, errors="coerce")
    return df.sort_values("bib_num").drop(columns="bib_num")


def main(days_ahead=3, out="data", delay=1.5):
    today = date.today()
    season = current_season(today)
    fetcher = fs.Fetcher(delay=delay)
    os.makedirs(f"{out}/startlists", exist_ok=True)
    events = {}
    for month in (10, 11, 12, 1, 2, 3, 4):
        # all events, not only those with results, so upcoming races are found too
        url = fs.events_url(season, month).replace("eventselection=results", "eventselection=")
        for e in fs.parse_events(fetcher.get(url, use_cache=False)):
            if not e["cancelled"] and e["event_id"]:
                events[e["event_id"]] = e
    results, upcoming, seen = [], [], set()
    for ev in events.values():
        comps = fs.parse_competitions(fetcher.get(
            f"{fs.BASE}/event-details.html?sectorcode=AL&eventid={ev['event_id']}", use_cache=False))
        venue = ev["place"].translate(fs.UMLAUTS).strip()
        for c in comps:
            if c["cancelled"] or not c["race_id"] or c["race_id"] in seen or not c["date"]:
                continue
            seen.add(c["race_id"])
            if c["competition"] == fs.TRAINING_NAME and c["category"] == "TRA":
                disc, run_type = "DH", "training"
            elif c["competition"] in fs.DISCIPLINE_MAP and c["category"] == "WC":
                disc, run_type = fs.DISCIPLINE_MAP[c["competition"]], "race"
            else:
                continue
            d = date.fromisoformat(c["date"][:10])
            url = f"{fs.BASE}/results.html?sectorcode=AL&raceid={c['race_id']}"
            if d <= today:                                   # may have results already
                html = fetcher.get(url)
                try:
                    runs = fs.race_to_runs(html, two_runs=disc in TECH and run_type == "race")
                except Exception as e:                       # page without results table
                    print(f"  {venue} {disc} {c['date'][:10]}: no results ({type(e).__name__})", file=sys.stderr)
                    runs = []
                if runs:
                    for r in runs:
                        results.append(dict(season=season, venue=venue, gender=c["gender"], discipline=disc,
                                            run_type=run_type, date=c["date"][:10], race_id=c["race_id"], **r))
                    continue
                fetcher.forget(url)                          # not run yet: fetch fresh next time
            if today <= d <= today + timedelta(days=days_ahead) and run_type == "race":
                html = fetcher.get(url, use_cache=False)
                sl = parse_startlist(html)
                key = f"{season}_{venue.replace(' ', '')}_{c['gender']}_{disc}_{c['date'][:10]}"
                if not len(sl):                              # keep the page so the parser can be fixed
                    os.makedirs(f"{out}/debug", exist_ok=True)
                    open(f"{out}/debug/{key}.html", "w", encoding="utf-8").write(html)
                upcoming.append(dict(key=key, date=c["date"][:10], venue=venue, gender=c["gender"],
                                     discipline=disc, starters=len(sl)))
                if len(sl):
                    sl.to_csv(f"{out}/startlists/{key}.csv", index=False)
    if results:
        fs.to_model_format(pd.DataFrame(results)).to_csv(f"{out}/fis_{season}_runs.csv", index=False)
    status = dict(ran_at=datetime.now(timezone.utc).isoformat(timespec="minutes"), season=season,
                  races_with_results=len({r["race_id"] for r in results if r["run_type"] == "race"}),
                  result_rows=len(results), upcoming=upcoming)
    json.dump(status, open(f"{out}/status.json", "w"), indent=1)
    print(json.dumps(status, indent=1))
    return status


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--out", default="data")
    a = ap.parse_args()
    main(a.days, a.out)
