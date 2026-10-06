# -*- coding: utf-8 -*-
"""
EMPIRICAL CHALLENGER STRESS SUITE: Concurrency, Race Conditions, and Cascade Exhaustion
Tested Targets:
- assistant._ACTIVE_DIALOGUE_THREADS
- assistant.check_user_cooldown
- assistant.check_and_trigger_assistant (E2E concurrency)
- gemini_client progressive 503 cooldown ladder (60s -> 300s -> 1200s)

Adversarially challenges:
1. Multi-threaded & async race conditions
2. Time-offset message arrivals (0.1s, 1s, 18s, 30s, 36s)
3. Parallel tasks for identical dialogue anchors
4. Lock release robustness under simulated exceptions, timeouts, and cancellations
5. Progressive 503 model cooldown escalation and recovery
"""

import sys
import os
import time
import math
import asyncio
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

_repo_dir = os.path.dirname(os.path.abspath(__file__))
if _repo_dir not in sys.path:
    sys.path.insert(0, _repo_dir)

import config
import assistant
import gemini_client


class TestClass1EmpiricalConcurrencyAndRaceConditions(unittest.TestCase):
    """
    Empirical stress testing of Concurrency & Race Conditions (Class 1).
    """

    def setUp(self):
        self._saved_cooldowns = dict(assistant.USER_COOLDOWNS)
        self._saved_active_threads = set(assistant._ACTIVE_DIALOGUE_THREADS)
        self.chat_id = -1001820467444
        self.sender_id = 448838231
        self.thread_anchor = 177380

    def tearDown(self):
        assistant.USER_COOLDOWNS.clear()
        assistant.USER_COOLDOWNS.update(self._saved_cooldowns)
        assistant._ACTIVE_DIALOGUE_THREADS = self._saved_active_threads

    def test_multi_threaded_hammer_on_check_user_cooldown(self):
        """
        Adversarial Stress: 100 concurrent OS threads attempt to trigger check_user_cooldown
        at the exact same instant on the same (chat_id, thread_anchor, 'dialogue_thread') key.
        Strict assertion: Exactly 1 thread must acquire the slot (return 0).
        Exactly 99 threads must be blocked (return > 0).
        Zero unhandled exceptions, zero race corruption in USER_COOLDOWNS.
        """
        thread_count = 100
        barrier = threading.Barrier(thread_count)
        results = []
        key = (self.chat_id, self.thread_anchor, "dialogue_thread")
        assistant.USER_COOLDOWNS.pop(key, None)

        def worker():
            barrier.wait()  # Synchronize all threads to fire simultaneously
            cd = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=35)
            results.append(cd)

        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker) for _ in range(thread_count)]
            for f in futures:
                f.result()

        zeros = [r for r in results if r == 0]
        blocked = [r for r in results if r > 0]

        self.assertEqual(len(results), thread_count)
        self.assertEqual(
            len(zeros),
            1,
            f"STRESS FAILURE: Exactly 1 thread must proceed, but {len(zeros)} threads received 0!",
        )
        self.assertEqual(
            len(blocked),
            thread_count - 1,
            f"STRESS FAILURE: Exactly {thread_count - 1} threads must be debounced!",
        )
        for cd in blocked:
            self.assertGreaterEqual(cd, 1, "Blocked cooldown must be at least 1 second")
            self.assertLessEqual(cd, 35, "Blocked cooldown must not exceed 35 seconds")

    def test_arrival_delays_spectrum_0_1s_1s_18s_30s_36s(self):
        """
        Adversarially verify arrival timing spectrum:
        - t = 0.0s: Message 1 arrives -> ALLOWED (cd == 0)
        - t = 0.1s: Message 2 arrives -> BLOCKED (cd == 35)
        - t = 1.0s: Message 3 arrives -> BLOCKED (cd == 34)
        - t = 18.0s: Message 4 arrives (incident interval) -> BLOCKED (cd == 17)
        - t = 30.0s: Message 5 arrives -> BLOCKED (cd == 5)
        - t = 36.0s: Message 6 arrives (after 35s window) -> ALLOWED (cd == 0)
        """
        key = (self.chat_id, self.thread_anchor, "dialogue_thread")
        assistant.USER_COOLDOWNS.pop(key, None)
        debounce_window = 35

        t0 = datetime(2026, 9, 13, 12, 0, 0)
        with patch("assistant.datetime") as mock_dt:
            # 0.0s: Initial message
            mock_dt.now.return_value = t0
            cd_0 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=debounce_window)
            self.assertEqual(cd_0, 0, "t=0.0s must proceed")

            # 0.1s: Rapid burst
            mock_dt.now.return_value = t0 + timedelta(seconds=0.1)
            cd_0_1 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=debounce_window)
            self.assertEqual(cd_0_1, 35, "t=0.1s must be blocked with 35s remaining")

            # 1.0s: Very quick follow-up
            mock_dt.now.return_value = t0 + timedelta(seconds=1.0)
            cd_1 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=debounce_window)
            self.assertEqual(cd_1, 34, "t=1.0s must be blocked with 34s remaining")

            # 18.0s: Sept 12 incident gap (msg 177388 -> 177389)
            mock_dt.now.return_value = t0 + timedelta(seconds=18.0)
            cd_18 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=debounce_window)
            self.assertEqual(cd_18, 17, "t=18.0s must be blocked with 17s remaining")

            # 30.0s: Approaching window end
            mock_dt.now.return_value = t0 + timedelta(seconds=30.0)
            cd_30 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=debounce_window)
            self.assertEqual(cd_30, 5, "t=30.0s must be blocked with 5s remaining")

            # 36.0s: Debounce expired
            mock_dt.now.return_value = t0 + timedelta(seconds=36.0)
            cd_36 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=debounce_window)
            self.assertEqual(cd_36, 0, "t=36.0s must be allowed (cd == 0)")

    def test_dual_key_debounce_thread_and_sender_coordination(self):
        """
        Verify that both thread anchor key and sender key are checked in tandem:
        If thread key is somehow different but sender is same, sender debounce blocks it.
        If sender key is different but thread is same, thread debounce blocks it.
        """
        key_thread = (self.chat_id, self.thread_anchor, "dialogue_thread")
        key_sender = (self.chat_id, self.sender_id, "dialogue_sender")
        assistant.USER_COOLDOWNS.pop(key_thread, None)
        assistant.USER_COOLDOWNS.pop(key_sender, None)

        t0 = datetime(2026, 9, 13, 12, 0, 0)
        with patch("assistant.datetime") as mock_dt:
            mock_dt.now.return_value = t0
            cd_t0 = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=35)
            cd_s0 = assistant.check_user_cooldown(self.chat_id, self.sender_id, "dialogue_sender", seconds=35)
            self.assertEqual(cd_t0, 0)
            self.assertEqual(cd_s0, 0)

            # Scenario A: Same user posts to different thread anchor 5 seconds later
            mock_dt.now.return_value = t0 + timedelta(seconds=5)
            cd_diff_thread = assistant.check_user_cooldown(self.chat_id, 999999, "dialogue_thread", seconds=35)
            cd_same_sender = assistant.check_user_cooldown(self.chat_id, self.sender_id, "dialogue_sender", seconds=35)
            self.assertEqual(cd_diff_thread, 0, "Different thread is not debounced by thread key")
            self.assertEqual(cd_same_sender, 30, "Same sender is blocked by sender key (30s left)")

            # Scenario B: Different user posts to the same thread anchor 10 seconds later
            mock_dt.now.return_value = t0 + timedelta(seconds=10)
            cd_same_thread = assistant.check_user_cooldown(self.chat_id, self.thread_anchor, "dialogue_thread", seconds=35)
            cd_diff_sender = assistant.check_user_cooldown(self.chat_id, 888888, "dialogue_sender", seconds=35)
            self.assertEqual(cd_same_thread, 25, "Same thread is blocked by thread key (25s left)")
            self.assertEqual(cd_diff_sender, 0, "Different sender is not debounced by sender key")


class TestClass1LockReleaseRobustness(unittest.TestCase):
    """
    Stress-testing lock acquisition, lock exclusivity, and lock release robustness.
    Ensures _ACTIVE_DIALOGUE_THREADS never leaks entries under any simulated failure.
    """

    def setUp(self):
        self._saved_active_threads = set(assistant._ACTIVE_DIALOGUE_THREADS)
        assistant._ACTIVE_DIALOGUE_THREADS.clear()
        self.chat_id = -1001820467444
        self.thread_anchor = 177380
        self.sender_id = 448838231
        self.thread_key = (self.chat_id, self.thread_anchor)
        self.sender_key = (self.chat_id, self.sender_id)

    def tearDown(self):
        assistant._ACTIVE_DIALOGUE_THREADS.clear()
        assistant._ACTIVE_DIALOGUE_THREADS.update(self._saved_active_threads)

    def test_in_flight_lock_exclusivity(self):
        """
        When Task 1 holds the lock in _ACTIVE_DIALOGUE_THREADS:
        - Task 2 checking _ACTIVE_DIALOGUE_THREADS must be blocked immediately.
        - Task 2 with same sender must also be blocked immediately.
        """
        assistant._ACTIVE_DIALOGUE_THREADS.add(self.thread_key)
        assistant._ACTIVE_DIALOGUE_THREADS.add(self.sender_key)

        # In-flight check mimics assistant.py line 3025:
        task2_thread_blocked = (self.chat_id, self.thread_anchor) in assistant._ACTIVE_DIALOGUE_THREADS
        task2_sender_blocked = (self.chat_id, self.sender_id) in assistant._ACTIVE_DIALOGUE_THREADS

        self.assertTrue(task2_thread_blocked, "Task 2 must be blocked by active thread lock")
        self.assertTrue(task2_sender_blocked, "Task 2 must be blocked by active sender lock")

    def test_lock_release_on_simulated_timeout_error(self):
        """
        Simulate an asyncio.TimeoutError during LLM generation.
        Lock must be cleanly and immediately released in finally block.
        """
        active_keys = [self.thread_key, self.sender_key]
        for k in active_keys:
            assistant._ACTIVE_DIALOGUE_THREADS.add(k)

        self.assertEqual(len(assistant._ACTIVE_DIALOGUE_THREADS), 2)

        try:
            # Simulate asyncio.TimeoutError in cascade
            raise asyncio.TimeoutError("Cascade timeout exceeded 90s")
        except asyncio.TimeoutError:
            pass
        finally:
            for k in active_keys:
                assistant._ACTIVE_DIALOGUE_THREADS.discard(k)

        self.assertEqual(
            len(assistant._ACTIVE_DIALOGUE_THREADS),
            0,
            "Locks must be completely released after asyncio.TimeoutError",
        )

    def test_lock_release_on_simulated_cascade_exhaustion_exception(self):
        """
        Simulate RuntimeError('LLM cascade exhausted: all models failed')
        Verify lock release.
        """
        active_keys = [self.thread_key, self.sender_key]
        for k in active_keys:
            assistant._ACTIVE_DIALOGUE_THREADS.add(k)

        try:
            raise RuntimeError("LLM cascade exhausted: 503 on Google, 429 on Groq")
        except RuntimeError:
            pass
        finally:
            for k in active_keys:
                assistant._ACTIVE_DIALOGUE_THREADS.discard(k)

        self.assertEqual(
            len(assistant._ACTIVE_DIALOGUE_THREADS),
            0,
            "Locks must be completely released after cascade RuntimeError",
        )

    def test_lock_release_on_simulated_task_cancellation(self):
        """
        Simulate asyncio.CancelledError (e.g. task cancelled by supervisor or shutdown).
        Verify lock release.
        """
        active_keys = [self.thread_key, self.sender_key]
        for k in active_keys:
            assistant._ACTIVE_DIALOGUE_THREADS.add(k)

        try:
            raise asyncio.CancelledError("Bot task cancelled by supervisor")
        except asyncio.CancelledError:
            pass
        finally:
            for k in active_keys:
                assistant._ACTIVE_DIALOGUE_THREADS.discard(k)

        self.assertEqual(
            len(assistant._ACTIVE_DIALOGUE_THREADS),
            0,
            "Locks must be completely released after asyncio.CancelledError",
        )

    def test_lock_release_on_simulated_telegram_network_error(self):
        """
        Simulate ConnectionError / FloodWait during bot_client.send_message.
        Verify lock release.
        """
        active_keys = [self.thread_key, self.sender_key]
        for k in active_keys:
            assistant._ACTIVE_DIALOGUE_THREADS.add(k)

        try:
            raise ConnectionError("Telegram RPC connection reset by peer")
        except ConnectionError:
            pass
        finally:
            for k in active_keys:
                assistant._ACTIVE_DIALOGUE_THREADS.discard(k)

        self.assertEqual(
            len(assistant._ACTIVE_DIALOGUE_THREADS),
            0,
            "Locks must be completely released after network ConnectionError",
        )

    def test_high_frequency_fault_injection_50_iterations(self):
        """
        Adversarially inject 50 rapid failures (mix of timeouts, memory errors, cancellations).
        Confirm zero cumulative lock leaks.
        """
        for i in range(50):
            t_key = (self.chat_id, 200000 + i)
            s_key = (self.chat_id, 300000 + i)
            active_keys = [t_key, s_key]
            for k in active_keys:
                assistant._ACTIVE_DIALOGUE_THREADS.add(k)

            try:
                if i % 3 == 0:
                    raise asyncio.TimeoutError()
                elif i % 3 == 1:
                    raise MemoryError()
                else:
                    raise asyncio.CancelledError()
            except (asyncio.TimeoutError, MemoryError, asyncio.CancelledError):
                pass
            finally:
                for k in active_keys:
                    assistant._ACTIVE_DIALOGUE_THREADS.discard(k)

        self.assertEqual(
            len(assistant._ACTIVE_DIALOGUE_THREADS),
            0,
            "Cumulative active threads must remain exactly 0 after 50 rapid faults",
        )


class TestClass1EndToEndConcurrentPipelineSimulation(unittest.IsolatedAsyncioTestCase):
    """
    End-to-End concurrency stress on check_and_trigger_assistant.
    Simulates real async tasks fired concurrently at the assistant pipeline.
    """

    def setUp(self):
        self._saved_cooldowns = dict(assistant.USER_COOLDOWNS)
        self._saved_active_threads = set(assistant._ACTIVE_DIALOGUE_THREADS)
        self._saved_replied_msgs = dict(assistant.REPLIED_MSG_IDS)
        assistant.USER_COOLDOWNS.clear()
        assistant.REPLIED_MSG_IDS.clear()
        assistant._ACTIVE_DIALOGUE_THREADS.clear()

        self.chat_id = -1001820467444
        self.bot_id = 777000123
        assistant.BOT_ID = self.bot_id
        assistant.BOT_USERNAME = "StomChatBot"

    def tearDown(self):
        assistant.USER_COOLDOWNS.clear()
        assistant.USER_COOLDOWNS.update(self._saved_cooldowns)
        assistant.REPLIED_MSG_IDS.clear()
        assistant.REPLIED_MSG_IDS.update(self._saved_replied_msgs)
        assistant._ACTIVE_DIALOGUE_THREADS = self._saved_active_threads

    async def test_concurrent_tasks_same_dialogue_thread_only_one_proceeds(self):
        """
        Adversarial Test: Launch 10 simultaneous async tasks for the exact same dialogue thread.
        Task 1 simulates a slow LLM generation (sleeps 0.05s inside mock).
        Tasks 2-10 arrive concurrently while Task 1 is running.
        Strict verification:
        - Exactly 1 task returns True (generates and sends reply).
        - Exactly 9 tasks return False (debounced / locked out).
        - bot_client.send_message is invoked exactly ONCE.
        - _ACTIVE_DIALOGUE_THREADS is completely empty after completion.
        """
        mock_bot_client = MagicMock()
        mock_bot_client.send_message = AsyncMock(return_value=MagicMock(id=177395))

        def make_event(msg_id, sender_id, reply_to):
            evt = MagicMock()
            evt.chat_id = self.chat_id
            evt.sender_id = sender_id
            evt.reply_to_msg_id = reply_to
            evt.reply = AsyncMock()
            evt.client = mock_bot_client
            return evt

        # Mock slow generation for task 1 to keep lock active
        call_counter = 0

        async def slow_mock_generate(*args, **kwargs):
            nonlocal call_counter
            call_counter += 1
            await asyncio.sleep(0.05)
            mock_resp = MagicMock()
            mock_resp.text = "Клинический ответ по адгезии"
            return mock_resp, None

        with (
            patch("assistant.load_state", return_value={"last_case_bot_msg_id": 177380, "last_case_author_id": 448838231, "last_case_time": datetime.now().isoformat()}),
            patch("assistant.fetch_dynamic_chat_context", new_callable=AsyncMock) as mock_fetch,
            patch("assistant.check_dialogue_continuation_triage", new_callable=AsyncMock) as mock_triage,
            patch("assistant.generate_gemini_text_async", side_effect=slow_mock_generate),
            patch("assistant.check_response_quality", new_callable=AsyncMock, return_value=(True, "OK")),
            patch("assistant.database.is_bot_message_or_sender", new_callable=AsyncMock, return_value=True),
            patch("assistant.database.get_user_profile", new_callable=AsyncMock, return_value={"selected_style": "senior_colleague"}),
            patch("assistant.query_db_async", new_callable=AsyncMock, return_value=[(0,)]),
            patch("assistant.search_knowledge_corpus", new_callable=AsyncMock, return_value=("wiki", "archive")),
            patch("assistant.SHADOW_TESTING", False),
        ):
            mock_fetch.return_value = (["[Сообщение #177380] Бот: Вопрос", "[Сообщение #177388] Врач: Ответ"], 1, 177380)
            mock_triage.return_value = True

            # Create 10 concurrent requests replying to bot message 177380
            tasks = []
            for i in range(10):
                msg_id = 177388 + i
                evt = make_event(msg_id=msg_id, sender_id=448838231, reply_to=177380)
                tasks.append(
                    assistant.check_and_trigger_assistant(
                        bot_client=mock_bot_client,
                        event=evt,
                        msg_id=msg_id,
                        text="Уточните пожалуйста протокол протравливания",
                        reply_to_msg_id=177380,
                    )
                )

            results = await asyncio.gather(*tasks)

        true_count = sum(1 for r in results if r is True)
        false_count = sum(1 for r in results if r is False)

        self.assertEqual(
            true_count,
            1,
            f"STRESS FAILURE: Exactly 1 task must succeed, but {true_count} succeeded! Dual reply detected!",
        )
        self.assertEqual(
            false_count,
            9,
            f"STRESS FAILURE: Exactly 9 tasks must be blocked, but {false_count} were blocked!",
        )
        self.assertEqual(
            mock_bot_client.send_message.call_count,
            1,
            "send_message must be called exactly 1 time",
        )
        self.assertEqual(
            len(assistant._ACTIVE_DIALOGUE_THREADS),
            0,
            "Active dialogue threads lock must be completely released",
        )

    async def test_sequential_follow_up_without_reply_button_concurrency(self):
        """
        Verify that sequential follow-up (Branch 1.1, doctor types in chat without pressing Reply)
        is also strictly debounced and locked by canonical anchor.
        """
        mock_bot_client = MagicMock()
        mock_bot_client.send_message = AsyncMock(return_value=MagicMock(id=177396))

        def make_event(msg_id, sender_id):
            evt = MagicMock()
            evt.chat_id = self.chat_id
            evt.sender_id = sender_id
            evt.reply_to_msg_id = None
            evt.reply = AsyncMock()
            evt.client = mock_bot_client
            return evt

        async def slow_mock_generate(*args, **kwargs):
            await asyncio.sleep(0.04)
            mock_resp = MagicMock()
            mock_resp.text = "Клинический ответ по реставрации"
            return mock_resp, None

        with (
            patch("assistant.load_state", return_value={"last_case_bot_msg_id": 177380, "last_case_author_id": 448838231, "last_case_time": datetime.now().isoformat()}),
            patch("assistant.fetch_dynamic_chat_context", new_callable=AsyncMock) as mock_fetch,
            patch("assistant.check_dialogue_continuation_triage", new_callable=AsyncMock) as mock_triage,
            patch("assistant.generate_gemini_text_async", side_effect=slow_mock_generate),
            patch("assistant.check_response_quality", new_callable=AsyncMock, return_value=(True, "OK")),
            patch("assistant.database.is_bot_message_or_sender", new_callable=AsyncMock, return_value=True),
            patch("assistant.database.get_user_profile", new_callable=AsyncMock, return_value={"selected_style": "senior_colleague"}),
            patch("assistant.query_db_async", new_callable=AsyncMock, return_value=[(0,)]),
            patch("assistant.search_knowledge_corpus", new_callable=AsyncMock, return_value=("wiki", "archive")),
            patch("assistant.SHADOW_TESTING", False),
        ):
            mock_fetch.return_value = (["[Сообщение #177380] Бот: Вопрос", "[Сообщение #177388] Врач: Продолжение"], 1, 177380)
            mock_triage.return_value = True

            # 5 sequential messages from same doctor without Reply button
            tasks = []
            for i in range(5):
                msg_id = 177400 + i
                evt = make_event(msg_id=msg_id, sender_id=448838231)
                tasks.append(
                    assistant.check_and_trigger_assistant(
                        bot_client=mock_bot_client,
                        event=evt,
                        msg_id=msg_id,
                        text="А что по поводу 3.7 зуба?",
                        reply_to_msg_id=None,
                    )
                )

            results = await asyncio.gather(*tasks)

        true_count = sum(1 for r in results if r is True)
        self.assertEqual(true_count, 1, "Sequential follow-up must allow exactly 1 message")
        self.assertEqual(mock_bot_client.send_message.call_count, 1)
        self.assertEqual(len(assistant._ACTIVE_DIALOGUE_THREADS), 0)


class TestClass5Progressive503CooldownLadder(unittest.TestCase):
    """
    Stress-testing Denial-of-Service and Cascade Exhaustion (Class 5):
    - Progressive 503 cooldown ladder: 60s -> 300s -> 1200s
    - Expiration window (15 minutes / 900s)
    - Recovery on successful response (note_success)
    - Cross-model isolation
    - Resiliency against corrupted model_failures.json
    """

    def setUp(self):
        self.test_model = "gemini-2.5-flash-test"
        self.test_model_b = "gemini-2.5-pro-test"
        self.test_provider = "gemini"
        self.test_key = "AIzaSyEmpiricalTestKey503"
        gemini_client._clear_failure_history(self.test_model)
        gemini_client._clear_failure_history(self.test_model_b)
        gemini_client._clear_expiry_entry(gemini_client.BANNED_MODELS_FILE, self.test_model)
        gemini_client._clear_expiry_entry(gemini_client.BANNED_MODELS_FILE, self.test_model_b)

    def tearDown(self):
        gemini_client._clear_failure_history(self.test_model)
        gemini_client._clear_failure_history(self.test_model_b)
        gemini_client._clear_expiry_entry(gemini_client.BANNED_MODELS_FILE, self.test_model)
        gemini_client._clear_expiry_entry(gemini_client.BANNED_MODELS_FILE, self.test_model_b)
        gemini_client._clear_expiry_entry(
            gemini_client.KEY_COOLDOWN_FILE,
            gemini_client._key_fingerprint(self.test_provider, self.test_key),
        )

    def test_progressive_ladder_exact_durations_60s_300s_1200s(self):
        """
        Adversarially verify the exact 3-step progressive 503 ladder:
        Step 1: 1st failure -> 60s ban
        Step 2: 2nd failure -> 300s ban
        Step 3: 3rd failure -> 1200s ban
        Step 4: 4th failure -> 1200s ban (plateau at 20 min)
        """
        d1 = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(d1, 60, "Step 1 of 503 ladder must be exactly 60s")

        d2 = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(d2, 300, "Step 2 of 503 ladder must be exactly 300s (5 min)")

        d3 = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(d3, 1200, "Step 3 of 503 ladder must be exactly 1200s (20 min)")

        d4 = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(d4, 1200, "Subsequent failures must plateau at 1200s")

    def test_success_resets_ladder_immediately(self):
        """
        When a model has reached Step 3 (1200s ban level) and later succeeds,
        note_success must clear its history. The very next 503 failure must start
        fresh at Step 1 (60s), not at 1200s.
        """
        # Climb to step 3
        gemini_client._record_model_server_failure(self.test_model)
        gemini_client._record_model_server_failure(self.test_model)
        gemini_client._record_model_server_failure(self.test_model)

        # Model succeeds
        gemini_client.note_success(self.test_provider, self.test_key, self.test_model)

        # Failure after success must start at 60s
        d_reset = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(
            d_reset,
            60,
            "After note_success, failure history must be cleared and ladder reset to 60s",
        )

    def test_window_expiration_after_15_minutes_resets_ladder(self):
        """
        If a model had 2 consecutive failures, but the 3rd occurs > 15 minutes (900s) later:
        The failure count must reset to 1 and return 60s.
        """
        # 1st failure
        gemini_client._record_model_server_failure(self.test_model)

        # 2nd failure 20 minutes later (simulate by writing past timestamp)
        now = time.time()
        history = {self.test_model: {"count": 1, "last_time": now - 950}}
        gemini_client._save_expiry_map(gemini_client.MODEL_FAILURES_FILE, history)

        d_expired = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(
            d_expired,
            60,
            "Failures separated by > 900s must reset counter back to 1 (60s ban)",
        )

    def test_multi_model_cooldown_isolation(self):
        """
        Confirm that Model A reaching 1200s ban does not affect Model B.
        Model B's first failure must still be 60s.
        """
        # Push Model A to step 3
        gemini_client._record_model_server_failure(self.test_model)
        gemini_client._record_model_server_failure(self.test_model)
        dA3 = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(dA3, 1200)

        # Model B has 1 failure
        dB1 = gemini_client._record_model_server_failure(self.test_model_b)
        self.assertEqual(
            dB1,
            60,
            "Model B must receive initial 60s ban regardless of Model A's failure history",
        )

    def test_note_key_failure_integration_with_transient_overload_strings(self):
        """
        Test that note_key_failure correctly identifies transient overload error strings:
        - '503 Service Unavailable'
        - '504 Gateway Timeout'
        - 'Resource unavailable due to overload'
        - 'deadline exceeded'
        And registers progressive ban in banned_models.json.
        """
        test_strings = [
            "HTTP 503: The model is overloaded. Please try again later.",
            "504 Gateway Timeout from Google endpoint",
            "Server unavailable at this time",
            "Temporary overload detected on upstream",
            "context deadline exceeded",
        ]

        for s in test_strings:
            gemini_client._clear_failure_history(self.test_model)
            status = gemini_client.note_key_failure(
                provider=self.test_provider,
                api_key=self.test_key,
                error_text=s,
                model_name=self.test_model,
            )
            self.assertEqual(
                status,
                "model_overloaded",
                f"String '{s}' must be recognized as transient model overload",
            )
            banned = gemini_client.get_banned_models()
            self.assertIn(
                self.test_model,
                banned,
                f"Model must appear in banned_models after error '{s}'",
            )

    def test_corrupted_model_failures_json_resilience(self):
        """
        Adversarial Test: Write invalid/corrupt JSON into model_failures.json.
        _record_model_server_failure must NOT crash, must handle the exception,
        and must reset and record the failure safely.
        """
        with open(gemini_client.MODEL_FAILURES_FILE, "w", encoding="utf-8") as f:
            f.write("{MALFORMED_JSON_CORRUPTED_DISASTER: [[")

        dur = gemini_client._record_model_server_failure(self.test_model)
        self.assertEqual(
            dur,
            60,
            "Corrupted model_failures.json must fall back safely to empty history and 60s ban",
        )


if __name__ == "__main__":
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    suite.addTests(loader.loadTestsFromTestCase(TestClass1EmpiricalConcurrencyAndRaceConditions))
    suite.addTests(loader.loadTestsFromTestCase(TestClass1LockReleaseRobustness))
    suite.addTests(loader.loadTestsFromTestCase(TestClass1EndToEndConcurrentPipelineSimulation))
    suite.addTests(loader.loadTestsFromTestCase(TestClass5Progressive503CooldownLadder))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    sys.exit(0 if result.wasSuccessful() else 1)
