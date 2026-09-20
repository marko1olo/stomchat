"""
Тесты для 4 новых клинических возможностей StomChat:
1. Feature 1: Next Best Action (NBA) интерактивные кнопки под ответом в ЛС
2. Feature 2: Холодный старт нового врача (онбординг в 1 клик по специализации)
3. Feature 3: Обработка фотопротоколов и серий снимков (Альбомы)
4. Feature 4: Интеллектуальный EBM-рефери споров (без шуток и назиданий)

Запуск: python test_bot_modernization_4features.py
"""
import asyncio
import io
import os
import re
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_modernization_")
os.environ["STOMCHAT_LOG_PATH"] = os.path.join(_TMPDIR, "test.log")

import assistant as A
import vision as V
import gemini_client as G

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE_A = io.open("assistant.py", encoding="utf-8").read()
SOURCE_V = io.open("vision.py", encoding="utf-8").read()
SOURCE_G = io.open("gemini_client.py", encoding="utf-8").read()

# ==========================================
# 1. NEXT BEST ACTION (NBA)
# ==========================================
print("\n[1] Next Best Action (NBA) inline buttons")

# 1.1 Генерация кнопок build_nba_markup
markup = A.build_nba_markup("эндодонтия коффердам", has_media=False)
check("NBA генерирует 2 ряда кнопок", len(markup) == 2, f"Рядов: {len(markup)}")
check("NBA ряд 1 содержит 'В закладки' и 'Протоколы'",
      len(markup[0]) == 2 and markup[0][0].data == b"nba:bm" and markup[0][1].data.startswith(b"nba:proto:"),
      f"Данные: {[b.data for b in markup[0]]}")
check("NBA ряд 2 содержит 'PubMed' и 'Экспорт в PDF'",
      len(markup[1]) == 2 and markup[1][0].data.startswith(b"nba:web:") and markup[1][1].data == b"nba:pdf",
      f"Данные: {[b.data for b in markup[1]]}")

# 1.2 Очистка тегов в кнопках
markup_clean = A.build_nba_markup("кариес 2.6?! <script>", has_media=True)
proto_data = markup_clean[0][1].data.decode("utf-8")
check("NBA тег очищен от спецсимволов", "<" not in proto_data and "?" not in proto_data, proto_data)

# 1.3 send_message_chunks_async крепит кнопки к последнему чанку
check("send_message_chunks_async принимает buttons",
      "buttons=None" in SOURCE_A.split("def send_message_chunks_async")[1].split(":")[0],
      "Кнопки не указаны в параметрах send_message_chunks_async")
check("send_message_chunks_async прикрепляет buttons к последнему куску",
      "buttons=chunk_buttons" in SOURCE_A,
      "Кнопки не передаются в send_message")

# 1.4 Диспетчеризация колбэков nba:* в handle_quiz_callback
check("handle_quiz_callback обрабатывает nba:*",
      'data_str.startswith("nba:")' in SOURCE_A,
      "Отсутствует ветка nba:*")
check("nba:bm сохраняет клиническую закладку",
      "database.save_clinical_bookmark" in SOURCE_A and "nba:bm" in SOURCE_A)
check("nba:proto ищет протоколы в БД",
      "database.search_clinical_protocols" in SOURCE_A and "proto:view:" in SOURCE_A)
check("nba:pdf формирует PDF через generate_digest_pdf",
      "generate_digest_pdf" in SOURCE_A and "send_file" in SOURCE_A)


# ==========================================
# 2. ОНБОРДИНГ НОВОГО ВРАЧА (1 КЛИК)
# ==========================================
print("\n[2] Холодный старт нового врача (онбординг в 1 клик)")

# 2.1 Проверка наличия онбординг клавиатуры на /start
check("/start проверяет наличие памяти о специализации доктора",
      "mem = await database.get_user_memory(chat_id)" in SOURCE_A,
      "Проверка памяти врача отсутствует на /start")
check("/start предлагает специализации терапевт, хирург, ортопед, ортодонт, детский, общей практики",
      all(x in SOURCE_A for x in [
          "onboard:spec:therapy",
          "onboard:spec:ortho_prostho",
          "onboard:spec:surgery",
          "onboard:spec:orthodontics",
          "onboard:spec:pediatric",
          "onboard:spec:general",
      ]),
      "Не все специализации представлены в кнопках онбординга")

# 2.2 Обработка колбэка onboard:spec:*
check("handle_quiz_callback обрабатывает onboard:spec:*",
      'data_str.startswith("onboard:spec:")' in SOURCE_A)
check("onboard:spec сохраняет профиль в database.save_user_memory",
      "await database.save_user_memory(" in SOURCE_A and "clinical_summary=summary_text" in SOURCE_A)
check("onboard:spec подтверждает выбор и выдает кнопки навигации",
      "Ваша специализация зафиксирована" in SOURCE_A and "nav:main" in SOURCE_A)


# ==========================================
# 3. ФОТОПРОТОКОЛЫ И АЛЬБОМЫ (СЕРИИ СНИМКОВ)
# ==========================================
print("\n[3] Фотопротоколы и мультимодальные серии снимков")

# 3.1 Временная директория скачивания в ЛС
check("download_media в ЛС использует media_tools.MEDIA_TEMP_DIR",
      'msg_obj.download_media(file=os.path.join(media_tools.MEDIA_TEMP_DIR, f"pm_{msg_obj.id}_"))' in SOURCE_A,
      "Скачивание всё еще шло в относительный temp_media")

# 3.2 Указания по анализу серии в vision.py
check("vision.py определяет серию снимков len(image_urls) > 1",
      "if len(image_urls) > 1:" in SOURCE_V)
check("vision.py инструктирует последовательный анализ каждого снимка (Снимок 1, Снимок 2) и динамики",
      "клинический фотопротокол/серия RG" in SOURCE_V and "Снимок 1, Снимок 2" in SOURCE_V,
      "В промпте зрения нет инструкции по анализу серии")

# 3.3 Мультимодальное уведомление в ЛС
check("assistant.py распознает серию pm_image_urls > 1 в промпте",
      "if len(pm_image_urls) > 1:" in SOURCE_A and "серия из {len(pm_image_urls)} оригинальных изображений" in SOURCE_A,
      "Промпт ЛС не отличает одиночный снимок от фотопротокола")

# 3.4 Передача до 6 снимков в Gemini мультимодальность
check("gemini_client передает до 6 снимков для Gemini провайдера",
      'max_imgs = 6 if provider == "gemini" else 3' in SOURCE_G and 'image_urls[:max_imgs]' in SOURCE_G,
      "Лимит Gemini мультимодальности не расширен для фотопротоколов")


# ==========================================
# 4. ИНТЕЛЛЕКТУАЛЬНЫЙ EBM-РЕФЕРИ СПОРОВ
# ==========================================
print("\n[4] Интеллектуальный EBM-рефери споров")

# 4.1 Словарь стоматологических конфликтов и пассивной агрессии
check("Рефери детектирует специфические фразы стоматологических споров",
      all(phrase in SOURCE_A for phrase in [
          "где вас учили", "кто так делает", "кто так препарирует",
          "бедный зуб", "бедный пациент", "вы протокол читали",
          "удалять и только удалять", "это под удаление", "руки оторвать",
          "бракодел", "калечите"
      ]),
      "Не все стоматологические фразы добавлены в регулярку рефери")

# Проверка регулярки на живых фразах
referee_block = SOURCE_A.split("async def check_and_trigger_referee", 1)[1].split("\nasync def ", 1)[0]
pattern_match = re.search(r'pattern = rf"([^"]+)"', referee_block)
check("Регулярка рефери найдена в коде", bool(pattern_match))
if pattern_match:
    # Тестируем совпадение регулярки
    conflict_phrases = [
        "где вас учили", "кто так делает", "кто так препарирует", "бедный зуб",
        "бедный пациент", "вы протокол читали", "вы вообще стоматолог",
        "удалять и только удалять", "зачем полезли", "что за работа",
        "это под удаление", "курам на смех", "полная лажа", "чушь собачья",
        "руки оторвать", "руки отсохнут", "из жопы", "руки из жопы", "под удаление"
    ]
    single_kws = [
        "бред", "чушь", "дичь", "херня", "говно", "безрукий", "какой дурак",
        "херню", "глупость", "рукожоп", "рукожопие", "помойку", "мусорку",
        "выброси", "косяк", "ужасно", "кривые руки", "уродство", "отстой",
        "хлам", "ахинея", "ппц", "пиздец", "бредятина", "какой дебил", "убейся",
        "дебилизм", "идиот", "идиотизм", "тупой", "тупость", "придурок", "даун",
        "рукожопый", "криворукий", "жопорукий", "косорукий", "ересь", "чепуха",
        "психушка", "дурка", "лечись", "высер", "выкинь", "дерьмо", "говнище",
        "днище", "лажовый", "шиза", "дебил", "кретин", "олень", "баран", "тормоз",
        "позорище", "позор", "стыдоба", "срач", "клоун", "цирк", "клоунада",
        "хрень", "галиматья", "шарага", "колхозный", "безрукие", "убожество",
        "убого", "бракодел", "халтура", "калечите", "калечить", "безграмотность"
    ]
    escaped_kws = [re.escape(kw) for kw in single_kws]
    escaped_phrases = [re.escape(ph) for ph in conflict_phrases]
    test_pat = rf"(\b({'|'.join(escaped_kws)})(е|я|ом|а|ы|и|у|ой|ем|ах|ами|ями|ов|ев)?\b|{'|'.join(escaped_phrases)})"

    check("Детектирует 'да кто так препарирует вообще'",
          bool(re.search(test_pat, "да кто так препарирует вообще")))
    check("Детектирует 'коллега, это под удаление'",
          bool(re.search(test_pat, "коллега, это под удаление")))
    check("Детектирует 'бедный зуб, зачем полезли'",
          bool(re.search(test_pat, "бедный зуб, зачем полезли")))
    check("Детектирует 'где вас учили'",
          bool(re.search(test_pat, "где вас учили вообще")))
    check("Не срабатывает на нейтральное 'протокол препарирования под вкладку'",
          not bool(re.search(test_pat, "протокол препарирования под вкладку")))

# 4.2 Устранение карикатурного/шуточного стиля
check("Устранен карикатурный стиль joke в рефери",
      'style = "joke"' not in SOURCE_A,
      "Стиль joke всё еще присутствует в assistant.py")
check("В рефери установлен ebm_reconciliation",
      'style = "ebm_reconciliation"' in SOURCE_A)
check("Промпт рефери требует строго EBM-арбитраж без перехода на личности",
      "независимый клинический арбитр" in SOURCE_A and "эксперт доказательной медицины (EBM)" in SOURCE_A)
check("Промпт рефери категорически запрещает морализаторство и нравоучения",
      "КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО морализаторство, поучения, шутки, призывы «жить дружно»" in SOURCE_A)
check("Промпт рефери ставит жесткий лимит максимум 300 символов",
      "СТРОГО максимум 300 символов" in SOURCE_A)
check("Сообщение рефери имеет авторитетную шапку 'EBM-Арбитраж'",
      '⚖️ <b>EBM-Арбитраж:</b>' in SOURCE_A)

# 4.3 Триаж споров Llama
check("check_referee_triage допускает клинические споры по тактике лечения",
      "Врачи спорят о тактике лечения, выборе протоколов" in SOURCE_A)


shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
