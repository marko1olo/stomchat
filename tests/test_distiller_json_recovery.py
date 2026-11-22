# -*- coding: utf-8 -*-
"""
Тесты пуленепробиваемости парсера JSON дистиллятора (Domain 4: distiller.py).
Проверяют устойчивость к:
- Чистому JSON (объекты и списки)
- Markdown-кодовым блокам (```json ... ```) с текстовыми преамбулами и комментариями
- Висячим запятым (trailing commas) перед } и ]
- BOM (\\ufeff), невидимым пробелам и управляющим спецсимволам ASCII 0-31
- Одинарным кавычкам (Python literal / ast.literal_eval)
- JS-комментариям (// и /* ... */)
- Обрывам ответа и частичному спасению фактов (status: salvaged)
- Валидации в gemini_knowledge.validate_fact_payload
"""
import pytest
import distiller
import gemini_knowledge


def test_clean_json_parsing():
    """Тест 1: Парсинг чистого JSON в различных стандартных формах."""
    # 1.1 Стандартный объект с ключом facts
    sample_obj = '{"facts": [{"c": "1.1.1", "f": "Ирригация корневого канала 3% раствором NaOCl.", "s": [101, 102], "case": false}]}'
    res_obj = distiller.extract_resilient_json(sample_obj)
    assert isinstance(res_obj, dict)
    assert "facts" in res_obj
    assert len(res_obj["facts"]) == 1
    assert res_obj["facts"][0]["c"] == "1.1.1"

    facts, status = distiller.parse_facts(sample_obj)
    assert status == "ok"
    assert len(facts) == 1
    assert facts[0]["s"] == [101, 102]

    # 1.2 Верхнеуровневый список фактов
    sample_list = '[{"c": "1.1.2", "f": "Ультразвуковая активация ирриганта.", "s": [201], "case": true}]'
    res_list = distiller.extract_resilient_json(sample_list)
    assert isinstance(res_list, list)
    assert len(res_list) == 1

    facts_list, status_list = distiller.parse_facts(sample_list)
    assert status_list == "ok"
    assert len(facts_list) == 1
    assert facts_list[0]["f"] == "Ультразвуковая активация ирриганта."

    # 1.3 Одиночный факт в объекте без обертки facts
    sample_single = '{"c": "1.1.3", "f": "Применение ЭДТА 17% для удаления смазанного слоя.", "s": [301]}'
    facts_single, status_single = distiller.parse_facts(sample_single)
    assert status_single == "ok"
    assert len(facts_single) == 1
    assert facts_single[0]["c"] == "1.1.3"


def test_markdown_fenced_with_preamble_and_comments():
    """Тест 2: JSON внутри ```json ... ``` с преамбулой до и комментариями после."""
    # 2.1 Блок ```json ... ``` с вводным текстом и заключительными пожеланиями
    noisy_text = """
    Здравствуйте, коллега! Вот извлеченные из чата факты по эндодонтии:
    ```json
    {
      "facts": [
        {
          "c": "1.1.1",
          "f": "Создание эндодонтического доступа с сохранением перицервикального дентина.",
          "s": [401, 402]
        },
        {
          "c": "1.1.4",
          "f": "Калибровка апикального уступа ручными файлами до размера #25.",
          "s": [403]
        }
      ]
    }
    ```
    Надеюсь, эта выжимка будет полезна для клинической википедии!
    """
    res = distiller.extract_resilient_json(noisy_text)
    assert isinstance(res, dict)
    assert len(res["facts"]) == 2

    facts, status = distiller.parse_facts(noisy_text)
    assert status == "ok"
    assert len(facts) == 2
    assert facts[0]["c"] == "1.1.1"
    assert facts[1]["c"] == "1.1.4"

    # 2.2 Блок ``` ... ``` без указания языка
    generic_fence = """
    Ответ модели:
    ```
    [
      {"c": "2.1.1", "f": "Адгезивный протокол с селективным протравливанием эмали.", "s": [501]}
    ]
    ```
    """
    facts_fence, status_fence = distiller.parse_facts(generic_fence)
    assert status_fence == "ok"
    assert len(facts_fence) == 1

    # 2.3 Без кодовых блоков, но с текстом вокруг JSON
    raw_embedded = """
    Ниже приведена структура:
    {"facts": [{"c": "3.1.1", "f": "Апекслокация на сухом канале.", "s": [601]}]}
    Конец сообщения.
    """
    facts_embedded, status_embedded = distiller.parse_facts(raw_embedded)
    assert status_embedded == "ok"
    assert len(facts_embedded) == 1
    assert facts_embedded[0]["f"] == "Апекслокация на сухом канале."


def test_trailing_commas_recovery():
    """Тест 3: JSON с висячими запятыми (trailing commas) в объектах и массивах."""
    broken_commas = """
    {
      "facts": [
        {
          "c": "1.1.5",
          "f": "Обтурация методом вертикальной конденсации гуттаперчи, } с силером AH Plus.",
          "s": [701, 702, ],
          "case": false,
        },
      ],
    }
    """
    res = distiller.extract_resilient_json(broken_commas)
    assert isinstance(res, dict)
    assert len(res["facts"]) == 1

    facts, status = distiller.parse_facts(broken_commas)
    assert status == "ok"
    assert len(facts) == 1
    fact = facts[0]
    # Запятая со скобкой ВНУТРИ строки не должна быть повреждена:
    assert fact["f"] == "Обтурация методом вертикальной конденсации гуттаперчи, } с силером AH Plus."
    assert fact["s"] == [701, 702]
    assert fact["case"] is False


def test_bom_and_invisible_control_characters():
    """Тест 4: JSON с BOM (\\ufeff), невидимыми пробелами и управляющими символами."""
    # \\ufeff (BOM), \\u200b (zero-width space), ASCII 0x00, 0x07, 0x1F
    dirty_json = (
        "\ufeff\u200b\x00\x05"
        "{\n"
        '  "facts": [\n'
        '    {\n'
        '      "c": "1.1.6",\n'
        '      "f": "Применение гидроксида кальция на 14 дней при деструктивных формах периодонтита.\\x1f",\n'
        '      "s": [801]\n'
        "    }\n"
        "  ]\n"
        "}\x00"
    )
    res = distiller.extract_resilient_json(dirty_json)
    assert isinstance(res, dict)
    assert len(res["facts"]) == 1

    facts, status = distiller.parse_facts(dirty_json)
    assert status == "ok"
    assert len(facts) == 1
    assert facts[0]["c"] == "1.1.6"
    assert "гидроксида кальция" in facts[0]["f"]


def test_single_quotes_and_python_literals():
    """Тест 5: Парсинг одинарных кавычек и синтаксиса Python-словарей через ast.literal_eval."""
    # Одинарные кавычки + JS-булевы значения
    py_style = "{'facts': [{'c': '1.1.7', 'f': 'Удаление штифтовой конструкции с ультразвуком.', 's': [901], 'case': False}],}"
    res = distiller.extract_resilient_json(py_style)
    assert isinstance(res, dict)
    assert len(res["facts"]) == 1

    facts, status = distiller.parse_facts(py_style)
    assert status == "ok"
    assert len(facts) == 1
    assert facts[0]["c"] == "1.1.7"
    assert facts[0]["case"] is False


def test_inline_comments_handling():
    """Тест 6: Удаление JS-комментариев // и /* ... */ без повреждения текста статьи."""
    commented = """
    {
      // Это список клинических фактов
      "facts": [
        {
          "c": "1.1.8", /* Код эндодонтии */
          "f": "Ирригация NaOCl // золотой стандарт антисептики в эндодонтии",
          "s": [1001]
        }
      ]
    }
    """
    res = distiller.extract_resilient_json(commented)
    assert isinstance(res, dict)
    assert len(res["facts"]) == 1

    facts, status = distiller.parse_facts(commented)
    assert status == "ok"
    assert len(facts) == 1
    # Подстрока // внутри строки не должна быть удалена
    assert "// золотой стандарт" in facts[0]["f"]


def test_truncated_json_salvage():
    """Тест 7: Спасение фактов при обрыве генерации (токен-лимит) со статусом 'salvaged'."""
    # Внешняя скобка массива и объекта не закрыта
    truncated_raw = (
        '{"facts": ['
        '{"c": "1.1.9", "f": "Первый законченный факт.", "s": [1101]}, '
        '{"c": "1.1.10", "f": "Второй законченный факт.", "s": [1102]}, '
        '{"c": "1.1.11", "f": "Обрезанный факт, который не допе'
    )
    facts, status = distiller.parse_facts(truncated_raw)
    assert status == "salvaged"
    assert len(facts) == 2
    assert facts[0]["c"] == "1.1.9"
    assert facts[1]["c"] == "1.1.10"


def test_regex_fact_salvage_when_outer_structure_destroyed():
    """Тест 8: Резервное извлечение фактов регулярным выражением, когда структура повреждена."""
    corrupted_wrapper = """
    Извините, произошел сбой, но вот фрагменты:
    {"c": "1.2.1", "f": "Пломбирование каналов методом латеральной компакции.", "s": [1201], "case": true}
    затем последовало продолжение:
    {"f": "Применение МТА при перфорациях бифуркации.", "c": "1.2.2", "s": [1202]}
    конец потока.
    """
    facts, status = distiller.parse_facts(corrupted_wrapper)
    assert status == "salvaged"
    assert len(facts) == 2
    codes = {f["c"] for f in facts}
    assert codes == {"1.2.1", "1.2.2"}


def test_unparseable_rubbish_returns_parse_error():
    """Тест 9: Полный мусор без фактов возвращает parse_error без исключений."""
    rubbish = "Извините, я языковая модель и не могу обработать данный запрос."
    facts, status = distiller.parse_facts(rubbish)
    assert status == "parse_error"
    assert facts == []

    empty_facts, empty_status = distiller.parse_facts(None)
    assert empty_status == "no_response"
    assert empty_facts == []


def test_gemini_knowledge_validate_payload_resilience():
    """Тест 10: Проверка, что gemini_knowledge.validate_fact_payload принимает resilient JSON."""
    raw_gemini_resp = """
    Вот выжимка:
    ```json
    {
      "facts": [
        {
          "c": "1.1.1",
          "f": "Клинический протокол обработки каналов никель-титановыми инструментами.",
          "s": [1301],
          "case": false,
        },
      ],
    }
    ```
    """
    verdict, why = gemini_knowledge.validate_fact_payload(raw_gemini_resp)
    assert verdict == "ok", f"Expected 'ok', got {verdict}: {why}"
