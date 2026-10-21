# -*- coding: utf-8 -*-
"""
tests/test_chairside_tools.py — Комплексный тестовый набор для chairside_tools.py.

Проверяет:
1. Анатомическую базу FDI 11-48: все 32 зуба, MB2, Danger Zones, рабочая длина.
2. Каталог эндо-систем: ProTaper, WaveOne, SOCO, Rogin, Denco, VDW Rotate (RPM, торк, реципрок).
3. Экстренную анестезию: буферизация 8.4% NaHCO3, Gow-Gates, Vazirani-Akinosi.
4. Скорую помощь по обломкам: Bypass за изгибом vs извлечение в устье.
5. Фармакотерапию: дозировки детям по весу, взрослые, аллергия на пенициллин, НПВП.
6. Консилиум «Спасать или удалять?»: феррул, вертикальная трещина, костная опора.
7. Калькулятор сложности AAE: Class 1 Low, Class 2 Moderate, Class 3 High.
8. Интерактивные Telethon-разметки и центральный Callback Dispatcher.
"""

import asyncio
import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import chairside_tools as ct


# ==============================================================================
# [1] Зубная формула FDI 11–48 (32 зуба)
# ==============================================================================
print("\n[1] Анатомическая база FDI 11–48: все 32 зуба")

ALL_TEETH = (
    [18, 17, 16, 15, 14, 13, 12, 11] +
    [21, 22, 23, 24, 25, 26, 27, 28] +
    [31, 32, 33, 34, 35, 36, 37, 38] +
    [41, 42, 43, 44, 45, 46, 47, 48]
)

check("база содержит ровно 32 зуба", len(ct.FDI_TEETH_DB) == 32)

for fdi in ALL_TEETH:
    anat = ct.get_tooth_anatomy(fdi)
    check(f"зуб {fdi} присутствует в базе", anat is not None)
    if anat:
        check(f"зуб {fdi}: длина > 15 мм", anat.avg_length_mm >= 15.0)
        check(f"зуб {fdi}: Danger Zone заполнена", len(anat.danger_zone) > 10)
        card = ct.format_tooth_card(anat)
        check(f"зуб {fdi}: карточка форматируется в HTML", f"Зуб {fdi}" in card and "<b>" in card)

# Специфические ключевые клинические факты
m16 = ct.get_tooth_anatomy(16)
check("16 зуб: высокая вероятность MB2 (>90%)", "93%" in m16.mb2_or_extra_prob or "MB2" in m16.canals_prob)
check("16 зуб: опасность strip-перфорации", "strip" in m16.danger_zone.lower())

m36 = ct.get_tooth_anatomy(36)
check("36 зуб: опасность перфорации дистальной стенки", "дистальная" in m36.danger_zone.lower())

c13 = ct.get_tooth_anatomy(13)
check("13 клык: самый длинный зуб (>25 мм)", c13.avg_length_mm >= 26.0)

i31 = ct.get_tooth_anatomy(31)
check("31 резец: второй язычный канал в ~40%", "40%" in i31.canals_prob or "язычный" in i31.danger_zone.lower())


# ==============================================================================
# [2] Каталог эндо-систем (RPM, Торк, Кинематика)
# ==============================================================================
print("\n[2] Каталог систем файлов и кинематика")

SYSTEM_ALIASES = [
    ("protaper", "ProTaper Ultimate"),
    ("протейпер", "ProTaper Ultimate"),
    ("waveone", "WaveOne Gold"),
    ("вейван", "WaveOne Gold"),
    ("soco", "SOCO SC / SC Pro"),
    ("соко", "SOCO SC / SC Pro"),
    ("vdw rotate", "VDW Rotate"),
    ("ротейт", "VDW Rotate"),
    ("reciproc", "Reciproc Blue"),
    ("rogin", "Rogin Super Files / Niti Flex"),
    ("denco", "Denco Super Files III / Blue"),
    ("edgetaper", "EdgeTaper Platinum"),
    ("xp-endo", "XP-endo Shaper / Finisher"),
]

for query, expected_name in SYSTEM_ALIASES:
    data = ct.lookup_file_system(query)
    check(f"поиск по алиасу '{query}' находит {expected_name}", data is not None and data["system_name"] == expected_name)

# Проверка конкретных параметров
soco = ct.lookup_file_system("soco")
check("SOCO: предупреждение о мягком сплаве (торк <= 1.8)", "1.8" in soco["standard_torque"] and "мягкий" in soco["tip"])

waveone = ct.lookup_file_system("waveone")
check("WaveOne: реципрокная кинематика", "Reciprocating" in waveone["motion"])
check("WaveOne: углы CCW 150 / CW 30", any("150" in f.angles for f in waveone["files"]))

xp = ct.lookup_file_system("xp-endo")
check("XP-endo: высокие обороты (800-1000 RPM) и торк 1.0", xp["standard_rpm"] >= 800 and "1.0" in xp["standard_torque"])

card_pt = ct.format_file_system_card(ct.lookup_file_system("protaper"))
check("карточка файлов форматируется в HTML", "ProTaper Ultimate" in card_pt and "RPM" in card_pt)


# ==============================================================================
# [3] Экстренная анестезия (Буферизация содой, Gow-Gates, Akinosi)
# ==============================================================================
print("\n[3] Экстренная анестезия у кресла")

buf = ct.CHAIRSIDE_ANESTHESIA_RESCUE["buffering"]
check("буферизация: точная доза 0.18 мл 8.4% соды", "0.18 мл" in buf["recipe"] and "8.4%" in buf["recipe"])
check("буферизация: физиологический pH 7.35-7.45", "7.35" in buf["recipe"])
check("буферизация: онсет 60-90 секунд", "60-90 секунд" in buf["recipe"])

gg = ct.CHAIRSIDE_ANESTHESIA_RESCUE["gow_gates"]
check("Гоу-Гейтс: глубина погружения 25 мм", "25 мм" in gg["protocol"])
check("Гоу-Гейтс: обязательный костный упор", "костного упора" in gg["protocol"])

va = ct.CHAIRSIDE_ANESTHESIA_RESCUE["vazirani_akinosi"]
check("Акинози: закрытый рот / тризм", "сомкнуты" in va["protocol"] or "тризм" in va["title"].lower())
check("Акинози: костный упор НЕ требуется", "НЕ ТРЕБУЕТСЯ" in va["protocol"])


# ==============================================================================
# [4] Протоколы сепарации инструментов (Bypass vs Извлечение)
# ==============================================================================
print("\n[4] Скорая помощь по обломкам инструментов")

coronal = ct.get_fragment_protocol("coronal_orifice")
check("устьевой отломок: извлечение ультразвуком разрешено", coronal.action.startswith("ИЗВЛЕЧЕНИЕ") and coronal.ultrasound_allowed)

curved = ct.get_fragment_protocol("middle_curved")
check("отломок ЗА изгибом: ультразвук СТРОГО ЗАПРЕЩЕН", not curved.ultrasound_allowed)
check("отломок ЗА изгибом: тактика строго BYPASS", "BYPASS" in curved.action)
check("отломок ЗА изгибом: предсогнутый ручной K-файл #08/#10", any("#08" in step for step in curved.technique_steps))
check("юридический скрипт для пациента в наличии", len(curved.patient_disclosure_script) > 30)

apical = ct.get_fragment_protocol("apical_beyond")
check("выход за апекс: консервация биокерамикой / хирургия", "Хирургия" in apical.action or "КОНСЕРВАЦИЯ" in apical.action)


# ==============================================================================
# [5] Фармакотерапия (Антибиотики, Аллергия, Анальгезия, Дексаметазон)
# ==============================================================================
print("\n[5] Фармакотерапия прямо у кресла")

# Ребенок 20 кг без аллергии: Амоксиклав суспензия
kid = ct.calculate_antibiotic(weight_kg=20, is_child=True)
check("ребенок 20 кг: Амоксиклав суспензия", "Амоксициллин" in kid.drug_name or "Амоксиклав" in kid.patient_message_text)
check("ребенок 20 кг: разовая доза 10.0 мл (500 мг/сут)", "10.0 мл" in kid.dosage)

# Ребенок 20 кг с аллергией на пенициллин: Азитромицин
kid_all = ct.calculate_antibiotic(weight_kg=20, is_child=True, allergy_penicillin=True)
check("ребенок 20 кг аллергик: Азитромицин суспензия", "Азитромицин" in kid_all.drug_name)
check("курс 3 дня", "3 дня" in kid_all.duration_days)

# Взрослый стандартный
adult = ct.calculate_antibiotic(weight_kg=70, is_child=False)
check("взрослый: Амоксиклав 875/125", "875/125" in adult.drug_name)

# Взрослый аллергик
adult_all = ct.calculate_antibiotic(weight_kg=70, is_child=False, allergy_penicillin=True)
check("взрослый аллергик: Кларитромицин 500 мг", "Кларитромицин" in adult_all.drug_name)

# Периостит / флегмона: Амоксиклав + Метронидазол
severe = ct.calculate_antibiotic(weight_kg=75, is_child=False, severe_infection=True)
check("тяжелая инфекция: Амоксиклав + Метронидазол", "Метронидазол" in severe.drug_name)
check("предупреждение об алкоголе", "АЛКОГОЛЬ" in severe.patient_message_text)

# Анальгезия: синергия ибупрофен + парацетамол
analgesia = ct.calculate_analgesia()
check("золотой стандарт анальгезии: Ибупрофен 400 + Парацетамол 500", "Ибупрофен" in analgesia and "Парацетамол" in analgesia)

# Дексаметазон
dex = ct.dexamethasone_emergency_card()
check("дексаметазон: 4-8 мг в/м", "4 мг – 8 мг" in dex)


# ==============================================================================
# [6] Экспресс-консилиум «Спасать или удалять?»
# ==============================================================================
print("\n[6] Экспресс-консилиум «Спасать или удалять?»")

# Идеальный зуб: феррул 2.0, нет трещины, кость 80%
res_good = ct.evaluate_tooth_preservation(2.0, 1.5, False, 80, False, False)
check("благоприятный прогноз (>=75 баллов)", res_good.total_score >= 75 and "СПАСАТЬ" in res_good.verdict)

# Вертикальная трещина корня -> безусловное удаление (0 баллов)
res_crack = ct.evaluate_tooth_preservation(2.0, 1.5, True, 80, False, False)
check("вертикальная трещина: 0 баллов и БЕЗУСЛОВНОЕ УДАЛЕНИЕ", res_crack.total_score == 0 and "БЕЗУСЛОВНОЕ УДАЛЕНИЕ" in res_crack.verdict)

# Отсутствие феррула (<0.5 мм) и потеря кости
res_poor = ct.evaluate_tooth_preservation(0.4, 0.8, False, 25, True, True)
check("разрушенный зуб: неблагоприятный прогноз", res_poor.total_score < 50 and "НЕБЛАГОПРИЯТНЫЙ" in res_poor.verdict)


# ==============================================================================
# [7] Калькулятор сложности AAE (Class 1, 2, 3)
# ==============================================================================
print("\n[7] AAE Endodontic Case Difficulty Calculator")

# 1. High Risk (любая тройка: изгиб >30° или отломок или тризм)
aae_high = ct.AAEDifficultyCalculator.evaluate_case((3, 1, 1), tooth_num="Зуб 46")
check("тройка на шаге 1 -> HIGH RISK (Class 3 AAE)", aae_high.tier == "HIGH" and "RED ZONE" in aae_high.title)
check("high risk: микроскоп обязателен", any("микроскоп" in eq.lower() for eq in aae_high.equipment_mandatory))

# 2. Moderate Risk (две двойки)
aae_mod = ct.AAEDifficultyCalculator.evaluate_case((2, 2, 1), tooth_num="Зуб 16")
check("две двойки -> MODERATE RISK (Class 2 AAE)", aae_mod.tier == "MODERATE" and "CAUTION" in aae_mod.title)

# 3. Low Risk (все единицы)
aae_low = ct.AAEDifficultyCalculator.evaluate_case((1, 1, 1), tooth_num="Зуб 11")
check("все единицы -> LOW RISK (Class 1 AAE)", aae_low.tier == "LOW" and "CLEAR ROAD" in aae_low.title)
check("готовый текст для Telegram сформирован", "КЛИНИЧЕСКИЙ РАСЧЕТ СЛОЖНОСТИ ЭНДО" in aae_low.shareable_card_text)


# ==============================================================================
# [8] Интерактивные разметки и Callback Dispatcher
# ==============================================================================
print("\n[8] Telethon UI Widgets и Callback Dispatcher")

# Проверка сборки инлайн-кнопок
check("разметка хаба формируется", len(ct.build_chairside_hub_markup()) >= 4)
check("разметка квадрантов FDI формируется", len(ct.build_fdi_quadrant_selector_markup()) >= 3)
check("разметка зубов квадранта 1 (8 зубов)", len(ct.build_fdi_quadrant_teeth_markup(1)) == 3)
check("разметка действий над зубом", len(ct.build_fdi_tooth_actions_markup(16)) == 3)
check("разметка систем файлов", len(ct.build_files_catalog_markup()) >= 5)
check("разметка скорой помощи по файлам", len(ct.build_broken_file_matrix_markup()) == 5)
check("разметка быстрой фармы", len(ct.build_pharma_quick_markup()) == 5)
check("разметка AAE шаг 1", len(ct.build_aae_markup(1)) == 4)


class MockCallbackEvent:
    def __init__(self, data_str: str):
        self.data = data_str.encode("utf-8")
        self.chat_id = -1001234567890
        self.edit = AsyncMock()
        self.answer = AsyncMock()


async def run_callback_tests():
    bot = MagicMock()

    # FDI callbacks
    ev_fdi = MockCallbackEvent("fdi:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_fdi, "fdi:menu")
    check("fdi:menu диспетчеризируется", ok and ev_fdi.edit.called and ev_fdi.answer.called)

    ev_tooth = MockCallbackEvent("fdi:tooth:16")
    ok = await ct.dispatch_chairside_callback(bot, ev_tooth, "fdi:tooth:16")
    check("fdi:tooth:16 диспетчеризируется", ok and ev_tooth.edit.called)

    # Files catalog callback
    ev_files = MockCallbackEvent("file_sys:protaper_ultimate")
    ok = await ct.dispatch_chairside_callback(bot, ev_files, "file_sys:protaper_ultimate")
    check("file_sys:protaper_ultimate диспетчеризируется", ok and ev_files.edit.called)

    # Anesthesia SOS callback
    ev_anes = MockCallbackEvent("anes_sos:buffering")
    ok = await ct.dispatch_chairside_callback(bot, ev_anes, "anes_sos:buffering")
    check("anes_sos:buffering диспетчеризируется", ok and ev_anes.edit.called)

    # Broken file callback
    ev_frag = MockCallbackEvent("file_frag:middle_curved")
    ok = await ct.dispatch_chairside_callback(bot, ev_frag, "file_frag:middle_curved")
    check("file_frag:middle_curved диспетчеризируется", ok and ev_frag.edit.called)

    # Pharma callback
    ev_pharma = MockCallbackEvent("pharma:kid:20")
    ok = await ct.dispatch_chairside_callback(bot, ev_pharma, "pharma:kid:20")
    check("pharma:kid:20 диспетчеризируется", ok and ev_pharma.edit.called)

    # AAE callback step 3 eval
    ev_aae = MockCallbackEvent("aae:s3:3:1:1:Зуб46")
    ok = await ct.dispatch_chairside_callback(bot, ev_aae, "aae:s3:3:1:1:Зуб46")
    check("aae:s3:3:1:1:Зуб46 диспетчеризируется и выдает результат", ok and ev_aae.edit.called)

    # Неизвестный колбэк -> False
    ev_unknown = MockCallbackEvent("unknown:cb")
    ok = await ct.dispatch_chairside_callback(bot, ev_unknown, "unknown:cb")
    check("чужой callback не перехватывается", not ok)

    # Командный фасад handle_if_chairside_command
    mock_cmd_ev = MagicMock()
    mock_cmd_ev.chat_id = -1001234567890
    bot.send_message = AsyncMock()

    ok_cmd = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/chairside")
    check("/chairside перехватывается", ok_cmd and bot.send_message.called)

    ok_fdi = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/fdi")
    check("/fdi перехватывается", ok_fdi and bot.send_message.called)

    ok_aae = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/aae")
    check("/aae перехватывается", ok_aae and bot.send_message.called)

    ok_other = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/help")
    check("/help не перехватывается chairside-фасадом", not ok_other)


asyncio.run(run_callback_tests())

print("\n" + "=" * 62)
print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("FAILS:", FAIL)
sys.exit(1 if FAIL else 0)
