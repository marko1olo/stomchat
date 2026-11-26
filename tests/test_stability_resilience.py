import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import backup_db
import config
import database
import db_worker
import gemini_client
import main
import runtime_guard


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate all state/cooldown/DB/log files into tmp_path so prod files are untouched."""
    db_file = tmp_path / "test_stom.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(runtime_guard, "SUMMARY_STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(runtime_guard, "HEARTBEAT_PATH", str(tmp_path / "hb.json"))
    monkeypatch.setattr(runtime_guard, "STARTUP_STATE_PATH", str(tmp_path / "startup.json"))
    monkeypatch.setattr(runtime_guard, "WATCHDOG_DUMP_PATH", str(tmp_path / "dump.txt"))
    monkeypatch.setattr(gemini_client, "BANNED_MODELS_FILE", str(tmp_path / "banned_models.json"))
    monkeypatch.setattr(gemini_client, "KEY_COOLDOWN_FILE", str(tmp_path / "key_cooldowns.json"))
    monkeypatch.setattr(gemini_client, "MODEL_KEY_COOLDOWN_FILE", str(tmp_path / "model_key_cooldowns.json"))
    monkeypatch.setattr(gemini_client, "MODEL_FAILURES_FILE", str(tmp_path / "model_failures.json"))
    monkeypatch.setattr(config, "GOOGLE_KEYS", [f"AIzaTestKey000000000000000000000000{i}" for i in range(8)])
    monkeypatch.setattr(config, "GROQ_KEYS", [f"gsk_testkey000000000000000000000000000000000000000{i}" for i in range(4)])
    monkeypatch.setattr(config, "AGENTROUTER_KEYS", [])
    return tmp_path


def test_gcp_per_model_429_does_not_knock_out_all_google_keys(isolated_env):
    """
    Reproduces exact prod bug from bot.log (2026-11-24 22:57 / 23:04):
    GenerateRequestsPerDayPerProjectPerModel-FreeTier on gemini-3.6-flash must NOT
    place all 8 Google keys on cooldown for healthy models like gemini-3.5-flash-lite.
    """
    err_429 = (
        "Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current quota... "
        "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, "
        "limit: 20, model: gemini-3.6-flash, description: Generate content requests per day limit exceeded', "
        "'status': 'RESOURCE_EXHAUSTED', 'details': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
        "'quotaDimensions': {'location': 'global', 'model': 'gemini-3.6-flash'}}]}}] project_number:886969145965"
    )
    assert gemini_client.is_gcp_project_quota_error(err_429) is False

    keys = list(config.GOOGLE_KEYS)
    reason = gemini_client.note_key_failure("gemini", keys[0], err_429, model_name="gemini-3.6-flash")
    assert reason == "key_rate_limited"

    # Key 0 is cooled ONLY for gemini-3.6-flash, NOT for gemini-3.5-flash-lite or other keys!
    fresh_36, _, _ = gemini_client.available_keys("gemini", keys, model_name="gemini-3.6-flash")
    fresh_35, _, _ = gemini_client.available_keys("gemini", keys, model_name="gemini-3.5-flash-lite")
    assert keys[0] not in fresh_36
    assert len(fresh_36) == 7
    assert len(fresh_35) == 8


def test_genuine_gcp_project_rpm_exhaustion_cools_pool_and_logs_accurate_seconds(isolated_env, caplog):
    err_proj = (
        "429 RESOURCE_EXHAUSTED: GenerateContentRequestsPerMinutePerProjectPerRegion exceeded"
    )
    assert gemini_client.is_gcp_project_quota_error(err_proj) is True
    with caplog.at_level(logging.INFO, logger="gemini_client"):
        reason = gemini_client.note_key_failure("gemini", config.GOOGLE_KEYS[0], err_proj, model_name="gemini-3.5-flash-lite")
    assert reason == "key_rate_limited"
    fresh, _, _ = gemini_client.available_keys("gemini", config.GOOGLE_KEYS)
    assert fresh == []
    assert any("60s cooldown" in rec.message for rec in caplog.records)
    assert not any("300s cooldown" in rec.message for rec in caplog.records)


def test_groq_tpd_model_scoped_429_keeps_other_groq_models_alive(isolated_env):
    err_tpd = (
        "Error code: 429 - {'error': {'message': 'Rate limit reached for model `qwen/qwen3.8-27b` "
        "in organization `org_123` service tier `on_demand` on tokens per day (TPD): "
        "Limit 200000, Used 199896, Requested 6415. Please try again in 45m26.352s.'}}"
    )
    keys = list(config.GROQ_KEYS)
    reason = gemini_client.note_key_failure("groq", keys[0], err_tpd, model_name="qwen/qwen3.8-27b")
    assert reason == "key_rate_limited"

    fresh_qwen, _, _ = gemini_client.available_keys("groq", keys, model_name="qwen/qwen3.8-27b")
    fresh_oss, _, _ = gemini_client.available_keys("groq", keys, model_name="openai/gpt-oss-120b")
    assert keys[0] not in fresh_qwen
    assert keys[0] in fresh_oss


def test_multimodal_stt_skips_when_all_keys_cooling(isolated_env, tmp_path):
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS" + b"\x00" * 64)
    for k in config.GOOGLE_KEYS:
        gemini_client.set_key_cooldown("gemini", k, seconds=120)

    called = []

    class BoomAsyncClient:
        def __init__(self, *args, **kwargs):
            called.append(True)

    with patch("gemini_client._httpx.AsyncClient", BoomAsyncClient):
        text, err = asyncio.run(gemini_client.transcribe_audio_gemini_multimodal(str(audio), duration_secs=3.0))

    assert text is None
    assert "cooldown" in (err or "")
    assert called == []


def test_pm_supplement_skips_cooling_keys_without_hammering(isolated_env):
    for k in config.GOOGLE_KEYS:
        gemini_client.set_key_cooldown("gemini", k, seconds=120)
    for k in config.GROQ_KEYS:
        gemini_client.set_key_cooldown("groq", k, seconds=120)

    called = []
    with patch("gemini_client.get_openai_client", side_effect=lambda *a, **kw: called.append(a)):
        res = gemini_client.generate_pm_supplement("зуб 36 болит", "проверьте каналы", timeout=30.0)

    assert res is None
    assert called == []


def test_lite_model_caps_reasoning_effort_on_tight_timeout(isolated_env):
    captured_kwargs = []

    class FakeCompletions:
        def create(self, **kwargs):
            captured_kwargs.append(kwargs)
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "OK"})()})()]})()

    class FakeClient:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": FakeCompletions()})()
        def close(self):
            pass

    with patch("gemini_client.get_openai_client", return_value=FakeClient()), \
         patch("gemini_client.cascade_for_context", return_value=[("gemini-3.5-flash-lite", "gemini")]):
        res = gemini_client.generate_text("тест", {"kind": "poll_content_gen", "thinking_level": "HIGH"}, timeout=20.0)

    assert res is not None and res.text == "OK"
    assert len(captured_kwargs) == 1
    assert captured_kwargs[0].get("reasoning_effort") in ("low", "medium")


def test_outbox_html_p_tag_sanitized_and_single_terminal_prefix(isolated_env):
    sent_payloads = []
    failed_marks = []

    async def fake_send(client, chat_id, text, parse_mode="html", timeout=25.0, op="outbox_worker:send"):
        sent_payloads.append((chat_id, text, parse_mode))
        return type("Out", (), {"ok": False, "kind": "terminal", "reason": "terminal", "error": "User is blocked", "flood_seconds": None})()

    item = {
        "id": 54,
        "chat_id": 6162895349,
        "text": "<b>Протокол</b>\n\n<p><b>Основа</b></p><p>Детали</p>",
        "parse_mode": "html",
        "retry_count": 0,
    }

    async def run_once():
        with patch("main.database.get_pending_outbox_messages", AsyncMock(side_effect=[[item], asyncio.CancelledError()])), \
             patch("main.database.mark_outbox_failed", AsyncMock(side_effect=lambda oid, msg: failed_marks.append((oid, msg)))), \
             patch("main.tg_safety.send_message", side_effect=fake_send), \
             patch("main.assistant.set_ping_opt_out", MagicMock()):
            await main.outbox_worker_task(MagicMock())

    asyncio.run(run_once())
    assert len(sent_payloads) == 1
    assert "<p>" not in sent_payloads[0][1] and "</p>" not in sent_payloads[0][1]
    assert len(failed_marks) == 1
    assert failed_marks[0][1].startswith("terminal: ")
    assert not failed_marks[0][1].startswith("terminal:terminal:")


def test_checkpoint_wal_does_not_create_missing_db_file(isolated_env):
    missing_db = Path(config.DB_PATH)
    assert not missing_db.exists()
    ok = database.checkpoint_wal(mode="TRUNCATE", max_retries=1)
    assert ok is False
    assert not missing_db.exists()


def test_backup_database_atomic_and_verified(isolated_env):
    src = Path(config.DB_PATH)
    conn = sqlite3.connect(str(src))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO t (val) VALUES ('клиника')")
    conn.commit()
    conn.close()

    dest = isolated_env / "backups" / "out.db"
    res1 = backup_db.backup_database(src_path=src, dest_path=dest)
    res2 = backup_db.backup_database(src_path=src, dest_path=dest)
    assert res1["status"] == "ok" and res2["status"] == "ok"
    assert dest.exists()
    assert not dest.with_suffix(".tmp").exists()
    ok, msg = backup_db.verify_database_integrity(dest)
    assert ok, msg


def test_acquire_single_instance_lock_exits_75_and_start_bat_handles_75():
    bat_text = Path(os.path.join(_REPO_ROOT, "start.bat")).read_text(encoding="utf-8")
    assert 'if "%BOT_EXIT%"=="75"' in bat_text

    if sys.platform == "win32":
        with patch("ctypes.windll.kernel32.CreateMutexW", return_value=999), \
             patch("ctypes.windll.kernel32.GetLastError", return_value=183), \
             patch("ctypes.windll.kernel32.CloseHandle"), \
             patch("time.sleep"):
            with pytest.raises(SystemExit) as exc_info:
                main.acquire_single_instance_lock(max_retries=2, retry_interval=0.01)
            assert exc_info.value.code == 75

        proc = subprocess.run(
            ["cmd", "/c", os.path.join(_REPO_ROOT, "start.bat")],
            env={**os.environ, "STOMCHAT_DRY_RUN": "1"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0


def test_runtime_guard_utf8_and_loop_scoped_semaphore(isolated_env, tmp_path):
    log_path = tmp_path / "utf8_test.log"
    orig_log = runtime_guard.LOG_PATH
    try:
        runtime_guard.LOG_PATH = str(log_path)
        runtime_guard.configure_logging()
        logging.getLogger("stability_test").info("Кириллица без кракозябр: пульпит 3.6")
        for h in logging.getLogger().handlers:
            h.flush()
        content = log_path.read_text(encoding="utf-8")
        assert "Кириллица без кракозябр: пульпит 3.6" in content
    finally:
        runtime_guard.LOG_PATH = orig_log
        runtime_guard.configure_logging()

    async def get_sem():
        return runtime_guard.get_subprocess_semaphore()

    sem1 = asyncio.run(get_sem())
    sem2 = asyncio.run(get_sem())
    assert sem1 is not sem2
