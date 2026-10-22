"""
Strict Automated Test Suite: Clinician Memory Hygiene & DB Invariants.
Red Team verification across StomChat and EndoChat:
1. Portrait Sanitization: Clean profile portraits (zero hanging colons/suffixes, zero stuttering, zero markdown artifacts).
2. Fact Hygiene & Medical Safety: Strict filtration of chat-bot meta facts, off-topic chatter, negative meta sentences;
   100% preservation of clinical endodontic/surgical/restorative facts (BioRoot, ProRoot, root canals, FDI notation).
3. Specialty Integrity & Hierarchy: Rejection of generic/invalid specialties, resolution hierarchy protection.
4. Live DB Invariant Auditing: Zero residual anomalies in live sqlite database (stomat_bot.db / endo_bot.db).

Execution: python tests/test_clinician_memory_hygiene.py
"""

import os
import sys
import re
import json
import sqlite3
import unittest
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import user_memory
import database


class TestClinicianPortraitHygiene(unittest.TestCase):
    """Тестирование очистки и дедупликации профильных портретов (profile_portrait)."""

    def test_hanging_colon_and_suffix_elimination(self):
        """Проверяет устранение висячих двоеточий и заголовков 'Специализация:' на концах и в начале."""
        cases = [
            ("Стоматолог-терапевт. Специализация:", "Стоматолог-терапевт"),
            ("Врач-эндодонтист. Специализация: ", "Врач-эндодонтист"),
            ("Стоматолог-ортопед. Клинический профиль:", "Стоматолог-ортопед"),
            ("Хирург-имплантолог.   Специализация:   ", "Хирург-имплантолог"),
            ("Специализация: Детский стоматолог", "Детский стоматолог"),
            ("**Специализация:** Стоматолог-терапевт", "Стоматолог-терапевт"),
            ("### Клинический профиль:\nОртодонт", "Ортодонт"),
            ("Терапевт. Специализация: *", "Терапевт"),
        ]
        for raw, expected in cases:
            cleaned = user_memory.clean_profile_portrait(raw)
            self.assertEqual(cleaned, expected, f"Failed on raw: {raw!r}")

    def test_stuttering_elimination(self):
        """Проверяет устранение заиканий вида {X}. Специализация: {X} и generic-overwrites."""
        cases = [
            # Точный дубль
            (
                "Стоматолог-ортопед. Специализация: Стоматолог-ортопед",
                "Стоматолог-ортопед"
            ),
            # Дубль с префиксом
            (
                "Стоматология: ортопедия и терапевтическая стоматология. Специализация: Стоматология: ортопедия и терапевтическая стоматология",
                "Стоматология: ортопедия и терапевтическая стоматология"
            ),
            # Generic слева, конкретная специальность справа
            (
                "Стоматолог. Специализация: Стоматолог-хирург",
                "Стоматолог-хирург"
            ),
            # Конкретная специальность слева, generic справа
            (
                "Стоматолог-эндодонтист. Специализация: Стоматолог",
                "Стоматолог-эндодонтист"
            ),
            # Две разные узкие специализации объединяются через точку
            (
                "Стоматолог-терапевт. Специализация: Стоматолог-ортопед",
                "Стоматолог-терапевт. Стоматолог-ортопед"
            ),
        ]
        for raw, expected in cases:
            cleaned = user_memory.clean_profile_portrait(raw)
            self.assertEqual(cleaned, expected, f"Stutter elimination failed on: {raw!r}")

    def test_pure_header_fallback(self):
        """Если портрет содержит только заголовок, он восстанавливается из specialty/summary."""
        cleaned = user_memory.clean_profile_portrait(
            "Специализация:",
            specialty="Стоматолог-ортопед",
            summary="Клинический протокол: фиксация коронок на Variolink Esthetic."
        )
        self.assertEqual(cleaned, "Стоматолог-ортопед")

        # Если specialty generic, берет первую строку из summary
        cleaned_gen = user_memory.clean_profile_portrait(
            "Специализация:",
            specialty="Стоматолог",
            summary="Работает под микроскопом с увеличением 16x."
        )
        self.assertEqual(cleaned_gen, "Работает под микроскопом с увеличением 16x.")


class TestClinicianFactHygiene(unittest.TestCase):
    """Тестирование очистки фактов (clean_clinician_facts) и защиты клинических терминов."""

    def test_bot_and_chat_meta_facts_filtered(self):
        """Все факты об общении с ботом, чат-ботах и флуде в чате должны блокироваться."""
        meta_facts = [
            "Считает чат-бота полезным справочником и интересуется, обучен ли он на сообщениях чата.",
            "В чате обнаружил, что собеседник оказался ботом.",
            "Связал быстрые и многочисленные ответы собеседника с тем, что это бот.",
            "Считает, что бот «научен ответить всем и все», то есть воспринимает его как шаблонно отвечающего собеседника.",
            "В шутливой форме обсуждает чат-ботов и допускает, что собеседник может быть ботом; держит наготове фразу.",
            "Участвует в неформальной беседе стоматологического чата.",
            "Участвует в стоматологическом клиническом чате под ником @arsanbekov.",
            "Участвует в стоматологических диагностических обсуждениях.",
            "Данила Гончаренко активно участвует в жизни сообщества, проявляет дружелюбие и чувство юмора в общении с коллегами",
            "Общается в непринужденной манере, участвует в шутливых диалогах с коллегами по чату",
        ]
        cleaned = user_memory.clean_clinician_facts(meta_facts)
        self.assertEqual(cleaned, [], f"Expected 0 meta facts, got: {cleaned}")

    def test_offtopic_and_negative_meta_filtered(self):
        """Бытовой оффтоп и отрицательные псевдо-факты должны блокироваться."""
        junk_facts = [
            "Упоминает тему утреннего плова: «Утренний плов тема».",
            "Считает, что «утренний плов везде хорош».",
            "Поздравляет коллег с днём рождения и желает отличных пациентов.",
            "Валерия Александровна в данной беседе не обсуждает конкретные клинические случаи, протоколы лечения или стоматологические материалы",
            "Запрашивает мнение коллег по клиническому случаю; данных о микроскопе, стаже, городе, конкретных брендах и протоколах в беседе нет.",
            "В беседе отсутствуют клинические взгляды, протоколы, бренды, материалы и оборудование.",
            "Клинический профиль по данной беседе не обновляется из-за отсутствия релевантных данных.",
            "Не упоминает используемый силер.",
            "Информация о стаже отсутствует.",
            "Ок",
            "Спасибо",
        ]
        cleaned = user_memory.clean_clinician_facts(junk_facts)
        self.assertEqual(cleaned, [], f"Expected 0 junk facts, got: {cleaned}")

    def test_clinical_facts_preserved(self):
        """Клинические факты с терминологией (BioRoot, ProRoot, root canal, FDI) обязаны сохраняться."""
        clinical_facts = [
            "Использует биокерамический силер BioRoot RCS при обтурации методом одиночного конуса.",
            "Применяет ProRoot MTA для закрытия апикальной перфорации корня.",
            "Проводит распломбировку корневых каналов с ультразвуковой активацией гипохлорита 3%.",
            "Участвует в удалении зубов мудрости (8-х зубов).",
            "Участвует в выборе дентального микроскопа для клиники, рассматривает бренды CJ, Zumax, Алтион.",
            "Участвует в междисциплинарных обсуждениях с коллегами и главным врачом клиники по вопросам выбора между эндодонтическим лечением, резекцией (апико) и удалением зубов.",
            "Сталкивается с крайне плохой гигиеной у пациента (остатки еды, кириешки, семечки); проводила беседы и чистила зубы, затем отправила домой без лечения.",
            "Ориентируется на апекслокатор как на рутинный инструмент; критикует ситуацию, когда 10 лет назад на собеседовании не все врачи с ним работали.",
            "Ведёт детей: пациент 2012 года рождения, зуб 4.6, ранее зуб был открыт из-за отека; доктор полечила в одно посещение и поставила пломбу.",
        ]
        cleaned = user_memory.clean_clinician_facts(clinical_facts)
        self.assertEqual(len(cleaned), len(clinical_facts), f"Some clinical facts were wrongly dropped: {len(cleaned)} vs {len(clinical_facts)}")
        for fact in clinical_facts:
            self.assertIn(fact, cleaned)


class TestSpecialtyValidationAndResolution(unittest.TestCase):
    """Тестирование валидации специализаций и защиты узкого профиля."""

    def test_invalid_specialty_candidates_rejected(self):
        """Generic и системные строки не признаются кандидатами на специализацию."""
        invalid_candidates = [
            "",
            "неизвестно",
            "нет данных",
            "не определена",
            "admin",
            "root access",
            "собеседник",
            "пациент",
            "информация отсутствует",
        ]
        for cand in invalid_candidates:
            self.assertFalse(
                user_memory.is_valid_specialty_candidate(cand),
                f"Candidate {cand!r} should be rejected"
            )

    def test_narrow_specialty_protection(self):
        """Подтвержденная узкая специализация не затирается generic-названием."""
        # Ортопед не должен даунгрейдиться до общего стоматолога
        res1 = user_memory.resolve_clinician_specialty("Стоматолог-ортопед", "Стоматолог")
        self.assertEqual(res1, "Стоматолог-ортопед")

        # Эндодонтист в фоновом демоне не даунгрейдится до терапевта
        res2 = user_memory.resolve_clinician_specialty("Стоматолог-эндодонтист", "Стоматолог-терапевт", is_group_daemon=True)
        self.assertEqual(res2, "Стоматолог-эндодонтист")

        # Обогащение разрешено
        res3 = user_memory.resolve_clinician_specialty("Терапевт", "Стоматолог-терапевт, эндодонтист")
        self.assertEqual(res3, "Стоматолог-терапевт, эндодонтист")


class TestLiveDatabaseHygiene(unittest.TestCase):
    """Инспекция активной SQLite БД проекта на отсутствие аномалий и соблюдение инвариантов."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = None
        try:
            import config
            if hasattr(config, "DB_PATH") and config.DB_PATH:
                p = Path(config.DB_PATH)
                if not p.is_absolute():
                    p = _REPO_ROOT / p
                if p.exists():
                    cls.db_path = p
        except Exception:
            pass
        if not cls.db_path or not cls.db_path.exists():
            try:
                import backup_db
                p = backup_db.get_default_db_path()
                if p.exists():
                    cls.db_path = p
            except Exception:
                pass
        if not cls.db_path or not cls.db_path.exists():
            for candidate in ("stomat_bot.db", "endo_bot.db"):
                p = _REPO_ROOT / candidate
                if p.exists():
                    # Проверяем, что в базе есть таблицы, а не пустой файл
                    try:
                        c = sqlite3.connect(str(p))
                        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
                        c.close()
                        if "user_memories" in tables:
                            cls.db_path = p
                            break
                    except Exception:
                        continue

    def test_live_db_profile_portraits_hygiene(self):
        """В таблице user_profiles не должно быть висячих префиксов/суффиксов и заиканий специализации."""
        if not self.db_path:
            self.skipTest("Live database not found in repo root")

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("SELECT user_id, profile_portrait FROM user_profiles WHERE profile_portrait IS NOT NULL AND profile_portrait != ''")
        rows = cur.fetchall()
        conn.close()

        defects = []
        for uid, port in rows:
            port_lower = port.lower()
            if any(h in port_lower for h in (". специализация:", "специализация:", "клинический профиль:")):
                defects.append((uid, port))

        self.assertEqual(
            defects, [],
            f"Found {len(defects)} defective profile_portraits in {self.db_path.name}: {defects}"
        )

    def test_live_db_memories_specialty_hygiene(self):
        """В таблице user_memories не должно быть невалидных специальностей ('неизвестно', 'нет данных')."""
        if not self.db_path:
            self.skipTest("Live database not found in repo root")

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("SELECT user_id, specialty FROM user_memories WHERE specialty IS NOT NULL AND specialty != ''")
        rows = cur.fetchall()
        conn.close()

        invalid_spec = {"неизвестно", "нет данных", "не определена", "не указана"}
        defects = [(uid, spec) for uid, spec in rows if spec.lower().strip() in invalid_spec]

        self.assertEqual(
            defects, [],
            f"Found {len(defects)} invalid specialties in {self.db_path.name}: {defects}"
        )

    def test_live_db_facts_chat_bot_hygiene(self):
        """В таблице user_memories.facts_json не должно быть мета-фактов про чат-ботов."""
        if not self.db_path:
            self.skipTest("Live database not found in repo root")

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("SELECT user_id, facts_json FROM user_memories WHERE facts_json IS NOT NULL AND facts_json != ''")
        rows = cur.fetchall()
        conn.close()

        bot_fact_defects = []
        for uid, raw_facts in rows:
            try:
                facts = json.loads(raw_facts)
            except Exception:
                continue
            for f in facts:
                f_lower = f.lower()
                if any(bad in f_lower for bad in ("чат-бот", "чатбот", "оказался ботом", "собеседник оказался ботом")):
                    bot_fact_defects.append((uid, f))

        self.assertEqual(
            bot_fact_defects, [],
            f"Found {len(bot_fact_defects)} bot meta facts in {self.db_path.name}: {bot_fact_defects}"
        )

    def test_live_db_sqlite_integrity(self):
        """Проверка PRAGMA integrity_check на живой БД."""
        if not self.db_path:
            self.skipTest("Live database not found in repo root")

        conn = sqlite3.connect(str(self.db_path))
        cur = conn.cursor()
        cur.execute("PRAGMA integrity_check")
        res = cur.fetchone()[0]
        conn.close()
        self.assertEqual(res, "ok", f"PRAGMA integrity_check failed on {self.db_path.name}: {res}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
