import io
import wave

import httpx
import pytest
from fastapi import HTTPException

from core.chat.audio_transcription import AudioModel, transcribe, validate_wav


def wav(seconds=0.5, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as f:
        f.setnchannels(channels)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(b"\0\0" * int(rate * seconds) * channels)
    return output.getvalue()


def test_audio_is_validated_by_contents():
    assert validate_wav(wav()) == 0.5
    for data in (b"not audio", wav(121), wav(rate=8000), wav(channels=2), wav()[:-10]):
        with pytest.raises(HTTPException):
            validate_wav(data)


@pytest.mark.asyncio
async def test_transcription_uses_audio_wire_and_preserves_text():
    def respond(request):
        assert request.url == "http://asr.invalid/v1/audio/transcriptions"
        assert request.headers["authorization"] == "Bearer private-key"
        assert b'name="model"' in request.content
        assert b"asr-model" in request.content
        assert b"RIFF" in request.content
        return httpx.Response(200, json={"text": "你好 HugAgentOS"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await transcribe(AudioModel("http://asr.invalid/v1", "private-key", "asr-model"), wav(), client=client)
    assert result == "你好 HugAgentOS"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(401, text="private-key"), httpx.Response(200, text="bad"), httpx.Response(200, json={"text": ["bad"]})])
async def test_upstream_failure_is_sanitized(response):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(HTTPException) as e:
            await transcribe(AudioModel("http://asr.invalid/v1", "private-key", "asr-model"), wav(), client=client)
    assert "private-key" not in str(e.value.detail)


@pytest.mark.parametrize("chunk", [b"f\xfft ", b"\xa1ata"])
def test_hostile_wav_chunks_are_rejected(chunk):
    data = wav().replace(b"fmt " if chunk.startswith(b"f") else b"data", chunk, 1)
    with pytest.raises(HTTPException) as error:
        validate_wav(data)
    assert error.value.status_code == 422


@pytest.mark.parametrize("url", ["http://[bad", "http://localhost:invalid/v1", "http://localhost:999999/v1", "http://localhost:0/v1"])
def test_malformed_model_urls_are_sanitized(url):
    from core.chat.audio_transcription import transcription_url
    with pytest.raises(HTTPException) as error:
        transcription_url(url)
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_deeply_nested_upstream_json_is_sanitized():
    response = httpx.Response(200, text="[" * 20000 + "0" + "]" * 20000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(HTTPException) as error:
            await transcribe(AudioModel("http://asr.invalid/v1", "private-key", "asr-model"), wav(), client=client)
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_compression_bomb_is_rejected_before_decoding():
    class Bomb(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("compressed body must not be consumed")
            yield b""
    def respond(request):
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, headers={"content-encoding":"gzip"}, stream=Bomb())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(HTTPException) as error:
            await transcribe(AudioModel("http://asr.invalid/v1", "", "asr-model"), wav(), client=client)
    assert error.value.status_code == 502


def test_fuzzed_wav_headers_never_escape_validation():
    import random
    randomizer = random.Random(42)
    for _ in range(10000):
        data = bytearray(wav())
        for _ in range(randomizer.randint(1, 5)):
            data[randomizer.randrange(48)] = randomizer.randrange(256)
        try:
            validate_wav(bytes(data))
        except HTTPException as error:
            assert error.status_code in (413, 422)
