"""
Download FIS Alpine World Cup results (DH, SG, GS, SL + downhill training) and
write them in the format alpine_model.py expects.

Usage (on your own computer):
    pip install requests beautifulsoup4 pandas
    python fis_scraper.py --seasons 2022 2023 2024 2025 2026 2027

    season = the year the season ENDS (2025/26 -> 2026), as FIS codes it.

Output:
    fis_raw_runs.csv      one row per skier per run, with raw times in seconds
    fis_world_cup.csv     same rows with gap_pct added -> feed this to the model

Pages are cached in ./fis_cache, so if the script stops you can simply rerun
it and it continues where it left off. Requests are spaced out (--delay) to
be gentle on the FIS servers; please check FIS's terms of use before running.

The page parsing follows the structure of the FIS results database as of
2025 (the same structure the open-source R package 'fisdata' relies on).
If FIS changes its site, the parse_* functions are the place to fix.
"""

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://www.fis-ski.com/DB/general"
CACHE = Path("fis_cache")
DISCIPLINE_MAP = {"Downhill": "DH", "Super G": "SG", "Super-G": "SG",
                  "Giant Slalom": "GS", "Slalom": "SL"}
TRAINING_NAME = "Downhill Training"
TIME_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}\.\d{1,2})$")
UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
                         "é": "e", "è": "e", "ê": "e", "à": "a", "á": "a", "ß": "ss",
                         "ø": "o", "å": "a", "č": "c", "š": "s", "ž": "z", "ñ": "n"})


# --------------------------------------------------------------------------
# HTTP with on-disk cache
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, delay=2.0, cache=CACHE):
        self.delay, self.cache = delay, cache
        self.cache.mkdir(exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (personal research script)"
        self._last = 0.0

    def _path(self, url):
        return self.cache / (hashlib.md5(url.encode()).hexdigest() + ".html")

    def forget(self, url):
        self._path(url).unlink(missing_ok=True)

    def get(self, url, use_cache=True):
        path = self._path(url)
        if use_cache and path.exists():
            return path.read_text(encoding="utf-8")
        for attempt in range(4):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.session.get(url, timeout=30)
                if r.status_code == 200:
                    path.write_text(r.text, encoding="utf-8")
                    return r.text
                print(f"  HTTP {r.status_code} for {url}", file=sys.stderr)
            except requests.RequestException as e:
                print(f"  {e} for {url}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"Could not download {url}")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def events_url(season, month):
    year = season if month <= 6 else season - 1
    return (f"{BASE}/calendar-results.html?eventselection=results&place=&sectorcode=AL"
            f"&seasoncode={season}&categorycode=WC&disciplinecode=&gendercode=&racedate="
            f"&racecodex=&nationcode=&seasonmonth={month:02d}-{year}")


def parse_events(html):
    """-> list of dicts: event_id, place, cancelled"""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.table__body")
    out = []
    if body is None:
        return out
    for row in body.select("div.table-row"):
        if "No events found" in row.get_text():
            continue
        cont = row.select_one("div.container")
        texts = [a.get_text(" ", strip=True) for a in cont.select("a")] if cont else []
        if len(texts) > 1 and texts[1].endswith(" live"):   # live events carry an extra date cell
            texts.pop(1)
        place = texts[3] if len(texts) > 3 else ""
        place = re.sub(r"\s+live$", "", place)
        out.append(dict(event_id=row.get("id"), place=place, cancelled=_cancelled(row)))
    return out


def parse_competitions(html):
    """-> list of dicts: date, competition, category, gender, race_id, cancelled"""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.table__body")
    out = []
    if body is None:
        return out
    for row in body.select("div.table-row"):
        c = row.select_one("div.container")
        if c is None:
            continue
        d = c.select_one("div.timezone-date")
        clip = c.select_one("div.clip")
        cat = c.select_one("div.g-row a.g-sm-8 div.g-row div.g-xs-12")
        g = c.select_one("div.gender__item")
        a = row.select_one("a[href*='raceid=']")
        m = re.search(r"raceid=(\d+)", a["href"]) if a else None
        out.append(dict(date=d.get("data-date") if d else None,
                        competition=clip.get_text(strip=True) if clip else "",
                        category=cat.get_text(strip=True) if cat else "",
                        gender=g.get_text(strip=True) if g else "",
                        race_id=m.group(1) if m else None,
                        cancelled=_cancelled(row)))
    return out


def _cancelled(row):
    return any(s.get("title") == "Cancelled"
               for s in row.select("div.status span.status__item"))


def parse_time(tok):
    m = TIME_RE.match(tok)
    if not m:
        return None
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def _split_row(tokens):
    """Find the year + nation pair; return (fis_code, name, values_after_nation)."""
    for i in range(len(tokens) - 1):
        if re.fullmatch(r"(19|20)\d\d", tokens[i]) and re.fullmatch(r"[A-Z]{3}", tokens[i + 1]):
            # FIS code is the first all-digit token of 4+ digits before the name
            code_idx = next(j for j in range(i) if re.fullmatch(r"\d{4,}", tokens[j]))
            bib = tokens[code_idx - 1] if code_idx > 0 and tokens[code_idx - 1].isdigit() else ""
            brand = " ".join(tokens[code_idx + 2:i]).strip("- ") or ""
            return tokens[code_idx], tokens[code_idx + 1], tokens[i + 2:], bib, brand
    return None


def parse_race(html):
    """-> (columns_after_nation, main_rows, sections)
    main_rows: list of (fis_code, name, {column: value})
    sections : {section title: list of (fis_code, name, [values after nation])}"""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("div.table__body#events-info-results")
    if body is None:
        return [], [], {}
    table = body.find_parent("div", class_="table")
    head = table.select_one("div.thead")
    header = [d.get_text(" ", strip=True) for d in head.select("div.g-row > div")]
    cols = header[header.index("Nation") + 1:] if "Nation" in header else []

    main, sections, current = [], {}, None
    for el in table.find_all(["div", "a"]):
        classes = el.get("class") or []
        if el.name == "div" and "thead_slave" in classes:
            current = el.get_text(" ", strip=True)
            sections.setdefault(current, [])
        elif el.name == "a" and "table-row" in classes:
            cont = el.select_one("div.container") or el
            parsed = _split_row(cont.get_text("|", strip=True).split("|"))
            if parsed is None:
                continue
            code, name, vals, bib, brand = parsed
            if current is None:
                main.append((code, name, dict(zip(cols, vals)), bib, brand))
            else:
                sections[current].append((code, name, vals, bib, brand))
    return cols, main, sections


def race_to_runs(html, two_runs):
    """Turn one results page into per-run rows: skier, run_no, time_sec, dnf."""
    cols, main, sections = parse_race(html)
    rows = []
    has_run2 = two_runs and "Run 2" in cols and any(parse_time(r[2].get("Run 2", "")) for r in main)
    for code, name, vals, bib, brand in main:
        skier = f"{code} {name}"
        extra = dict(bib=bib, brand=brand)
        if has_run2:
            rows.append(dict(skier=skier, run_no=1, time_sec=parse_time(vals.get("Run 1", "")), dnf=0, **extra))
            rows.append(dict(skier=skier, run_no=2, time_sec=parse_time(vals.get("Run 2", "")), dnf=0, **extra))
        else:
            t = vals.get("Run 1") or vals.get("Time") or vals.get("Tot. Time") or ""
            rows.append(dict(skier=skier, run_no=1, time_sec=parse_time(t), dnf=0, **extra))
    for title, entries in sections.items():
        t = title.lower()
        out = ("finish" in t) or ("disqualified" in t) or ("dsq" in t)
        for code, name, vals, bib, brand in entries:
            skier = f"{code} {name}"
            extra = dict(bib=bib, brand=brand)
            times = [parse_time(v) for v in vals if parse_time(v)]
            if "2nd run" in t:
                if times:
                    rows.append(dict(skier=skier, run_no=1, time_sec=times[0], dnf=0, **extra))
                if out:
                    rows.append(dict(skier=skier, run_no=2, time_sec=None, dnf=1, **extra))
            elif "qualif" in t:                       # did not qualify for run 2
                if times:
                    rows.append(dict(skier=skier, run_no=1, time_sec=times[0], dnf=0, **extra))
            elif out:                                 # DNF / DSQ (1st run or one-run race)
                rows.append(dict(skier=skier, run_no=1, time_sec=None, dnf=1, **extra))
            # "did not start" and anything else: no row
    return [r for r in rows if r["dnf"] == 1 or r["time_sec"] is not None]


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------
def crawl(seasons, fetcher):
    rows, seen = [], set()
    current = max(seasons)
    for season in seasons:
        events = {}
        for month in (10, 11, 12, 1, 2, 3, 4):
            for e in parse_events(fetcher.get(events_url(season, month), use_cache=season < current)):
                if not e["cancelled"] and e["event_id"]:
                    events[e["event_id"]] = e
        print(f"Season {season}: {len(events)} World Cup events")
        for ev in events.values():
            url = f"{BASE}/event-details.html?sectorcode=AL&eventid={ev['event_id']}"
            comps = parse_competitions(fetcher.get(url, use_cache=season < current))
            venue = ev["place"].translate(UMLAUTS).strip()
            for c in comps:
                if c["cancelled"] or not c["race_id"] or c["race_id"] in seen:
                    continue
                seen.add(c["race_id"])
                if c["competition"] == TRAINING_NAME and c["category"] == "TRA":
                    disc, run_type = "DH", "training"
                elif c["competition"] in DISCIPLINE_MAP and c["category"] == "WC":
                    disc, run_type = DISCIPLINE_MAP[c["competition"]], "race"
                else:
                    continue
                rurl = f"{BASE}/results.html?sectorcode=AL&raceid={c['race_id']}"
                html = fetcher.get(rurl)
                runs = race_to_runs(html, two_runs=disc in ("GS", "SL") and run_type == "race")
                if not runs:
                    fetcher.forget(rurl)                  # not raced yet: fetch again next week
                    print(f"  ! no results parsed: {venue} {c['competition']} {c['date']} "
                          f"race {c['race_id']}", file=sys.stderr)
                for r in runs:
                    rows.append(dict(season=season, venue=venue, gender=c["gender"],
                                     discipline=disc, run_type=run_type, date=c["date"],
                                     race_id=c["race_id"], **r))
            print(f"  {venue}: {sum(1 for c in comps if not c['cancelled'])} competitions")
    return pd.DataFrame(rows)


def to_model_format(raw):
    """Add event_id / run_id and gap_pct (% behind the fastest time in each run)."""
    df = raw.copy()
    races = (df[df["run_type"] == "race"][["season", "venue", "gender", "discipline",
                                           "date", "race_id"]]
             .drop_duplicates("race_id").sort_values(["date", "race_id"]))
    races["n"] = races.groupby(["season", "venue", "gender", "discipline"]).cumcount() + 1
    races["event_id"] = (races["season"].astype(str) + "_" + races["venue"].str.replace(" ", "")
                         + "_" + races["gender"] + "_" + races["discipline"]
                         + races["n"].map(lambda n: "" if n == 1 else str(n)))
    trains = (df[df["run_type"] == "training"][["season", "venue", "gender", "date", "race_id"]]
              .drop_duplicates("race_id").sort_values(["date", "race_id"]))
    trains["n"] = trains.groupby(["season", "venue", "gender"]).cumcount() + 1
    trains["event_id"] = (trains["season"].astype(str) + "_" + trains["venue"].str.replace(" ", "")
                          + "_" + trains["gender"] + "_DH_training")
    ids = pd.concat([races[["race_id", "event_id", "n"]], trains[["race_id", "event_id", "n"]]])
    trains_n = dict(zip(trains["race_id"], trains["n"]))
    df = df.merge(ids[["race_id", "event_id"]], on="race_id", how="left")
    df["run_id"] = [f"{e}_t{trains_n[r]}" if t == "training" else f"{e}_r{n}"
                    for e, r, t, n in zip(df["event_id"], df["race_id"], df["run_type"], df["run_no"])]
    best = df.groupby("run_id")["time_sec"].transform("min")
    df["gap_pct"] = 100 * (df["time_sec"] - best) / best
    cols = ["event_id", "run_id", "season", "venue", "gender", "discipline", "run_type",
            "run_no", "skier", "bib", "brand", "time_sec", "gap_pct", "dnf", "date", "race_id"]
    return df[cols].sort_values(["date", "run_id", "gap_pct"]).reset_index(drop=True)


def sanity_report(df):
    print("\nRaces per season / gender / discipline:")
    ev = df[df["run_type"] == "race"].drop_duplicates("event_id")
    print(ev.pivot_table(index=["season", "gender"], columns="discipline",
                         values="event_id", aggfunc="count", fill_value=0))
    print(f"\nTraining runs: {df[df.run_type == 'training']['run_id'].nunique()}")
    print(f"Skiers: {df['skier'].nunique()}, rows: {len(df)}")
    print("Venues:", ", ".join(sorted(df["venue"].unique())))
    big = df[df["gap_pct"] > 15]
    if len(big):
        print(f"\nWarning: {len(big)} rows more than 15% behind - check these races:")
        print(big[["run_id", "skier", "time_sec", "gap_pct"]].head(10).to_string())


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests")
    args = ap.parse_args()
    raw = crawl(args.seasons, Fetcher(delay=args.delay))
    raw.to_csv("fis_raw_runs.csv", index=False)
    df = to_model_format(raw)
    df.to_csv("fis_world_cup.csv", index=False)
    sanity_report(df)
    print("\nWrote fis_raw_runs.csv and fis_world_cup.csv")
