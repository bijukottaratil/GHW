#!/usr/bin/env python3
"""Hourly updater for Global Hazard Watch.

Rebuilds data.json (live events) and merges notable events into history.json
(the permanent archive). Runs in GitHub Actions; see README.md.

Feeds:      USGS, GDACS, NASA EONET, Smithsonian GVP  (public data feeds)
Storm paths: Zoom Earth best tracks + forecast positions, per named storm
Modelers:   Moody's RMS Event Response, Verisk ALERT   (public web pages, scraped)
Optional:   Claude API (set ANTHROPIC_API_KEY) to read the modeler pages more
            reliably and to research economic / insured loss estimates.

If any source fails, its previous entries are carried over, so a bad hour never
blanks the site.
"""
import datetime as dt
import email.utils
import html
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA, HIST = ROOT / "data.json", ROOT / "history.json"
UA = {"User-Agent": "GlobalHazardWatch/1.0 (+https://github.com)"}
NOW = dt.datetime.now(dt.timezone.utc)
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
MODEL = os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5"
MAX_RESEARCH = int(os.environ.get("MAX_LOSS_RESEARCH") or 4)      # events researched per run
RESEARCH_EVERY_H = int(os.environ.get("RESEARCH_EVERY_HOURS") or 12)  # per-event cooldown

RMS_URL = "https://moodys.com/web/en/us/insights/catastrophe-risk-insights/event-response.html"
VAL_URL = "https://alert.risksolutions.verisk.com/"
AON_URL = "https://www.aon.com/en/capabilities/catastrophe-risk-management/impact-forecasting"
ZE_URL = "https://zoom.earth/data/storms/"
SRC = {
    "usgs": ("USGS Earthquake Hazards", "Earthquakes: M5+ past 7 days and significant quakes past 2 weeks", "https://earthquake.usgs.gov/earthquakes/map/"),
    "gdacs": ("GDACS (UN / EU JRC)", "Alert levels, tropical cyclones and satellite-detected wildfires", "https://www.gdacs.org/"),
    "eonet": ("NASA EONET", "Tropical cyclones and large US wildfires", "https://eonet.gsfc.nasa.gov/"),
    "gvp": ("Smithsonian GVP / USGS", "Weekly volcanic activity report", "https://volcano.si.edu/reports_weekly.cfm"),
    "rms": ("Moody's RMS Event Response", "Modeler coverage of active loss events (full reports need a Moody's login)", RMS_URL),
    "verisk": ("Verisk ALERT (formerly AIR)", "Modeler event alerts with status and loss estimates", VAL_URL),
    "aon": ("Aon (Impact Forecasting)", "Modeler event notes gathered via web research (no public live event page)", AON_URL),
    "zoomearth": ("Zoom Earth", "Tropical cyclone best tracks and forecast positions", "https://zoom.earth/"),
}
RANK = {"red": 4, "orange": 3, "yellow": 2, "green": 1}

log = lambda *a: print(*a, file=sys.stderr, flush=True)
iso = lambda d: d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
clamp = lambda v: round(max(0.8, min(5.0, v)), 2)


def get(url, **kw):
    r = requests.get(url, headers=UA, timeout=40, **kw)
    r.raise_for_status()
    return r


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError:
        pass
    try:
        return email.utils.parsedate_to_datetime(s).astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def text_of(html_str):
    s = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html_str)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def haversine(a, b, c, d):
    p = math.pi / 180
    x = 0.5 - math.cos((c - a) * p) / 2 + math.cos(a * p) * math.cos(c * p) * (1 - math.cos((d - b) * p)) / 2
    return 12742 * math.asin(math.sqrt(x))


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- hazard feeds
def fetch_usgs():
    feats = {}
    for url, keep in [
        ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson", lambda m, t: m >= 5.0),
        ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
         lambda m, t: NOW - t < dt.timedelta(days=14)),
    ]:
        for f in get(url).json()["features"]:
            p, (lo, la, dep) = f["properties"], f["geometry"]["coordinates"][:3]
            m = p.get("mag") or 0
            t = dt.datetime.fromtimestamp(p["time"] / 1000, dt.timezone.utc)
            if keep(m, t):
                feats[f["id"]] = (f["id"], p, m, t, la, lo, dep)
    out = []
    for fid, p, m, t, la, lo, dep in feats.values():
        bits = [f"Depth {round(dep)} km"]
        if p.get("tsunami"):
            bits.append("Tsunami flag raised by USGS")
        if p.get("alert"):
            bits.append(f"USGS PAGER {p['alert']} alert")
        if p.get("felt"):
            bits.append(f"Felt reports: {p['felt']:,}")
        out.append(dict(t="quake", n=f"M{m:.1f} earthquake", p=p.get("place") or "Unknown location", d=iso(t),
                        la=round(la, 4), lo=round(lo, 4), a=p.get("alert"), m=f"M{m:.1f}", x=" · ".join(bits),
                        s="USGS", u=p.get("url"), v=clamp((m - 4.5) * 1.4), uid=fid))
    return out


def _local(tag):
    return tag.split("}", 1)[-1]


def fetch_gdacs():
    root = ET.fromstring(get("https://www.gdacs.org/xml/rss.xml").content)
    storms, fires, quakes = [], [], []
    for item in root.iter("item"):
        f = {}
        for el in item.iter():
            k = _local(el.tag)
            if k not in f:
                f[k] = (el.text or "").strip()
                if k == "severity" and el.get("value"):
                    f["severity_value"] = el.get("value")
        typ, alert = f.get("eventtype"), (f.get("alertlevel") or "").lower() or None
        try:
            la, lo = float(f.get("lat")), float(f.get("long"))
        except (TypeError, ValueError):
            pt = (f.get("point") or "").split()
            if len(pt) != 2:
                continue
            la, lo = float(pt[0]), float(pt[1])
        d = parse_date(f.get("fromdate")) or parse_date(f.get("pubDate")) or NOW
        d2 = parse_date(f.get("todate"))
        if d2 and NOW - d2 > dt.timedelta(days=10):
            continue
        country, link = f.get("country") or "", f.get("link")
        sev, pop = f.get("severity", ""), f.get("population", "")
        if typ == "TC":
            name = re.search(r"tropical cyclone ([A-Z0-9\-]+)", f.get("title", ""), re.I)
            nm = name.group(1).rsplit("-", 1)[0].title() if name else "Tropical cyclone"
            kmh = re.search(r"([\d.]+)\s*km/h", sev)
            kmh = float(kmh.group(1)) if kmh else 100
            storms.append(dict(t="storm", n=f"Tropical Cyclone {nm}", p=country or "Ocean", d=iso(d2 or d), la=la, lo=lo,
                               a=alert, m=f"{round(kmh)} km/h max", x=pop[:220], s="GDACS", u=link, v=clamp(kmh / 50)))
        elif typ == "WF":
            ha = re.search(r"([\d,]+)\s*ha", sev)
            ha = int(ha.group(1).replace(",", "")) if ha else 5000
            fires.append(dict(t="fire", n=f"Wildfire, {ha:,} ha", p=country, d=iso(d), la=la, lo=lo, a=alert,
                              m=f"{ha:,} ha", x=f"Burnt area {ha:,} ha · {pop}".strip(" ·"), s="GDACS", u=link,
                              v=clamp(1 + math.log10(max(ha, 500) / 4000) * 1.6)))
        elif typ == "EQ":
            quakes.append(dict(la=la, lo=lo, d=d, a=alert, u=link, pop=pop))
    return storms, fires, quakes


def fetch_eonet():
    js = get("https://eonet.gsfc.nasa.gov/api/v3/events", params={"status": "open", "days": 30}).json()
    out = []
    for e in js.get("events", []):
        cat = (e.get("categories") or [{}])[0].get("id")
        geo = (e.get("geometry") or [None])[-1]
        if not geo or geo.get("type") != "Point":
            continue
        lo, la = geo["coordinates"][:2]
        mag, unit = geo.get("magnitudeValue"), geo.get("magnitudeUnit") or ""
        src = (e.get("sources") or [{}])[0].get("url") or e.get("link")
        d = parse_date(geo.get("date")) or NOW
        if cat == "severeStorms":
            kts = float(mag) if mag and "kts" in unit else 40
            out.append(dict(t="storm", n=e["title"], p="", d=iso(d), la=la, lo=lo, a=None,
                            m=f"{round(kts)} kt" if mag else "Storm", x=f"Latest position {d:%d %b %H:%M} UTC",
                            s="NASA EONET / JTWC", u=src, v=clamp(kts * 0.058 - 0.3)))
        elif cat == "wildfires" and not e["title"].lower().startswith("prescribed"):
            ac = float(mag) if mag and "acre" in unit.lower() else 500
            title = re.sub(r"^Wildfire\s+", "", e["title"])
            nm, _, place = title.partition(",")
            out.append(dict(t="fire", n=f"{nm.strip().title()} Fire", p=(place.strip() or "USA") + ", USA", d=iso(d),
                            la=la, lo=lo, a=None, m=f"{round(ac):,} acres",
                            x=f"{round(ac):,} acres (~{round(ac * 0.4047):,} ha) · open incident",
                            s="NASA EONET / IRWIN", u=src, v=clamp(0.9 + math.log10(max(ac, 100) / 400) * 1.2)))
    return out


def fetch_zoom_tracks():
    """Best tracks + forecast positions for every storm Zoom Earth is currently carrying.

    Returns {storm-name-lower: {"tr": [...], "ze": {...}}} so callers can attach a real
    multi-point track (map path) and an intensity history (dossier chart) to any storm
    event whose name matches. Zoom Earth names a storm just by its name ("Josephine"),
    occasionally with an alternate regional name in parentheses ("Champi (Rosal)") or,
    for an unnamed invest, its numeric code ("98B") -- we key on the first word only so
    that matches against our own "Tropical Cyclone <name>" titles.
    """
    # Zoom Earth's index only accepts a date snapped to a 6-hourly synoptic time
    # (00/06/12/18 UTC, no seconds) -- anything else comes back as a JSON error body.
    snap = NOW.replace(hour=NOW.hour // 6 * 6, minute=0, second=0, microsecond=0)
    idx = get(ZE_URL, params={"date": snap.strftime("%Y-%m-%dT%H:%MZ"), "to": 12}).json()
    ids = (idx.get("storms") or []) + (idx.get("disturbances") or [])
    out = {}
    for sid in ids:
        try:
            d = get(ZE_URL, params={"id": sid}).json()
        except Exception as ex:  # noqa: BLE001
            log(f"zoomearth {sid} failed: {ex}")
            continue
        track = sorted((p for p in d.get("track", []) if p.get("coordinates") and p.get("wind") is not None),
                       key=lambda p: p["date"])
        if len(track) < 2:
            continue
        obs = [p["date"] for p in track if not p.get("forecast")]
        key = re.split(r"[\s(]", (d.get("name") or sid).strip())[0].lower()
        out[key] = dict(
            tr=[[p["date"], p["coordinates"][1], p["coordinates"][0], p["wind"]] for p in track],
            ze=dict(pts=[[p["date"], round(p["wind"] * 1.852)] for p in track],
                    lm=max(obs) if obs else track[-1]["date"],
                    u=f"https://zoom.earth/storms/{sid}/"),
        )
    return out


def fetch_volcanoes():
    root = ET.fromstring(get("https://volcano.si.edu/news/WeeklyVolcanoRSS.xml").content)
    out = []
    for item in root.iter("item"):
        f = {_local(el.tag): (el.text or "").strip() for el in item.iter()}
        pt = (f.get("point") or "").split()
        if len(pt) != 2:
            continue
        desc = text_of(f.get("description", ""))
        first = re.split(r"(?<=[.!?])\s+", desc)[0][:240]
        level = re.search(r"(Alert Level|Aviation Color Code|Volcano Alert Level)\s+(?:was\s+|remained\s+|is\s+)?"
                          r"(?:(?:lowered|raised)\s+)?(?:at\s+|to\s+)?(\d|[A-Z][a-z]+)", desc)
        val = level.group(2) if level else ""
        hot = val in ("3", "4", "Orange", "Red", "Watch", "Warning") or bool(re.search(r"Level (3|4)\b|\bOrange\b", desc))
        name, _, country = f.get("title", "").partition("(")
        out.append(dict(t="volcano", n=name.strip(), p=country.strip(" )") or "", d=iso(parse_date(f.get("pubDate")) or NOW),
                        la=float(pt[0]), lo=float(pt[1]), a="orange" if hot else "yellow",
                        m=(f"{level.group(1)} {val}" if level else "Active"), x=first, s="Smithsonian GVP / USGS",
                        u="https://volcano.si.edu/reports_weekly.cfm", v=2.6 if hot else 1.6))
    return out


# ----------------------------------------------------------- Claude API helper
def claude(prompt, search=False, max_tokens=4000):
    """Call the Claude Messages API; returns the final text. Handles pause_turn."""
    body = {"model": MODEL, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    hdr = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for _ in range(4):
        r = requests.post("https://api.anthropic.com/v1/messages", headers=hdr, json=body, timeout=300)
        r.raise_for_status()
        js = r.json()
        if js.get("stop_reason") == "pause_turn":
            body["messages"] = [body["messages"][0], {"role": "assistant", "content": js["content"]}]
            continue
        return "".join(b.get("text", "") for b in js["content"] if b.get("type") == "text")
    raise RuntimeError("Claude API: too many pause_turn continuations")


def json_from(text):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    text = m.group(1) if m else text
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=-1)
    return json.loads(text[start:]) if start >= 0 else None


MODEL_SCHEMA = ('Return ONLY JSON: a list of objects {"name": event name, "peril": one of earthquake|tropical cyclone|'
                'wildfire|volcano|flood|winter storm|severe convective storm|civil unrest|other, "region": affected places, '
                '"date": ISO date of impact or landfall (YYYY-MM-DD), "status": Active|Monitoring|Closed, '
                '"note": 2-4 sentences in your own words covering what happened, affected areas, and current impacts, '
                '"loss": any insured or industry loss estimate quoted on the page or null, '
                '"id": any event ID, reference code, or catastrophe number explicitly shown on the page for this event '
                '(e.g. a code in parentheses next to the name) or null — never invent one, '
                '"url": event link if shown or null, "lat": approx latitude of the main impact, '
                '"lon": approx longitude}.')


# ------------------------------------------------------------- modeler pages
PERIL_WORDS = r"(Super Typhoon|Typhoon|Major Hurricane|Hurricane|Tropical Storm|Subtropical Storm|Tropical Cyclone|Cyclone|Winter Storm|Storm|Earthquake|Wildfire|Flood(?:ing|s)?)"


def fetch_rms():
    page = get(RMS_URL).text
    if API_KEY:
        evs = json_from(claude("This is the text of Moody's RMS public Event Response page. List every catastrophe "
                               "event it currently covers. " + MODEL_SCHEMA + "\n\n" + text_of(page)[:60000])) or []
        for e in evs:
            e["status"] = e.get("status") or "Active"
        return evs
    # No API key: pick event names (and any visible reference code) out of the page text.
    names, codes = set(), {}
    for m in re.finditer(PERIL_WORDS + r"\s+([A-Z][a-z]+)(?:\s*\(([^)]+)\))?", text_of(page)):
        if m.group(1) in ("Earthquake", "Wildfire", "Storm") and not m.group(2):
            continue
        nm = f"{m.group(1)} {m.group(2)}"
        names.add(nm)
        if m.group(3):
            codes[nm] = m.group(3)
    return [dict(name=n, peril="tropical cyclone" if re.search("Typhoon|Hurricane|Tropical|Cyclone", n) else "other",
                 status="Active", note="Listed on Moody's RMS Event Response", url=RMS_URL, loss=None,
                 id=codes.get(n)) for n in names]


def fetch_verisk():
    page = get(VAL_URL).text
    links = []
    pat = re.compile(r'href="((?:https://alert\.risksolutions\.verisk\.com)?/([a-z\-]+)/(\d{4})/([^"/#?]+)/?)"')
    found = list(pat.finditer(page))
    for i, m in enumerate(found):
        url = m.group(1) if m.group(1).startswith("http") else VAL_URL.rstrip("/") + m.group(1)
        if url not in [l[0] for l in links]:
            nxt = next((f.start() for f in found[i + 1:] if f.group(1) != m.group(1)), m.end() + 1500)
            st = re.search(r"\b(Active|Monitoring|Closed)\b", text_of(page[m.end(): min(nxt, m.end() + 1500)]))
            links.append((url, m.group(2), m.group(4), st.group(1) if st else "Active"))
    if API_KEY:
        evs = json_from(claude("This is the text of Verisk's public ALERT catastrophe page. List every event shown. "
                               + MODEL_SCHEMA + "\nKnown event links: " + json.dumps([l[0] for l in links])
                               + "\n\n" + text_of(page)[:60000])) or []
        for e in evs:  # enrich open events from their own pages
            if e.get("status") != "Closed" and e.get("url"):
                try:
                    detail = json_from(claude("Event page from Verisk ALERT. Return ONLY JSON {\"note\": 3-4 "
                                              "sentences in your own words on impacts, affected areas, and current "
                                              "status, \"loss\": Verisk insured loss estimate quoted on the page or "
                                              "null}.\n\n" + text_of(get(e["url"]).text)[:40000]))
                    e.update({k: v for k, v in (detail or {}).items() if v})
                except Exception as ex:  # noqa: BLE001
                    log("verisk detail", e.get("url"), ex)
        for e in evs:  # Verisk never publishes a public event ID
            e["id"] = None
        return evs
    peril_map = {"tropical-cyclone": "tropical cyclone", "earthquake": "earthquake", "other-event": "civil unrest",
                 "flood": "flood", "wildfire": "wildfire", "winter-storm": "winter storm",
                 "severe-thunderstorm": "severe convective storm", "extratropical-cyclone": "winter storm"}
    out = []
    for url, kind, s, status in links:
        name = re.sub(r"-\d{4}$", "", s).replace("-", " ")
        name = re.sub(r"\bm(\d)\.?(\d)\b", r"M\1.\2", name).title().replace("M7.", "M7.")
        loss, note = None, "Listed on Verisk ALERT"
        if status != "Closed":
            try:
                body = text_of(get(url).text)
                lm = re.search(r"insured (?:industry )?loss(?:es)?[^.]{0,120}?(USD|US\$)\s?[\d.,]+\s*(?:billion|million)"
                               r"(?:\s*(?:and|to|–|-)\s*(?:USD|US\$)?\s?[\d.,]+\s*(?:billion|million))?", body, re.I)
                if lm:
                    loss = re.search(r"(USD|US\$)\s?[\d.,]+.*", lm.group(0)).group(0)
                    rng = re.match(r"(?:USD|US\$)\s?([\d.,]+)\s*(billion|million)\s*(?:and|to|–|-)\s*(?:USD|US\$)?\s?([\d.,]+)\s*(billion|million)", loss)
                    if rng and rng.group(2) == rng.group(4):
                        loss = f"USD {rng.group(1)}–{rng.group(3)} {rng.group(2)}"
                sent = re.search(r"([A-Z][^.]{60,260}\.)", body)
                note = sent.group(1) if sent else note
            except Exception as ex:  # noqa: BLE001
                log("verisk page", url, ex)
        out.append(dict(name=name, peril=peril_map.get(kind, "other"), status=status, note=note, url=url, loss=loss,
                        id=None))
    return out


def geocode(q):
    try:
        r = get("https://nominatim.openstreetmap.org/search", params={"q": q, "format": "json", "limit": 1})
        time.sleep(1.1)
        js = r.json()
        return (float(js[0]["lat"]), float(js[0]["lon"])) if js else None
    except Exception:  # noqa: BLE001
        return None


PERIL_T = {"earthquake": "quake", "tropical cyclone": "storm", "wildfire": "fire", "volcano": "volcano",
           "civil unrest": "unrest"}


def attach_modelers(events, org, items, previous):
    """Attach modeler entries to matching events; add unmatched ones as new events."""
    prev_by_name = {e["n"].lower(): e for e in previous}
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        key = name.split()[-1].lower()  # storm name or country token
        mr = dict(o=org, st=it.get("status") or "Active", nt=(it.get("note") or "")[:600], u=it.get("url") or
                  (RMS_URL if org == "Moody's RMS" else VAL_URL), d=(it.get("date") or NOW.date().isoformat())[:10])
        if it.get("id"):
            mr["id"] = str(it["id"])
        if it.get("loss"):
            mr["nt"] = (mr["nt"] + f" · {org} loss estimate: {it['loss']}").strip(" ·")
        match = None
        for e in events:
            if it.get("peril") == "earthquake" and e["t"] == "quake":
                mm = re.search(r"M\s?(\d\.\d)", name)
                if mm and e["m"] == f"M{mm.group(1)}" and (it.get("lat") is None or haversine(
                        it["lat"], it["lon"], e["la"], e["lo"]) < 300 or key in e["p"].lower()):
                    match = e
                    break
            elif key in e["n"].lower() and e["t"] != "quake" and len(key) > 3:
                match = e
                break
        if match:
            match.setdefault("mr", [])
            if not any(x["o"] == org for x in match["mr"]):
                match["mr"].append(mr)
            continue
        old = prev_by_name.get(name.lower()) or next((p for p in previous for x in p.get("mr", [])
                                                      if x["o"] == org and x["u"] == mr["u"] and x["u"] not in (RMS_URL, VAL_URL)), None)
        la, lo = it.get("lat"), it.get("lon")
        if la is None and old:
            la, lo = old["la"], old["lo"]
        if la is None:
            g = geocode(it.get("region") or re.sub(PERIL_WORDS + r"|\bM\d\.\d\b|\bCivil Unrest In\b", "", name).strip())
            la, lo = g if g else (None, None)
        t = PERIL_T.get(it.get("peril"), "storm")
        closed = mr["st"] == "Closed"
        events.append(dict(t=t, n=old["n"] if old else name, p=it.get("region") or (old or {}).get("p") or "",
                           d=(it.get("date") + "T00:00:00Z") if it.get("date") else (old or {}).get("d", iso(NOW)),
                           la=la, lo=lo, a=None, m=(old or {}).get("m") or (it.get("peril") or "").title(),
                           x=mr["nt"], s=org, u=mr["u"], v=2.4 if closed else (3.4 if it.get("loss") else 2.9), mr=[mr]))


# ------------------------------------------------------------------- history
def hist_id(e):
    if e.get("uid"):
        return "q-" + e["uid"]
    return f"{e['d'][:4]}-{slug(e['n'])}"


def notable(e):
    named = e["t"] == "storm" and re.search(r"(Typhoon|Hurricane|Storm|Cyclone) [A-Z][a-z]+", e["n"])
    mag = re.match(r"M(\d\.\d)", e.get("m", ""))
    return (RANK.get(e.get("a"), 0) >= 3 or (mag and float(mag.group(1)) >= 6.0) or named or e.get("mr")
            or e["t"] == "unrest")


def merge_history(hist, events):
    by_id = {h["id"]: h for h in hist["events"]}
    by_name = {h["n"].lower(): h for h in hist["events"]}
    added = 0
    for e in events:
        if e["t"] == "volcano" or not notable(e):
            continue
        h = by_id.get(hist_id(e)) or by_name.get(e["n"].lower())
        if not h and e["t"] == "quake":  # same quake already archived under another id
            h = next((x for x in hist["events"] if x["t"] == "quake" and x.get("la") is not None and e.get("la") is not None
                      and abs((parse_date(x["d"]) - parse_date(e["d"])).total_seconds()) < 86400
                      and haversine(x["la"], x["lo"], e["la"], e["lo"]) < 150), None)
        if not h and e["t"] == "quake":  # aftershock of an archived, larger quake: fold into it
            mag = float(e["m"][1:4]) if re.match(r"M\d\.\d", e["m"]) else 0
            parent = next((x for x in hist["events"] if x["t"] == "quake" and x.get("la") is not None
                           and haversine(x["la"], x["lo"], e["la"], e["lo"]) < 300
                           and 0 <= (parse_date(e["d"]) - parse_date(x["d"])).total_seconds() < 14 * 86400
                           and re.match(r"M(\d\.\d)", x["m"]) and float(re.match(r"M(\d\.\d)", x["m"]).group(1)) > mag), None)
            if parent:
                continue
        if not h:
            h = dict(id=hist_id(e), t=e["t"], n=e["n"], p=e["p"], d=e["d"], d2=None, la=e["la"], lo=e["lo"], m=e["m"],
                     a=e.get("a"), sum=e.get("x", ""), deaths=None, econ=[dict(v="Not published", by="—", u=None)],
                     ins=[dict(v="Not published", by="—", u=None)], mr=[], src=[dict(name=e["s"], u=e["u"])])
            hist["events"].append(h)
            by_id[h["id"]] = h
            added += 1
        if RANK.get(e.get("a"), 0) > RANK.get(h.get("a"), 0):
            h["a"] = e["a"]
        if e["t"] == "storm" and parse_date(e["d"]) and parse_date(e["d"]) > (parse_date(h.get("d2") or h["d"]) or NOW):
            h["d2"] = e["d"]
        for m in e.get("mr", []):
            cur = next((x for x in h["mr"] if x["o"] == m["o"]), None)
            if cur:
                cur.update(st=m["st"], nt=m["nt"] or cur["nt"], u=m["u"])
                if m.get("id"):
                    cur["id"] = m["id"]
            else:
                entry = dict(o=m["o"], st=m["st"], nt=m["nt"], u=m["u"])
                if m.get("id"):
                    entry["id"] = m["id"]
                h["mr"].append(entry)
            loss = re.search(r"loss estimate: (.+)$", m["nt"] or "")
            if loss:
                upsert_loss(h, "ins", loss.group(1), m["o"], m["u"])
        if not any(s["u"] == e["u"] for s in h["src"]) and e.get("u"):
            h["src"].append(dict(name=e["s"], u=e["u"]))
    return added


def upsert_loss(h, kind, value, by, url):
    lst = [x for x in h.get(kind, []) if not re.match(r"(?i)^(no|not)\b", x.get("v", ""))]
    cur = next((x for x in lst if x.get("by") == by), None)
    if cur:
        cur.update(v=value, u=url or cur.get("u"))
    else:
        lst.append(dict(v=value, by=by, u=url))
    h[kind] = lst or [dict(v="Not published", by="—", u=None)]


def research_losses(hist):
    if not API_KEY:
        return 0
    cands = []
    for h in hist["events"]:
        d = parse_date(h["d"])
        if not d or NOW - d > dt.timedelta(days=90):
            continue
        last = parse_date(h.get("chk"))
        if last and NOW - last < dt.timedelta(hours=RESEARCH_EVERY_H):
            continue
        mag = re.match(r"M(\d\.\d)", h.get("m", ""))
        if (RANK.get(h.get("a"), 0) >= 3 or (mag and float(mag.group(1)) >= 6.5) or h.get("mr")
                or (h["t"] == "storm" and re.search(r"(Typhoon|Hurricane|Storm) [A-Z]", h["n"]))):
            cands.append(h)
    cands.sort(key=lambda h: (RANK.get(h.get("a"), 0), h["d"]), reverse=True)
    found = 0
    for h in cands[:MAX_RESEARCH]:
        prompt = (f"Research the catastrophe \"{h['n']}\" ({h['p']}, starting {h['d'][:10]}). Search the web for the latest "
                  "published ECONOMIC loss and INSURED loss estimates (modelers and brokers such as Verisk, Moody's RMS, "
                  "Karen Clark & Co, Cotality, Aon, Gallagher Re, Swiss Re, Munich Re, Guy Carpenter, PCS; governments; "
                  "UN/World Bank), and the latest death toll. Also specifically check Aon's Impact Forecasting coverage "
                  "(its Catastrophe Insight page and Global Catastrophe Recap reports) for any note on this event. Make "
                  "sure each figure refers to THIS event in THIS year (storm names repeat across years). Only report "
                  "numbers and notes you actually saw in a source. "
                  'Return ONLY JSON: {"summary": 4-6 sentences in your own words covering what happened, the scale of '
                  'the event, affected areas, and current status, "deaths": short text or null, '
                  '"econ": [{"v": figure as text e.g. "USD 2-3 billion", "by": estimator, "u": source url}], '
                  '"ins": [same shape], "aon": {"note": 2-4 sentences in your own words on what Aon Impact Forecasting '
                  'has published about this event, "loss": any Aon loss estimate as text or null, "url": the Aon page '
                  'you found this on} or null if Aon has published nothing on this event}. Use empty lists when '
                  "nothing is published.")
        try:
            res = json_from(claude(prompt, search=True)) or {}
        except Exception as ex:  # noqa: BLE001
            log("research", h["n"], ex)
            continue
        h["chk"] = iso(NOW)
        if res.get("summary"):
            h["sum"] = res["summary"]
        if res.get("deaths"):
            h["deaths"] = res["deaths"]
        for kind in ("econ", "ins"):
            for x in res.get(kind) or []:
                if x.get("v") and x.get("by") and x.get("u"):
                    upsert_loss(h, kind, x["v"], x["by"], x["u"])
                    found += 1
        aon = res.get("aon")
        if aon and aon.get("note"):
            nt = aon["note"][:600]
            if aon.get("loss"):
                nt = (nt + f" · Aon loss estimate: {aon['loss']}").strip(" ·")
            entry = dict(o="Aon (Impact Forecasting)", st="Noted", nt=nt, u=aon.get("url") or AON_URL,
                         d=iso(NOW)[:10])
            cur = next((x for x in h["mr"] if x["o"] == entry["o"]), None)
            if cur:
                cur.update(st=entry["st"], nt=entry["nt"], u=entry["u"])
            else:
                h["mr"].append(entry)
            if aon.get("loss"):
                upsert_loss(h, "ins", aon["loss"], "Aon", aon.get("url"))
                found += 1
    return found


def sync_aon_to_live(hist, events):
    """Push Aon (Impact Forecasting) notes found via research_losses() back onto matching live events,
    since Aon has no scrapable live event page to attach_modelers() from directly."""
    for h in hist["events"]:
        aon = next((m for m in h.get("mr", []) if m["o"] == "Aon (Impact Forecasting)"), None)
        if not aon:
            continue
        tgt = next((e for e in events if e["n"].lower() == h["n"].lower()), None)
        if not tgt:
            continue
        tgt.setdefault("mr", [])
        cur = next((x for x in tgt["mr"] if x["o"] == aon["o"]), None)
        if cur:
            cur.update(st=aon["st"], nt=aon["nt"], u=aon.get("u"))
        else:
            tgt["mr"].append(dict(aon))


# ---------------------------------------------------------------------- main
def main():
    prev = json.loads(DATA.read_text()) if DATA.exists() else {"events": [], "sources": []}
    hist = json.loads(HIST.read_text()) if HIST.exists() else {"events": []}
    prev_src = {s["name"]: s for s in prev.get("sources", [])}
    by_source = lambda names: [e for e in prev["events"] if e.get("s") in names]
    ok, failed, sources = {}, [], []

    def run(key, fn):
        try:
            res = fn()
            ok[key] = True
            return res
        except Exception as ex:  # noqa: BLE001
            log(f"{key} failed: {ex}")
            failed.append(key)
            return None

    quakes = run("usgs", fetch_usgs)
    gd = run("gdacs", fetch_gdacs)
    eonet = run("eonet", fetch_eonet)
    vols = run("gvp", fetch_volcanoes)
    events = []
    events += quakes if quakes is not None else by_source({"USGS"})
    if gd is not None:
        storms, fires, gq = gd
        for g in gq:  # upgrade USGS quakes with GDACS alert level
            for e in events:
                if e["t"] == "quake" and haversine(g["la"], g["lo"], e["la"], e["lo"]) < 100 and \
                        abs((parse_date(e["d"]) - g["d"]).total_seconds()) < 3600 and \
                        RANK.get(g["a"], 0) > RANK.get(e.get("a"), 0):
                    e["a"] = g["a"]
                    e["x"] += f" · GDACS {g['a']} alert" + (f", {g['pop']}" if g["pop"] else "")
        events += storms + fires
    else:
        events += by_source({"GDACS"})
    if eonet is not None:
        names = {e["n"].split()[-1].lower() for e in events if e["t"] == "storm"}
        events += [e for e in eonet if not (e["t"] == "storm" and e["n"].split()[-1].lower() in names)]
    else:
        events += by_source({"NASA EONET / JTWC", "NASA EONET / IRWIN"})
    events += vols if vols else by_source({"Smithsonian GVP / USGS"})

    zoom = run("zoomearth", fetch_zoom_tracks)
    prev_tracks = {e["n"].split()[-1].lower(): (e["tr"], e["ze"]) for e in prev["events"]
                   if e.get("t") == "storm" and e.get("tr") and e.get("ze")}
    for e in events:
        if e["t"] != "storm":
            continue
        key = e["n"].split()[-1].lower()
        if zoom is not None and key in zoom:
            e["tr"], e["ze"] = zoom[key]["tr"], zoom[key]["ze"]
        elif key in prev_tracks:  # Zoom Earth run failed or dropped this storm: keep the last track
            e["tr"], e["ze"] = prev_tracks[key]

    for key, org, fn in (("rms", "Moody's RMS", fetch_rms), ("verisk", "Verisk", fetch_verisk)):
        items = run(key, fn)
        if items is not None:
            for e in events:
                if e.get("mr"):
                    e["mr"] = [m for m in e["mr"] if m["o"] != org]
        if items is None:  # carry over previous modeler coverage
            for p in prev["events"]:
                pm = [m for m in p.get("mr", []) if m["o"] == org]
                if not pm:
                    continue
                tgt = next((e for e in events if e["n"] == p["n"] and e["d"][:10] == p["d"][:10]), None)
                if tgt:
                    have = {(m["o"], m["u"]) for m in tgt.get("mr", [])}
                    tgt.setdefault("mr", []).extend(m for m in pm if (m["o"], m["u"]) not in have)
                elif p.get("s") == org:
                    events.append(p)
        else:
            attach_modelers(events, org, items, prev["events"])

    for e in events:
        e["v"] = clamp(e.get("v", 1.5))
    added = merge_history(hist, events)
    found = research_losses(hist)
    sync_aon_to_live(hist, events)
    ok["aon"] = bool(API_KEY)  # Aon has no live page; its data comes from the research pass above

    for key, (name, covers, url) in SRC.items():
        asof = iso(NOW) if ok.get(key) else prev_src.get(name, {}).get("asof", iso(NOW))
        if key == "gvp" and vols:
            asof = max(e["d"] for e in vols)
        sources.append(dict(name=name, covers=covers, asof=asof, url=url))

    for e in events:
        e.pop("uid", None)

    DATA.write_text(json.dumps({"updated": iso(NOW), "sources": sources, "events": events},
                               ensure_ascii=False, separators=(",", ":")))
    hist["updated"] = iso(NOW)
    HIST.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")))
    counts = {}
    for e in events:
        counts[e["t"]] = counts.get(e["t"], 0) + 1
    log(f"live {counts} · archive {len(hist['events'])} (+{added}) · new loss estimates {found} · "
        f"failed: {', '.join(failed) or 'none'} · claude api: {'on' if API_KEY else 'off'}")
    if len(failed) >= 5:
        sys.exit(1)  # nearly everything failed: let the workflow show red rather than publish stale data silently


if __name__ == "__main__":
    main()
