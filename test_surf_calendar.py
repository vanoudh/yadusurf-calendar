import math
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import surf_calendar as sc

TZ = ZoneInfo("Europe/Paris")
LAT, LON = 43.708, -1.439
OFFSHORE = -1.3744467859455352  # Le Penon: offshore wind blows toward ~281°


def hour(t, hs=1.5, tp=10, from_deg=90, speed=4.0):
    """Wind coming FROM from_deg at speed m/s."""
    toward = math.radians(from_deg + 180)
    return sc.Hour(t, hs, tp, speed * math.sin(toward), speed * math.cos(toward))


def day(d, stars):
    return sc.DayInfo(d, stars, "comment", "70", "16")


class Tests(unittest.TestCase):
    def test_wind_quality_offshore_vs_onshore(self):
        t = datetime(2026, 11, 21, 9, tzinfo=timezone.utc)
        self.assertGreaterEqual(sc.wind_quality(hour(t, from_deg=90, speed=8), OFFSHORE), 0.95)
        self.assertEqual(sc.wind_quality(hour(t, from_deg=270, speed=8), OFFSHORE), 0.0)

    def test_title_format(self):
        t = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)
        w = sc.Window(day(date(2026, 11, 21), 3), [hour(t, 1.5, 10.2), hour(t + timedelta(hours=1), 1.5, 9.8)])
        self.assertEqual(sc.title(w, "Le Penon"), "3* Le Penon 1,5m 10s E")

    def test_windows_respect_stars_daylight_and_wind(self):
        start = datetime(2026, 11, 20, 23, tzinfo=timezone.utc)  # local midnight on the 21st
        hours = []
        for i in range(48):
            t = start + timedelta(hours=i)
            onshore = t.astimezone(TZ).hour >= 14
            hours.append(hour(t, from_deg=270 if onshore else 90, speed=8))
        days = {date(2026, 11, 21): day(date(2026, 11, 21), 2),
                date(2026, 11, 22): day(date(2026, 11, 22), 0)}
        ws = sc.find_windows(hours, days, TZ, LAT, LON, OFFSHORE)
        self.assertEqual(len(ws), 1)  # 0-star day ignored
        w = ws[0]
        self.assertEqual(w.start.astimezone(TZ).hour, 8)   # sunrise ~8:05, 30 min margin
        self.assertEqual(w.end.astimezone(TZ).hour, 14)    # wind turns onshore at 14h

    def test_select_best_one_spot_at_a_time(self):
        t = datetime(2026, 11, 21, 8, tzinfo=timezone.utc)
        d = date(2026, 11, 21)
        spot = lambda i: sc.Spot(i, f"S{i}", "", TZ)
        win = lambda stars, hs, start, n: sc.Window(
            day(d, stars), [hour(start + timedelta(hours=k), hs) for k in range(n)])
        a = (spot(1), win(2, 1.5, t, 4))                       # 2*, ideal swell
        b = (spot(2), win(3, 2.5, t + timedelta(hours=1), 4))  # 3*, overlaps a -> wins
        c = (spot(3), win(2, 1.4, t + timedelta(hours=1), 2))  # overlaps b -> dropped
        e = (spot(4), win(1, 1.5, t + timedelta(hours=6), 2))  # no overlap -> kept
        f = (spot(5), win(1, 2.8, t + timedelta(hours=6), 2))  # same slot, worse swell
        best = sc.select_best([a, b, c, e, f])
        self.assertEqual(sorted(s.id for s, _ in best), [2, 4])

    def test_ics_escaping_and_folding(self):
        line = sc.fold("DESCRIPTION:" + "é" * 100)
        for part in line.split("\r\n"):
            self.assertLessEqual(len(part.encode()), 75)
        self.assertEqual(sc.ics_escape("a,b;c\nd"), "a\\,b\\;c\\nd")


if __name__ == "__main__":
    unittest.main()
