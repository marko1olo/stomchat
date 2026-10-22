"""
Test suite: test_concurrency_ram_inquisitor.py
Red Team верификация семафоров (MAX_CONCURRENT_SUBPROCESSES <= 2),
защиты RAM 1GB Micro-VPS, очистки кэшей и утечек asyncio задач.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

# Принудительно UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import runtime_guard
import blocking_tools
import media_tools
import database


class TestConcurrencyAndRamInvariants(unittest.IsolatedAsyncioTestCase):

    async def test_01_global_subprocess_semaphore_invariant(self):
        """Инвариант 12.4: MAX_CONCURRENT_SUBPROCESSES <= 2 и общий семафор между модулями."""
        sem_rg = runtime_guard.get_subprocess_semaphore()
        self.assertIsInstance(sem_rg, asyncio.Semaphore)
        self.assertLessEqual(runtime_guard._MAX_CONCURRENT_SUBPROCESSES, 2)
        self.assertEqual(sem_rg._value, runtime_guard._MAX_CONCURRENT_SUBPROCESSES)

        # blocking_tools обязан разделять единый семафор с runtime_guard
        sem_bt = blocking_tools._get_subprocess_semaphore()
        self.assertIs(sem_rg, sem_bt, "blocking_tools must use the identical semaphore instance as runtime_guard")

    async def test_02_concurrency_limit_enforced_under_burst(self):
        """Проверка, что при одновременном запуске 5 задач семафор пропускает максимум 2 одновременно."""
        sem = runtime_guard.get_subprocess_semaphore()
        active_count = 0
        max_seen = 0
        lock = asyncio.Lock()

        async def worker():
            nonlocal active_count, max_seen
            async with sem:
                async with lock:
                    active_count += 1
                    if active_count > max_seen:
                        max_seen = active_count
                await asyncio.sleep(0.05)
                async with lock:
                    active_count -= 1

        tasks = [asyncio.create_task(worker()) for _ in range(5)]
        await asyncio.gather(*tasks)

        self.assertLessEqual(max_seen, 2, f"Expected concurrency <= 2, but saw {max_seen} concurrent workers")

    async def test_03_create_task_metadata_and_cleanup(self):
        """Проверка, что create_task регистрирует метаданные и корректно удаляет их по завершении."""
        task_name = "test_transient_probe_task"
        ran = False

        async def dummy_coro():
            nonlocal ran
            await asyncio.sleep(0.02)
            ran = True

        task = runtime_guard.create_task(dummy_coro(), name=task_name)
        self.assertIn(task, runtime_guard._ACTIVE_TASKS)
        self.assertIn(task, runtime_guard._ACTIVE_TASK_METADATA)
        self.assertEqual(runtime_guard._ACTIVE_TASK_METADATA[task]["name"], task_name)

        # Проверка get_active_tasks_report
        report = runtime_guard.get_active_tasks_report()
        names = [r["name"] for r in report]
        self.assertIn(task_name, names)

        await task
        self.assertTrue(ran)
        # После завершения задача должна удалиться из множества и словаря метаданных
        await asyncio.sleep(0.01)
        self.assertNotIn(task, runtime_guard._ACTIVE_TASKS)
        self.assertNotIn(task, runtime_guard._ACTIVE_TASK_METADATA)

    async def test_04_dump_active_tasks(self):
        """Проверка, что dump_active_tasks корректно дампит стек задач без исключений."""
        async def sleeper():
            await asyncio.sleep(1.0)

        t = runtime_guard.create_task(sleeper(), name="test_dump_probe")
        with tempfile.NamedTemporaryFile("w+", encoding="utf-8", delete=False) as tf:
            tf_name = tf.name

        try:
            with open(tf_name, "a", encoding="utf-8") as dump_file:
                runtime_guard.dump_active_tasks(dump_file)

            with open(tf_name, "r", encoding="utf-8") as dump_file:
                content = dump_file.read()

            self.assertIn("ACTIVE ASYNCIO TASKS", content)
            self.assertIn("test_dump_probe", content)
        finally:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
            if os.path.exists(tf_name):
                os.remove(tf_name)

    async def test_05_clear_runtime_memory_caches_and_sqlite_shrink(self):
        """Проверка, что вызов clear_runtime_memory_caches и shrink_memory_sync отрабатывает без ошибок."""
        media_tools._IMAGE_BASE64_CACHE[999999] = "data:image/jpeg;base64,probe"
        self.assertIn(999999, media_tools._IMAGE_BASE64_CACHE)

        runtime_guard.clear_runtime_memory_caches()
        self.assertNotIn(999999, media_tools._IMAGE_BASE64_CACHE)

        res = database.shrink_memory_sync()
        self.assertTrue(res, "SQLite shrink_memory_sync should succeed")

    async def test_06_enforce_memory_headroom_shedding(self):
        """Проверка защитного механизма памяти: отказ в подпроцессе при критическом давлении."""
        with patch.object(runtime_guard, "get_memory_usage_mb", return_value=830.0):
            allowed = runtime_guard.enforce_memory_headroom()
            self.assertFalse(allowed, "Must reject subprocess when host RAM exceeds safety ceiling (830MB >= 800MB)")

        with patch.object(runtime_guard, "get_memory_usage_mb", return_value=450.0):
            allowed = runtime_guard.enforce_memory_headroom()
            self.assertTrue(allowed, "Must allow subprocess when host RAM is normal (450MB)")

    async def test_07_media_tools_run_tool_uses_semaphore(self):
        """Проверка, что _run_tool в media_tools уважает глобальный семафор."""
        sem = runtime_guard.get_subprocess_semaphore()
        init_val = sem._value
        called_with_sem = False

        async def fake_guarded(action, file_path, timeout):
            nonlocal called_with_sem
            # Семафор должен быть захвачен во время исполнения _run_tool_guarded (_value уменьшено)
            if sem._value < init_val:
                called_with_sem = True
            return {"ok": True}, None

        with patch("media_tools._run_tool_guarded", side_effect=fake_guarded):
            res, err = await media_tools._run_tool("probe", "path", timeout=5)
            self.assertIsNone(err)
            self.assertTrue(called_with_sem, "media_tools._run_tool must be guarded by subprocess semaphore")


if __name__ == "__main__":
    unittest.main()
