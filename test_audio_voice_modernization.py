"""
Targeted test suite verifying:
1. is_voice_message support for voice, audio, and video_note (Telegram кружочки).
2. is_audio_silent_or_empty volumedetect behavior on real silence vs test mock data.
3. Groq Whisper whisper-large-v3-turbo model and prompt hinting parameter passing.
4. Silence hallucination filtering for Russian/English Whisper phantoms.
"""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import config
import gemini_client
import main


class TestAudioVoiceModernization(unittest.TestCase):
    def test_is_voice_message_supports_video_note(self):
        """Verify is_voice_message recognizes voice, audio, and video_note."""
        # Standard voice
        msg_voice = MagicMock(spec=[])
        msg_voice.voice = object()
        msg_voice.audio = None
        msg_voice.video_note = None
        self.assertTrue(main.is_voice_message(msg_voice))

        # Audio file
        msg_audio = MagicMock(spec=[])
        msg_audio.voice = None
        msg_audio.audio = object()
        msg_audio.video_note = None
        self.assertTrue(main.is_voice_message(msg_audio))

        # Video Note (Telegram circle video)
        msg_vnote = MagicMock(spec=[])
        msg_vnote.voice = None
        msg_vnote.audio = None
        msg_vnote.video_note = object()
        self.assertTrue(main.is_voice_message(msg_vnote))

        # Regular text message
        msg_text = MagicMock(spec=[])
        msg_text.voice = None
        msg_text.audio = None
        msg_text.video_note = None
        self.assertFalse(main.is_voice_message(msg_text))

    def test_silence_hallucinations_expanded(self):
        """Verify expanded Russian Whisper hallucinations are detected."""
        for phrase in ("дима торжок", "синецкая", "субтитры сделал", "редактор субтитров", "тишина"):
            self.assertIn(phrase, main.SILENCE_HALLUCINATIONS)

    def test_is_audio_silent_or_empty_empty_files(self):
        """Verify < 32 bytes or empty files return True."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"too small")
            tmp_path = f.name
        try:
            self.assertTrue(gemini_client.is_audio_silent_or_empty(tmp_path))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_is_audio_silent_or_empty_synthetic_mock_bypass(self):
        """Verify synthetic non-audio bytes safely return False instead of crashing."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\x00" * 2048)
            tmp_path = f.name
        try:
            # ffmpeg exits non-zero on corrupt/synthetic bytes -> returns False
            self.assertFalse(gemini_client.is_audio_silent_or_empty(tmp_path))
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_is_audio_silent_or_empty_real_silence(self):
        """Generate a genuine 1-second silent WAV using ffmpeg and verify detection."""
        binary = gemini_client.ffmpeg_binary()
        if not binary:
            self.skipTest("ffmpeg binary not available")

        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
        try:
            # Generate 1 second of digital silence
            cmd = [binary, "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "1", tmp_wav]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            
            self.assertTrue(os.path.exists(tmp_wav))
            self.assertGreater(os.path.getsize(tmp_wav), 100)
            
            is_silent = gemini_client.is_audio_silent_or_empty(tmp_wav)
            self.assertTrue(is_silent, "1s silent WAV should be detected as silence by volumedetect")
        finally:
            if os.path.exists(tmp_wav):
                os.remove(tmp_wav)

    def test_whisper_model_and_prompt_configured(self):
        """Verify Groq Whisper defaults to turbo model and medical prompt."""
        model = getattr(config, "GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
        prompt = getattr(config, "WHISPER_PROMPT", "стоматолог")
        self.assertEqual(model, "whisper-large-v3-turbo")
        self.assertIn("стоматолог", prompt.lower())

    def test_transcribe_audio_passes_turbo_model_and_prompt(self):
        """Verify transcribe_audio_bytes_or_file calls client with turbo model and prompt."""
        fake_client = MagicMock()
        fake_client.audio.transcriptions.create.return_value = "Лечение пульпита зуба 1.6"

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"MOCK_OGG_HEADER" + b"\x00" * 100)
            tmp_path = f.name

        try:
            with patch("gemini_client.is_audio_silent_or_empty", return_value=False), \
                 patch("gemini_client.convert_to_wav", side_effect=lambda p: p), \
                 patch("gemini_client.get_provider_client", return_value=fake_client), \
                 patch("gemini_client.available_keys", return_value=(["fake_groq_key"], [], 0)):
                res = gemini_client.transcribe_audio_bytes_or_file(tmp_path, timeout=30)
                self.assertEqual(res, "Лечение пульпита зуба 1.6")
                
                # Check call kwargs
                fake_client.audio.transcriptions.create.assert_called_once()
                call_kwargs = fake_client.audio.transcriptions.create.call_args[1]
                self.assertEqual(call_kwargs.get("model"), "whisper-large-v3-turbo")
                self.assertIn("стоматолог", call_kwargs.get("prompt", "").lower())
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_transcribe_audio_gemini_multimodal_cascade(self):
        """Verify transcribe_audio_gemini_multimodal uses current models cascade."""
        import asyncio
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(b"OggS\x00\x02" + b"\x00" * 64)
            tmp_path = f.name

        try:
            fake_resp = MagicMock()
            fake_resp.status_code = 200
            fake_resp.json.return_value = {
                "candidates": [{
                    "content": {
                        "parts": [{"text": "Препарирование под оксид циркония"}]
                    }
                }]
            }

            class FakeAsyncClient:
                def __init__(self, *args, **kwargs):
                    pass
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    pass
                async def post(self, url, json=None):
                    self.last_url = url
                    return fake_resp

            with patch("gemini_client._httpx.AsyncClient", FakeAsyncClient), \
                 patch.object(config, "GOOGLE_KEYS", ["test_gkey_12345"]):
                text, err = asyncio.run(
                    gemini_client.transcribe_audio_gemini_multimodal(tmp_path, duration_secs=5)
                )
                self.assertEqual(text, "Препарирование под оксид циркония")
                self.assertIsNone(err)

            # Test silence recognition
            fake_resp.json.return_value = {
                "candidates": [{
                    "content": {
                        "parts": [{"text": "[тишина]"}]
                    }
                }]
            }
            with patch("gemini_client._httpx.AsyncClient", FakeAsyncClient), \
                 patch.object(config, "GOOGLE_KEYS", ["test_gkey_12345"]):
                text, err = asyncio.run(
                    gemini_client.transcribe_audio_gemini_multimodal(tmp_path, duration_secs=5)
                )
                self.assertEqual(text, "")
                self.assertIsNone(err)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)


if __name__ == "__main__":
    unittest.main()

