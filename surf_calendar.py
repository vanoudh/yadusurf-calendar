#!/usr/bin/env python3
"""Turn a YaDuSurf spot forecast into an iCalendar (.ics) feed of good surf windows.

Data source: https://www.yadusurf.com/WebApi/SurfometerData?spotId=<id>
  - SurfometreInfosXml: one <Day> per forecast day with Stars (0-3), Comment, Coefficient, WaterTemp
  - WawJson: hourly items with Swell (Hs, Tp) and Weather (VentU / VentV wind components, m/s)

An event is created for each run of consecutive daylight hours, on a day rated >= MIN_STARS,
where the wind quality (same formula as yadusurf.com's graph colours) is >= MIN_WIND_QUALITY.

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API_URL = "https://www.yadusurf.com/WebApi/SurfometerData?spotId={spot_id}"
PAGE_URL = "https://www.yadusurf.com/Meteo-Surf-Report/{slug}/{spot_id}"

MIN_STARS = 1
MIN_WIND_QUALITY = 0.6     # yadusurf colours wind >= 0.6 as good
MIN_WINDOW_HOURS = 2
MIN_SWELL_M = 0.5
IDEAL_SWELL_M = 1.5        # "best spot": prefer swell closest to this
LARGE_SWELL_M = 1.5        # tide marks: swell >= this uses the "large swell" tide range
SPOT_TIDES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spot_tides.csv")
DELAY_BETWEEN_SPOTS_S = 5  # pause between API calls, to be gentle with the site
DAWN_MARGIN = timedelta(minutes=30)  # surf from 30 min before sunrise

# Tide heights are estimated from yadusurf's high/low times + coefficients (port of Le Boucau),
# with the French "unité de hauteur" method: height = mean level ± unit * coef / 100,
# heights above chart datum (zéro hydrographique). Approximate values for Le Boucau.
TIDE_MEAN_LEVEL_M = 2.5
TIDE_UNIT_M = 2.1

COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
BEAUFORT_MS = [0.3, 1.6, 3.4, 5.5, 8.0, 10.8, 13.9, 17.2, 20.8, 24.5, 28.5, 32.7]


# ---------------------------------------------------------------- data model

@dataclass
class Hour:
    time: datetime          # UTC
    hs: float               # significant swell height, m
    tp: float               # peak period, s
    wind_u: float           # m/s, positive = blowing toward east
    wind_v: float           # m/s, positive = blowing toward north

    @property
    def wind_speed(self) -> float:
        return math.hypot(self.wind_u, self.wind_v)

    @property
    def wind_toward_rad(self) -> float:
        """Compass bearing (radians) the wind blows toward - same as yadusurf's WindDir."""
        return math.atan2(self.wind_u, self.wind_v)


@dataclass
class DayInfo:
    day: date
    stars: int
    comment: str
    coefficient: str
    water_temp: str


@dataclass
class Window:
    day: DayInfo
    hours: list[Hour]

    @property
    def start(self) -> datetime:
        return self.hours[0].time

    @property
    def end(self) -> datetime:
        return self.hours[-1].time + timedelta(hours=1)


# ---------------------------------------------------------------- fetch/parse

def get(url: str, attempts: int = 2) -> str:
    """GET with one gentle retry. If every attempt fails the exception propagates."""
    req = urllib.request.Request(
        urllib.parse.quote(url, safe=":/?=&"),
        headers={"User-Agent": "Mozilla/5.0 (surf-calendar)"},
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8")
        except OSError as e:  # URLError, timeouts
            if attempt == attempts:
                raise
            print(f"   {url}: attempt {attempt} failed ({e}), retrying...", file=sys.stderr)
            time.sleep(30)
    raise AssertionError("unreachable")


def fetch(spot_id: int) -> dict:
    """If this fails the workflow fails before deploying, so the last good calendars stay live."""
    return json.loads(get(API_URL.format(spot_id=spot_id)))


def parse_days(xml_text: str) -> dict[date, DayInfo]:
    xml_text = xml_text.lstrip("\ufeff").replace('encoding="utf-16"', "")
    days = {}
    for d in ET.fromstring(xml_text).findall("Day"):
        day = datetime.strptime(d.findtext("SurfDayDate"), "%Y%m%d").date()
        days[day] = DayInfo(
            day=day,
            stars=int(d.findtext("Stars") or 0),
            comment=(d.findtext("Comment") or "").strip(),
            coefficient=d.findtext("Coefficient") or "",
            water_temp=d.findtext("WaterTemp") or "",
        )
    return days


def parse_hours(waw: dict) -> list[Hour]:
    hours = []
    for wd in waw.get("_WAWDays") or waw.get("WAWDays") or []:
        for item in wd.get("Items", []):
            sw, we = item.get("Swell"), item.get("Weather")
            if not sw or not we:
                continue
            hours.append(Hour(
                time=datetime.fromisoformat(item["Date"].replace("Z", "+00:00")),
                hs=sw["Hs"], tp=sw["Tp"],
                wind_u=we["VentU"], wind_v=we["VentV"],
            ))
    hours.sort(key=lambda h: h.time)
    return fill_hourly(hours)


def fill_hourly(hours: list[Hour], max_gap_h: int = 6) -> list[Hour]:
    """Beyond ~5 days yadusurf switches to 3-hourly data (long-range model). Fill the missing
    hours by linear interpolation so windows and hourly lines work the same on every day.
    Gaps longer than max_gap_h are left as-is (no invention over long holes)."""
    out: list[Hour] = []
    for a, b in zip(hours, hours[1:]):
        out.append(a)
        gap = int((b.time - a.time).total_seconds() // 3600)
        if 1 < gap <= max_gap_h:
            for k in range(1, gap):
                f = k / gap
                out.append(Hour(
                    time=a.time + timedelta(hours=k),
                    hs=a.hs + (b.hs - a.hs) * f, tp=a.tp + (b.tp - a.tp) * f,
                    wind_u=a.wind_u + (b.wind_u - a.wind_u) * f,
                    wind_v=a.wind_v + (b.wind_v - a.wind_v) * f,
                ))
    if hours:
        out.append(hours[-1])
    return out


# ---------------------------------------------------------------- tides

@dataclass
class TideExtreme:
    time: datetime   # UTC
    high: bool
    height: float    # m above chart datum (estimated)


def parse_tides(html: str, first_day: date, tz: ZoneInfo) -> list[TideExtreme]:
    """Parse the spot page's `daysTides[i] = { BM1:"06h 30", HM1:"00h 45", ..., Coef:"46", Coef2:"51" }`.
    daysTides[0] is today. Coef applies to morning tides, Coef2 to afternoon ones."""
    extremes = []
    for m in re.finditer(r"daysTides\[(\d+)\]\s*=\s*\{([^}]*)\}", html):
        d = first_day + timedelta(days=int(m.group(1)))
        fields = dict(re.findall(r'(\w+)\s*:\s*"([^"]*)"', m.group(2)))
        for key, high in (("HM1", True), ("BM1", False), ("HM2", True), ("BM2", False)):
            t = re.fullmatch(r"\s*(\d{1,2})h\s*(\d{2})\s*", fields.get(key, ""))
            if not t:
                continue
            local = datetime(d.year, d.month, d.day, int(t[1]), int(t[2]), tzinfo=tz)
            try:
                coef = float(fields.get("Coef" if local.hour < 12 else "Coef2") or fields["Coef"])
            except (KeyError, ValueError):
                continue
            amplitude = TIDE_UNIT_M * coef / 100
            extremes.append(TideExtreme(
                local.astimezone(timezone.utc), high,
                TIDE_MEAN_LEVEL_M + (amplitude if high else -amplitude)))
    extremes.sort(key=lambda e: e.time)
    return extremes


def tide_height(extremes: list[TideExtreme], t: datetime) -> float | None:
    """Cosine interpolation between the surrounding high and low tide; None if unknown."""
    for a, b in zip(extremes, extremes[1:]):
        if a.time <= t <= b.time:
            if a.high == b.high:
                return None
            frac = (t - a.time) / (b.time - a.time)
            return a.height + (b.height - a.height) * (1 - math.cos(math.pi * frac)) / 2
    return None


# ---------------------------------------------------------------- surf logic

def beaufort(speed_ms: float) -> int:
    return sum(speed_ms >= t for t in BEAUFORT_MS)


def wind_type(h: Hour, offshore_heading_rad: float) -> str:
    """'off', 'side' or 'on' - port of yadusurf.com VentCalculateur.GetWindOffShore."""
    c = math.cos(h.wind_toward_rad - offshore_heading_rad)
    return "off" if c > 0.2 else "on" if c < -0.2 else "side"


def wind_quality(h: Hour, offshore_heading_rad: float) -> float:
    """Port of yadusurf.com VentCalculateur.GetWindQuality."""
    bft = beaufort(h.wind_speed)
    kind = wind_type(h, offshore_heading_rad)
    if kind == "off":
        return 1.0 if bft > 3 else 0.95
    if kind == "on":
        return 0.7 if bft < 1 else 0.6 if bft < 2 else 0.5 if bft < 3 else 0.2 if bft < 4 else 0.0
    return 0.8 if bft < 2 else 0.6 if bft < 3 else 0.4 if bft < 4 else 0.2


def sun_times(day: date, lat: float, lon: float) -> tuple[datetime, datetime]:
    """Approximate sunrise/sunset in UTC (NOAA simplified, accurate to a few minutes)."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    la = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(la) * math.cos(decl))
              - math.tan(la) * math.tan(decl))
    ha = math.degrees(math.acos(max(-1.0, min(1.0, cos_ha))))
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rise = midnight + timedelta(minutes=720 - 4 * (lon + ha) - eqtime)
    sset = midnight + timedelta(minutes=720 - 4 * (lon - ha) - eqtime)
    return rise, sset


def find_windows(hours: list[Hour], days: dict[date, DayInfo], tz: ZoneInfo,
                 lat: float, lon: float, offshore_heading_rad: float) -> list[Window]:
    windows: list[Window] = []
    current: list[Hour] = []
    current_day: DayInfo | None = None

    def flush():
        nonlocal current
        if current_day and len(current) >= MIN_WINDOW_HOURS:
            windows.append(Window(current_day, current))
        current = []

    for h in hours:
        local_day = h.time.astimezone(tz).date()
        info = days.get(local_day)
        rise, sset = sun_times(local_day, lat, lon)
        good = (
            info is not None
            and info.stars >= MIN_STARS
            and rise - DAWN_MARGIN <= h.time and h.time + timedelta(hours=1) <= sset + timedelta(minutes=30)
            and h.hs >= MIN_SWELL_M
            and wind_quality(h, offshore_heading_rad) >= MIN_WIND_QUALITY
        )
        contiguous = current and h.time - current[-1].time == timedelta(hours=1) and info is current_day
        if not (good and contiguous):
            flush()
        if good:
            current_day = info
            current.append(h)
    flush()
    return windows


# ---------------------------------------------------------------- formatting

def fmt_num(x: float, decimals: int = 1) -> str:
    s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")


def compass_from(h_list: list[Hour]) -> str:
    """Average direction the wind comes FROM, 8-point compass."""
    u = sum(h.wind_u for h in h_list)
    v = sum(h.wind_v for h in h_list)
    from_deg = (math.degrees(math.atan2(u, v)) + 180) % 360
    return COMPASS[round(from_deg / 45) % 8]


def title(w: Window, spot_name: str) -> str:
    hs = sum(h.hs for h in w.hours) / len(w.hours)
    tp = sum(h.tp for h in w.hours) / len(w.hours)
    return f"{w.day.stars}* {spot_name} {fmt_num(hs)}m {round(tp)}s {compass_from(w.hours)}"


def tide_text(tides: list[TideExtreme], t: datetime) -> str:
    """e.g. '↑3,9m' (rising) / '↓3,7m' (falling); '' when unknown."""
    now = tide_height(tides, t)
    later = tide_height(tides, t + timedelta(minutes=10))
    if now is None:
        return ""
    arrow = "" if later is None else "↑" if later > now else "↓"
    return f"{arrow}{now:.1f}m".replace(".", ",")


def load_tide_prefs(path: str = SPOT_TIDES_CSV) -> dict[int, dict[str, tuple[float, float]]]:
    """spot_id -> {"small": (min_m, max_m), "large": (min_m, max_m)} from spot_tides.csv; {} if missing."""
    if not os.path.exists(path):
        return {}
    prefs = {}
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            prefs[int(r["spot_id"])] = {
                size: (float(r[f"{size}_swell_tide_min_m"]), float(r[f"{size}_swell_tide_max_m"]))
                for size in ("small", "large")
            }
    return prefs


def tide_mark(h: Hour, spot: Spot, tides: list[TideExtreme]) -> str:
    """'✓' if the tide suits this spot for this hour's swell size, '✗' if not, '' if unknown."""
    height = tide_height(tides, h.time)
    if height is None or not spot.tide_ranges:
        return ""
    lo, hi = spot.tide_ranges["large" if h.hs >= LARGE_SWELL_M else "small"]
    return "✓" if lo <= height <= hi else "✗"


def hour_line(h: Hour, spot: Spot, tides: list[TideExtreme]) -> str:
    """e.g. '14:00 1,5m 11s side 9km/h ↑3,9m ✓'"""
    parts = [
        f"{h.time.astimezone(spot.tz):%H:%M}",
        f"{fmt_num(h.hs)}m",
        f"{round(h.tp)}s",
        wind_type(h, spot.offshore_rad),
        f"{round(h.wind_speed * 3.6)}km/h",
        tide_text(tides, h.time),
        tide_mark(h, spot, tides),
    ]
    return " ".join(p for p in parts if p)


def description(w: Window, spot: Spot, tides: list[TideExtreme] | None = None) -> str:
    lines = [w.day.comment, ""]
    lines.append(f"Coef marée: {w.day.coefficient} · Eau: {w.day.water_temp}°C")
    lines.append("")
    lines += [hour_line(h, spot, tides or []) for h in w.hours]
    lines += ["", spot.page_url]
    return "\n".join(lines)


def ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\r\n", "\\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    out, buf = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(buf) + len(b) > (75 if not out else 74):
            out.append(buf.decode("utf-8"))
            buf = b""
        buf += b
    out.append(buf.decode("utf-8"))
    return "\r\n ".join(out)


@dataclass
class Spot:
    id: int
    name: str
    page_url: str
    tz: ZoneInfo
    offshore_rad: float = 0.0  # compass bearing an offshore wind blows toward
    tide_ranges: dict[str, tuple[float, float]] | None = None  # from spot_tides.csv


def best_score(w: Window) -> tuple:
    """Sort key, lower is better: more stars, then swell closer to IDEAL_SWELL_M, then longer."""
    hs = sum(h.hs for h in w.hours) / len(w.hours)
    return (-w.day.stars, round(abs(hs - IDEAL_SWELL_M), 1), -len(w.hours))


def select_best(events: list[tuple["Spot", Window]]) -> list[tuple["Spot", Window]]:
    """Keep only one spot at any given time: greedily take the best-scored window,
    drop every window overlapping it, repeat."""
    chosen: list[tuple[Spot, Window]] = []
    for spot, w in sorted(events, key=lambda e: (best_score(e[1]), e[0].id)):
        if all(w.end <= c.start or w.start >= c.end for _, c in chosen):
            chosen.append((spot, w))
    return chosen


def build_ics(events: list[tuple[Spot, Window]], cal_name: str, tz: ZoneInfo,
              generated: datetime, tides: list[TideExtreme] | None = None) -> str:
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//surf-calendar//yadusurf//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(cal_name)}",
        f"X-WR-TIMEZONE:{tz.key}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for spot, w in sorted(events, key=lambda e: (e[1].start, e[0].name)):
        lines += [
            "BEGIN:VEVENT",
            f"UID:{spot.id}-{w.start:%Y%m%dT%H%M}@surf-calendar",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{w.start:%Y%m%dT%H%M%SZ}",
            f"DTEND:{w.end:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{ics_escape(title(w, spot.name))}",
            f"DESCRIPTION:{ics_escape(description(w, spot, tides))}",
            f"LOCATION:{ics_escape(spot.name)}",
            f"URL:{spot.page_url}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- main

def process_spot(spot_id: int, data: dict,
                 tide_prefs: dict[int, dict[str, tuple[float, float]]] | None = None) -> tuple[Spot, list[Window]]:
    waw = json.loads(data["WawJson"])
    s = waw["Spot"]
    name = s["Name"].strip()
    spot = Spot(
        id=spot_id,
        name=name,
        page_url=PAGE_URL.format(slug=name.replace(" ", "-"), spot_id=spot_id),
        tz=ZoneInfo(s.get("TimeZoneIana") or "Europe/Paris"),
        offshore_rad=s["OffshoreWindHeading"]["AngleRad"],
        tide_ranges=(tide_prefs or {}).get(spot_id),
    )
    days = parse_days(data["SurfometreInfosXml"])
    windows = find_windows(parse_hours(waw), days, spot.tz,
                           s["Position"]["Latitude"], s["Position"]["Longitude"],
                           s["OffshoreWindHeading"]["AngleRad"])
    return spot, windows


def write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spot-id", type=int, nargs="+", default=[1280],
                    help="one or more yadusurf spot ids")
    ap.add_argument("--input", help="read API JSON from a file instead of fetching (single spot only)")
    ap.add_argument("--tide-html", help="read the tide table from a saved spot page instead of fetching")
    ap.add_argument("--output-dir", "-o", default="public",
                    help="writes <spot-id>.ics per spot, plus all.ics and best.ics when several spots are given")
    args = ap.parse_args()
    if args.input and len(args.spot_id) > 1:
        ap.error("--input works with a single --spot-id")

    now = datetime.now(timezone.utc)
    tide_prefs = load_tide_prefs()
    results: list[tuple[Spot, list[Window]]] = []
    for i, spot_id in enumerate(args.spot_id):
        if args.input:
            with open(args.input, encoding="utf-8") as f:
                data = json.load(f)
        else:
            if i:
                time.sleep(DELAY_BETWEEN_SPOTS_S)  # be gentle with yadusurf.com
            data = fetch(spot_id)
        results.append(process_spot(spot_id, data, tide_prefs))

    # Tide table: one page fetch for all spots (they share the port of Le Boucau).
    # Optional: if it fails, calendars are still published, just without tide heights.
    tz = results[0][0].tz
    html = ""
    if args.tide_html:
        with open(args.tide_html, encoding="utf-8") as f:
            html = f.read()
    elif not args.input:
        time.sleep(DELAY_BETWEEN_SPOTS_S)
        try:
            html = get(results[0][0].page_url)
        except OSError as e:
            print(f"warning: tide page unavailable ({e}), no tide heights", file=sys.stderr)
    tides = parse_tides(html, now.astimezone(tz).date(), tz) if html else []
    if html and not tides:
        print("warning: no tide table found on the page", file=sys.stderr)
    print(f"Tides: {len(tides)} high/low tides parsed")

    all_events: list[tuple[Spot, Window]] = []
    for spot, windows in results:
        events = [(spot, w) for w in windows]
        all_events += events
        path = os.path.join(args.output_dir, f"{spot.id}.ics")
        write(path, build_ics(events, f"Surf {spot.name}", spot.tz, now, tides))

        print(f"== {spot.name} ({spot.id}): {len(windows)} window(s) -> {path}")
        for w in windows:
            print(f"   {w.start.astimezone(spot.tz):%a %d %H:%M}-{w.end.astimezone(spot.tz):%H:%M}  "
                  f"{title(w, spot.name)}")

    if len(args.spot_id) > 1:
        path = os.path.join(args.output_dir, "all.ics")
        write(path, build_ics(all_events, "Surf - all spots", tz, now, tides))
        print(f"\n{len(all_events)} window(s) across {len(args.spot_id)} spots -> {path}")

        best = select_best(all_events)
        path = os.path.join(args.output_dir, "best.ics")
        write(path, build_ics(best, "Surf - best spot", tz, now, tides))
        print(f"\nBest spot: {len(best)} window(s) -> {path}")
        for spot, w in sorted(best, key=lambda e: e[1].start):
            print(f"   {w.start.astimezone(spot.tz):%a %d %H:%M}-{w.end.astimezone(spot.tz):%H:%M}  "
                  f"{title(w, spot.name)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
