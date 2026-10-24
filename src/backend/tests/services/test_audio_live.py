"""Opt-in real ASR contract checks. Uses only an isolated in-memory database.

HUGAGENT_TEST_ASR_URL=http://127.0.0.1:18761/v1
HUGAGENT_TEST_ASR_SAMPLES=/tmp/hugagent-voice-samples
PYTHONPATH=src/backend .venv/bin/python -m pytest src/backend/tests/services/test_audio_live.py

The six PCM WAV fixtures are public samples and deterministic noise, described in
 internal design docs. Never point at business data.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.models.repository import assign_role, create_provider
from tests.services.test_audio_routes import setup as setup

BASE_URL = os.environ.get("HUGAGENT_TEST_ASR_URL")
SAMPLES = os.environ.get("HUGAGENT_TEST_ASR_SAMPLES")
pytestmark = pytest.mark.skipif(not BASE_URL or not SAMPLES, reason="requires opt-in local ASR and public WAV fixtures")


@pytest.mark.parametrize("name,status,fragment", [
    ("zh", 200, "甚至出现交易几乎停滞的情况"),
    ("en", 200, "writing for other people"),
    ("zh_noise_15db", 200, "甚至出现交易几乎停滞的情况"),
    ("silence", 422, "未识别到语音"),
    ("noise_only", 422, "未识别到语音"),
    ("short_100ms", 422, "时长"),
])
def test_real_asr_through_audio_route(setup, name, status, fragment):
    app, db = setup
    row = create_provider(db, display_name="Isolated ASR test", provider_type="audio",
        provider="audio_compatible", base_url=BASE_URL, api_key="", model_name="Qwen3-ASR-0.6B")
    assign_role(db, "audio_transcription", row.provider_id)
    with TestClient(app) as client:
        assert client.get("/v1/audio/status").json()["data"]["configured"] is True
        response = client.post("/v1/audio/transcriptions",
            content=(Path(SAMPLES) / f"{name}_16k.wav").read_bytes(), headers={"Content-Type": "audio/wav"})
    assert response.status_code == status, response.text
    payload = response.json()
    text = payload["data"]["text"] if status == 200 else payload["detail"]
    assert fragment in text
