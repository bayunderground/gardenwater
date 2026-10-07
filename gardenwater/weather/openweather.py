"""OpenWeather provider — DISABLED for now (not registered in `service.py`).

Why disabled (live-tested 2026-10-07, see `docs/api-notes.md`):

- One Call API 4.0 (`timeline/1day`) and 3.0 (`onecall`, `day_summary`) both
  answer HTTP 401: they require the separate "One Call by Call" subscription.
- The free `data/2.5/forecast` endpoint has no daily history and our key was
  not valid there yet either, so it cannot serve the 7-day rain window.

To re-enable: subscribe the key to One Call by Call, capture a live fixture into
`tests/fixtures/openweather_timeline_1day.json`, then implement the provider per
the One Call 4.0 section of `docs/api-notes.md` and register it in
`weather/service.py`.
"""
