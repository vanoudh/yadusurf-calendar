import io
import math
import unittest
import urllib.error
from unittest import mock
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

    def test_select_best_hour_by_hour(self):
        t = datetime(2026, 12, 1, 7, tzinfo=timezone.utc)  # 08:00 Paris
        d = date(2026, 12, 1)
        spot = lambda i, ideal=1.5: sc.Spot(i, f"S{i}", "", TZ, ideal_swell=ideal)
        win = lambda stars, hs, start, n: sc.Window(
            day(d, stars), [hour(t + timedelta(hours=start + k), hs) for k in range(n)])
        local = lambda ev: [(s.id, w.start.astimezone(TZ).hour, w.end.astimezone(TZ).hour) for s, w in ev]

        # Tuesday 1 Dec: a short big-swell window no longer wipes out a long one
        penon = (spot(1280), win(3, 1.4, 0, 10))          # 08-18, 1.4 m vs ideal 1.5
        nord = (spot(1285, 2.0), win(3, 1.5, 8, 2))       # 16-18, 1.5 m vs ideal 2.0
        self.assertEqual(local(sc.select_best([penon, nord])), [(1280, 8, 18)])

        # more stars wins only the hours it covers; the other spot keeps the rest
        a = (spot(1), win(2, 1.5, 0, 10))
        b = (spot(2), win(3, 2.0, 4, 3))                  # 12-15
        self.assertEqual(local(sc.select_best([a, b])), [(1, 8, 12), (2, 12, 15), (1, 15, 18)])

        # same stars: stay on the current spot unless another is clearly (>= 0.2 m) closer to ideal
        a = (spot(1), win(2, 1.7, 0, 6))                  # 0.2 m off ideal
        c = (spot(3), win(2, 1.6, 2, 4))                  # 0.1 m off: not enough to switch
        self.assertEqual(local(sc.select_best([a, c])), [(1, 8, 14)])
        e = (spot(4), win(2, 1.5, 2, 4))                  # 0.0 m off: switch
        self.assertEqual(local(sc.select_best([a, e])), [(1, 8, 10), (4, 10, 14)])

        # a leftover run shorter than MIN_WINDOW_HOURS is dropped
        f = (spot(5), win(2, 1.5, 1, 5))                  # 09-14 takes over a at 09:00
        self.assertEqual(local(sc.select_best([a, f])), [(5, 9, 14)])

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
        spot = sc.Spot(1, "X", "", TZ, OFFSHORE, "(height < 1.8 and swell >= 1.5) or swell > 3")
        low = datetime(2026, 11, 20, 5, 30, tzinfo=timezone.utc)   # 06:30 Paris = low tide (~1.5 m)
        high = datetime(2026, 11, 20, 12, 6, tzinfo=timezone.utc)  # 13:06 Paris = high tide
        self.assertTrue(sc.hour_line(hour(low, hs=1.0), spot, tides).endswith(" ✓"))
        self.assertTrue(sc.hour_line(hour(low, hs=2.0), spot, tides).endswith(" ✗"))
        self.assertEqual(sc.tide_mark(hour(high, hs=2.0), spot, tides), "✓")
        self.assertEqual(sc.tide_mark(hour(high, hs=3.5), spot, tides), "✗")
        height, rising = sc.tide_state(tides, low + timedelta(hours=1))
        self.assertTrue(rising)
        self.assertAlmostEqual(height, sc.tide_height(tides, low + timedelta(hours=1)))
        self.assertFalse(sc.tide_state(tides, high + timedelta(hours=1))[1])
        self.assertFalse(sc.tide_state(tides, high)[1])  # exactly at high tide: falling, like the arrow
        self.assertTrue(sc.tide_text(tides, high).startswith("↓"))
        last = tides[-1]  # 19:04 low: just before it the tide is still falling
        self.assertFalse(sc.tide_state(tides, last.time - timedelta(minutes=5))[1])
        self.assertTrue(sc.tide_state(tides, last.time)[1])  # exactly at the last low: rising
        last_high = tides[:3][-1]  # table ending on the 13:06 high
        self.assertTrue(last_high.high)
        self.assertFalse(sc.tide_state(tides[:3], last_high.time)[1])  # exactly at high: falling
        deep = sc.Spot(1, "X", "", TZ, OFFSHORE, "height > 4.2")
        self.assertEqual(sc.tide_mark(hour(high), deep, tides), "✓")  # coef 51 high is only ~3,6 m
        no_rule = sc.Spot(1, "X", "", TZ, OFFSHORE)
        self.assertRegex(sc.hour_line(hour(low), no_rule, tides), r"m$")  # no mark without a rule

    def test_description_shows_avoid_rule(self):
        html = 'daysTides[0] = { BM1:"06h 30", HM1:"00h 45", BM2:"19h 04", HM2:"13h 06", Coef:"46", Coef2:"51" };'
        tides = sc.parse_tides(html, date(2026, 11, 20), TZ)
        t = datetime(2026, 11, 20, 9, tzinfo=timezone.utc)
        w = sc.Window(day(date(2026, 11, 20), 2), [hour(t), hour(t + timedelta(hours=1))])
        spot = sc.Spot(1, "X", "", TZ, OFFSHORE, "height > 3.2 or swell > 2")
        self.assertIn("\n✗ = height > 3.2 or swell > 2\n", sc.description(w, spot, tides))
        self.assertNotIn("✗ =", sc.description(w, spot, []))  # no tide data -> no marks, no rule

    def test_spot_config(self):
        config = sc.load_spot_config()
        self.assertEqual(set(config), {1280, 1282, 1283, 1284, 1285, 1286, 1289, 1290, 1291})
        self.assertEqual(config[1285]["ideal_swell"], 2.0)  # La Nord: big-swell spot
        self.assertEqual(config[1280]["ideal_swell"], 1.5)
        self.assertTrue(all(c["avoid"] for c in config.values()))
        for bad in ("__import__('os')", "swell.real > 1", "tide = 'low'", "coef > 80", "wind > 3", "swell >"):
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
        spot = sc.Spot(1, "X", "", TZ, OFFSHORE, "height > 3.2")
        start = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)  # 07:00 Paris, near low tide, rising to high at 13:06
        w = sc.Window(day(date(2026, 11, 20), 2), [hour(start + timedelta(hours=k)) for k in range(10)])
        marks = [sc.tide_mark(h, spot, tides) for h in w.hours]
        kept = sc.drop_bad_tide([w], spot, tides)
        self.assertIn("✗", marks)
        self.assertTrue(all(sc.tide_mark(h, spot, tides) == "✓" for k in kept for h in k.hours))
        self.assertEqual(sum(len(k.hours) for k in kept), marks.count("✓"))
        # no prefs -> unchanged
        self.assertEqual(len(sc.drop_bad_tide([w], sc.Spot(1, "X", "", TZ), tides)[0].hours), 10)

    def test_get_does_not_retry_client_errors(self):
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, \
                mock.patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                sc.get("https://example.com/x")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    def test_page_url_is_percent_encoded(self):
        data = {"WawJson": '{"Spot": {"Name": "La Gravière", "OffshoreWindHeading": {"AngleRad": 0},'
                           ' "Position": {"Latitude": 43.7, "Longitude": -1.4}}, "_WAWDays": []}',
                "SurfometreInfosXml": "<Days/>"}
        spot, _ = sc.process_spot(1284, data)
        self.assertEqual(spot.page_url, "https://www.yadusurf.com/Meteo-Surf-Report/La-Gravi%C3%A8re/1284")

    def test_ics_escaping_and_folding(self):
        line = sc.fold("DESCRIPTION:" + "é" * 100)
        for part in line.split("\r\n"):
            self.assertLessEqual(len(part.encode()), 75)
        self.assertEqual(sc.ics_escape("a,b;c\nd"), "a\\,b\\;c\\nd")


if __name__ == "__main__":
    unittest.main()
