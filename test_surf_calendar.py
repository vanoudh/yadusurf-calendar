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

    def test_tides(self):
        html = ('var daysTides = new Array();\n'
                'daysTides[0] = { BM1:"06h 30", HM1:"00h 45", BM2:"19h 04", HM2:"13h 06", Coef:"46", Coef2:"51" };\n'
                'daysTides[1] = { BM1:"07h 23", HM1:"01h 34", BM2:"-", HM2:"13h 54", Coef:"57", Coef2:"64" };')
        ext = sc.parse_tides(html, date(2026, 11, 20), TZ)
        self.assertEqual(len(ext), 7)  # "-" skipped
        self.assertEqual([e.high for e in ext[:4]], [True, False, True, False])
        # 13:06 Paris = 12:06 UTC, afternoon coef 51
        self.assertEqual(ext[2].time, datetime(2026, 11, 20, 12, 6, tzinfo=timezone.utc))
        self.assertAlmostEqual(ext[2].height, sc.TIDE_MEAN_LEVEL_M + sc.TIDE_UNIT_M * 0.51)
        # exactly at low tide -> low height; halfway -> mean of high and low
        self.assertAlmostEqual(sc.tide_height(ext, ext[1].time), ext[1].height)
        mid = ext[1].time + (ext[2].time - ext[1].time) / 2
        self.assertAlmostEqual(sc.tide_height(ext, mid), (ext[1].height + ext[2].height) / 2)
        self.assertIsNone(sc.tide_height(ext, ext[0].time - timedelta(hours=1)))
        self.assertIsNone(sc.tide_height(ext, ext[-1].time + timedelta(hours=1)))

    def test_hour_line(self):
        html = 'daysTides[0] = { BM1:"06h 30", HM1:"00h 45", BM2:"19h 04", HM2:"13h 06", Coef:"46", Coef2:"51" };'
        tides = sc.parse_tides(html, date(2026, 11, 20), TZ)
        spot = sc.Spot(1280, "Le Penon", "", TZ, OFFSHORE)
        rising = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)    # 10:00 Paris, between BM 06:30 and PM 13:06
        falling = datetime(2026, 11, 20, 15, tzinfo=timezone.utc)  # 16:00 Paris, between PM 13:06 and BM 19:04
        h = hour(rising, hs=1.5, tp=10.6, from_deg=90, speed=2.5)  # east wind = offshore at Le Penon
        line = sc.hour_line(h, spot, tides)
        self.assertRegex(line, r"^10:00 1,5m 11s off 9km/h ↑\d,\dm$")
        self.assertIn(" on ", sc.hour_line(hour(falling, from_deg=270), spot, tides))
        self.assertIn(" side ", sc.hour_line(hour(falling, from_deg=0), spot, tides))
        self.assertRegex(sc.hour_line(hour(falling), spot, tides), r"↓\d,\dm$")
        self.assertRegex(sc.hour_line(h, spot, []), r"km/h$")  # no tide data -> no tide field

    def test_tide_mark(self):
        html = 'daysTides[0] = { BM1:"06h 30", HM1:"00h 45", BM2:"19h 04", HM2:"13h 06", Coef:"46", Coef2:"51" };'
        tides = sc.parse_tides(html, date(2026, 11, 20), TZ)
        spot = sc.Spot(1, "X", "", TZ, OFFSHORE, "(tide=low and swell>=1.5) or swell > 3")
        low = datetime(2026, 11, 20, 5, 30, tzinfo=timezone.utc)   # 06:30 Paris = low tide (~1.5 m)
        high = datetime(2026, 11, 20, 12, 6, tzinfo=timezone.utc)  # 13:06 Paris = high tide
        self.assertTrue(sc.hour_line(hour(low, hs=1.0), spot, tides).endswith(" ✓"))
        self.assertTrue(sc.hour_line(hour(low, hs=2.0), spot, tides).endswith(" ✗"))
        self.assertEqual(sc.tide_mark(hour(high, hs=2.0), spot, tides), "✓")
        self.assertEqual(sc.tide_mark(hour(high, hs=3.5), spot, tides), "✗")
        tide, height, rising, coef = sc.tide_state(tides, low + timedelta(hours=1))
        self.assertEqual((tide, rising, coef), ("low", True, 51))
        self.assertAlmostEqual(height, sc.tide_height(tides, low + timedelta(hours=1)))
        self.assertEqual(sc.tide_state(tides, high)[0], "high")
        deep = sc.Spot(1, "X", "", TZ, OFFSHORE, "height > 4.2")
        self.assertEqual(sc.tide_mark(hour(high), deep, tides), "✓")  # coef 51 high is only ~3,6 m
        no_rule = sc.Spot(1, "X", "", TZ, OFFSHORE)
        self.assertRegex(sc.hour_line(hour(low), no_rule, tides), r"m$")  # no mark without a rule

    def test_avoid_rules(self):
        rules = sc.load_avoid_rules()
        self.assertEqual(set(rules), {1280, 1282, 1283, 1284, 1285, 1286, 1289, 1290, 1291})
        for bad in ("__import__('os')", "swell.real > 1", "tide = 'low'", "wind > 3", "swell >"):
            with self.assertRaises(ValueError, msg=bad):
                sc.compile_avoid(bad)

    def test_fill_hourly_three_hourly_data(self):
        t = datetime(2026, 11, 28, 6, tzinfo=timezone.utc)
        pts = [sc.Hour(t + timedelta(hours=3 * k), 1.0 + 0.3 * k, 12, 3.0, 0.0) for k in range(4)]
        filled = sc.fill_hourly(pts)
        self.assertEqual(len(filled), 10)  # 06..15 hourly
        self.assertTrue(all(b.time - a.time == timedelta(hours=1) for a, b in zip(filled, filled[1:])))
        self.assertAlmostEqual(filled[1].hs, 1.1)
        # a long hole (> 6 h) is not filled
        self.assertEqual(len(sc.fill_hourly([pts[0], sc.Hour(t + timedelta(hours=12), 1, 12, 0, 0)])), 2)

    def test_hour_score(self):
        t = datetime(2026, 11, 28, 10, tzinfo=timezone.utc)
        ideal = hour(t, hs=1.8, tp=12, from_deg=90, speed=2)        # offshore, ideal size and period
        self.assertAlmostEqual(sc.hour_score(ideal, OFFSHORE), 0.95)
        self.assertEqual(sc.score_to_stars(sc.hour_score(ideal, OFFSHORE)), 3)
        self.assertEqual(sc.hour_score(hour(t, hs=4.0, tp=14), OFFSHORE), 0.0)   # too big
        self.assertEqual(sc.hour_score(hour(t, hs=0.2), OFFSHORE), 0.0)          # flat
        onshore = hour(t, hs=1.8, tp=12, from_deg=270, speed=8)                  # strong onshore
        self.assertEqual(sc.score_to_stars(sc.hour_score(onshore, OFFSHORE)), 0)
        short = hour(t, hs=1.8, tp=7, from_deg=90, speed=2)                      # short period
        self.assertLess(sc.hour_score(short, OFFSHORE), sc.hour_score(ideal, OFFSHORE))

    def test_predict_day_stars(self):
        start = datetime(2026, 11, 27, 23, tzinfo=timezone.utc)  # local midnight on the 28th
        hours = [hour(start + timedelta(hours=k), hs=1.3, tp=12, from_deg=90, speed=3) for k in range(24)]
        self.assertEqual(sc.predict_day_stars(hours, date(2026, 11, 28), TZ, LAT, LON, OFFSHORE), 3)
        self.assertIsNone(sc.predict_day_stars(hours, date(2026, 11, 30), TZ, LAT, LON, OFFSHORE))

    def test_drop_bad_tide(self):
        html = 'daysTides[0] = { BM1:"06h 30", HM1:"00h 45", BM2:"19h 04", HM2:"13h 06", Coef:"46", Coef2:"51" };'
        tides = sc.parse_tides(html, date(2026, 11, 20), TZ)
        spot = sc.Spot(1, "X", "", TZ, OFFSHORE, "tide=high")
        start = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)  # 07:00 Paris, near low tide, rising to high at 13:06
        w = sc.Window(day(date(2026, 11, 20), 2), [hour(start + timedelta(hours=k)) for k in range(10)])
        marks = [sc.tide_mark(h, spot, tides) for h in w.hours]
        kept = sc.drop_bad_tide([w], spot, tides)
        self.assertIn("✗", marks)
        self.assertTrue(all(sc.tide_mark(h, spot, tides) == "✓" for k in kept for h in k.hours))
        self.assertEqual(sum(len(k.hours) for k in kept), marks.count("✓"))
        # no prefs -> unchanged
        self.assertEqual(len(sc.drop_bad_tide([w], sc.Spot(1, "X", "", TZ), tides)[0].hours), 10)

    def test_ics_escaping_and_folding(self):
        line = sc.fold("DESCRIPTION:" + "é" * 100)
        for part in line.split("\r\n"):
            self.assertLessEqual(len(part.encode()), 75)
        self.assertEqual(sc.ics_escape("a,b;c\nd"), "a\\,b\\;c\\nd")


if __name__ == "__main__":
    unittest.main()
