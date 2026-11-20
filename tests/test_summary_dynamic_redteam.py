# -*- coding: utf-8 -*-
"""
tests/test_summary_dynamic_redteam.py — Red Team верификация динамической структуры саммари и дедупликации альбомов.

Верифицирует:
1. Динамическую структуру разделов саммари:
   - Если в чате нет финансовых данных, раздел «Рынок и Деньги» НЕ содержит шаблонных
     отписок («в логе не приведены», «в логе не обсуждались», «данные отсутствуют»),
     а ПОЛНОСТЬЮ опускается из итогового документа.
   - Если финансовые данные есть (цены, зарплаты, закупки) — раздел «Рынок и Деньги»
     корректно сохраняется со всеми цифрами.
2. Дедупликацию подписей к серии снимков / альбомам:
   - Подписи figcaption к изображениям одного альбома НЕ дублируются 4 раза подряд.
   - Различающиеся подписи к разным этапам кейса полностью сохраняются.

Запуск: python tests/test_summary_dynamic_redteam.py
"""

import html
import os
import re
import sys
import types

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Защита от зависимостей окружения
for name in ("database", "config", "runtime_guard", "user_memory"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

import summarizer as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    status = "OK  " if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def sanitize_summary_test(text: str) -> str:
    """
    Применяет пайплайн очистки разметки и динамических разделов саммари.
    """
    result = text
    # Очистка через clean_markdown_to_html
    if hasattr(S, "clean_markdown_to_html"):
        result = S.clean_markdown_to_html(result)
    # Если в summarizer есть специализированная функция санации пустых разделов
    for fn_name in ("prune_empty_summary_sections", "sanitize_summary_sections", "filter_empty_sections"):
        if hasattr(S, fn_name):
            result = getattr(S, fn_name)(result)
    return result


print("\n[1] Red Team: Динамическое исключение раздела «Рынок и Деньги» при отсутствии данных")

empty_financial_samples = [
    (
        "Стандартная отписка 'в логе не приведены'",
        """## 1. 🔥 ТЕМА ДНЯ: ЭНДОДОНТИЯ
Разбор анатомии MB2 каналов верхних моляров под операционным микроскопом.

## 3. 💰 РЫНОК И ДЕНЬГИ (ИНСАЙДЫ)
В логе не приведены данные о ценах на материалы, зарплатах и расходах клиники.

## 4. 🎓 КЛИНИЧЕСКИЕ ТОНКОСТИ
Ирригация 3% раствором гипохлорита натрия с ультразвуковой активацией."""
    ),
    (
        "Отписка 'в логе переписки финансовые данные не обсуждались'",
        """## 1. 🔥 ТЕМА ДНЯ
Препарирование зубов под виниры.

## 3. 💰 РЫНОК И ДЕНЬГИ
В логе переписки финансовые данные не обсуждались и не упоминались участниками.

## 4. 🎓 КЛИНИЧЕСКИЕ ТОНКОСТИ
Ретракционная нить 000 с гемостатиком."""
    ),
    (
        "Короткая отписка 'данные отсутствуют'",
        """## 2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ
Кейс ретрита 4.6 зуба.

## 💰 РЫНОК И ДЕНЬГИ (ИНСАЙДЫ)
Данные в логе отсутствуют.

## 5. 📝 ПРАКТИЧЕСКИЕ СОВЕТЫ
Используйте раббердам во всех клинических случаях."""
    ),
]

for label, raw_text in empty_financial_samples:
    cleaned = sanitize_summary_test(raw_text)
    
    # 1. Проверяем, что в выводе нет отписок
    has_boilerplate = bool(re.search(
        r'в\s+логе\s+(?:не\s+приведены|не\s+обсуждались|не\s+упоминались|отсутствуют)|данные\s+отсутствуют',
        cleaned,
        re.IGNORECASE
    ))
    check(f"{label}: шаблонная отписка удалена", not has_boilerplate, f"Обнаружена отписка в: {cleaned!r}")

    # 2. Проверяем, что заголовок раздела полностью опущен
    has_market_header = bool(re.search(r'РЫНОК\s+И\s+ДЕНЬГИ', cleaned, re.IGNORECASE))
    check(f"{label}: заголовок 'Рынок и Деньги' полностью опущен", not has_market_header, f"Заголовок остался в: {cleaned!r}")

    # 3. Соседние разделы не повреждены
    check(f"{label}: соседние разделы сохранены", "КЛИНИЧЕСК" in cleaned or "ТЕМА ДНЯ" in cleaned)


print("\n[2] Red Team: Сохранение раздела «Рынок и Деньги» при наличии реальных данных")

real_financial_text = """## 1. 🔥 ТЕМА ДНЯ: ОРТОПЕДИЯ
Препарирование уступа под оксид циркония.

## 3. 💰 РЫНОК И ДЕНЬГИ (ИНСАЙДЫ)
В чате активно обсуждали себестоимость безметалловых коронок:
- Фрезерованный диоксид циркония в лаборатории стоит от 4 500 до 6 500 рублей за единицу.
- Стоимость фиксации на композитный цемент в клиниках варьируется от 15 000 до 28 000 руб.
- Зарплата ортопеда составляет в среднем 25-30% от прайса.

## 4. 🎓 КЛИНИЧЕСКИЕ ТОНКОСТИ
Просушивание культи перед адгезивной фиксацией."""

cleaned_real = sanitize_summary_test(real_financial_text)
check("Раздел 'Рынок и Деньги' сохранен при наличии цен", bool(re.search(r'РЫНОК\s+И\s+ДЕНЬГИ', cleaned_real, re.IGNORECASE)))
check("Цифра 4 500 руб сохранена", "4 500" in cleaned_real or "4500" in cleaned_real)
check("Цифра 28 000 руб сохранена", "28 000" in cleaned_real or "28000" in cleaned_real)
check("Процент зарплаты 25-30% сохранен", "25-30%" in cleaned_real or "30%" in cleaned_real)


print("\n[3] Red Team: Дедупликация подписей к изображениям альбомов (Anti-Duplication)")

base_html = """<b>2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ</b>

Кейс доктора Смирнова: эндодонтическое перелечивание зуба 4.6 с периапикальным очагом деструкции.

[IMG_101]
[IMG_102]
[IMG_103]
[IMG_104]

Проведена дезобтурация корневых каналов, временное пломбирование гидроксидом кальция."""

media_map_4 = {
    101: "https://telegra.ph/file/img_1.jpg",
    102: "https://telegra.ph/file/img_2.jpg",
    103: "https://telegra.ph/file/img_3.jpg",
    104: "https://telegra.ph/file/img_4.jpg",
}

# Ситуация дефекта: все 4 кадра альбома имеют идентичную подпись
identical_caption = "Прицельная рентгенограмма зуба 4.6 до повторного эндодонтического лечения"
media_captions_identical = {i: identical_caption for i in (101, 102, 103, 104)}

result_html = S.embed_media_into_summary_html(base_html, media_map_4, media_captions_identical, max_images=4)
captions_in_html = re.findall(r'<figcaption>(.*?)</figcaption>', result_html, re.DOTALL)

# Проверяем, что нет 4 подряд одинаковых подписей
has_4_identical_consecutive = False
for i in range(len(captions_in_html) - 3):
    if (
        captions_in_html[i].strip()
        == captions_in_html[i + 1].strip()
        == captions_in_html[i + 2].strip()
        == captions_in_html[i + 3].strip()
    ):
        has_4_identical_consecutive = True
        break

check(
    "Подписи альбома НЕ дублируются 4 раза подряд",
    not has_4_identical_consecutive,
    f"Обнаружено 4 одинаковых подписи подряд: {captions_in_html}"
)
check("В статье присутствует хотя бы один снимок из альбома", len(captions_in_html) >= 1)


print("\n[4] Red Team: Различающиеся подписи к этапам кейса сохраняются")

distinct_captions = {
    101: "1. Исходный снимок: периапикальный деструктивный очаг 4.6",
    102: "2. Инструментальная обработка каналов до 35/.04",
    103: "3. Временная обтурация гидроксидом кальция на 14 дней",
    104: "4. Финальная обтурация биокерамическим силером и гуттаперчей",
}

result_distinct = S.embed_media_into_summary_html(base_html, media_map_4, distinct_captions, max_images=4)
captions_distinct_out = re.findall(r'<figcaption>(.*?)</figcaption>', result_distinct, re.DOTALL)

for m_id, expected_cap in distinct_captions.items():
    escaped_cap = html.escape(expected_cap)
    found = any(expected_cap in c or escaped_cap in c for c in captions_distinct_out)
    check(f"Подпись кадра #{m_id} сохранена", found, f"Ожидалась: {expected_cap}")


print("\n[5] Red Team: Проверка контракта промпта в summarizer.py")

with open(os.path.join(_REPO_ROOT, "summarizer.py"), "r", encoding="utf-8") as f:
    summ_source = f.read()

# Проверяем, что промпт содержит инструкции по динамическому пропуску блоков без данных
check(
    "Промпт требует пропускать блоки без данных",
    "пропусти этот блок" in summ_source or "полностью опусти" in summ_source or "не пиши отписок" in summ_source,
    "В summarizer.py отсутствует требование пропускать блоки при нехватке материала!"
)


if __name__ == "__main__":
    print(f"\n{'='*62}\nTOTAL CHECKS: PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print(f"FAILED TESTS ({len(FAIL)}): {FAIL}")
        sys.exit(1)
    print("ALL RED TEAM DYNAMIC SUMMARY TESTS PASSED 100% GREEN!\n")
