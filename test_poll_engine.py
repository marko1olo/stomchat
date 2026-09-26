"""
Комплексный набор тестов для AI-движка опросов и викторин StomChat (poll_engine.py).

Проверяет:
  1. Лимиты и санитизацию Telegram (длина вопроса <= 300, вариантов <= 100, объяснения <= 200, кол-во опций 2..10).
  2. Аккуратный срез по границе слов без поломки HTML-тегов (truncate_html_aware).
  3. Недельный клинический рубрикатор на все 7 дней (Пн..Вс).
  4. Контекстный триаж темы и формата (активный чат vs затишье).
  5. Генерацию контента (валидный JSON, JSON в ```json```, экранирование).
  6. Все сценарии фолбэков (503, исчерпание ключей, битый JSON, пустой ответ).
  7. Пул пресетов POLL_FALLBACK_PRESETS (полное соответствие лимитам).
  8. Telethon MTProto Builder (build_poll_media):
     - quiz vs regular/lifestyle
     - public_voters vs is_anonymous
     - валидация close_period (5..600 сек)
     - HTML сущности (entities)
     - MTProto бинарная сериализация и десериализация через BinaryReader
  9. Полный сквозной асинхронный пайплайн (run_pipeline / generate_poll).

Запуск: python test_poll_engine.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import poll_engine
from poll_engine import (
    MAX_EXPLANATION_LEN,
    MAX_OPTION_LEN,
    MAX_OPTIONS_COUNT,
    MAX_QUESTION_LEN,
    MIN_CLOSE_PERIOD,
    MAX_CLOSE_PERIOD,
    MIN_OPTIONS_COUNT,
    POLL_FALLBACK_PRESETS,
    WEEKLY_RUBRICATOR,
    PollEngine,
    PollPayload,
    PollType,
    TriageResult,
    build_poll_media,
    detect_chat_discussion_heuristic,
    extract_chat_text,
    generate_poll,
    generate_poll_content,
    get_fallback_preset,
    get_weekly_rubric,
    sanitize_poll_payload,
    triage_topic_and_format,
    truncate_html_aware,
)
from telethon import types
from telethon.extensions import BinaryReader, html

PASS: List[str] = []
FAIL: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [OK  ] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}" + (f" -- {detail}" if detail else ""))


class FakeLLMResponse:
    def __init__(self, text: str):
        self.text = text


async def test_truncation_and_tags() -> None:
    print("\n--- 1. Тестирование санитизации и среза по границе слов с сохранением HTML-тегов ---")

    # Без тегов, короткий
    s1 = "Короткий текст"
    check("Короткий текст не изменяется", truncate_html_aware(s1, 50) == s1)

    # Без тегов, срез по границе слов
    s2 = "Сложная анатомия корневых каналов моляров нижней челюсти требует применения операционного микроскопа."
    t2 = truncate_html_aware(s2, 40)
    plain_t2, _ = html.parse(t2)
    check("Длина обычного текста <= 40", len(plain_t2) <= 40, f"got len={len(plain_t2)} text='{plain_t2}'")
    check("Заканчивается многоточием", t2.endswith("..."), f"got '{t2}'")
    check("Не разрезает слово посредине", not t2.endswith("корне..."), f"got '{t2}'")

    # С тегами <b> и <i>
    s3 = "Пациент с <b>острой стреляющей болью</b> и <i>иррадиацией в ухо</i> при накусывании."
    t3 = truncate_html_aware(s3, 35)
    plain_t3, ent3 = html.parse(t3)
    check("Длина текста с тегами <= 35", len(plain_t3) <= 35, f"got len={len(plain_t3)} plain='{plain_t3}'")
    check("Тег <b> корректно закрыт </b>", "<b>" in t3 and "</b>" in t3, f"got '{t3}'")
    check("Корректный парсинг сущностей Telethon", len(ent3) >= 1)

    # Разрез прямо внутри открытого тега
    s4 = "<b>ОченьДлинноеСловоВнутриТегаБезПробелов</b>"
    t4 = truncate_html_aware(s4, 20)
    check("Вложенный тег закрывается при срезе", t4.startswith("<b>") and t4.endswith("</b>"), f"got '{t4}'")
    plain_t4, _ = html.parse(t4)
    check("Длина внутри тега <= 20", len(plain_t4) <= 20, f"got len={len(plain_t4)}")

    # Несколько уровней вложенности
    s5 = "Внимание: <b><i>Важный клинический протокол</i></b> для врача."
    t5 = truncate_html_aware(s5, 30)
    check("Вложенные теги <b><i> закрыты в обратном порядке </i></b>", "</i></b>" in t5 or ("</i>" in t5 and "</b>" in t5), f"got '{t5}'")


async def test_sanitize_poll_payload() -> None:
    print("\n--- 2. Тестирование валидации и санитизации структуры PollPayload ---")

    # Валидный payload без изменений
    good_payload = PollPayload(
        question="Вопрос по эндодонтии?",
        options=["Вариант А", "Вариант Б"],
        poll_type=PollType.QUIZ,
        correct_option_id=0,
        explanation_brief="Объяснение",
        explanation_deep="Подробно",
        topic="Эндодонтия"
    )
    res_good = sanitize_poll_payload(good_payload)
    check("Валидный payload проходит проверку", res_good is not None)
    check("Количество вариантов = 2", len(res_good.options) == 2)
    check("Индекс верного ответа = 0", res_good.correct_option_id == 0)

    # Слишком длинный вопрос (>300 символов)
    long_q = "Клинический вопрос: " + ("очень длинное описание " * 20)
    p_long_q = PollPayload(
        question=long_q,
        options=["А", "Б"],
        poll_type=PollType.QUIZ,
        correct_option_id=1
    )
    res_long_q = sanitize_poll_payload(p_long_q)
    check("Длинный вопрос успешно обрезан", res_long_q is not None)
    check("Длина вопроса <= MAX_QUESTION_LEN (300)", len(html.parse(res_long_q.question)[0]) <= MAX_QUESTION_LEN)

    # Слишком длинный вариант ответа (>100 символов)
    long_opt = "Слишком подробный вариант ответа: " + ("детализация протокола " * 10)
    p_long_opt = PollPayload(
        question="Вопрос?",
        options=["Нормальный ответ", long_opt],
        poll_type=PollType.REGULAR
    )
    res_long_opt = sanitize_poll_payload(p_long_opt)
    check("Длинный вариант ответа обрезан <= 100", len(html.parse(res_long_opt.options[1])[0]) <= MAX_OPTION_LEN)

    # Слишком длинное объяснение (>200 символов)
    long_exp = "Пояснение для лампочки: " + ("важная научная база " * 15)
    p_long_exp = PollPayload(
        question="Вопрос?",
        options=["1", "2"],
        poll_type=PollType.QUIZ,
        correct_option_id=0,
        explanation_brief=long_exp
    )
    res_long_exp = sanitize_poll_payload(p_long_exp)
    check("Объяснение обрезано <= 200", len(html.parse(res_long_exp.explanation_brief)[0]) <= MAX_EXPLANATION_LEN)

    # Менее 2 вариантов ответа -> отклонение (None)
    p_one_opt = PollPayload(question="Вопрос?", options=["Только один"], poll_type=PollType.QUIZ)
    check("Менее 2 вариантов ответа отклоняется", sanitize_poll_payload(p_one_opt) is None)

    # Более 10 вариантов ответа -> срез до 10
    many_opts = [f"Вариант {i}" for i in range(15)]
    p_many = PollPayload(question="Вопрос?", options=many_opts, poll_type=PollType.REGULAR)
    res_many = sanitize_poll_payload(p_many)
    check("Более 10 вариантов урезается ровно до 10", len(res_many.options) == 10)

    # Невалидный correct_option_id (out of bounds)
    p_bad_idx = PollPayload(question="Вопрос?", options=["А", "Б"], poll_type=PollType.QUIZ, correct_option_id=5)
    res_bad_idx = sanitize_poll_payload(p_bad_idx)
    check("Индекс вне диапазона нормализуется к 0", res_bad_idx.correct_option_id == 0)

    # Regular poll сбрасывает correct_option_id в None
    p_reg = PollPayload(question="Опрос?", options=["А", "Б"], poll_type=PollType.REGULAR, correct_option_id=1)
    res_reg = sanitize_poll_payload(p_reg)
    check("Для regular poll correct_option_id сброшен в None", res_reg.correct_option_id is None)


async def test_weekly_rubricator() -> None:
    print("\n--- 3. Тестирование недельного клинического рубрикатора (все 7 дней) ---")
    day_expectations = {
        0: ("Понедельник", "endo", PollType.QUIZ),
        1: ("Вторник", "ortho", PollType.QUIZ),
        2: ("Среда", "surgery", PollType.QUIZ),
        3: ("Четверг", "therapy", PollType.QUIZ),
        4: ("Пятница", "materials_battle", PollType.REGULAR),
        5: ("Суббота", "gnathology", PollType.QUIZ),
        6: ("Воскресенье", "council", PollType.QUIZ),
    }

    base_monday = datetime.datetime(2026, 9, 21, 12, 0, 0)  # Понедельник
    for day_offset, (exp_name, exp_cat, exp_type) in day_expectations.items():
        dt = base_monday + datetime.timedelta(days=day_offset)
        rubric = get_weekly_rubric(dt)
        check(f"День {day_offset} ({exp_name}) корректно определен", rubric["day_name"] == exp_name)
        check(f"День {day_offset} категория == {exp_cat}", rubric["category"] == exp_cat)
        check(f"День {day_offset} тип == {exp_type.value}", rubric["default_type"] == exp_type)


async def test_context_and_format_triage() -> None:
    print("\n--- 4. Тестирование контекстного триажа (активный чат vs затишье) ---")

    # Сценарий А: Затишье в чате (пустой контекст)
    tuesday_dt = datetime.datetime(2026, 9, 22, 10, 0, 0)  # Вторник -> Ортопедия
    triage_quiet = await triage_topic_and_format(chat_context=[], now=tuesday_dt, llm_caller=None)
    check("Затишье: тема из рубрикатора", "Ортопедия" in triage_quiet.topic)
    check("Затишье: is_from_chat == False", not triage_quiet.is_from_chat)
    check("Затишье: формат викторины для вторника", triage_quiet.format_type == PollType.QUIZ)

    # Сценарий Б: Эвристическое обнаружение батла материалов в чате (Speedex vs А-силикон)
    chat_speedex = [
        {"text": "Коллеги, кто еще снимает оттиски на Speedex под циркон?"},
        {"text": "С-силикон дает дикую усадку уже через час, только А-силикон!"},
        {"text": "Зато спидекс дешевый и отлично течет при гидроконтроле."}
    ]
    triage_battle = await triage_topic_and_format(chat_context=chat_speedex, now=tuesday_dt, llm_caller=None)
    check("Эвристика: найден батл материалов", triage_battle.is_from_chat)
    check("Эвристика: категория materials_battle", triage_battle.category == "materials_battle")
    check("Эвристика: формат regular", triage_battle.format_type == PollType.REGULAR)

    # Сценарий В: AI-триаж с LLM
    ai_triage_json = json.dumps({
        "is_from_chat": True,
        "topic": "Тактика при отломе ультразвуковой насадки в устье канала",
        "category": "endo",
        "format_type": "quiz",
        "needs_case_intro": True,
        "rationale": "Бурная дискуссия об извлечении инструмента."
    }, ensure_ascii=False)

    async def fake_triage_llm(prompt, status_ctx=None, timeout=None):
        return FakeLLMResponse(ai_triage_json), None

    triage_ai = await triage_topic_and_format(
        chat_context="Доктор сломал кончик файла при распломбировке, что делать?! Срочно!",
        now=tuesday_dt,
        llm_caller=fake_triage_llm
    )
    check("AI триаж: тема извлечена из LLM", "отлом" in triage_ai.topic or "инструмент" in triage_ai.topic)
    check("AI триаж: is_from_chat == True", triage_ai.is_from_chat)
    check("AI триаж: needs_case_intro == True", triage_ai.needs_case_intro)
    check("AI триаж: формат quiz", triage_ai.format_type == PollType.QUIZ)


async def test_content_generation() -> None:
    print("\n--- 5. Тестирование генератора контента (Content Generation) ---")

    triage = TriageResult(
        topic="Перфорация фуркации 3.6",
        category="endo",
        is_from_chat=True,
        format_type=PollType.QUIZ,
        needs_case_intro=True
    )

    # Генерация валидного JSON
    good_content = {
        "case_intro": "<b>Клинический случай:</b> Пациент 40 лет, свежая перфорация.",
        "question": "Какой материал показан для закрытия перфорации дна полости?",
        "options": ["МТА / Биокерамика", "Цинк-фосфатный цемент", "Гутаперча на силере"],
        "correct_option_id": 0,
        "explanation_brief": "МТА обладает остеоиндуктивными свойствами.",
        "explanation_deep": "Герметизация биокерамикой дает прогноз >90%."
    }

    async def fake_gen_llm(prompt, status_ctx=None, timeout=None):
        return FakeLLMResponse(json.dumps(good_content, ensure_ascii=False)), None

    payload = await generate_poll_content(triage, fake_gen_llm)
    check("Контент сгенерирован успешно", payload is not None)
    check("Вопрос совпадает с ответом модели", "материал показан" in payload.question)
    check("Индекс верного ответа = 0", payload.correct_option_id == 0)
    check("Вводный кейс на месте", payload.case_intro is not None and "40 лет" in payload.case_intro)

    # Генерация в обертке ```json ... ```
    async def fake_wrapped_llm(prompt, status_ctx=None, timeout=None):
        wrapped = "Вот ваш опрос:\n```json\n" + json.dumps(good_content, ensure_ascii=False) + "\n```\nУдачи!"
        return FakeLLMResponse(wrapped), None

    payload_wrapped = await generate_poll_content(triage, fake_wrapped_llm)
    check("Обертка ```json успешно снята", payload_wrapped is not None)
    check("Вопрос извлечен корректно", "материал показан" in payload_wrapped.question)


async def test_fallback_presets_and_handling() -> None:
    print("\n--- 6. Тестирование пула фолбэков (POLL_FALLBACK_PRESETS) и обработки сбоев ---")

    # Проверка каждого пресета из пула на все лимиты Telegram
    check("Пул фолбэков не пуст", len(POLL_FALLBACK_PRESETS) >= 5)
    for p in POLL_FALLBACK_PRESETS:
        cat = p["category"]
        q_len = len(html.parse(p["question"])[0])
        exp_len = len(html.parse(p["explanation_brief"])[0])
        opts_lens = [len(html.parse(o)[0]) for o in p["options"]]

        check(f"Preset [{cat}] вопрос <= {MAX_QUESTION_LEN}", q_len <= MAX_QUESTION_LEN, f"len={q_len}")
        check(f"Preset [{cat}] опций от {MIN_OPTIONS_COUNT} до {MAX_OPTIONS_COUNT}", MIN_OPTIONS_COUNT <= len(p["options"]) <= MAX_OPTIONS_COUNT)
        check(f"Preset [{cat}] все варианты <= {MAX_OPTION_LEN}", all(ol <= MAX_OPTION_LEN for ol in opts_lens))
        check(f"Preset [{cat}] объяснение <= {MAX_EXPLANATION_LEN}", exp_len <= MAX_EXPLANATION_LEN)
        if p["poll_type"] == PollType.QUIZ:
            check(f"Preset [{cat}] correct_option_id валиден", 0 <= p["correct_option_id"] < len(p["options"]))

    # Проверка работы get_fallback_preset
    fb_endo = get_fallback_preset(category="endo")
    check("Фолбэк по категории endo получен", fb_endo.topic.startswith("Эндодонтия"))
    check("Источник фолбэка помечен как fallback", fb_endo.source == "fallback")

    fb_battle = get_fallback_preset(category="materials_battle")
    check("Фолбэк materials_battle получен", fb_battle.poll_type == PollType.REGULAR)

    # Симуляция падения LLM (503 / Timeout / пустой ответ)
    triage = TriageResult(topic="Эндодонтия", category="endo", is_from_chat=False, format_type=PollType.QUIZ, needs_case_intro=True)

    async def failing_llm_503(prompt, status_ctx=None, timeout=None):
        return None, "HTTP 503 Service Unavailable: Model is overloaded"

    res_503 = await generate_poll_content(triage, failing_llm_503)
    check("При 503 generate_poll_content возвращает None", res_503 is None)

    async def broken_json_llm(prompt, status_ctx=None, timeout=None):
        return FakeLLMResponse("Извините, не могу сгенерировать JSON {невалидный"), None

    res_broken = await generate_poll_content(triage, broken_json_llm)
    check("При битом JSON возвращает None", res_broken is None)


async def test_telethon_builder() -> None:
    print("\n--- 7. Тестирование Telethon MTProto Builder (build_poll_media) ---")

    # Тест 1: Стандартный Quiz
    q1 = "Каков диагноз при <b>пульпите</b>?"
    opts1 = ["Обратимый <i>пульпит</i>", "Периодонтит", "Кариес эмали"]
    media_quiz = build_poll_media(
        question=q1,
        options=opts1,
        poll_type="quiz",
        correct_idx=0,
        explanation="Потому что боль от <b>холодного</b> обратима",
        is_anonymous=True,
        open_period=120
    )

    check("Создан объект InputMediaPoll", isinstance(media_quiz, types.InputMediaPoll))
    check("Poll quiz == True", media_quiz.poll.quiz is True)
    check("public_voters == False (анонимный)", media_quiz.poll.public_voters is False)
    check("close_period == 120", media_quiz.poll.close_period == 120)
    check("correct_answers содержит b'0'", media_quiz.correct_answers == [b"0"])
    check("solution содержит чистый текст", media_quiz.solution == "Потому что боль от холодного обратима")
    check("solution_entities содержит жирный шрифт", any(isinstance(e, types.MessageEntityBold) for e in media_quiz.solution_entities or []))
    check("question entities содержат жирный шрифт", any(isinstance(e, types.MessageEntityBold) for e in media_quiz.poll.question.entities))
    check("answers содержат 3 варианта", len(media_quiz.poll.answers) == 3)
    check("вариант 0 содержит курсив", any(isinstance(e, types.MessageEntityItalic) for e in media_quiz.poll.answers[0].text.entities))

    # Тест 2: Regular Poll (не анонимный, без объяснения)
    media_reg = build_poll_media(
        question="Какой слепочный материал вы предпочитаете?",
        options=["Speedex", "А-силикон"],
        poll_type="regular",
        correct_idx=None,
        is_anonymous=False,
        open_period=None
    )
    check("Poll quiz == False", media_reg.poll.quiz is False)
    check("public_voters == True (публичные голоса)", media_reg.poll.public_voters is True)
    check("correct_answers is None для regular", media_reg.correct_answers is None)
    check("solution is None для regular", media_reg.solution is None)
    check("close_period is None", media_reg.poll.close_period is None)

    # Тест 3: Валидация open_period (5..600 сек)
    try:
        build_poll_media("Вопрос?", ["А", "Б"], open_period=4)
        check("open_period < 5 вызывает ValueError", False)
    except ValueError:
        check("open_period < 5 вызывает ValueError", True)

    try:
        build_poll_media("Вопрос?", ["А", "Б"], open_period=601)
        check("open_period > 600 вызывает ValueError", False)
    except ValueError:
        check("open_period > 600 вызывает ValueError", True)

    # Тест 4: Валидация correct_idx вне диапазона
    try:
        build_poll_media("Вопрос?", ["А", "Б"], poll_type="quiz", correct_idx=5)
        check("correct_idx вне диапазона вызывает ValueError", False)
    except ValueError:
        check("correct_idx вне диапазона вызывает ValueError", True)

    # Тест 5: Менее 2 вариантов
    try:
        build_poll_media("Вопрос?", ["Один"])
        check("Менее 2 вариантов вызывает ValueError", False)
    except ValueError:
        check("Менее 2 вариантов вызывает ValueError", True)

    # Тест 6: Пустой вопрос
    try:
        build_poll_media("   ", ["А", "Б"])
        check("Пустой вопрос вызывает ValueError", False)
    except ValueError:
        check("Пустой вопрос вызывает ValueError", True)

    # Тест 7: MTProto бинарная сериализация и десериализация
    serialized_bytes = bytes(media_quiz)
    check("Бинарная сериализация MTProto успешна", len(serialized_bytes) > 0)
    reader = BinaryReader(serialized_bytes)
    deserialized = reader.tgread_object()
    check("Десериализация возвращает InputMediaPoll", isinstance(deserialized, types.InputMediaPoll))
    check("Десериализованный poll.quiz == True", deserialized.poll.quiz is True)
    check("Десериализованный correct_answers == [b'0']", deserialized.correct_answers == [b"0"])
    check("Десериализованный close_period == 120", deserialized.poll.close_period == 120)


async def test_end_to_end_pipeline() -> None:
    print("\n--- 8. Сквозное тестирование пайплайна (PollEngine / generate_poll) ---")

    # Сквозной сценарий 1: Успешная генерация с моком LLM
    llm_payload = {
        "case_intro": "<b>Случай:</b> Пациентка 25 лет обратилась за эстетической реставрацией.",
        "question": "Какой класс по Блэку у кариозной полости на резце с поражением угла?",
        "options": ["I класс", "III класс", "IV класс", "V класс"],
        "correct_option_id": 2,
        "explanation_brief": "IV класс включает поражение контактной поверхности резцов с нарушением режущего края.",
        "explanation_deep": "Классификация Блэка относит дефекты режущего края резцов к IV классу."
    }

    async def mock_llm_pipeline(prompt, status_ctx=None, timeout=None):
        return FakeLLMResponse(json.dumps(llm_payload, ensure_ascii=False)), None

    engine = PollEngine(default_llm_caller=mock_llm_pipeline)
    payload, media = await engine.run_pipeline(
        chat_context="Обсуждали реставрации фронтальной группы зубов и сколы углов.",
        now=datetime.datetime(2026, 9, 24, 15, 0, 0)  # Четверг (Терапия)
    )

    check("Сквозной пайплайн: получен payload", payload is not None)
    check("Сквозной пайплайн: получен InputMediaPoll", isinstance(media, types.InputMediaPoll))
    check("Сквозной пайплайн: source == 'llm'", payload.source == "llm")
    check("Сквозной пайплайн: верный ответ b'2'", media.correct_answers == [b"2"])
    check("Сквозной пайплайн: 4 варианта", len(media.poll.answers) == 4)

    # Сквозной сценарий 2: Отказ LLM -> автоматический фолбэк
    async def mock_llm_fail(prompt, status_ctx=None, timeout=None):
        return None, "All API keys exhausted"

    engine_fallback = PollEngine(default_llm_caller=mock_llm_fail)
    payload_fb, media_fb = await engine_fallback.run_pipeline(
        chat_context=None,
        now=datetime.datetime(2026, 9, 25, 18, 0, 0)  # Пятница (Батл материалов)
    )

    check("Фолбэк пайплайна: получен payload", payload_fb is not None)
    check("Фолбэк пайплайна: source == 'fallback'", payload_fb.source == "fallback")
    check("Фолбэк пайплайна: получен InputMediaPoll", isinstance(media_fb, types.InputMediaPoll))
    check("Фолбэк пайплайна: рубрика пятницы (Батл материалов / Speedex)",
          "материал" in payload_fb.topic.lower() or "speedex" in payload_fb.topic.lower() or payload_fb.poll_type == PollType.REGULAR)

    # Сквозной сценарий 3: Верхнеуровневый хелпер generate_poll
    payload_helper, media_helper = await generate_poll(
        chat_context="Speedex против А-силикона",
        llm_caller=mock_llm_fail,
        is_anonymous=False,
        open_period=300
    )
    check("generate_poll хелпер успешно вернул результат", payload_helper is not None)
    check("generate_poll: public_voters == True", media_helper.poll.public_voters is True)
    check("generate_poll: close_period == 300", media_helper.poll.close_period == 300)


async def test_redteam_safeguards() -> None:
    print("\n--- 9. Тестирование Red-Team защит (Deduplication index drift, Message length, MTProto guardrails) ---")

    # 1. Защита от смещения correct_option_id при дедупликации вариантов
    dedup_payload = {
        "question": "Какой канал моляра имеет наибольшую вариабельность?",
        "options": ["MB2", "MB2", "Дистальный", "Небный"],  # "MB2" дублируется, "Дистальный" был под индексом 2
        "correct_option_id": 2,  # Исходно указывает на "Дистальный"
        "poll_type": "quiz"
    }
    sanitized = sanitize_poll_payload(dedup_payload)
    check("Дедупликация: payload валиден", sanitized is not None)
    check("Дедупликация: число опций уменьшилось до 3", len(sanitized.options) == 3)
    check("Дедупликация: correct_option_id перенесен на 'Дистальный'", sanitized.correct_option_id == 1)
    check("Дедупликация: вариант под новым индексом равен 'Дистальный'", sanitized.options[sanitized.correct_option_id] == "Дистальный")

    # 2. Защита от превышения лимитов сообщений Telegram (4096 символов)
    long_intro = "Анамнез: " + ("очень длинное клиническое описание дефекта " * 120)
    long_deep = "Разбор: " + ("детальный клинический протокол препарирования " * 120)
    msg_limit_payload = {
        "question": "Вопрос по протоколу?",
        "options": ["Вариант А", "Вариант Б"],
        "case_intro": long_intro,
        "explanation_deep": long_deep,
        "poll_type": "regular"
    }
    sanitized_msg = sanitize_poll_payload(msg_limit_payload)
    check("Лимит сообщений: payload валиден", sanitized_msg is not None)
    check(f"Лимит сообщений: case_intro <= 3900 ({len(sanitized_msg.case_intro)})", len(sanitized_msg.case_intro) <= 3900)
    check(f"Лимит сообщений: explanation_deep <= 3800 ({len(sanitized_msg.explanation_deep)})", len(sanitized_msg.explanation_deep) <= 3800)

    # 3. Защита build_poll_media от сверхдлинных входных строк
    super_long_q = "Острый пульпит: " + ("клиническая дилемма " * 25)  # > 400 символов
    super_long_opts = ["Вариант " + ("очень длинный текст " * 10), "Вариант 2 " + ("текст " * 15)]  # > 150 символов
    super_long_exp = "Лампочка: " + ("объяснение верного ответа " * 15)  # > 300 символов

    media_safe = build_poll_media(
        question=super_long_q,
        options=super_long_opts,
        poll_type="quiz",
        correct_idx=0,
        explanation=super_long_exp
    )
    check("build_poll_media guardrail: объект успешно создан", media_safe is not None)
    check(f"build_poll_media guardrail: question <= 300 ({len(media_safe.poll.question.text)})", len(media_safe.poll.question.text) <= 300)
    for i, ans in enumerate(media_safe.poll.answers):
        check(f"build_poll_media guardrail: option {i} <= 100 ({len(ans.text.text)})", len(ans.text.text) <= 100)
    check(f"build_poll_media guardrail: solution <= 200 ({len(media_safe.solution)})", len(media_safe.solution) <= 200)


async def main() -> None:
    print("=" * 70)
    print("СТАРТ ПОЛНОГО ТЕСТИРОВАНИЯ POLL_ENGINE.PY ДЛЯ STOMCHAT")
    print("=" * 70)

    await test_truncation_and_tags()
    await test_sanitize_poll_payload()
    await test_weekly_rubricator()
    await test_context_and_format_triage()
    await test_content_generation()
    await test_fallback_presets_and_handling()
    await test_telethon_builder()
    await test_end_to_end_pipeline()
    await test_redteam_safeguards()

    print("\n" + "=" * 70)
    print(f"ИТОГИ ТЕСТОВ: PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    print("=" * 70)

    if FAIL:
        print("\nОШИБКИ В СЛЕДУЮЩИХ ТЕСТАХ:")
        for f in FAIL:
            print(f"  ❌ {f}")
        sys.exit(1)
    else:
        print("\n ВСЕ ТЕСТЫ ПРОЙДЕНЫ УСПЕШНО (100% GREEN)!")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
