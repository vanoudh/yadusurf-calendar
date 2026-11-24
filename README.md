# yadusurf-calendar

Turns a [YaDuSurf forecast](https://www.yadusurf.com/Meteo-Surf-Report/Le-Penon/1280)
into a subscribable Google Calendar feed of **good surf windows**.

Events look like `3* Le Penon 1,5m 10s E`:
stars of the day · spot · avg swell height · avg period · wind direction (where it comes from).
The description holds the YaDuSurf comment, tide coefficient, water temperature and an hour-by-hour breakdown,
e.g. `14:00 1,5m 11s side 9km/h ↑3,9m ✓`: time · swell · period · wind type (off / side / on, from the spot's offshore direction) · wind speed · tide · tide mark.

**Tide mark (✓ / ✗)**: ✗ when the spot's `avoid` rule in `spot_tides.csv` matches that hour, ✓ otherwise. Rules are
boolean expressions, e.g. `height > 3.2 or (height < 1.8 and swell >= 1.5) or swell > 2.5`, using:

| name | meaning |
|---|---|
| `height` | estimated water height (m above chart datum): what the sandbanks actually feel. Rough guide: < 1,8 low, 1,8–3,2 mid, > 3,2 high; a coef-80 high tide ≈ 4,2 m |
| `swell`, `period` | swell height (m) and period (s) |
| `rising` | `true` while the tide is rising (`not rising` = falling) |

with `and`, `or`, `not`, parentheses and `= != < <= > >=`. The rule is shown under the hourly lines (`✗ = …`). Rules are checked when the script starts, so a typo
fails the build instead of being ignored. The mark is information only in the normal calendars: YaDuSurf's stars ignore the
tide (their rating uses swell size, period, direction and wind), and the mark does not change which windows are chosen.

**Tide (`↑3,9m`)**: rising (↑) or falling (↓), estimated water height in metres above chart datum (≈1 m = low, ≈4 m = high spring tide).
YaDuSurf only publishes high/low tide times and coefficients (port of Le Boucau, read from the Le Penon page, so
one extra request per run); heights are estimated as `2.5 m ± 2.1 m × coef/100` (`TIDE_MEAN_LEVEL_M`, `TIDE_UNIT_M`)
and interpolated between tides with a cosine curve. Expect ±0.2–0.3 m. The tide does not affect which windows are chosen.

## How windows are chosen

- the day is rated **≥ 1★** by YaDuSurf (forecast covers 7 days)
- hour is in daylight (30 min before sunrise → 30 min after sunset)
- swell ≥ 0.5 m
- wind quality ≥ 0.6, using the same formula as the site's wind colours
  (offshore always fine; side-shore OK up to 2 Bft; onshore only when nearly calm)
- at least 2 consecutive hours

Beyond ~5 days YaDuSurf only has one data point every 3 hours (long-range model); the missing hours are
filled by linear interpolation, so later days get windows and hourly lines too (with interpolated values).

Tune the constants at the top of `surf_calendar.py`.

## Run locally

```sh
python3 surf_calendar.py --spot-id 1280 1282   # writes public/1280.ics, public/1282.ics, public/all.ics, public/best.ics
python3 -m unittest                    # tests
```

No dependencies; Python 3.9+.

## Publish & subscribe

1. Push this repo to GitHub (it must be public for free GitHub Pages).
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. Run the workflow once (**Actions → Update surf calendar → Run workflow**). It then refreshes 3 times a day (one API call per spot, 5 s apart, to stay gentle with yadusurf.com).
4. In Google Calendar: **Other calendars → + → From URL** →
   - `https://<your-user>.github.io/yadusurf-calendar/best.ics`: only the best spot at any time
   - `…/all.ics`: every spot in one calendar (best viewed one day at a time)
   - `…/<spot-id>.ics`: a single spot
   - `…/best_v2.ics`, `…/all_v2.ics`, `…/<spot-id>_v2.ics`: same calendars **without the hours matching the spot's avoid rule** (marked ✗); windows are split or dropped accordingly (still ≥ 2 h), and the best
     spot is chosen among the remaining windows. Hours with unknown tide are kept.

**Best spot**: when windows from several spots overlap, keep the one with the most stars,
then the average swell closest to 1.5 m (`IDEAL_SWELL_M`), then the longest; overlapping
windows from other spots are dropped.

Google re-fetches subscribed calendars on its own schedule (typically every 8–24 h), so the calendar may lag the forecast slightly.

Spots: edit the `--spot-id` list in the workflow (the number at the end of the yadusurf URL).
Currently: 1280 Le Penon, 1282 Les Estagnots, 1283 Les Culs Nus, 1284 La Gravière, 1285 La Nord, 1286 La Sud, 1289 Le Prévent, 1290 Le Santocha, 1291 La Piste.
