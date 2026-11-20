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
import json
import math
import sys
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
DAWN_MARGIN = timedelta(minutes=30)  # surf from 30 min before sunrise

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

def fetch(spot_id: int) -> dict:
    req = urllib.request.Request(
        API_URL.format(spot_id=spot_id),
        headers={"User-Agent": "Mozilla/5.0 (surf-calendar)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


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
    return hours


# ---------------------------------------------------------------- surf logic

def beaufort(speed_ms: float) -> int:
    return sum(speed_ms >= t for t in BEAUFORT_MS)


def wind_quality(h: Hour, offshore_heading_rad: float) -> float:
    """Port of yadusurf.com VentCalculateur.GetWindQuality."""
    bft = beaufort(h.wind_speed)
    c = math.cos(h.wind_toward_rad - offshore_heading_rad)
    if c > 0.2:   # offshore
        return 1.0 if bft > 3 else 0.95
    if c < -0.2:  # onshore
        return 0.7 if bft < 1 else 0.6 if bft < 2 else 0.5 if bft < 3 else 0.2 if bft < 4 else 0.0
    # sideshore
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


def description(w: Window, page_url: str, tz: ZoneInfo) -> str:
    lines = [w.day.comment, ""]
    lines.append(f"Coef marée: {w.day.coefficient} · Eau: {w.day.water_temp}°C")
    lines.append("")
    for h in w.hours:
        lines.append(
            f"{h.time.astimezone(tz):%H:%M}  {fmt_num(h.hs)}m {round(h.tp)}s  "
            f"vent {compass_from([h])} {round(h.wind_speed * 3.6)} km/h"
        )
    lines += ["", page_url]
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


def build_ics(windows: list[Window], spot_id: int, spot_name: str, page_url: str,
              tz: ZoneInfo, generated: datetime) -> str:
    stamp = generated.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//surf-calendar//yadusurf//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Surf {spot_name}",
        f"X-WR-TIMEZONE:{tz.key}",
        "REFRESH-INTERVAL;VALUE=DURATION:PT3H",
        "X-PUBLISHED-TTL:PT3H",
    ]
    for w in windows:
        lines += [
            "BEGIN:VEVENT",
            f"UID:{spot_id}-{w.start:%Y%m%dT%H%M}@surf-calendar",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{w.start:%Y%m%dT%H%M%SZ}",
            f"DTEND:{w.end:%Y%m%dT%H%M%SZ}",
            f"SUMMARY:{ics_escape(title(w, spot_name))}",
            f"DESCRIPTION:{ics_escape(description(w, page_url, tz))}",
            f"LOCATION:{ics_escape(spot_name)}",
            f"URL:{page_url}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spot-id", type=int, default=1280)
    ap.add_argument("--input", help="read API JSON from a file instead of fetching")
    ap.add_argument("--output", "-o", help="default: public/<spot-id>.ics")
    args = ap.parse_args()
    args.output = args.output or f"public/{args.spot_id}.ics"

    if args.input:
        with open(args.input, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = fetch(args.spot_id)

    waw = json.loads(data["WawJson"])
    spot = waw["Spot"]
    spot_name = spot["Name"]
    tz = ZoneInfo(spot.get("TimeZoneIana") or "Europe/Paris")
    lat, lon = spot["Position"]["Latitude"], spot["Position"]["Longitude"]
    offshore = spot["OffshoreWindHeading"]["AngleRad"]
    page_url = PAGE_URL.format(slug=spot_name.replace(" ", "-"), spot_id=args.spot_id)

    days = parse_days(data["SurfometreInfosXml"])
    hours = parse_hours(waw)
    windows = find_windows(hours, days, tz, lat, lon, offshore)

    ics = build_ics(windows, args.spot_id, spot_name, page_url, tz, datetime.now(timezone.utc))
    import os
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8", newline="") as f:
        f.write(ics)

    for d in sorted(days.values(), key=lambda d: d.day):
        print(f"{d.day}  {'*' * d.stars:<3}  {d.comment.splitlines()[0] if d.comment else ''}")
    print()
    for w in windows:
        print(f"{w.start.astimezone(tz):%a %d %H:%M}-{w.end.astimezone(tz):%H:%M}  {title(w, spot_name)}")
    print(f"\n{len(windows)} window(s) -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
