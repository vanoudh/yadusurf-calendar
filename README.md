# yadusurf-calendar

Turns the [YaDuSurf forecast for Le Penon](https://www.yadusurf.com/Meteo-Surf-Report/Le-Penon/1280)
into a subscribable Google Calendar feed of **good surf windows**.

Events look like `3* Le Penon 1,5m 10s E`:
stars of the day · spot · avg swell height · avg period · wind direction (where it comes from).
The description holds the YaDuSurf comment, tide coefficient, water temperature and an hour-by-hour breakdown.

## How windows are chosen

- the day is rated **≥ 1★** by YaDuSurf (forecast covers 7 days)
- hour is in daylight (30 min before sunrise → 30 min after sunset)
- swell ≥ 0.5 m
- wind quality ≥ 0.6, using the same formula as the site's wind colours
  (offshore always fine; side-shore OK up to 2 Bft; onshore only when nearly calm)
- at least 2 consecutive hours

Tune the constants at the top of `surf_calendar.py`.

## Run locally

```sh
python3 surf_calendar.py               # fetches live data, writes public/1280.ics
python3 -m unittest                    # tests
```

No dependencies; Python 3.9+.

## Publish & subscribe

1. Push this repo to GitHub (it must be public for free GitHub Pages).
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. Run the workflow once (**Actions → Update surf calendar → Run workflow**). It then refreshes every 3 hours.
4. In Google Calendar: **Other calendars → + → From URL** →
   `https://<your-user>.github.io/yadusurf-calendar/1280.ics`

Google re-fetches subscribed calendars on its own schedule (typically every 8–24 h), so the calendar may lag the forecast slightly.

Other spots: change `--spot-id` in the workflow (the number at the end of the yadusurf URL); the feed is published as `<spot-id>.ics`.
