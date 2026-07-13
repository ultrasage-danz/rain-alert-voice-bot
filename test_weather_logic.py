"""Tests for minutes_until_rain — the core rain-detection scan."""

import time
import pytest
import main


def make_minutely_data(precip_by_minute_offset):
    """Builds a fake One Call 4.0 timeline response. precip_by_minute_offset
    is a dict of {minutes_from_now: precipitation_mm_h}."""
    now = time.time()
    return [
        {"dt": now + offset * 60, "precipitation": precip}
        for offset, precip in precip_by_minute_offset.items()
    ]


def test_no_rain_returns_none():
    data = make_minutely_data({i: 0.0 for i in range(0, 60)})
    assert main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15) is None


def test_rain_within_lookahead_is_detected():
    data = make_minutely_data({5: 0.0, 6: 0.0, 7: 0.5, 8: 0.6})
    result = main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15)
    assert result == 7


def test_rain_beyond_lookahead_window_is_ignored():
    # Rain starts at minute 20, but we only care about the next 15.
    data = make_minutely_data({20: 1.0})
    assert main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15) is None


def test_rain_exactly_at_threshold_counts():
    data = make_minutely_data({3: 0.2})
    result = main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15)
    assert result == 3


def test_rain_just_below_threshold_does_not_count():
    data = make_minutely_data({3: 0.19})
    assert main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15) is None


def test_returns_first_matching_minute_not_the_heaviest():
    # A light drizzle at minute 4 should be reported before a heavier
    # downpour later, since it's the first minute crossing the threshold.
    data = make_minutely_data({4: 0.3, 10: 5.0})
    result = main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15)
    assert result == 4


def test_empty_forecast_returns_none():
    assert main.minutes_until_rain([], threshold_mm_h=0.2, lookahead_minutes=15) is None


def test_past_minutes_are_ignored():
    # A record with a timestamp in the past shouldn't be treated as upcoming rain.
    data = make_minutely_data({-2: 5.0, 6: 0.5})
    result = main.minutes_until_rain(data, threshold_mm_h=0.2, lookahead_minutes=15)
    assert result == 6
