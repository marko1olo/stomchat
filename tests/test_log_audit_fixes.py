import os
import sys
import time
import json
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import runtime_guard
import main


class DummyMessageService:
    """Mock for telethon MessageService."""
    id = 12345


def test_is_test_runner_detection():
    """Проверяет, что runtime_guard изолирует тестовые логи и файлы состояния при вызове из pytest."""
    assert runtime_guard._is_test_runner() is True
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("STOMCHAT_LOG_PATH", None)
        assert runtime_guard._default_log_path() == "bot_test.log"
    assert "bot_test_heartbeat.json" in runtime_guard._default_heartbeat_path()
    assert "bot_test_summary_status.json" in runtime_guard._default_summary_status_path()
    assert "bot_test_startup_state.json" in runtime_guard._default_startup_state_path()


@pytest.mark.asyncio
async def test_summary_watchdog_ignores_pm_chat_and_non_summary():
    """
    Проверяет, что сторож саммари НЕ вызывает os._exit(79) при зависании pm_chat,
    а вместо этого безопасно очищает устаревший статус.
    """
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
    mock_status = {
        "active": True,
        "kind": "pm_chat",
        "stage": "agentrouter_request",
        "utc": stale_time,
        "pid": 999999,
    }

    with patch("runtime_guard.read_summary_status", return_value=mock_status), \
         patch("runtime_guard.clear_summary_status") as mock_clear, \
         patch("os._exit") as mock_exit, \
         patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):

        try:
            await main.summary_watchdog_task()
        except asyncio.CancelledError:
            pass

        assert not mock_exit.called, "os._exit НЕ должен вызываться для non-summary kind"
        mock_clear.assert_called_with("stale_pm_chat")


@pytest.mark.asyncio
async def test_summary_watchdog_clears_dead_pid_summary_without_exit():
    """
    Проверяет, что если статус саммари остался от мертвого процесса прошлого запуска,
    сторож очищает его без аварийного перезапуска бота.
    """
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    mock_status = {
        "active": True,
        "kind": "daily",
        "stage": "gemini_generation",
        "utc": stale_time,
        "pid": 999998,
    }

    # PID 999998 заведомо не существует
    with patch("runtime_guard.read_summary_status", return_value=mock_status), \
         patch("runtime_guard.clear_summary_status") as mock_clear, \
         patch("os._exit") as mock_exit, \
         patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):

        try:
            await main.summary_watchdog_task()
        except asyncio.CancelledError:
            pass

        assert not mock_exit.called, "os._exit НЕ должен вызываться для мертвого PID"
        mock_clear.assert_called_with("dead_pid_summary")


@pytest.mark.asyncio
async def test_summary_watchdog_triggers_exit_79_on_real_stale_summary():
    """
    Проверяет, что сторож саммари корректно триггерит os._exit(79),
    если зависло настоящее саммари (daily) в текущем живом процессе.
    """
    stale_time = (datetime.now(timezone.utc) - timedelta(seconds=main.SUMMARY_STALE_SECONDS + 100)).isoformat()
    mock_status = {
        "active": True,
        "kind": "daily",
        "stage": "gemini_generation",
        "utc": stale_time,
        "pid": os.getpid(),
    }

    with patch("runtime_guard.read_summary_status", return_value=mock_status), \
         patch("runtime_guard.dump_runtime_state"), \
         patch("os._exit") as mock_exit, \
         patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):

        try:
            await main.summary_watchdog_task()
        except asyncio.CancelledError:
            pass

        assert mock_exit.called, "os._exit должен быть вызван для зависшего саммари текущего процесса"
        assert mock_exit.call_args[0][0] == 79


@pytest.mark.asyncio
async def test_forward_to_mirror_skips_message_service():
    """Проверяет, что forward_to_mirror пропускает MessageService без ошибок."""
    from telethon.tl.patched import MessageService

    mock_msg = MagicMock(spec=MessageService)
    mock_msg.id = 182974

    with patch("main.bot_client.forward_messages") as mock_forward:
        await main.forward_to_mirror(mock_msg, target_chat_id=-100123456)
        assert not mock_forward.called


def test_acquire_single_instance_lock_retries_on_windows():
    """Проверяет, что acquire_single_instance_lock пробует несколько попыток при занятом мьютексе."""
    if sys.platform != "win32":
        pytest.skip("Windows only test")

    import ctypes
    ERROR_ALREADY_EXISTS = 183

    # Симулируем: 2 раза мьютекс занят, на 3-й освободился
    call_count = 0
    def fake_get_last_error():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return ERROR_ALREADY_EXISTS
        return 0

    with patch("ctypes.windll.kernel32.GetLastError", side_effect=fake_get_last_error), \
         patch("ctypes.windll.kernel32.CreateMutexW", return_value=1234), \
         patch("ctypes.windll.kernel32.CloseHandle"), \
         patch("time.sleep"):

        mutex = main.acquire_single_instance_lock(max_retries=5, retry_interval=0.01)
        assert mutex == 1234
        assert call_count == 3
