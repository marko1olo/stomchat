"""
Согласованность того, что бот обещает, и того, что умеет.

У функций три поверхности, и они расходились:
  * меню команд Telegram — единственное место, где врач видит команды, ничего
    не читая; в нём не было /wiki и /style, две рабочие функции не находились;
  * текст /help;
  * правило 11 в промпте — по нему модель отвечает на «что ты умеешь», и оно
    перечисляло 4 функции из двенадцати.

Тест фиксирует инвариант: любая команда из меню и из /help должна быть
реализована, и наоборот — обещанное в промпте должно существовать. Иначе врач
тапает пункт меню и получает молчание, либо не узнаёт о работающей функции.

Плюс проверка арифметики справочника анестезии: там значения на килограмм без
абсолютных потолков давали перебор на 40% у пациента 100 кг.

Запуск: python test_commands_surface.py
"""
import io
import re
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SOURCE = io.open("assistant.py", encoding="utf-8").read()

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


def registered_commands():
    return set(re.findall(r"types\.BotCommand\(command='([^']+)'", SOURCE))


def help_commands():
    block = SOURCE.split("💡 <b>Доступные команды в ЛС:</b>", 1)[1].split('await bot_client', 1)[0]
    return set(re.findall(r"• /(\w+)", block))


def promised_in_prompt():
    # Строго один абзац правила 11: иначе окно захватывает правило 12, где
    # встречается «бредом/инфоцыганством», и оно читается как команда.
    block = SOURCE.split("11. ФУНКЦИОНАЛ БОТА:", 1)[1].split("\n12.", 1)[0]
    return set(re.findall(r"(?<![а-яА-ЯёЁ])/([a-z]\w*)", block))


def is_handled(command):
    """Есть ли ветка, сравнивающая текст с этой командой."""
    patterns = (
        f'== "/{command}"', f"== '/{command}'",
        f'"/{command}"', f"'/{command}'",
        f'startswith("/{command}', f"startswith('/{command}",
    )
    return any(p in SOURCE for p in patterns)


registered = registered_commands()
helped = help_commands()
promised = promised_in_prompt()

print("\n[1] Каждая команда меню Telegram реализована")
check("меню не пустое", len(registered) >= 10, f"got {len(registered)}")
for command in sorted(registered):
    check(f"/{command} обработчик есть", is_handled(command))

print("\n[2] Каждая команда из /help реализована")
check("/help не пустой", len(helped) >= 10, f"got {sorted(helped)}")
for command in sorted(helped):
    check(f"/{command} обработчик есть", is_handled(command))

print("\n[3] Меню и /help описывают одно и то же")
missing_in_menu = helped - registered
missing_in_help = registered - helped
check("нет команд, которые есть в /help, но не в меню",
      not missing_in_menu, f"отсутствуют в меню: {sorted(missing_in_menu)}")
check("нет команд, которые есть в меню, но не в /help",
      not missing_in_help, f"отсутствуют в /help: {sorted(missing_in_help)}")

print("\n[4] Промпт не обещает того, чего нет")
for command in sorted(promised):
    check(f"/{command} из промпта существует", is_handled(command))
check("промпт не обещает несуществующих команд",
      promised <= (registered | helped), f"лишние: {sorted(promised - registered - helped)}")

print("\n[5] Промпт рассказывает про все функции, а не про четверть")
# Раньше при вопросе «что ты умеешь» модель называла только quiz/wiki/case/calc,
# и про закладки, протоколы, поиск и статистику врачи не узнавали.
key_features = {"quiz", "wiki", "case", "calc", "bookmarks", "protocols", "search", "stats", "style"}
absent = key_features - promised
check("все ключевые функции упомянуты", not absent, f"не упомянуты: {sorted(absent)}")
check("модели запрещено выдумывать команды",
      "других команд у тебя нет" in SOURCE, "нет запрета на выдумывание команд")

print("\n[6] Справочник анестезии: абсолютные потолки на месте")
calc_block = SOURCE.split('🧮 <b>Справочник-калькулятор анестезии', 1)[1].split("await bot_client", 1)[0]
check("правило «меньшее из двух» сформулировано",
      "меньшее из двух" in calc_block.lower(), "правило не найдено")
for cap in ("500 мг", "400 мг"):
    check(f"потолок {cap} указан", cap in calc_block)
check("указан объём карпулы", "1.7 мл" in calc_block and "1.8 мл" in calc_block)
check("есть оговорка про референс, а не рекомендацию",
      "референсные максимумы" in calc_block.lower(), "оговорки нет")
check("сказано сверять с инструкцией препарата",
      "инструкцией" in calc_block, "нет отсылки к инструкции")

print("\n[7] Арифметика справочника сходится")
# Числа берём ИЗ ТЕКСТА справочника и сверяем с расчётом. Прежняя версия этой
# секции сравнивала расчёт с константой, записанной в самом тесте: появись в
# справочнике «потолок ≈ 10 карпул», проверка всё равно прошла бы. Для
# калькулятора доз, по которому врач может действовать, так нельзя.
import re as _re  # noqa: E402

for name, percent, reference_per_kg in [("артикаин", 4, 7.0),
                                        ("мепивакаин", 3, 4.4),
                                        ("лидокаин", 2, 7.0)]:
    # Привязываемся к маркеру списка, а не к первому вхождению слова: название
    # препарата раньше встречается в примере «артикаин 4%, ребёнок 20 кг».
    start = calc_block.lower().find("• <b>" + name)
    check(f"{name}: блок найден в справочнике", start != -1)
    if start == -1:
        continue
    tail = calc_block[start + 5:]
    stops = [p for p in (tail.find("• <b>"), tail.find("⚠️")) if p > 0]
    block = tail[:min(stops)] if stops else tail

    ml_match = _re.search(r"карпула\s+([\d.]+)\s*мл\s*=\s*(\d+)\s*мг", block)
    cap_match = _re.search(r"не более\s+(\d+)\s*мг", block)
    carts_match = _re.search(r"потолок\s*≈\s*(\d+)\s*карпул", block)
    weight_match = _re.search(r"весе?\s*≈\s*(\d+)\s*кг", block)
    kg_match = _re.search(r"([\d.]+)\s*мг/кг", block)

    parsed = all((ml_match, cap_match, carts_match, weight_match, kg_match))
    check(f"{name}: все числа блока читаются из текста", parsed,
          f"не разобрано: {block[:100]!r}")
    if not parsed:
        continue

    ml = float(ml_match.group(1))
    stated_mg = int(ml_match.group(2))
    cap = int(cap_match.group(1))
    stated_carts = int(carts_match.group(1))
    stated_weight = int(weight_match.group(1))
    stated_per_kg = float(kg_match.group(1))

    # Раствор N% = N x 10 мг/мл: 4% артикаина это 40 мг/мл.
    computed_mg = percent * 10 * ml
    check(f"{name}: {stated_mg} мг в карпуле = {percent}% x {ml} мл",
          abs(computed_mg - stated_mg) < 0.5,
          f"расчёт {computed_mg:.0f} мг, в тексте {stated_mg} мг")
    check(f"{name}: норма на килограмм не выше эталонной",
          stated_per_kg <= reference_per_kg,
          f"в тексте {stated_per_kg} мг/кг, эталон {reference_per_kg}")
    check(f"{name}: потолок в карпулах округлён ВНИЗ",
          int(cap / stated_mg) == stated_carts,
          f"расчёт {cap / stated_mg:.2f} -> {int(cap / stated_mg)}, в тексте {stated_carts}")
    check(f"{name}: {stated_carts} карпул не превышают {cap} мг",
          stated_carts * stated_mg <= cap,
          f"{stated_carts} x {stated_mg} = {stated_carts * stated_mg} мг > {cap} мг")
    check(f"{name}: вес включения потолка = {cap} / {stated_per_kg}",
          abs(round(cap / stated_per_kg) - stated_weight) <= 1,
          f"расчёт {cap / stated_per_kg:.1f} кг, в тексте {stated_weight} кг")

print("\n[8] Ограничитель дозировок есть и в промпте, а не только в справке")
# Считает именно модель, поэтому текст справки её не связывает.
check("правило расчёта доз добавлено в промпт", "РАСЧЁТ ДОЗ АНЕСТЕТИКОВ" in SOURCE)
check("в промпте есть оба предела", "абсолютный максимум" in SOURCE)
check("в промпте запрещено считать без веса",
      "нет веса" in SOURCE, "нет запрета считать без веса")
check("правило продублировано во всех копиях промпта",
      SOURCE.count("РАСЧЁТ ДОЗ АНЕСТЕТИКОВ") == SOURCE.count("11. ФУНКЦИОНАЛ БОТА:"),
      f"правил {SOURCE.count('РАСЧЁТ ДОЗ АНЕСТЕТИКОВ')}, промптов {SOURCE.count('11. ФУНКЦИОНАЛ БОТА:')}")

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
