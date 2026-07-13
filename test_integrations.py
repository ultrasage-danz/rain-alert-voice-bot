"""Tests for fetch_minutely_forecast and generate_voice_alert. Both talk to
external APIs (OpenWeather, ElevenLabs), so these are mocked — no real
network calls or API keys are used in this test suite."""

import main


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._json_data


def test_fetch_minutely_forecast_parses_data_field(monkeypatch):
    fake_payload = {"data": [{"dt": 1234567890, "precipitation": 0.5}]}

    def fake_get(url, params=None, timeout=None):
        assert "data/4.0/onecall/timeline/1min" in url
        assert params["lat"] == 6.5244
        assert params["lon"] == 3.3792
        return FakeResponse(fake_payload)

    monkeypatch.setattr(main.requests, "get", fake_get)
    result = main.fetch_minutely_forecast(6.5244, 3.3792)
    assert result == [{"dt": 1234567890, "precipitation": 0.5}]


def test_fetch_minutely_forecast_handles_missing_data_key(monkeypatch):
    def fake_get(url, params=None, timeout=None):
        return FakeResponse({})

    monkeypatch.setattr(main.requests, "get", fake_get)
    result = main.fetch_minutely_forecast(6.5244, 3.3792)
    assert result == []


def test_generate_voice_alert_joins_audio_chunks(monkeypatch):
    fake_chunks = [b"chunk-one-", b"chunk-two"]

    def fake_convert(text, voice_id, model_id, output_format):
        assert text == "test alert text"
        return iter(fake_chunks)

    monkeypatch.setattr(main.elevenlabs_client.text_to_speech, "convert", fake_convert)
    audio = main.generate_voice_alert("test alert text")
    assert audio == b"chunk-one-chunk-two"
