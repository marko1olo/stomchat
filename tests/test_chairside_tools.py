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

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

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

# Профилактика инфекционного эндокардита (AHA/ESE/ADA)
proph_adult = ct.calculate_antibiotic(70, is_prophylaxis=True)
check("профилактика эндокардита взрослый 70 кг: Амоксициллин 2.0 г (2000 мг)", "2.0 г" in proph_adult.drug_name and "2000 мг" in proph_adult.dosage)
check("профилактика разовая: курс 1 день", "однократно" in proph_adult.regimen.lower() or "1 день" in proph_adult.duration_days)

proph_all = ct.calculate_antibiotic(70, allergy_penicillin=True, is_prophylaxis=True)
check("профилактика при аллергии: Азитромицин 500 мг / Кларитромицин 500 мг", "Азитромицин" in proph_all.drug_name or "Кларитромицин" in proph_all.drug_name)
check("профилактика при аллергии: строгий запрет клиндамицина", "КЛИНДАМИЦИН" in proph_all.warning and "C. difficile" in proph_all.warning)

proph_kid = ct.calculate_antibiotic(20, is_child=True, is_prophylaxis=True)
check("профилактика ребенок 20 кг: 50 мг/кг (1000 мг = 20.0 мл)", "20.0 мл" in proph_kid.dosage and "1000 мг" in proph_kid.dosage)

proph_kid_all = ct.calculate_antibiotic(20, is_child=True, allergy_penicillin=True, is_prophylaxis=True)
check("профилактика ребенок аллергик 20 кг: 15 мг/кг (300 мг = 7.5 мл)", "7.5 мл" in proph_kid_all.dosage and "300" in proph_kid_all.dosage)

# Протокол дезинфекции каналов и профилактика парахлоранилина (PCA)
card_irr = ct.format_irrigation_protocol_card()
check("протокол дезинфекции каналов: NaOCl 3-5.25%", "NaOCl" in card_irr)
check("протокол дезинфекции каналов: 17% ЭДТА", "17% ЭДТА" in card_irr or "ЭДТА" in card_irr)
check("протокол дезинфекции каналов: промежуточный смыв физраствором", "0.9% физраствора" in card_irr or "промежуточный смыв" in card_irr.lower())
check("протокол дезинфекции каналов: 2% Хлоргексидин", "2% Хлоргексидин" in card_irr or "Хлоргексидин" in card_irr)
check("протокол дезинфекции каналов: запрет парахлоранилина PCA", "парахлоранилин" in card_irr and "PCA" in card_irr and "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО" in card_irr)


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
# [9] Протоколы фиксации реставраций (E.max, ZrO2, Полевой шпат)
# ==============================================================================
print("\\n[9] Протоколы адгезивной фиксации (E.max, ZrO2, Полевой шпат)")

# 1. E.max (дисиликат лития)
emax = ct.get_cementation_protocol("emax")
check("E.max: протокол найден по ключу 'emax'", emax is not None)
check("E.max: травление HF ровно 20 секунд", "20 секунд" in emax.etching_time)
check("E.max: силанизация 60 секунд", "60 секунд" in emax.priming_protocol and "Силан" in emax.priming_protocol)
check("E.max: КАТЕГОРИЧЕСКИЙ ЗАПРЕТ пескоструя корундом Al2O3", "ЗАПРЕЩЕНО пескоструить E.max" in emax.strict_prohibitions)
check("E.max: сниппет МИС в <code>...</code>", len(emax.mis_snippet) > 50 and "HF 5% 20 сек" in emax.mis_snippet)

emax_card = ct.format_cementation_card(emax)
check("E.max: карточка содержит HTML-теги и блок МИС", "<b>" in emax_card and "<code>" in emax_card)

# Поиск по алиасам E.max
check("E.max: поиск по алиасу 'дисиликат'", ct.get_cementation_protocol("дисиликат") is not None)
check("E.max: поиск по алиасу 'емакс'", ct.get_cementation_protocol("емакс") is not None)

# 2. Диоксид циркония (ZrO2)
zirconia = ct.get_cementation_protocol("zirconia")
check("ZrO2: протокол найден по ключу 'zirconia'", zirconia is not None)
check("ZrO2: пескоструй Al2O3 50 мкм", "Al2O3 50" in zirconia.etching_agent)
check("ZrO2: праймер с 10-MDP мономером", "10-MDP" in zirconia.priming_protocol)
check("ZrO2: ЗАПРЕТ плавиковой кислоты HF", "ЗАПРЕЩЕНО травление плавиковой кислотой" in zirconia.strict_prohibitions)
check("ZrO2: ЗАПРЕТ очистки H3PO4 после примерки", "ЗАПРЕЩЕНА очистка циркония ортофосфорной кислотой" in zirconia.strict_prohibitions)
check("ZrO2: сниппет МИС содержит APC протокол", "APC" in zirconia.mis_snippet and "Ivoclean" in zirconia.mis_snippet)

# 3. Полевошпатная керамика
feld = ct.get_cementation_protocol("feldspathic")
check("Полевой шпат: протокол найден", feld is not None)
check("Полевой шпат: травление HF 60-90 секунд", "60–90 секунд" in feld.etching_time)
check("Полевой шпат: светоотверждаемый композитный цемент", "Светоотверждаемый" in feld.recommended_cement)

# 4. Вкладки и металлокерамика
inlay = ct.get_cementation_protocol("composite_inlay")
check("Композитная вкладка: протокол в базе", inlay is not None)
mc = ct.get_cementation_protocol("metal_ceramic")
check("Металлокерамика: протокол в базе", mc is not None)


# ==============================================================================
# [10] Препарирование: Chamfer vs Vertiprep (Биоширина, Феррул)
# ==============================================================================
print("\\n[10] Препарирование: Chamfer vs Vertiprep")

# 1. Chamfer
chamfer = ct.get_prep_protocol("chamfer")
check("Chamfer: протокол найден", chamfer is not None)
check("Chamfer: закругленный уступ 0.8-1.2 мм", "0.8–1.2 мм" in chamfer.geometry_and_depth)
check("Chamfer: обязателен феррул >= 1.5-2.0 мм", "1.5–2.0 мм" in chamfer.ferrule_requirement)
check("Chamfer: сохранение биоширины 2.04 мм", "2.04 мм" in chamfer.biologic_width_rules)
check("Chamfer: бор 856 (торпеда)", "856" in chamfer.recommended_burs)

chamfer_card = ct.format_prep_card(chamfer)
check("Chamfer: карточка содержит блок МИС", "<code>" in chamfer_card and "856-016" in chamfer_card)

# 2. Vertiprep / B.O.P.T.
verti = ct.get_prep_protocol("vertiprep")
check("Vertiprep: протокол найден", verti is not None)
check("Vertiprep: тангенциальный сход без уступа", "без уступа" in verti.geometry_and_depth.lower())
check("Vertiprep: бор 862 (пламя)", "862" in verti.recommended_burs)
check("Vertiprep: ЗАПРЕТ для бескаркасной полевошпатной керамики и тонкого E.max", "ЗАПРЕЩЕНО для бескаркасной" in verti.contraindications)
check("Vertiprep: обязательная временная коронка PMMA для профиля прорезывания", "PMMA" in verti.impression_and_temp)

# 3. Сравнительный консилиум
comp = ct.get_prep_protocol("comparison")
check("Сравнение Chamfer vs Vertiprep: протокол в базе", comp is not None and "Золотое правило" in comp.clinical_pearl)


# ==============================================================================
# [11] Торки имплантатов и абатментов (15, 25, 30, 32, 35 Н·см)
# ==============================================================================
print("\\n[11] Торки имплантатов и абатментов")

# 1. Клинические стандарты (general)
gen_t = ct.get_implant_torque("general")
check("Стандарты торка: протокол найден", gen_t is not None)
check("Формирователь: ручная затяжка 10-15 Нсм", "10–15 Н·см" in gen_t.healing_abutment_torque)
check("Временный абатмент: 15-20 Нсм", "15–20 Н·см" in gen_t.temporary_abutment_torque)
check("Постоянный абатмент: 30-35 Нсм с дотягом через 10 мин", "30–35 Н·см" in gen_t.permanent_abutment_torque and "settling effect" in gen_t.permanent_abutment_torque)
check("Мультиюнит винт моста: СТРОГО 15 Нсм", "СТРОГО 15 Н·см" in gen_t.prosthetic_screw_torque)

# 2. Straumann
straumann_t = ct.get_implant_torque("straumann")
check("Straumann: торк постоянного абатмента 35 Нсм", "35 Н·см" in straumann_t.permanent_abutment_torque)
check("Straumann: отвертка SCS / TorcFit", "SCS" in straumann_t.screwdriver_profile)

# 3. Neodent Grand Morse
neodent_t = ct.get_implant_torque("neodent")
check("Neodent GM: торк 32 Нсм", "32 Н·см" in neodent_t.permanent_abutment_torque)
check("Neodent GM: отвертка Neo (шестилучевой)", "Neo" in neodent_t.screwdriver_profile)
check("Neodent GM: ЗАПРЕТ шестигранника 1.2 мм", "ЗАПРЕЩЕНО затягивать винты Neodent GM шестигранником" in neodent_t.warning_and_pearl)

# 4. Osstem TSIII
osstem_t = ct.get_implant_torque("osstem")
check("Osstem: торк 30 Нсм", "30 Н·см" in osstem_t.permanent_abutment_torque)
check("Osstem: отвертка 1.2 Hex", "1.2 Hex" in osstem_t.screwdriver_profile)

# 5. Dentium SuperLine
dentium_t = ct.get_implant_torque("dentium")
check("Dentium: торк 30 Нсм", "30 Н·см" in dentium_t.permanent_abutment_torque)
check("Dentium: отвертка 1.27 Hex (.050 inch)", "1.27" in dentium_t.screwdriver_profile)

# 6. Nobel Biocare
nobel_t = ct.get_implant_torque("nobel")
check("Nobel: торк 35 Нсм", "35 Н·см" in nobel_t.permanent_abutment_torque)
check("Nobel: отвертка Unigrip", "Unigrip" in nobel_t.screwdriver_profile)

# 7. Astra Tech EV
astra_t = ct.get_implant_torque("astra")
check("Astra Tech: торк постоянного абатмента строго 25 Нсм (не 35!)", "25 Н·см" in astra_t.permanent_abutment_torque)

# 8. Мультиюниты (All-on-4 / All-on-6)
multi_t = ct.get_implant_torque("multiunit")
check("Мультиюниты: абатмент в имплантат 30-35 Нсм", "30–35 Н·см" in multi_t.permanent_abutment_torque or "30–35 Н·см" in multi_t.multiunit_torque)
check("Мультиюниты: протетический винт моста СТРОГО 15 Нсм", "15 Н·см" in multi_t.prosthetic_screw_torque and "СТРОГО" in multi_t.prosthetic_screw_torque)
check("Мультиюниты: предупреждение о срезе микрорезьбы M1.4", "M1.4" in multi_t.prosthetic_screw_torque and "срезает" in multi_t.prosthetic_screw_torque)

torque_card = ct.format_implant_torque_card(multi_t)
check("Торк карточка форматируется в HTML с блоком МИС", "<code>" in torque_card and "All-on-4" in torque_card)



# ==============================================================================
# [12] Оценка коронок непосредственно после фиксации (Контроль излишков, краевой зазор, окклюзия)
# ==============================================================================
print("\n[12] Оценка коронок непосредственно после фиксации")

# 1. Удаление излишков цемента (excess)
chk_excess = ct.get_post_cementation_check("excess")
check("Контроль излишков цемента: протокол найден", chk_excess is not None)
check("Контроль излишков: риск периимплантита", "периимплантит" in chk_excess.clinical_risk.lower() or "пародонтит" in chk_excess.clinical_risk.lower())
check("Контроль излишков: фаза геля (tack-cure 1-2 сек)", any("гель" in step.lower() or "tack-cure" in step.lower() for step in chk_excess.protocol_steps))
check("Контроль излишков: интердентальный флосс с узелками", any("узелк" in step for step in chk_excess.protocol_steps))
check("Контроль излишков: DeOx глицериновый гель", any("DeOx" in step or "глицерин" in step for step in chk_excess.protocol_steps))
check("Контроль излишков: критерий допуска - борозда свободна, рентген без рентгеноконтрастных теней", "борозд" in chk_excess.tolerance_criteria.lower())
check("Контроль излишков: сниппет МИС в <code>...</code>", "<code>" in ct.format_post_cement_card(chk_excess) and "Удаление излишков" in chk_excess.mis_snippet)

# 2. Краевое прилегание (margin)
chk_margin = ct.get_post_cementation_check("margin")
check("Краевое прилегание: протокол найден", chk_margin is not None)
check("Краевое прилегание: риск вторичного кариеса при зазоре >80-120 мкм", "кариес" in chk_margin.clinical_risk.lower() or "80" in chk_margin.clinical_risk)
check("Краевое прилегание: зондирование острым зондом #17/23", any("зондом" in step for step in chk_margin.protocol_steps))
check("Краевое прилегание: критерий зазора < 50-80 мкм", "< 50–80 мкм" in chk_margin.tolerance_criteria or "50" in chk_margin.tolerance_criteria)
check("Краевое прилегание: сниппет МИС", "краево" in chk_margin.mis_snippet.lower())

# 3. Окклюзионный контроль (occlusion)
chk_occ = ct.get_post_cementation_check("occlusion")
check("Окклюзионный контроль: протокол найден", chk_occ is not None)
check("Окклюзионный контроль: фольга Бауша 8-12 мкм (Shimstock)", any("Shimstock" in step or "8" in step or "12" in step for step in chk_occ.protocol_steps))
check("Окклюзионный контроль: клыковое ведение без суперконтактов", any("клыков" in step for step in chk_occ.protocol_steps))
check("Окклюзионный контроль: имплантаты - инфраокклюзия 10-15 мкм при легком смыкании", any("10" in s and ("infra" in s.lower() or "инфр" in s.lower()) for s in chk_occ.protocol_steps))
check("Окклюзионный контроль: сниппет МИС", "Окклюзионная" in chk_occ.mis_snippet or "Bausch" in chk_occ.mis_snippet)

# 4. Сводный чек-лист (all)
chk_all = ct.get_post_cementation_check("all")
check("Сводный чек-лист сдачи: протокол найден", chk_all is not None)
check("Сводный чек-лист: карточка форматируется в HTML", "<code>" in ct.format_post_cement_card(chk_all))


# ==============================================================================
# [13] Траблшутинг имплантатов (Не садится трансфер, Bone Profiler, Защемление десны, Слизанный винт)
# ==============================================================================
print("\n[13] Траблшутинг имплантатов")

# 1. Трансфер не садится (transfer_not_seating)
imp_tr = ct.get_implant_trouble_protocol("transfer_not_seating")
check("Трансфер не садится: протокол найден", imp_tr is not None)
check("Трансфер не садится: обязательный рентген параллельным лучом", "параллельным" in imp_tr.diagnostic_step.lower() or "прицельный" in imp_tr.diagnostic_step.lower())
check("Трансфер не садится: поиск граней индекса ручной отверткой", any("индекс" in s.lower() or "провал" in s.lower() for s in imp_tr.chairside_solution))
check("Трансфер не садится: строгий запрет силовой затяжки ключом", "ЗАПРЕЩЕНО затягивать" in imp_tr.strict_prohibition)
check("Трансфер не садится: сниппет МИС", "Припасовка трансфера" in imp_tr.mis_snippet)

# 2. Костная интерференция и Bone Profiler (bone_interference)
imp_bone = ct.get_implant_trouble_protocol("bone_interference")
check("Костная интерференция: протокол найден", imp_bone is not None)
check("Bone Profiler: направляющий пин Guide Pin обязателен", any("Guide Pin" in s or "направляющий пин" in s for s in imp_bone.chairside_solution))
check("Bone Profiler: скорость 50-100 об/мин с физраствором", any("50" in s and "об/мин" in s for s in imp_bone.chairside_solution))
check("Bone Profiler: строгий запрет работы без направляющего пина", "ЗАПРЕЩЕНО работать костной фрезой без оригинального направляющего пина" in imp_bone.strict_prohibition)
check("Bone Profiler: сниппет МИС", "Bone Profiler" in imp_bone.mis_snippet)

# 3. Защемление десны (soft_tissue_impingement)
imp_soft = ct.get_implant_trouble_protocol("soft_tissue_impingement")
check("Защемление десны: протокол найден", imp_soft is not None)
check("Защемление десны: ретракция нитью #000 или микрогингивотомия", any("нить" in s or "гингивотоми" in s for s in imp_soft.chairside_solution))
check("Защемление десны: запрет зажимать слизистую под трансфер", "ЗАПРЕЩЕНО зажимать десну" in imp_soft.strict_prohibition)
check("Защемление десны: сниппет МИС", "Коррекция десневого профиля" in imp_soft.mis_snippet)

# 4. Слизанный винт (stripped_hex_screw)
imp_screw = ct.get_implant_trouble_protocol("stripped_hex_screw")
check("Слизанный винт: протокол найден", imp_screw is not None)
check("Слизанный винт: алмазная/фрикционная паста Grip", any("паст" in s.lower() for s in imp_screw.chairside_solution))
check("Слизанный винт: реверсивный экстрактор Rescue Extractor 15-20 об/мин", any("экстрактор" in s.lower() or "Rescue" in s for s in imp_screw.chairside_solution))
check("Слизанный винт: КАТЕГОРИЧЕСКИЙ ЗАПРЕТ турбинного рассверливания", "ЗАПРЕЩЕНО рассверливать поврежденный винт турбинным бором" in imp_screw.strict_prohibition)
check("Слизанный винт: сниппет МИС", "Screw Rescue" in imp_screw.mis_snippet)


# ==============================================================================
# [14] Атравматичное снятие коронок и мостов (Без ударного молотка)
# ==============================================================================
print("\n[14] Атравматичное снятие коронок")

# 1. Стандартное распиливание (sectioning_standard)
cr_std = ct.get_crown_removal_spec("sectioning_standard")
check("Атравматичное снятие: протокол найден", cr_std is not None)
check("Атравматичное снятие: бор Tri Hawk Talon / Komet", any("Tri Hawk" in b or "Komet" in b for b in cr_std.burs_and_technique))
check("Атравматичное снятие: глубина строго до слоя цемента", any("цемент" in b for b in cr_std.burs_and_technique))
check("Атравматичное снятие: раскрытие ручным рычагом на 90 градусов", "90°" in cr_std.splitting_instrument or "рычаг" in cr_std.splitting_instrument)
check("Атравматичное снятие: СТРОЖАЙШИЙ ЗАПРЕТ молотка Коппа / грузика", "ОПАСНОСТЬ УДАРНЫХ КОРОНКОСНИМАТЕЛЕЙ" in cr_std.danger_of_impact_hammer and "ЗАПРЕЩЕН" in cr_std.danger_of_impact_hammer)
check("Атравматичное снятие: сниппет МИС", "Атравматичное снятие коронки" in cr_std.mis_snippet)

# 2. Диоксид циркония (zirconia_cutting)
cr_zr = ct.get_crown_removal_spec("zirconia_cutting")
check("Распил циркония: протокол найден", cr_zr is not None)
check("Распил циркония: специальные боры Komet 4ZR / Z-Rex", any("4ZR" in b or "Z-Rex" in b or "Z-Cut" in b for b in cr_zr.burs_and_technique))
check("Распил циркония: обильное охлаждение >=50 мл/мин (защита от перегрева 800°C)", any("50" in b and "мл/мин" in b for b in cr_zr.burs_and_technique))
check("Распил циркония: прерывистые клюющие движения pecking motion", any("pecking" in b or "клюющ" in b for b in cr_zr.burs_and_technique))
check("Распил циркония: сниппет МИС", "Komet 4ZR" in cr_zr.mis_snippet or "циркон" in cr_zr.mis_snippet.lower())

# 3. Коронка на имплантате (implant_crown_removal)
cr_imp = ct.get_crown_removal_spec("implant_crown_removal")
check("Снятие коронки на имплантате: протокол найден", cr_imp is not None)
check("Снятие на имплантате: доступ к шахте винта через тефлон PTFE", any("PTFE" in b or "тефлон" in b for b in cr_imp.burs_and_technique))
check("Снятие на имплантате: СТРОЖАЙШИЙ ЗАПРЕТ ударов молотком (потеря остеоинтеграции)", "потере имплантата" in cr_imp.danger_of_impact_hammer or "остеоинтеграции" in cr_imp.danger_of_impact_hammer)
check("Снятие на имплантате: сниппет МИС", "Снятие коронки на имплантате" in cr_imp.mis_snippet)

# 4. Проверка алиаса get_crown_removal_protocol
check("Алиас get_crown_removal_protocol работает идентично get_crown_removal_spec", ct.get_crown_removal_protocol("zirconia_cutting") == cr_zr)


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
check("разметка быстрой фармы", len(ct.build_pharma_quick_markup()) >= 5)
check("разметка AAE шаг 1", len(ct.build_aae_markup(1)) == 4)
check("разметка контроля после фиксации формируется", len(ct.build_post_cement_menu_markup()) >= 3)
check("разметка траблшутинга имплантатов формируется", len(ct.build_implant_trouble_menu_markup()) >= 3)
check("разметка снятия коронок формируется", len(ct.build_crown_removal_menu_markup()) >= 3)


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

    # Prophylaxis callback
    ev_proph = MockCallbackEvent("pharma:prophylaxis:70")
    ok = await ct.dispatch_chairside_callback(bot, ev_proph, "pharma:prophylaxis:70")
    check("pharma:prophylaxis:70 диспетчеризируется", ok and ev_proph.edit.called)

    # Irrigation callback
    ev_irr = MockCallbackEvent("chairside:irrigation")
    ok = await ct.dispatch_chairside_callback(bot, ev_irr, "chairside:irrigation")
    check("chairside:irrigation диспетчеризируется", ok and ev_irr.edit.called)

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

    ok_irr = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/irrigation")
    check("/irrigation перехватывается", ok_irr and bot.send_message.called)

    # Cementation callbacks
    ev_cem_menu = MockCallbackEvent("cement:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_cem_menu, "cement:menu")
    check("cement:menu диспетчеризируется", ok and ev_cem_menu.edit.called)

    ev_emax = MockCallbackEvent("cement:emax")
    ok = await ct.dispatch_chairside_callback(bot, ev_emax, "cement:emax")
    check("cement:emax диспетчеризируется", ok and ev_emax.edit.called)

    ev_zirconia = MockCallbackEvent("cement:zirconia")
    ok = await ct.dispatch_chairside_callback(bot, ev_zirconia, "cement:zirconia")
    check("cement:zirconia диспетчеризируется", ok and ev_zirconia.edit.called)

    # Prep callbacks
    ev_prep_menu = MockCallbackEvent("prep:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_prep_menu, "prep:menu")
    check("prep:menu диспетчеризируется", ok and ev_prep_menu.edit.called)

    ev_chamfer = MockCallbackEvent("prep:chamfer")
    ok = await ct.dispatch_chairside_callback(bot, ev_chamfer, "prep:chamfer")
    check("prep:chamfer диспетчеризируется", ok and ev_chamfer.edit.called)

    ev_verti = MockCallbackEvent("prep:vertiprep")
    ok = await ct.dispatch_chairside_callback(bot, ev_verti, "prep:vertiprep")
    check("prep:vertiprep диспетчеризируется", ok and ev_verti.edit.called)

    # Implant torque callbacks
    ev_t_menu = MockCallbackEvent("torque_imp:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_t_menu, "torque_imp:menu")
    check("torque_imp:menu диспетчеризируется", ok and ev_t_menu.edit.called)

    ev_neodent = MockCallbackEvent("torque_imp:neodent")
    ok = await ct.dispatch_chairside_callback(bot, ev_neodent, "torque_imp:neodent")
    check("torque_imp:neodent диспетчеризируется", ok and ev_neodent.edit.called)

    ev_multi = MockCallbackEvent("torque_imp:multiunit")
    ok = await ct.dispatch_chairside_callback(bot, ev_multi, "torque_imp:multiunit")
    check("torque_imp:multiunit диспетчеризируется", ok and ev_multi.edit.called)

    # Новые команды в handle_if_chairside_command
    ok_cem = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/cement")
    check("/cement перехватывается", ok_cem and bot.send_message.called)

    ok_prep = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/prep")
    check("/prep перехватывается", ok_prep and bot.send_message.called)

    ok_torque_imp = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/implant_torque")
    check("/implant_torque перехватывается", ok_torque_imp and bot.send_message.called)


    # Post-cementation callbacks
    ev_pc_menu = MockCallbackEvent("post_cement:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_pc_menu, "post_cement:menu")
    check("post_cement:menu диспетчеризируется", ok and ev_pc_menu.edit.called)

    ev_pc_excess = MockCallbackEvent("post_cement:excess")
    ok = await ct.dispatch_chairside_callback(bot, ev_pc_excess, "post_cement:excess")
    check("post_cement:excess диспетчеризируется", ok and ev_pc_excess.edit.called)

    ev_pc_margin = MockCallbackEvent("post_cement:margin")
    ok = await ct.dispatch_chairside_callback(bot, ev_pc_margin, "post_cement:margin")
    check("post_cement:margin диспетчеризируется", ok and ev_pc_margin.edit.called)

    ev_pc_occ = MockCallbackEvent("post_cement:occlusion")
    ok = await ct.dispatch_chairside_callback(bot, ev_pc_occ, "post_cement:occlusion")
    check("post_cement:occlusion диспетчеризируется", ok and ev_pc_occ.edit.called)

    # Implant troubleshooting callbacks
    ev_it_menu = MockCallbackEvent("imp_trouble:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_it_menu, "imp_trouble:menu")
    check("imp_trouble:menu диспетчеризируется", ok and ev_it_menu.edit.called)

    ev_it_bone = MockCallbackEvent("imp_trouble:bone_interference")
    ok = await ct.dispatch_chairside_callback(bot, ev_it_bone, "imp_trouble:bone_interference")
    check("imp_trouble:bone_interference диспетчеризируется", ok and ev_it_bone.edit.called)

    ev_it_seating = MockCallbackEvent("imp_trouble:transfer_not_seating")
    ok = await ct.dispatch_chairside_callback(bot, ev_it_seating, "imp_trouble:transfer_not_seating")
    check("imp_trouble:transfer_not_seating диспетчеризируется", ok and ev_it_seating.edit.called)

    ev_it_screw = MockCallbackEvent("imp_trouble:stripped_hex_screw")
    ok = await ct.dispatch_chairside_callback(bot, ev_it_screw, "imp_trouble:stripped_hex_screw")
    check("imp_trouble:stripped_hex_screw диспетчеризируется", ok and ev_it_screw.edit.called)

    # Crown removal callbacks
    ev_cr_menu = MockCallbackEvent("crown_rem:menu")
    ok = await ct.dispatch_chairside_callback(bot, ev_cr_menu, "crown_rem:menu")
    check("crown_rem:menu диспетчеризируется", ok and ev_cr_menu.edit.called)

    ev_cr_std = MockCallbackEvent("crown_rem:sectioning_standard")
    ok = await ct.dispatch_chairside_callback(bot, ev_cr_std, "crown_rem:sectioning_standard")
    check("crown_rem:sectioning_standard диспетчеризируется", ok and ev_cr_std.edit.called)

    ev_cr_zr = MockCallbackEvent("crown_rem:zirconia_cutting")
    ok = await ct.dispatch_chairside_callback(bot, ev_cr_zr, "crown_rem:zirconia_cutting")
    check("crown_rem:zirconia_cutting диспетчеризируется", ok and ev_cr_zr.edit.called)

    ev_cr_imp = MockCallbackEvent("crown_rem:implant_crown_removal")
    ok = await ct.dispatch_chairside_callback(bot, ev_cr_imp, "crown_rem:implant_crown_removal")
    check("crown_rem:implant_crown_removal диспетчеризируется", ok and ev_cr_imp.edit.called)

    # Новые команды: /crown_eval, /transfer_sos, /crown_removal
    ok_ce = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/crown_eval")
    check("/crown_eval перехватывается", ok_ce and bot.send_message.called)

    ok_ts = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/transfer_sos")
    check("/transfer_sos перехватывается", ok_ts and bot.send_message.called)

    ok_cr = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/crown_removal")
    check("/crown_removal перехватывается", ok_cr and bot.send_message.called)

    ok_other = await ct.handle_if_chairside_command(bot, mock_cmd_ev, "/help")
    check("/help не перехватывается chairside-фасадом", not ok_other)


def test_all_chairside_tools():
    asyncio.run(run_callback_tests())
    assert not FAIL, f"Chairside tools failed: {FAIL}"


if __name__ == "__main__":
    test_all_chairside_tools()
    print("\n" + "=" * 62)
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print("FAILS:", FAIL)
    sys.exit(1 if FAIL else 0)
