"""
Shared pytest fixtures and setup.

main.py reads several required environment variables (TELEGRAM_BOT_TOKEN,
OPENWEATHER_API_KEY, ELEVENLABS_API_KEY) at import time and raises KeyError
if they're missing — that's intentional fail-fast behavior for real runs.
For tests, we set harmless dummy values here BEFORE any test module imports
main, so the module loads cleanly without needing real credentials or
network access. pytest always collects conftest.py first in a directory,
so this runs before test_*.py files are imported.
"""

import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("OPENWEATHER_API_KEY", "test-openweather-key")
os.environ.setdefault("ELEVENLABS_API_KEY", "test-elevenlabs-key")
os.environ.setdefault("ELEVENLABS_VOICE_ID", "test-voice-id")
