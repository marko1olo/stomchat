import os
import re
import json
import sqlite3
import asyncio
import logging
import random
from datetime import datetime, timedelta
from blocking_tools import generate_gemini_text_async
import vision
import database
logger = logging.getLogger("assistant")

STATE_PATH = "assistant_state.json"
LOG_PATH = "shadow_assistant.log"
TEST_CHAT_ID = -1003735006121
TEST_TOPIC_ID = 26

SHADOW_TESTING = os.getenv("SHADOW_TESTING", "False").lower() in ("true", "1", "yes")
BOT_ID = None
LAST_REFEREE_RUN = datetime(2000, 1, 1)
USER_COOLDOWNS = {}

def check_user_cooldown(chat_id, user_id, command, seconds=30):
    key = (chat_id, user_id, command)
    now = datetime.now()
    if key in USER_COOLDOWNS:
        elapsed = (now - USER_COOLDOWNS[key]).total_seconds()
        if elapsed < seconds:
            return int(seconds - elapsed)
    USER_COOLDOWNS[key] = now
    return 0

async def send_message_chunks_async(bot_client, chat_id, text, **kwargs):
    """Sends a long message in chunks of <= 4000 characters, splitting by paragraphs if possible."""
    if len(text) <= 4000:
        await bot_client.send_message(entity=chat_id, message=text, **kwargs)
        return
        
    paragraphs = text.split("\n\n")
    current_chunk = ""
    for p in paragraphs:
        if len(current_chunk) + len(p) + 2 > 4000:
            if current_chunk:
                await bot_client.send_message(entity=chat_id, message=current_chunk.strip(), **kwargs)
                current_chunk = ""
            if len(p) > 4000:
                for i in range(0, len(p), 4000):
                    await bot_client.send_message(entity=chat_id, message=p[i:i+4000], **kwargs)
            else:
                current_chunk = p
        else:
            if current_chunk:
                current_chunk += "\n\n" + p
            else:
                current_chunk = p
                
    if current_chunk:
        await bot_client.send_message(entity=chat_id, message=current_chunk.strip(), **kwargs)

async def init_assistant(bot_client):
    global BOT_ID
    try:
        me = await bot_client.get_me()
        BOT_ID = me.id
        logger.info(f"Assistant initialized with BOT_ID: {BOT_ID}")
        
        # Set inline bot command suggestions in Telegram UI
        from telethon import functions, types
        await bot_client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code='',
            commands=[
                types.BotCommand(command='start', description='Запустить приветствие и инициализировать бота'),
                types.BotCommand(command='help', description='Показать памятку по работе с ассистентом'),
                types.BotCommand(command='protocols', description='Показать доступные клинические протоколы в базе'),
                types.BotCommand(command='calc', description='Открыть шпаргалку-калькулятор анестезии'),
                types.BotCommand(command='quiz', description='Запустить клиническую викторину'),
                types.BotCommand(command='stats', description='Показать популярные темы обсуждений в чате'),
                types.BotCommand(command='bookmarks', description='Показать сохраненные вами клинические закладки'),
                types.BotCommand(command='search', description='Прямой поиск по базе знаний стоматологии'),
                types.BotCommand(command='case', description='Запустить интерактивный клинический симулятор'),
                types.BotCommand(command='abort', description='Сбросить активный клинический симулятор'),
            ]
        ))
        logger.info("Bot inline command suggestions successfully registered.")
    except Exception as e:
        logger.error(f"Failed to initialize assistant or set commands: {e}")

STOP_WORDS = {
    "это", "как", "для", "или", "что", "этот", "себя", "себе", "меня", "тебя", 
    "было", "быть", "если", "хочу", "только", "когда", "тоже", "есть", "было", 
    "будет", "просто", "здесь", "очень", "даже", "если", "тоже", "типа", "вообще",
    "надо", "можно", "хотя", "коллеги", "привет", "здравствуйте", "какой", "такой",
    "какие", "такие", "очень", "этого", "чтобы", "один", "одна", "одно", "будет",
    "всем", "всех", "этом", "этой", "этих", "были", "была", "были", "того", "тому"
}

DENTAL_KEYWORDS = {
    "зуб", "канал", "уступ", "бор", "файл", "цемент", "коронка", "коронок", "имплант", 
    "активация", "винил", "преп", "эндо", "гнатол", "окклюз", "сустав", "внчс",
    "сплинт", "капп", "слеп", "оттис", "трансфер", "абатм", "циркон", "пмма", "pmma",
    "керам", "ультразвук", "эйтис", "петл", "спредер", "визуали", "микроскоп",
    "пескоструй", "коффердам", "раббердам", "кламп", "плавиков", "силан", "бонд",
    "травлен", "адгезив", "композит", "клинич", "диагно", "анестез", "артикаин",
    "мепивакаин", "убистезин", "ультракаин", "лидокаин", "пульп", "апекс", "периодонт",
    "периодонтит", "пульпит", "кариес", "гингивит", "пародонт", "пародонтоз", "рецесс",
    "десна", "десны", "десневой", "костн", "альвеол", "синус", "остеот", "мембран",
    "шовн", "викрил", "пролен", "монофил", "хирург", "удален", "экстракц", "лунк",
    "кюрет", "остеоинтегр", "формировател", "заглушк", "крошк", "биоосс", "bio-oss",
    "аллоплант", "ксенотрансп", "брекет", "элайнер", "ретейнер", "дистализ", "мезиализ",
    "дуга", "дуги", "лигатур", "ортодонт", "ортопед", "терапевт", "кт", "клкт", "оптг",
    "визиограф", "рентген", "снимок", "снимка", "бинокуляр", "лупы", "эндомотор",
    "апекслокатор", "автоклав", "стерилиз", "дентин", "эмаль", "эмали", "челюст",
    "прикус", "резец", "резц", "клык", "премоляр", "моляр", "реципрок", "протейпер",
    "мту", "mtwo", "пасс-файл", "гипохлорит", "хлоргексидин", "эдта", "edta",
    "гуттаперч", "силер", "обтурац", "латеральн", "вертикальн", "распломбиров",
    "анкерн", "штифт", "платок", "ирригац", "мост", "протез", "вкладк", "накладк",
    "оверлей", "рондоклип", "сканмаркер", "ложка", "артикулятор", "депрограмматор",
    "коис", "миостимуляц", "шина", "емакс", "e.max", "e-max", "полевошпат", "каркас",
    "полимеризац", "фотополимер", "клиновидн", "абфракц", "стираемост", "бруксизм",
    "флюороз", "гипоплази", "фиссур", "герметизац", "карман", "грейси", "gracey",
    "скалер", "чистк", "налет", "камень", "сст", "сдг", "трансплантат", "вестибулопласт",
    "микроимплант", "тяга", "пломб", "девитал", "мышьяк", "резекц", "цистэкт",
    "гранулем", "кист", "киста", "фистул", "свищ", "перфорац", "полость", "полости",
    "кариозн", "поддеснев", "наддеснев", "шейка", "верхушка", "апикальн", "дентальный",
    "стоматолог", "эндодонт", "пародонтолог", "кофердам", "остеопласт", "винир",
    "синуслифт", "костнаяпласт", "аугмент", "регенер", "остеосинтез", "стекловолокн",
    "свш", "металлокерам", "ортодонтич", "обтуратор", "термафил", "thermafil",
    "airflow", "air-flow", "пазух", "гайморов", "мандибул", "ментальн", "подбородоч",
    "альвеолярн", "остеотоми"
}

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading assistant state: {e}")
    return {
        "last_passive_run": "2000-01-01T00:00:00",
        "last_passive_text_run": "2000-01-01T00:00:00",
        "last_passive_media_run": "2000-01-01T00:00:00",
        "processed_threads": []
    }
def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Error saving assistant state: {e}")
def write_to_shadow_log(message):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        logger.error(f"Error writing to shadow log: {e}")

def extract_keywords(text):
    cleaned = re.sub(r"[^\w\s-]", " ", text.lower())
    words = cleaned.split()
    keywords = []
    for w in words:
        if len(w) >= 4 and w not in STOP_WORDS and not w.isdigit():
            stem = w
            for suffix in ["ами", "ями", "ыми", "ями", "ом", "ем", "ам", "ям", "ах", "ях", "ых", "их", "ов", "ев", "ие", "ия", "ию", "ии", "ей", "ой", "а", "у", "е", "ы", "и", "о"]:
                if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                    stem = w[:-len(suffix)]
                    break
            keywords.append(stem)
            
    # Deduplicate
    keywords = list(set(keywords))
    
    # Prioritize dental-specific keywords at the front of the list
    dental_matches = []
    other_matches = []
    for kw in keywords:
        is_dental = any(dk in kw for dk in DENTAL_KEYWORDS)
        if is_dental:
            dental_matches.append(kw)
        else:
            other_matches.append(kw)
            
    return dental_matches + other_matches

def search_knowledge_corpus(keywords):
    if not keywords:
        return "", ""
        
    wiki_facts = []
    archive_msgs = []
    
    # 1. Search stomat_wiki.db
    if os.path.exists("stomat_wiki.db"):
        try:
            conn = sqlite3.connect("stomat_wiki.db", timeout=30)
            conn.execute("PRAGMA busy_timeout = 30000")
            c = conn.cursor()
            for kw in keywords:
                c.execute("SELECT category_code, content FROM distilled_facts WHERE content LIKE ? LIMIT 4", (f"%{kw}%",))
                for row in c.fetchall():
                    fact = f"[{row[0]}] {row[1]}"
                    if fact not in wiki_facts:
                        wiki_facts.append(fact)
                if len(wiki_facts) >= 25:
                    break
            conn.close()
        except Exception as e:
            logger.error(f"Error searching stomat_wiki.db: {e}")
            
    # 2. Search stomat_archive.db
    if os.path.exists("stomat_archive.db"):
        try:
            conn = sqlite3.connect("stomat_archive.db", timeout=30)
            conn.execute("PRAGMA busy_timeout = 30000")
            c = conn.cursor()
            for kw in keywords:
                c.execute("SELECT sender_name, text FROM archive_messages WHERE text LIKE ? AND text != '' LIMIT 4", (f"%{kw}%",))
                for row in c.fetchall():
                    msg = f"{row[0]}: {row[1]}"
                    if msg not in archive_msgs:
                        archive_msgs.append(msg)
                if len(archive_msgs) >= 25:
                    break
            conn.close()
        except Exception as e:
            logger.error(f"Error searching stomat_archive.db: {e}")
            
    wiki_corpus = "\n".join(wiki_facts[:20])
    archive_corpus = "\n".join(archive_msgs[:20])
    return wiki_corpus, archive_corpus

async def query_db_async(query_sql, params=()):
    # Helper to query the main bot database stomat_bot.db
    loop = asyncio.get_running_loop()
    def operation():
        conn = sqlite3.connect("stomat_bot.db", timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            c = conn.cursor()
            c.execute(query_sql, params)
            return c.fetchall()
        finally:
            conn.close()
    return await loop.run_in_executor(None, operation)
def clean_html_formatting(text):
    if not text:
        return ""
    # Strip database codes/fact indexes (e.g. [2.1.1], [1.3])
    text = re.sub(r'\s*\[\d+(?:\.\d+)+\]', '', text)
    # Convert Markdown bold **text** to HTML bold <b>text</b>
    text = re.sub(r'\*\*(.*?)\*\*', r'<b>\1</b>', text)
    # Temporarily hide valid HTML tags we want to support
    text = text.replace("<b>", "__B_OPEN__").replace("</b>", "__B_CLOSE__")
    text = text.replace("<i>", "__I_OPEN__").replace("</i>", "__I_CLOSE__")
    text = text.replace("<code>", "__C_OPEN__").replace("</code>", "__C_CLOSE__")
    # Escape raw HTML syntax characters to prevent Telegram parse errors
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Restore valid tags
    text = text.replace("__B_OPEN__", "<b>").replace("__B_CLOSE__", "</b>")
    text = text.replace("__I_OPEN__", "<i>").replace("__I_CLOSE__", "</i>")
    text = text.replace("__C_OPEN__", "<code>").replace("__C_CLOSE__", "</code>")
    return text


async def check_and_trigger_assistant(bot_client, event, msg_id, text, reply_to_msg_id, sender_first_name=None):
    global BOT_ID
    state = load_state()
    triggered = False
    trigger_reason = ""
    context_msgs = []
    is_dialogue = False
    
    # Try dynamic BOT_ID resolution if it is missing
    if reply_to_msg_id and not BOT_ID:
        try:
            me = await bot_client.get_me()
            BOT_ID = me.id
            logger.info(f"Dynamically resolved BOT_ID: {BOT_ID}")
        except Exception as e:
            logger.error(f"Failed to dynamically resolve BOT_ID: {e}")

    # 1. Check Dialogue Reaction (direct reply to the bot's own message)
    if reply_to_msg_id and BOT_ID:
        try:
            parent_msg = await event.client.get_messages(event.chat_id, ids=reply_to_msg_id)
            if parent_msg and parent_msg.sender_id == BOT_ID:
                is_dialogue = True
                triggered = True
                trigger_reason = f"Dialogue reply to bot message {reply_to_msg_id}"
                
                # Reconstruct reply chain
                chain = []
                curr = event.message
                for _ in range(5):
                    if not curr:
                        break
                    sender = await curr.get_sender()
                    name = "User"
                    if sender:
                        if hasattr(sender, 'first_name') and sender.first_name:
                            name = f"{sender.first_name} {getattr(sender, 'last_name', '') or ''}".strip()
                        elif hasattr(sender, 'title') and sender.title:
                            name = sender.title
                    name = name or "User"
                    
                    # Truncate extremely long dialogue messages to 1000 characters
                    msg_text = curr.message or ''
                    if len(msg_text) > 1000:
                        msg_text = msg_text[:1000] + "... [сообщение обрезано]"
                        
                    chain.append(f"{name}: {msg_text}")
                    if curr.reply_to and curr.reply_to.reply_to_msg_id:
                        curr = await event.client.get_messages(event.chat_id, ids=curr.reply_to.reply_to_msg_id)
                    else:
                        break
                context_msgs = chain[::-1]
        except Exception as e:
            logger.error(f"Error checking dialogue parent: {e}")
            
    # Cooldown check for all passive text triggers (1.5 hours)
    if not is_dialogue:
        last_run = datetime.fromisoformat(state.get("last_passive_text_run", "2000-01-01T00:00:00"))
        if datetime.now() - last_run < timedelta(minutes=90):
            return  # Within 1.5-hour cooldown, skip all passive text triggers!

    # 2. Check Reply Thread Reaction
    if not triggered and reply_to_msg_id:
        # Check if parent has media
        parent_rows = await query_db_async("SELECT has_media, text FROM messages WHERE msg_id = ?", (reply_to_msg_id,))
        if parent_rows and (parent_rows[0][0] == 1 or parent_rows[0][0] == True):
            # Count replies
            reply_count_rows = await query_db_async("SELECT COUNT(*) FROM messages WHERE reply_to_msg_id = ?", (reply_to_msg_id,))
            reply_count = reply_count_rows[0][0] if reply_count_rows else 0
            
            if reply_count >= 3 and reply_to_msg_id not in state.get("processed_threads", []):
                # We have a discussion under a clinical post!
                triggered = True
                trigger_reason = f"Clinical post {reply_to_msg_id} discussion thread (reply_count={reply_count})"
                # Mark thread as processed
                state.setdefault("processed_threads", []).append(reply_to_msg_id)
                # Keep thread in processed bounds
                if len(state["processed_threads"]) > 100:
                    state["processed_threads"].pop(0)
                
                # Update last passive run timestamp
                state["last_passive_text_run"] = datetime.now().isoformat()
                save_state(state)
                
                # Fetch parent + last replies for context
                rows = await query_db_async(
                    "SELECT sender_name, text FROM messages WHERE msg_id = ? OR reply_to_msg_id = ? ORDER BY date ASC",
                    (reply_to_msg_id, reply_to_msg_id)
                )
                context_msgs = [f"{r[0]}: {r[1]}" for r in rows]                
    # 2. Check Passive Trigger (General Chat Flow)
    if not triggered:
        # Get last 20 messages from DB
        last_msgs = await query_db_async(
            "SELECT sender_name, text, msg_id FROM messages ORDER BY date DESC LIMIT 20"
        )
        # Reorder chronologically
        last_msgs = last_msgs[::-1]
        
        if last_msgs:
            # Check triggers: last message has '?' OR last messages contain dental trigger words
            last_text = last_msgs[-1][1] or ""
            has_question = "?" in last_text
            
            # Count dental keywords in context
            full_context_text = " ".join([m[1] for m in last_msgs if m[1]]).lower()
            has_dental_topic = any(kw in full_context_text for kw in DENTAL_KEYWORDS)
            
            # Trigger on ANY question OR if there is an active dental topic
            if has_question or has_dental_topic:
                triggered = True
                trigger_reason = f"Passive trigger (has_question={has_question}, has_dental_topic={has_dental_topic}). Keywords: {search_keywords}"
                state["last_passive_text_run"] = datetime.now().isoformat()
                save_state(state)
                context_msgs = [f"{r[0]}: {r[1]}" for r in last_msgs]
                
                # If the triggering message is a reply, prepend the parent chain for full context
                if reply_to_msg_id:
                    try:
                        thread_rows = await query_db_async(
                            "SELECT sender_name, text FROM messages WHERE msg_id = ? OR reply_to_msg_id = ? ORDER BY date ASC",
                            (reply_to_msg_id, reply_to_msg_id)
                        )
                        if thread_rows:
                            thread_msgs = [f"{r[0]}: {r[1]}" for r in thread_rows]
                            # Merge: thread first, then recent context (deduplicated)
                            seen = set(thread_msgs)
                            extra = [m for m in context_msgs if m not in seen]
                            context_msgs = thread_msgs + extra
                    except Exception as thread_err:
                        logger.warning(f"Failed to fetch reply thread for passive context: {thread_err}")


    if not triggered:
        return

    # EXTRACT KEYWORDS & SEARCH DB
    # Исключаем сообщения бота из извлечения ключевых слов (защита от галлюцинаций)
    user_context_msgs = [m for m in context_msgs if not m.startswith("Бот ")]
    if not user_context_msgs:
        user_context_msgs = context_msgs
    full_context_str = " ".join(user_context_msgs)
    keywords = extract_keywords(full_context_str)
    
    # Filter keywords to only those matching or relevant to dental keywords
    dental_kw_matches = [kw for kw in keywords if any(dk in kw for dk in DENTAL_KEYWORDS)]
    # We want up to 12 search keywords for a richer database search
    search_keywords = dental_kw_matches if dental_kw_matches else keywords[:12]
    if len(search_keywords) < 12:
        other_kws = [kw for kw in keywords if kw not in search_keywords]
        search_keywords = (search_keywords + other_kws)[:12]
                

    
    wiki_corpus, archive_corpus = search_knowledge_corpus(search_keywords)
    
    if not is_dialogue and not wiki_corpus and not archive_corpus:
        # If corpus is empty, do not output anything (avoid generic AI fluff)
        logger.info("No matching knowledge corpus found. Skipping assistant run.")
        return

    # Определяем обращение ДО промпта — сами, не делегируем модели.
    # Модель просто начнёт с готового префикса, выбор уже сделан.
    import random
    if is_dialogue:
        address_prefix = ""  # В диалоге без обращения
    else:
        unique_senders = set()
        for cm in context_msgs:
            if ": " in cm:
                unique_senders.add(cm.split(": ", 1)[0].strip())

        if len(unique_senders) > 2:
            # Несколько людей → 50% "Коллеги," / 50% без обращения
            address_prefix = "Коллеги, " if random.random() < 0.5 else ""
        elif sender_first_name:
            # Один автор → 33% имя / 33% "Коллега," / 33% без обращения
            roll = random.random()
            if roll < 0.33:
                address_prefix = f"{sender_first_name}, "
            elif roll < 0.66:
                address_prefix = "Коллега, "
            else:
                address_prefix = ""
        else:
            address_prefix = ""

    if address_prefix:
        address_line = f'Начни ответ строго с "{address_prefix}" — это первые слова. Не меняй, не перефразируй.'
    else:
        address_line = "Начни ответ сразу по делу, без обращения и без имён."

    # BUILD PROMPT
    if is_dialogue:
        prompt = f"""
Ты — опытный стоматолог-практик, читаешь переписку коллег в чате "StomChat" и решил ответить на заданный вопрос.
Тебе 15+ лет клинической практики, ты видел всякое, говоришь прямо и не любишь воду.
Не строишь из себя учебник — ты коллега, который знает ответ и выдаёт его точно и ёмко.

История диалога (последние сообщения):
{chr(10).join(context_msgs)}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}

Похожие обсуждения из Архива чата:
{archive_corpus}

ИНСТРУКЦИИ:
1. {address_line}
2. Длина по ситуации — если тема сложная, можно 2-3 коротких абзаца. Если всё ясно в одной фразе — достаточно одной.
3. Никаких приветствий, «Уважаемые коллеги», вводных фраз и пожеланий в конце. Сразу по делу.
4. Тон: прямой, уверенный, peer-to-peer, как живой опытный врач-стоматолог в чате с коллегами. Используй привычный профессиональный сленг (снимок вместо рентгенограмма, каналы вместо корневые каналы, коронка, ортопед, терапевт, хирург и т.д.). Полностью избегай канцелярщины и фраз типа "Как ИИ...", "Рад помочь", "С уважением".
5. Ограничение по теме: Используй термины и Базу Знаний строго по контексту разговора. Если врачи обсуждают объёмы работы, графики, усталость, деньги или другие организационные темы, а не конкретный лечебный случай — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО читать клинические лекции и давать медицинские советы по лечению (например, приплетать BOPT, протоколы фиксации циркона и т.п.) из Базы Знаний, если об этом прямо не спросили. В таких случаях общайся только по теме диалога (объёмы, выгорание и т.д.).
6. Только доказанные факты. Домыслы, выдуманные протоколы и дозировки — строго запрещены. Если данных нет — так и скажи прямо.
7. Не повторяй то, что уже написали в чате. Принеси что-то новое — факт, уточнение, протокол, нюанс.
8. СМАЙЛИКИ: Используй их строго в ОДНОМ месте за весь ответ (например, в конце предложения). Не раскидывай по тексту. Подряд можно писать только 2-3 ржущих смайла (😂😂😂). Все остальные смайлы — строго по ОДНОМУ (например, только один 😎 или один 😤).
9. Разметка: только HTML — <b>жирный</b>. Никакого Markdown (**текст**).
10. ПРОАКТИВНОСТЬ: Если суть обсуждаемого вопроса неясна или не хватает вводных (например, нужен снимок) — свободно и по-простому переспроси или уточни. Если видишь, что можно уберечь коллег от ошибки или подсказать лучший протокол — будь проактивен и подскажи, даже если прямо не спрашивали.
11. ФУНКЦИОНАЛ БОТА: Если у тебя спрашивают "что ты умеешь", "какие команды есть" или просят описать функционал — честно перечисли свои фишки: ответы на клинические вопросы, разбор снимков (Vision), викторина /quiz, энциклопедия /wiki, клинические кейсы /case, калькулятор анестезии /calc и ночные дайджесты. Опиши это кратко, по-свойски. НЕ ВЫДУМЫВАЙ темы, о которых не спрашивали!
"""
    else:
        prompt = f"""
Ты — опытный стоматолог-практик, читаешь переписку коллег в чате "StomChat" и вставляешь точную, полезную реплику.
Тебе 15+ лет практики, ты говоришь коротко и по делу — как тот человек в чате, которого все слушают.

Текущая переписка в чате:
{chr(10).join(context_msgs)}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}

Похожие обсуждения из Архива чата:
{archive_corpus}

ИНСТРУКЦИИ:
1. {address_line}
2. Длина по ситуации — если тема требует развёрнутости, можно 2-3 коротких абзаца. Если ответ умещается в одну фразу — не тяни.
3. Никаких вводных («Согласно справке», «Исходя из переписки»), приветствий и концовок. Сразу суть.
4. Тон: прямой, уверенный, peer-to-peer, как живой опытный врач-стоматолог в чате с коллегами. Используй привычный профессиональный сленг (снимок вместо рентгенограмма, каналы вместо корневые каналы, коронка, ортопед, терапевт, хирург и т.д.). Полностью избегай канцелярщины и фраз типа "Как ИИ...", "Рад помочь", "С уважением".
5. Ограничение по теме: Используй термины и Базу Знаний строго по контексту разговора. Если врачи обсуждают объёмы работы, графики, усталость, деньги или другие организационные темы, а не конкретный лечебный случай — КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО читать клинические лекции и давать медицинские советы по лечению (например, приплетать BOPT, протоколы фиксации циркона и т.п.) из Базы Знаний, если об этом прямо не спросили. В таких случаях общайся только по теме диалога (объёмы, выгорание и т.д.).
6. Только доказанные факты. Домыслы запрещены. Если данных нет — скажи прямо: «По базе данных нет, но на практике...»
7. Не повторяй то что уже сказали. Принеси что-то новое — нюанс, уточнение, факт из базы.
8. СМАЙЛИКИ: Используй их строго в ОДНОМ месте за весь ответ (например, в конце предложения). Не раскидывай по тексту. Подряд можно писать только 2-3 ржущих смайла (😂😂😂). Все остальные смайлы — строго по ОДНОМУ (например, только один 😎 или один 😤).
9. Разметка: только HTML — <b>жирный</b>. Никакого Markdown (**текст**).
10. ПРОАКТИВНОСТЬ: Если суть обсуждаемого вопроса неясна или не хватает вводных (например, нужен снимок) — свободно и по-простому переспроси или уточни. Если видишь, что можно уберечь коллег от ошибки или подсказать лучший протокол — будь проактивен и подскажи, даже если прямо не спрашивали.
11. ФУНКЦИОНАЛ БОТА: Если у тебя спрашивают "что ты умеешь", "какие команды есть" или просят описать функционал — честно перечисли свои фишки: ответы на клинические вопросы, разбор снимков (Vision), викторина /quiz, энциклопедия /wiki, клинические кейсы /case, калькулятор анестезии /calc и ночные дайджесты. Опиши это кратко, по-свойски. НЕ ВЫДУМЫВАЙ темы, о которых не спрашивали!

ЕСЛИ тема чата — чистый флуд, приветствия, погода, политика, оффтоп без связи со стоматологией или медициной — верни ровно одно слово: IGNORE
"""

    logger.info(f"Triggered assistant! Reason: {trigger_reason}. Keywords: {search_keywords}")
    
    # CALL GEMINI
    status_ctx = {"kind": "assistant", "chat_id": event.chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    
    if error:
        logger.error(f"Assistant Gemini generation error: {error}")
        return
        
    reply_text = getattr(response, "text", None)
    if not reply_text:
        logger.warning("Assistant Gemini returned empty text.")
        return
        
    reply_text = reply_text.strip()
    reply_text = clean_html_formatting(reply_text)

    if not is_dialogue:
        if reply_text.upper() == "IGNORE" or "ignore" in reply_text.lower():
            logger.info("Assistant: Query was classified as off-topic or chitchat. Ignoring.")
            return
            
    # SENDING
    if SHADOW_TESTING and event.chat_id != TEST_CHAT_ID:
        # Shadow testing: deliver to test chat & topic
        shadow_message = f"[SHADOW TEST]\n\n{reply_text}"
        write_to_shadow_log(f"Reason: {trigger_reason}\nKeywords: {search_keywords}\nContext:\n{chr(10).join(context_msgs[-4:])}\nResponse:\n{reply_text}\n---")
        try:
            await bot_client.send_message(
                entity=TEST_CHAT_ID,
                message=shadow_message,
                reply_to=TEST_TOPIC_ID,
                parse_mode='html'
            )
            logger.info("Sent shadow assistant message to Telegram test topic.")
        except Exception as e:
            logger.error(f"Failed to send shadow assistant message to Telegram: {e}")
    else:
        # Live mode OR direct reply in test chat: reply directly to user message!
        reply_message = reply_text

        try:
            await bot_client.send_message(
                entity=event.chat_id,
                message=reply_message,
                reply_to=msg_id,
                parse_mode='html'
            )
            logger.info(f"Sent direct assistant reply to chat {event.chat_id}, message {msg_id}.")
        except Exception as e:
            logger.error(f"Failed to send direct assistant reply: {e}")
async def check_and_trigger_assistant_media(bot_client, message, msg_id, text, media_description):
    import config
    
    is_direct_reply = False
    if getattr(message, 'reply_to_msg_id', None):
        try:
            parent = await bot_client.get_messages(message.chat_id, ids=message.reply_to_msg_id)
            if parent and parent.sender_id == (await bot_client.get_me()).id:
                is_direct_reply = True
        except Exception:
            pass

    is_mentioned = False
    if text and config.BOT_USERNAME.lower() in text.lower():
        is_mentioned = True

    # Enforce 1.5-hour cooldown for passive media trigger, unless it's a direct reply or mention
    if not (is_direct_reply or is_mentioned):
        state = load_state()
        last_run = datetime.fromisoformat(state.get("last_passive_media_run", "2000-01-01T00:00:00"))
        if datetime.now() - last_run < timedelta(minutes=90):
            return  # Within 1.5-hour cooldown, skip!

    # Construct a simple event-like object for direct compatibility
    class MediaEvent:
        def __init__(self, msg):
            self.message = msg
            self.client = msg.client
            self.chat_id = msg.chat_id
            
    event = MediaEvent(message)
    
    # 1. Parse keywords
    caption_text = text or ""
    full_context_str = caption_text + " " + media_description
    keywords = extract_keywords(full_context_str)
    
    # Check if there is dental content
    has_dental_topic = any(kw in full_context_str.lower() for kw in DENTAL_KEYWORDS)
    has_question = "?" in caption_text
    
    triggered = False
    trigger_reason = ""
    wiki_corpus = ""
    archive_corpus = ""
    is_dental = False
    
    # Limit keywords to 12
    dental_kw_matches = [kw for kw in keywords if any(dk in kw for dk in DENTAL_KEYWORDS)]
    search_keywords = dental_kw_matches if dental_kw_matches else keywords[:12]
    if len(search_keywords) < 12:
        other_kws = [kw for kw in keywords if kw not in search_keywords]
        search_keywords = (search_keywords + other_kws)[:12]
        
    if has_dental_topic or has_question:
        # Dental Case: Always query RAG!
        triggered = True
        trigger_reason = f"Dental media trigger (has_dental_topic={has_dental_topic}, has_question={has_question})"
        is_dental = True
        wiki_corpus, archive_corpus = search_knowledge_corpus(search_keywords)
    else:
        # Non-dental Meme/Coffee: Balancer probability check (35% chance to reply)
        # Bypassed only if the user explicitly asks a question (already handled above)
        roll = random.random()
        if roll < 0.35:
            triggered = True
            trigger_reason = f"Non-dental media chitchat balancer (roll={roll:.2f} < 0.35)"
            is_dental = False
            
    if not triggered:
        return
        
    state["last_passive_media_run"] = datetime.now().isoformat()
    save_state(state)

    # BUILD PROMPT
    if is_dental:
        prompt = f"""
Ты — опытный стоматолог-практик, читаешь чат коллег "StomChat". Тебе прислали изображение по стоматологической теме.
Дай короткий, точный клинический комментарий — как ответил бы врач с 15 годами практики: уверенно, без воды, по делу.

Описание изображения (распознано моделью):
{media_description}

Подпись пользователя к изображению (если есть):
{caption_text}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}

Похожие обсуждения из Архива чата:
{archive_corpus}

ИНСТРУКЦИИ:
1. Длина по ситуации — если снимок требует клинического разбора, можно 2-3 абзаца. Если всё понятно с ходу — одна фраза.
2. Тон — уверенный, peer-to-peer, чуть ироничный там где уместно.
3. Разметка: только HTML — <b>жирный</b>. Никакого Markdown.
4. Только доказанные факты. Если данных нет — скажи честно.
5. Добавляй ценность, не пересказывай подпись.

ЕСЛИ изображение явно не стоматологическое (мем, еда, бытовая сцена) — верни одно слово: IGNORE
"""
    else:
        prompt = f"""
Ты — участник стоматологического чата "StomChat" с чёрным юмором. Коллега кинул мем или бытовую картинку.
Одна острая реплика в духе «врач в конце рабочего дня».

Описание изображения:
{media_description}

Подпись (если есть):
{caption_text}

ИНСТРУКЦИИ:
1. Коротко — 1-2 предложения max. Это реплика, не монолог.
2. Сразу с места в карьер — никакого «Смотрю на это и думаю».
3. Юмор: цинизм, ирония, усталость стоматолога, кассовый аппарат, пациент-должник, сломанный файл, бормашина.
4. Разметка: <b>жирный</b> только если реально нужно.
"""

    logger.info(f"Triggered media assistant! Reason: {trigger_reason}. Keywords: {search_keywords}")
    
    # CALL GEMINI
    status_ctx = {"kind": "assistant_media", "chat_id": event.chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    
    if error:
        logger.error(f"Media Assistant Gemini generation error: {error}")
        return
        
    reply_text = getattr(response, "text", None)
    if not reply_text:
        logger.warning("Media Assistant Gemini returned empty text.")
        return
        
    reply_text = reply_text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    # Check IGNORE filter only for dental checks (non-dental balancer is already validated)
    if is_dental:
        if reply_text.upper() == "IGNORE" or "ignore" in reply_text.lower():
            logger.info("Media Assistant: Query was classified as off-topic. Ignoring.")
            return

    # SENDING
    if SHADOW_TESTING and event.chat_id != TEST_CHAT_ID:
        shadow_message = f"[SHADOW TEST]\n\n{reply_text}"
        write_to_shadow_log(f"Reason: {trigger_reason}\nKeywords: {search_keywords}\nImage description: {media_description}\nResponse:\n{reply_text}\n---")
        try:
            await bot_client.send_message(
                entity=TEST_CHAT_ID,
                message=shadow_message,
                reply_to=TEST_TOPIC_ID,
                parse_mode='html'
            )
            logger.info("Sent shadow media assistant message to Telegram test topic.")
        except Exception as e:
            logger.error(f"Failed to send shadow media assistant message: {e}")
    else:
        reply_message = reply_text
        try:
            await bot_client.send_message(
                entity=event.chat_id,
                message=reply_message,
                reply_to=msg_id,
                parse_mode='html'
            )
            logger.info(f"Sent direct media assistant reply to chat {event.chat_id}, message {msg_id}.")
        except Exception as e:
            logger.error(f"Failed to send direct media assistant reply: {e}")


async def handle_interactive_case_step(bot_client, chat_id, user_text, user_state):
    # Parse history
    try:
        history_raw = json.loads(user_state.get("history") or "[]")
        if isinstance(history_raw, dict):
            history_data = history_raw.get("messages", [])
        else:
            history_data = history_raw
    except Exception:
        history_data = []
        
    current_step = user_state.get("current_step", 1)
    
    # Add user message to history
    history_data.append({"role": "user", "content": user_text})
    
    # Send status "typing"
    status_msg = await bot_client.send_message(entity=chat_id, message="⚙️ <i>Анализирую ваши действия...</i>", parse_mode='html')
    
    # RAG-поддержка для экзаменатора (подтягиваем клинические факты для корректной оценки действий)
    keywords = extract_keywords(user_text + " " + history_str)
    wiki_corpus, _ = search_knowledge_corpus(keywords[:12])

    # Formulate simulation prompt
    # If step < 3, continue the case. If step >= 3, finish and evaluate.
    is_last_step = (current_step >= 3)
    
    history_str = ""
    for msg in history_data:
        role_name = "Экзаменатор (Бот)" if msg["role"] == "assistant" else "Врач (Вы)"
        history_str += f"{role_name}: {msg['content']}\n\n"
        
    if not is_last_step:
        prompt = f"""
Ты — старший стоматолог-экзаменатор. Ведешь интерактивный разбор клинического случая.
Вот история переписки на данный момент:

{history_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(справочная информация отсутствует)"}

Задачи на этот шаг (Шаг {current_step + 1} из 4):
1. Оцени последнее действие врача. Коротко укажи, насколько оно корректно и логично (опирайся на стандарты из Базы Знаний, если применимо).
2. Предоставь новые клинические данные, соответствующие его действию (например, если врач назначил КТ — опиши, что видно на КТ; если сделал анестезию — опиши начало действия и следующий этап работы).
3. Задай следующий конкретный вопрос о дальнейшей тактике.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Тон: экспертный, конструктивный.
2. Не давай готовых решений и не завершай случай раньше времени!
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
    else:
        prompt = f"""
Ты — старший стоматолог-экзаменатор. Нам нужно завершить интерактивный разбор клинического случая.
Вот вся история разбора:

{history_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(справочная информация отсутствует)"}

Задачи на этот финальный шаг:
1. Подведи итоги действий врача (опирайся на стандарты из Базы Знаний, если применимо).
2. Укажи на допущенные ошибки (если были) или похвали за верную тактику.
3. Выстави оценку по пятибалльной шкале (1/5 до 5/5) с краткой аргументацией.
4. Заверши диалог, пожелав успехов в практике.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Дай развернутый экспертный фидбек.
2. Разметка: только HTML. Без Markdown.
"""
    
    status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    
    if 'status_msg' in locals() and status_msg:
        try: await bot_client.delete_messages(chat_id, status_msg.id)
        except Exception: pass
    
    if error or not response or not getattr(response, "text", None):
        await bot_client.send_message(entity=chat_id, message="❌ <i>Ошибка симулятора при генерации ответа. Пожалуйста, отправьте ваш ответ еще раз.</i>", parse_mode='html')
        return
        
    reply_text = response.text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    if is_last_step:
        # Clear state
        await database.clear_user_interactive_state(chat_id)
        final_message = f"🏁 <b>Разбор случая завершен!</b>\n\n{reply_text}"
        await bot_client.send_message(entity=chat_id, message=final_message, parse_mode='html')
    else:
        # Update history and save state
        history_data.append({"role": "assistant", "content": reply_text})
        history_payload = {
            "messages": history_data,
            "last_updated": time.time()
        }
        await database.set_user_interactive_state(
            user_id=chat_id,
            state_type="case",
            current_step=current_step + 1,
            case_id="dynamic",
            history=json.dumps(history_payload, ensure_ascii=False)
        )
        await bot_client.send_message(entity=chat_id, message=reply_text, parse_mode='html')


async def handle_private_message(bot_client, event):
    """Глубокий обработчик входящих личных сообщений (ЛС) бота с RAG, зрением и памятью."""
    try:
        chat_id = event.chat_id
        text = (event.message.message or "").strip()

        # Map text menu button clicks to slash commands
        btn_mapping = {
            "📖 энциклопедия": "/wiki",
            "🎮 клинический кейс": "/case",
            "🎲 викторина": "/quiz",
            "🧮 калькулятор": "/calc",
            "⭐ закладки": "/bookmarks",
            "📊 статистика чата": "/stats"
        }
        if text.lower() in btn_mapping:
            text = btn_mapping[text.lower()]

        # 0. Voice Note / Audio processing
        is_voice = hasattr(event.message, "voice") and event.message.voice is not None and type(event.message.voice).__name__ != "MagicMock"
        is_audio_file = hasattr(event.message, "audio") and event.message.audio is not None and type(event.message.audio).__name__ != "MagicMock"
        is_audio = is_voice or is_audio_file
        transcribed_text = None
        if is_audio:
            os.makedirs("temp_media", exist_ok=True)
            status_msg = await bot_client.send_message(entity=chat_id, message="🎤 <i>Распознаю аудиосообщение... Подождите.</i>", parse_mode='html')
            temp_path = None
            try:
                temp_path = await event.message.download_media(file="temp_media/")
                if temp_path and os.path.exists(temp_path):
                    import blocking_tools
                    transcribed, error = await blocking_tools.transcribe_audio_async(temp_path, timeout=60)
                    if error:
                        logger.error(f"Audio transcription error: {error}")
                    elif transcribed:
                        raw_transcribed = transcribed.strip()
                        transcribed_text = await blocking_tools.correct_dental_transcription_async(raw_transcribed)
            except Exception as audio_err:
                logger.error(f"Error handling voice note: {audio_err}")
            finally:
                if 'status_msg' in locals() and status_msg:
                    try: await bot_client.delete_messages(chat_id, status_msg.id)
                    except Exception: pass
                if temp_path and os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except Exception: pass
            
            if transcribed_text:
                text = transcribed_text
                # Filter common Whisper silence hallucinations
                silence_hallucinations = {
                    "you", "thank you", "bye", "подпишитесь", 
                    "продолжение следует", "редактор субтитров", "субтитры", 
                    "youtube", "собачья чушь", "спасибо"
                }
                clean_transcribed = text.strip().lower().rstrip(".").rstrip(",")
                if clean_transcribed in silence_hallucinations:
                    logger.info(f"Filtered suspected Whisper silence hallucination: '{text}'")
                    await bot_client.send_message(entity=chat_id, message="🎤 <i>(Тишина или фоновый шум) Пожалуйста, говорите громче или пишите текстом.</i>", parse_mode='html')
                    return
                await bot_client.send_message(entity=chat_id, message=f"🎤 <b>Распознано:</b> «{text}»", parse_mode='html')
            else:
                await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось распознать аудио. Пожалуйста, повторите или напишите текстом.</i>", parse_mode='html')
                return

        if text and not text.startswith("/"):
            await database.save_pm_message(chat_id, "User", text)

        # 0.5. Interactive Simulator State Routing & Abort Check
        user_state = await database.get_user_interactive_state(chat_id)
        
        # Check for case expiration (1 hour inactivity)
        if user_state and user_state.get("state_type") == "case":
            try:
                history_raw = json.loads(user_state.get("history") or "[]")
                if isinstance(history_raw, dict) and "last_updated" in history_raw:
                    last_updated = history_raw["last_updated"]
                    if time.time() - last_updated > 3600:
                        await database.clear_user_interactive_state(chat_id)
                        user_state = None
                        await bot_client.send_message(
                            entity=chat_id, 
                            message="⏳ <i>Предыдущая сессия симулятора была автоматически завершена из-за неактивности более 1 часа.</i>", 
                            parse_mode='html'
                        )
            except Exception as exp_err:
                logger.error(f"Error checking case expiration: {exp_err}")
        
        # Автоматический выход из симулятора при вводе любой другой команды или нажатии кнопки меню
        is_command = text.startswith("/")
        if is_command and user_state and user_state.get("state_type") == "case" and text.lower() not in ("/abort", "/exit"):
            await database.clear_user_interactive_state(chat_id)
            user_state = None
            await bot_client.send_message(entity=chat_id, message="⏹️ <i>Активный клинический симулятор прерван для выполнения новой команды.</i>", parse_mode='html')

        if text.lower() in ("/abort", "/exit", "выход", "отмена"):
            if user_state:
                await database.clear_user_interactive_state(chat_id)
                await bot_client.send_message(entity=chat_id, message="⏹️ <i>Интерактивная сессия симулятора успешно сброшена.</i>", parse_mode='html')
            else:
                await bot_client.send_message(entity=chat_id, message="ℹ️ <i>У вас нет активной сессии симулятора.</i>", parse_mode='html')
            return

        if user_state and user_state.get("state_type") == "case":
            await handle_interactive_case_step(bot_client, chat_id, text, user_state)
            return

        # Admin Wipe command to delete recent bot messages
        if text.lower().startswith(("/wipe", "/del", "/delete")):
            if chat_id in (7716348189, 1890028643):
                is_authorized = True
            else:
                try:
                    import config
                    if str(chat_id) in [str(config.REPORT_CHAT_ID), str(config.SOURCE_CHAT_ID)]:
                        is_authorized = True
                    else:
                        if config.SOURCE_CHAT_ID:
                            permissions = await bot_client.get_permissions(config.SOURCE_CHAT_ID, chat_id)
                            if permissions.is_admin:
                                is_authorized = True
                except Exception as auth_err:
                    logger.error(f"Error checking PM admin auth: {auth_err}")
                
            if is_authorized:
                parts = text.split()
                count = 10
                if len(parts) > 1:
                    try:
                        count = int(parts[1])
                    except ValueError:
                        pass
                
                last_msgs = await database.get_last_bot_sent_messages(count)
                if not last_msgs:
                    await bot_client.send_message(entity=chat_id, message="🤷‍♂️ <i>Не найдено отправленных сообщений бота для удаления.</i>", parse_mode='html')
                    return
                
                deleted_count = 0
                from collections import defaultdict
                by_chat = defaultdict(list)
                for msg_id, c_id in last_msgs:
                    by_chat[c_id].append(msg_id)
                    
                for c_id, msg_ids in by_chat.items():
                    try:
                        await bot_client.delete_messages(c_id, msg_ids)
                        deleted_count += len(msg_ids)
                        for m_id in msg_ids:
                            await database.remove_bot_sent_message(m_id)
                    except Exception as del_err:
                        logger.error(f"Error deleting messages in chat {c_id}: {del_err}")
                        
                await bot_client.send_message(
                    entity=chat_id, 
                    message=f"🧹 <b>Успешно удалено последних сообщений бота: {deleted_count} шт.</b>", 
                    parse_mode='html'
                )
            else:
                await bot_client.send_message(entity=chat_id, message="⛔ <i>У вас нет прав для выполнения этой команды.</i>", parse_mode='html')
            return

        # 1. Обработка базовых команд
        if text.lower() == "/start":
            greeting = (
                "👋 <b>Приветствую! Я умный ассистент стоматологического сообщества StomChat.</b>\n\n"
                "Вы общаетесь со мной в режиме личных сообщений (ЛС). Здесь вы можете:\n"
                "1. 📚 <b>Задавать клинические вопросы</b> — просто отправьте свой вопрос, и я подробно отвечу на него с использованием базы знаний.\n"
                "2. 🖼️ <b>Анализировать снимки и фото</b> — пришлите рентген или фотографию клинического случая, и я сделаю подробный разбор.\n"
                "3. 💬 <b>Вести непрерывный диалог</b> — я запоминаю контекст нашей переписки (до 25 сообщений), поэтому вы можете задавать уточняющие вопросы.\n\n"
                "ℹ️ <i>Используйте кнопки меню внизу для быстрого доступа к функциям или напишите /help!</i>"
            )
            from telethon import types
            keyboard = types.ReplyKeyboardMarkup(
                rows=[
                    types.KeyboardButtonRow(buttons=[
                        types.KeyboardButton(text="📖 Энциклопедия"),
                        types.KeyboardButton(text="🎮 Клинический кейс")
                    ]),
                    types.KeyboardButtonRow(buttons=[
                        types.KeyboardButton(text="🎲 Викторина"),
                        types.KeyboardButton(text="🧮 Калькулятор")
                    ]),
                    types.KeyboardButtonRow(buttons=[
                        types.KeyboardButton(text="⭐ Закладки"),
                        types.KeyboardButton(text="📊 Статистика чата")
                    ])
                ],
                resize=True,
                single_use=True,
                persistent=False
            )
            await bot_client.send_message(entity=chat_id, message=greeting, buttons=keyboard, parse_mode='html')
            return
            
        if text.lower() == "/help":
            help_text = (
                "💡 <b>Доступные команды в ЛС:</b>\n\n"
                "• /start — перезапустить приветствие бота.\n"
                "• /help — показать эту памятку.\n"
                "• /protocols — вывести список доступных клинических протоколов.\n"
                "• /wiki — открыть интерактивную стоматологическую энциклопедию.\n"
                "• /calc — открыть шпаргалку-калькулятор по анестезии.\n"
                "• /quiz — запустить клиническую викторину.\n"
                "• /stats — показать самые обсуждаемые темы в чате сообщества.\n"
                "• /bookmarks — просмотреть сохраненные вами клинические закладки.\n"
                "• /search &lt;запрос&gt; — быстрый прямой поиск по базе знаний стоматологии.\n"
                "• /case — запустить интерактивный клинический симулятор.\n"
                "• /abort — сбросить текущий клинический симулятор.\n\n"
                "• <b>Текстовый/Голосовой вопрос:</b> Просто напишите его или отправьте голосовое сообщение. Я отвечу с использованием базы знаний.\n"
                "• <b>Анализ снимка:</b> Прикрепите фото или рентген. Я опишу, что на нем изображено, и предложу клиническую тактику.\n"
                "• <b>Контекстная память:</b> Я анализирую последние <b>25 сообщений</b> нашего диалога."
            )
            await bot_client.send_message(entity=chat_id, message=help_text, parse_mode='html')
            return

        if text.lower() == "/protocols":
            protocols_text = (
                "📚 <b>Основные клинические протоколы в Базе Знаний:</b>\n\n"
                "• <b>BOPT (Biologically Oriented Preparation Technique):</b> Концепция препарирования без уступа.\n"
                "• <b>Вертикальное препарирование:</b> Особенности ведения краев коронок, сохранение тканей.\n"
                "• <b>Травление керамики:</b> Протоколы работы с плавиковой кислотой и силанизацией (E.max, полевой шпат).\n"
                "• <b>Ирригация в эндодонтии:</b> Концентрации гипохлорита натрия, ЭДТА, протоколы активации (ультразвук, звуковая).\n"
                "• <b>Обтурация корневых каналов:</b> Методики латеральной конденсации и вертикальной горячей гуттаперчи.\n\n"
                "👇 <i>Выберите интересующий протокол ниже для детального изучения:</i>"
            )
            from telethon import Button
            buttons = [
                [Button.inline("🦷 BOPT", data="proto:bopt"), Button.inline("🧪 Травление", data="proto:etching")],
                [Button.inline("💧 Ирригация", data="proto:irrigation"), Button.inline("🩸 Обтурация", data="proto:obturation")]
            ]
            await bot_client.send_message(entity=chat_id, message=protocols_text, buttons=buttons, parse_mode='html')
            return

        if text.lower() in ("/wiki", "/encyclopedia"):
            wiki_text = (
                "📖 <b>Интерактивная Стоматологическая Энциклопедия</b>\n\n"
                "Здесь вы можете изучать клинические стандарты, классификации и протоколы напрямую из нашей базы знаний.\n\n"
                "👇 <i>Выберите раздел для детального просмотра:</i>"
            )
            from telethon import Button
            buttons = [
                [Button.inline("🦷 Препарирование и Ортопедия", data="wiki_cat:ortho"), Button.inline("💧 Эндодонтия и Лечение", data="wiki_cat:endo")],
                [Button.inline("🩹 Пародонтология и Десна", data="wiki_cat:perio"), Button.inline("🔩 Имплантация и Хирургия", data="wiki_cat:surg")],
                [Button.inline("🔍 Инструкция по поиску", data="wiki_cat:help")]
            ]
            await bot_client.send_message(entity=chat_id, message=wiki_text, buttons=buttons, parse_mode='html')
            return

        if text.lower() == "/calc":
            calc_text = (
                "🧮 <b>Справочник-калькулятор анестезии:</b>\n\n"
                "Вы можете отправить мне запрос напрямую (например, <i>«рассчитай артикаин 4% для ребенка 20 кг»</i>), и я рассчитаю безопасную дозу:\n\n"
                "• <b>Артикаин 4% (1:100 000 / 1:200 000):</b> Максимальная доза для взрослых — 7 мг/кг. Для детей — 5 мг/кг.\n"
                "• <b>Мепивакаин 3% (без адреналина):</b> Максимальная доза — 4.4 мг/кг.\n"
                "• <b>Лидокаин 2% (с адреналином):</b> Максимальная доза — 7 мг/кг (взрослые) / 4.4 мг/кг (дети).\n\n"
                "<i>Просто пришлите вес и название анестетика, и я помогу с математикой!</i>"
            )
            await bot_client.send_message(entity=chat_id, message=calc_text, parse_mode='html')
            return

        if text.lower() == "/quiz":
            status_msg = await bot_client.send_message(entity=chat_id, message="🎲 <i>Генерирую клиническую викторину для вас... Подождите.</i>", parse_mode='html')
            prompt = """
Ты — умный клинический ассистент-преподаватель в чате врачей-стоматологов "StomChat". 
Придумай и напиши интересную клиническую задачу-викторину для практикующего стоматолога. 
Задача должна быть сложной, реалистичной, из терапевтической, ортопедической или хирургической стоматологии.

Формат вывода:
1. Описание клинической ситуации (жалобы, осмотр, данные рентгенографии).
2. Четыре варианта ответа (A, B, C, D) с различными тактиками лечения или диагнозами.
3. Инструкция: напиши пользователю, что он может прислать свой ответ (например, "Мой ответ А"), чтобы ты проверил его и выдал подробное объяснение.

Не пиши правильный ответ сразу в сообщении викторины!
Будь лаконичен, профессионален.
"""
            async with bot_client.action(chat_id, 'typing'):
                status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "HIGH"}
                response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
                await bot_client.delete_messages(chat_id, status_msg.id)
                if error:
                    await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось сгенерировать викторину. Попробуйте позже.</i>", parse_mode='html')
                    return
                reply_text = getattr(response, "text", "Ошибка генерации").strip()
                reply_text = clean_html_formatting(reply_text)
                await bot_client.send_message(entity=chat_id, message=f"🎲 <b>Клиническая Викторина:</b>\n\n{reply_text}", parse_mode='html')
            return

        if text.lower() == "/stats":
            stats_text = (
                "📊 <b>Популярные клинические темы в чате StomChat</b>\n"
                "<i>(на основе анализа 117,000+ сообщений архива):</i>\n\n"
                "1. 👑 <b>Ортопедия и коронки</b> (~5,400+ упоминаний) — выбор материалов (диоксид циркония, PMMA, E.max) и методы фиксации.\n"
                "2. 🔪 <b>Вертипреп (Vertiprep) vs Уступы</b> (~4,300+ упоминаний) — дискуссии о границе препарирования и ведении мягких тканей.\n"
                "3. 🩸 <b>Состояние десны и биологическая ширина</b> (~3,800+ упоминаний) — реакция периодонта, ретракционные нити, временное протезирование.\n"
                "4. 🧪 <b>Адгезивные протоколы и композиты</b> (~1,800+ упоминаний) — бондинг к разным типам керамики, пескоструй, фиксация виниров.\n"
                "5. 🦷 <b>Эндодонтия (Лечение каналов)</b> (~1,300+ упоминаний) — инструментация, гипохлорит натрия, ультразвуковая активация.\n"
                "6. 🔩 <b>Имплантация и протезирование</b> (~1,000+ упоминаний) — позиционирование имплантатов, выбор абатментов."
            )
            await bot_client.send_message(entity=chat_id, message=stats_text, parse_mode='html')
            return

        if text.lower().startswith("/bookmarks"):
            arg = text[10:].strip()
            page = 1
            query_filter = None
            if arg:
                if arg.isdigit():
                    page = int(arg)
                else:
                    query_filter = arg
            
            if query_filter:
                rows = await database.get_clinical_bookmarks(chat_id, query=query_filter)
                title = f"📌 <b>Результаты поиска в закладках по запросу «{query_filter}»:</b>\n\n"
            else:
                rows = await database.get_clinical_bookmarks(chat_id)
                title = f"📌 <b>Ваши сохраненные клинические закладки (Страница {page}):</b>\n\n"

            if not rows:
                if query_filter:
                    await bot_client.send_message(entity=chat_id, message=f"🔍 В ваших закладках не найдено совпадений по запросу «{query_filter}».", parse_mode='html')
                else:
                    await bot_client.send_message(entity=chat_id, message="📌 <b>У вас пока нет сохраненных закладок</b> (или страница пуста).\nОтправьте <code>/save</code> в ответ на любое сообщение в общем чате, чтобы сохранить его.", parse_mode='html')
                return

            per_page = 10
            total_items = len(rows)
            total_pages = (total_items + per_page - 1) // per_page
            
            if not query_filter and page > total_pages:
                await bot_client.send_message(entity=chat_id, message=f"⚠️ Страница {page} не существует. Всего страниц: {total_pages}.", parse_mode='html')
                return
                
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page
            page_rows = rows[start_idx:end_idx]
            
            text_out = title
            for i, row in enumerate(page_rows, start_idx + 1):
                msg_id, chat_id_val, sender_name, msg_text, media_desc, date = row
                msg_text_snippet = (msg_text[:80] + "...") if len(msg_text) > 80 else msg_text
                text_out += f"{i}. <b>{sender_name}</b> ({date}):\n"
                text_out += f"«{msg_text_snippet}»\n"
                if media_desc:
                    text_out += f"🖼️ <i>Описание снимка:</i> {media_desc[:80]}...\n"
                clean_chat_id = str(chat_id_val).replace("-100", "")
                text_out += f"🔗 <a href='https://t.me/c/{clean_chat_id}/{msg_id}'>Перейти к сообщению</a>\n\n"
                
            if not query_filter and total_pages > 1:
                text_out += f"<i>Показано {len(page_rows)} из {total_items} закладок. Страница {page} из {total_pages}.\nИспользуйте <code>/bookmarks [номер_страницы]</code> для перехода.</i>"
                
            await bot_client.send_message(entity=chat_id, message=text_out, parse_mode='html', link_preview=False)
            return

        if text.lower().startswith("/search"):
            query_param = text[7:].strip()
            if not query_param:
                await bot_client.send_message(entity=chat_id, message="🔍 <b>Пожалуйста, укажите поисковый запрос.</b>\nПример: <code>/search BOPT</code>", parse_mode='html')
                return
            keywords = extract_keywords(query_param)
            wiki_facts = []
            if os.path.exists("stomat_wiki.db"):
                try:
                    conn = sqlite3.connect("stomat_wiki.db", timeout=10)
                    c = conn.cursor()
                    for kw in keywords:
                        c.execute("SELECT category_code, content FROM distilled_facts WHERE content LIKE ? LIMIT 5", (f"%{kw}%",))
                        for row in c.fetchall():
                            cat_code, content = row
                            import re
                            try:
                                content_hl = re.sub(f"(?i)({re.escape(kw)})", r"<u>\1</u>", content)
                            except Exception:
                                content_hl = content
                            fact = f"• {content_hl}"
                            if fact not in wiki_facts:
                                wiki_facts.append(fact)
                    conn.close()
                except Exception as e:
                    logger.error(f"Error direct searching wiki: {e}")
            if not wiki_facts:
                await bot_client.send_message(entity=chat_id, message=f"🔍 По запросу «{query_param}» ничего не найдено в базе знаний.", parse_mode='html')
                return
            search_out = f"🔍 <b>Результаты поиска по запросу «{query_param}»:</b>\n\n" + "\n\n".join(wiki_facts[:8])
            search_out = clean_html_formatting(search_out)
            await bot_client.send_message(entity=chat_id, message=search_out, parse_mode='html')
            return

        if text.lower() == "/case":
            status_msg = await bot_client.send_message(entity=chat_id, message="🎮 <i>Подготавливаю интерактивный клинический случай... Подождите.</i>", parse_mode='html')
            
            departments = [
                "эндодонтия/кариесология (терапевтическая стоматология)",
                "протезирование/виниры/коронки (ортопедическая стоматология)",
                "имплантация/удаление зуба (хирургическая стоматология)",
                "заболевания пародонта (пародонтология)",
                "окклюзия/ВНЧС (гнатология)"
            ]
            selected_dept = random.choice(departments)
            
            case_prompt = f"""
Ты — старший стоматолог-экзаменатор. Придумай и опиши начало сложного клинического случая из области: {selected_dept}.
Напиши:
1. Жалобы пациента и анамнез.
2. Данные визуального осмотра.
3. Задай ровно один конкретный вопрос о первом действии врача (например, какие дополнительные исследования назначить, или какой инструмент выбрать).

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Будь лаконичен, профессионален.
2. Не пиши правильный ответ и не давай вариантов! Врач должен ответить своими словами (или голосом).
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
            status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "HIGH"}
            response, error = await generate_gemini_text_async(case_prompt, status_ctx, timeout=90)
            await bot_client.delete_messages(chat_id, status_msg.id)
            if error or not response or not getattr(response, "text", None):
                await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось запустить симулятор. Попробуйте позже.</i>", parse_mode='html')
                return
            starting_text = response.text.strip()
            starting_text = clean_html_formatting(starting_text)
            
            history_payload = {
                "messages": [{"role": "assistant", "content": starting_text}],
                "last_updated": time.time()
            }
            await database.set_user_interactive_state(
                user_id=chat_id,
                state_type="case",
                current_step=1,
                case_id="dynamic",
                history=json.dumps(history_payload, ensure_ascii=False)
            )
            case_welcome = (
                "🎮 <b>Интерактивный клинический симулятор запущен!</b>\n"
                "Вы можете отвечать текстом или отправлять голосовые сообщения. Бот будет анализировать ваши действия и вести кейс дальше.\n"
                "Для отмены отправьте /abort.\n\n"
                f"{starting_text}"
            )
            await bot_client.send_message(entity=chat_id, message=case_welcome, parse_mode='html')
            return

        # 2. Обработка медиафайлов (фото/видео) в ЛС
        media_description = None
        temp_path = None
        has_media = event.message.photo is not None or event.message.video is not None
        
        if has_media:
            os.makedirs("temp_media", exist_ok=True)
            try:
                # Отправляем статус ожидания
                status_msg = await bot_client.send_message(entity=chat_id, message="📥 <i>Скачиваю и анализирую медиафайл... Подождите немного.</i>", parse_mode='html')
                
                temp_path = await event.message.download_media(file="temp_media/")
                file_to_analyze = temp_path
                
                # Если видео, извлекаем первый кадр
                if event.message.video:
                    logger.info("Извлечение первого кадр из видео в ЛС...")
                    from media_tools import extract_first_frame_async
                    file_to_analyze = await extract_first_frame_async(temp_path, timeout=60)
                    
                if file_to_analyze:
                    media_description = await vision.describe_image(file_to_analyze, caption=text)
                    
                # Удаляем статусное сообщение
                await bot_client.delete_messages(chat_id, status_msg.id)
            except Exception as e:
                logger.error(f"Error analyzing media in PM: {e}")
                if 'status_msg' in locals():
                    await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Не удалось обработать файл. Попробуйте еще раз.</i>", parse_mode='html')
            finally:
                # Очистка временных файлов
                if temp_path and os.path.exists(temp_path):
                    try: os.remove(temp_path)
                    except Exception: pass
                if 'file_to_analyze' in locals() and file_to_analyze != temp_path and os.path.exists(file_to_analyze):
                    try: os.remove(file_to_analyze)
                    except Exception: pass

        # 3. Восстановление динамического диалога (контекст до 25 сообщений)
        history = await database.get_last_pm_messages(chat_id, limit=25)
        context_msgs = []
        for msg in history:
            context_msgs.append(f"{msg['sender_name']}: {msg['text']}")
            
        # 4. RAG-поиск по стоматологической базе знаний с учетом контекста переписки
        # Собираем текст текущего запроса и последних 3 сообщений истории для детекции клинической темы
        history_context_text = " ".join([msg['text'] for msg in history[-3:]])
        full_context_str = (text or "") + " " + (media_description or "") + " " + history_context_text
        full_context_str_lower = full_context_str.lower()
        
        # Проверяем наличие стоматологической темы во всем контексте
        has_dental_topic = any(kw in full_context_str_lower for kw in DENTAL_KEYWORDS)
        
        # Извлекаем ключевые слова из всего контекста (текущий запрос + медиа + история), чтобы искать статьи
        keywords = extract_keywords(full_context_str)
                    
        wiki_corpus, archive_corpus = "", ""
        if has_dental_topic or has_media:
            # Ищем совпадения в стоматологической базе
            search_keywords = [kw for kw in keywords if any(dk in kw for dk in DENTAL_KEYWORDS)]
            search_keywords = search_keywords if search_keywords else keywords[:12]
            if len(search_keywords) < 12:
                other_kws = [kw for kw in keywords if kw not in search_keywords]
                search_keywords = (search_keywords + other_kws)[:12]
            wiki_corpus, archive_corpus = search_knowledge_corpus(search_keywords)

        # 5. Сборка индивидуального глубокого промпта
        if media_description:
            prompt = f"""
Ты — опытный стоматолог-практик и старший эксперт сообщества "StomChat". Твоя задача — помочь коллеге разобраться со снимком/фото, которое он прислал в личные сообщения.
Общайся как живой, очень опытный врач с коллегой: свободно, неформально, но умно и информативно. Без бюрократии и излишнего официоза.

Описание изображения (распознано Vision-моделью):
{media_description}

Вопрос или подпись пользователя:
{text or "(без подписи)"}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}

Похожие обсуждения из Архива чата:
{archive_corpus}

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. ФОРМАТ И ТОН: Забудь про жесткую структуру вроде "Предварительный диагноз -> Дифференциальный ряд". Отвечай как в чате: посмотри, что на снимке, в чем может быть подвох, и предложи, как лучше поступить. Используй профессиональный сленг, тон — уверенный, peer-to-peer.
2. ГЛУБИНА: Дай развернутый и клинически точный ответ. Если есть нюансы (толщина, материалы, риски) — обязательно подсвети их.
3. РАЗМЕТКА: Только HTML-теги — <b>жирный</b>. Никакого Markdown (**текст**, ## заголовки и т.д.).
4. НАУЧНАЯ ТОЧНОСТЬ: Опирайся на базу знаний и доказательную медицину. Не выдумывай протоколы. Если данных мало — честно скажи: "По этой фотке точно сказать сложно, но выглядит как...".
5. СМАЙЛИКИ: Используй их в меру (1-2 за весь ответ), чтобы разбавить текст, но не переборщи.
"""
        else:
            # Определяем тип запроса: это клинический вопрос или свободная тема
            has_clinical_topic = has_dental_topic or bool(wiki_corpus)
            if has_clinical_topic:
                system_role = (
                    "Ты — опытный стоматолог-практик и старший эксперт сообщества \"StomChat\". "
                    "Ты общаешься с коллегой в личных сообщениях. Отвечай как живой, очень опытный врач, а не как робот. "
                    "Опирайся исключительно на доказательную медицину и базу знаний сообщества."
                )
                instructions = """КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. ГЛУБИНА: Дай развёрнутый, подробный и клинически точный ответ. Структурируй ответ: суть → протокол/алгоритм → нюансы.
2. ФОРМАТ И ТОН: Никаких приветствий, вводных слов ("Отличный вопрос!") и концовок ("Успехов!", "С уважением"). Полностью избегай канцелярщины и фраз типа "Как ИИ...". Используй профессиональный сленг (снимок, каналы, коронка, ортопед и т.д.). Тон прямой, peer-to-peer.
3. РАЗМЕТКА: Только HTML-теги — <b>жирный</b>. Никакого Markdown.
4. СМАЙЛИКИ: Используй их строго в ОДНОМ месте за весь ответ. Не раскидывай по тексту. Подряд можно писать только 2-3 ржущих смайла (😂😂😂). Все остальные смайлы — строго по ОДНОМУ (например, один 😎 или один 😤).
5. НАУЧНАЯ ТОЧНОСТЬ: Только доказанные факты. Без выдумок. Если данных нет — честно укажи это.
6. КОНТЕКСТ: Учитывай всю историю диалога."""
            else:
                system_role = (
                    "Ты — умный, живой и остроумный врач-стоматолог из чата \"StomChat\". "
                    "Ты ведёшь диалог с коллегой в личных сообщениях и можешь свободно общаться на любые темы, шутить и помогать. "
                    "Ты — знающий и свойский коллега, а не занудный чат-бот."
                )
                instructions = """КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Отвечай живо, по-человечески, без академического занудства. Используй стоматологический сленг (если это уместно).
2. Никаких вводных, фраз "Как ИИ...", "С уважением" и концовок. Начинай сразу с сути.
3. РАЗМЕТКА: Только HTML-теги — <b>жирный</b>. Никакого Markdown.
4. СМАЙЛИКИ: Используй их строго в ОДНОМ месте за весь ответ. Не раскидывай по тексту. Подряд можно писать только 2-3 ржущих смайла (😂😂😂). Все остальные смайлы — строго по ОДНОМУ (например, один 😎).
5. Если тебя спрашивают о твоих возможностях, подробно и дружелюбно расскажи о следующем функционале:
   • <b>Общение в ЛС</b>:
     - 📚 Клинические вопросы — ищу ответы в базе данных 118 000+ сообщений врачей чата.
     - 📸 Анализ снимков — пришли рентген или фото, разберу через компьютерное зрение (Vision).
     - 🎤 Голосовые сообщения — можешь наговорить вопрос голосом, я его расшифрую и отвечу.
     - 💬 Память контекста — помню до 25 последних сообщений, можно уточнять детали.
   • <b>Интерактивные функции (кнопки внизу или команды)</b>:
     - 📖 <b>Энциклопедия</b> (/wiki) — поиск статей по базе знаний стоматологии.
     - 🎮 <b>Клинический кейс</b> (/case) — интерактивная игра, где нужно вести диагностику пациента.
     - 🎲 <b>Викторина</b> (/quiz) — случайные профессиональные вопросы для проверки знаний.
     - 🧮 <b>Калькулятор</b> (/calc) — расчет доз анестетиков в карпулах.
     - ⭐ <b>Закладки</b> (/bookmarks) — сохраненные тобой полезные сообщения из чата.
     - 📊 <b>Статистика</b> (/stats) — аналитика по чату StomChat.
   • <b>Работа в общем чате StomChat</b>:
     - Реагирую на стоматологические вопросы, если в диалоге есть ключевые слова.
     - Отвечаю на обращения к "боту" в живом дружеском тоне.
     - Каждую ночь генерирую подробный дайджест со всеми важными обсуждениями."""

            prompt = f"""
{system_role}

История вашего диалога (последние сообщения):
{chr(10).join(context_msgs)}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(не найдено — свободная беседа)"}

Похожие обсуждения из Архива чата:
{archive_corpus or ""}

{instructions}
"""

        logger.info(f"Processing deep PM query from chat_id={chat_id}. Has media={has_media}.")
        
        # 6. Отправка статуса "печатает"
        async with bot_client.action(chat_id, 'typing'):
            # Запрос к Gemini
            status_ctx = {"kind": "pm_chat", "chat_id": chat_id, "thinking_level": "HIGH"}
            response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
            
            if error:
                logger.error(f"PM Gemini generation error: {error}")
                await bot_client.send_message(entity=chat_id, message="❌ <i>Ошибка генерации ответа нейросетью. Пожалуйста, повторите запрос позже.</i>", parse_mode='html')
                return
                
            reply_text = getattr(response, "text", None)
            if not reply_text:
                logger.warning("PM Gemini returned empty text.")
                return
                
            reply_text = reply_text.strip()
            reply_text = clean_html_formatting(reply_text)
            
            # Отправка развернутого ответа
            await send_message_chunks_async(
                bot_client,
                chat_id,
                reply_text,
                parse_mode='html'
            )
            await database.save_pm_message(chat_id, "Assistant", reply_text)
            logger.info(f"Successfully sent deep PM response to chat_id={chat_id}")
            
    except Exception as e:
        logger.exception(f"Unexpected error in handle_private_message: {e}")


async def check_bot_mention_trigger(bot_client, event, msg_id, text, sender_first_name=None):
    """
    Срабатывает когда кто-то пишет 'бот' в чате.
    Этап 1: отправляет контекст в LLM с вопросом — стоит ли отвечать?
    Этап 2: если YES — генерирует живой ответ и отправляет (shadow mode пока не промотировано).
    """
    BOT_MENTION_SHADOW_MODE = True  # Сменить на False чтобы выкатить в боевой

    text_lower = (text or "").lower()
    # Триггер: упомянули "бот" во всех возможных падежах и числах (бот, бота, боту, ботом, боте, боты, ботов, ботам, ботами, ботах)
    bot_words = ["бот", "бота", "боту", "ботом", "боте", "боты", "ботов", "ботам", "ботами", "ботах"]
    if not any(w in text_lower.split() or text_lower == w for w in bot_words):
        # Ищем substring с границами слов и возможными окончаниями
        import re
        if not re.search(r'\bбот(а|у|ом|е|ы|ов|ам|ами|ах)?\b', text_lower):
            return

    chat_id = event.chat_id

    try:
        # Берём текущее сообщение + 5 до + 5 после из БД
        context_rows = await query_db_async(
            "SELECT sender_name, text FROM messages WHERE msg_id <= ? ORDER BY msg_id DESC LIMIT 6",
            (msg_id,)
        )
        context_rows = context_rows[::-1]  # хронологический порядок
        context_str = "\n".join(f"{r[0]}: {r[1]}" for r in context_rows if r[1])

        # ЭТАП 1: Спросить LLM — стоит ли отвечать?
        triage_prompt = f"""Ты — ИИ-ассистент в стоматологическом Telegram-чате StomChat.
Кто-то написал слово "бот" в переписке. Вот контекст:

{context_str}

Реши: стоит ли боту вступить в разговор с живым ответом?

Отвечай строго одним словом:
YES — если человек обращается к боту, задаёт вопрос, хочет чем-то помочь, или ждёт реакции.
NO — если это случайное упоминание, обсуждение другого бота, ругательство, или контекст никак не требует реакции бота.
"""
        triage_ctx = {"kind": "bot_mention_triage", "chat_id": chat_id, "thinking_level": "LOW"}
        triage_resp, triage_err = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=20)

        if triage_err or not triage_resp:
            logger.warning(f"Bot mention triage failed: {triage_err}")
            return

        decision = (getattr(triage_resp, "text", "") or "").strip().upper()
        logger.info(f"Bot mention triage decision: {decision!r} for msg_id={msg_id}")

        if "YES" not in decision:
            return

        # ЭТАП 2: Сгенерировать живой ответ
        address = f"{sender_first_name}, " if sender_first_name else ""
        reply_prompt = f"""Ты — опытный стоматолог-практик и живой участник чата StomChat. 
Тебя только что позвали или упомянули в чате. Вот контекст переписки:

{context_str}

Ответь максимально живо, коротко и по делу — как будто ты сидел рядом и тебя окликнули.
Начни с "{address}" если уместно, или без обращения если человек просто упомянул что ты есть.
Тон: максимально тёплый, свой, не официальный. Говори как человек, используй стоматологический сленг (снимок, каналы, коронка, ортопед, асик). Без канцелярщины и фраз типа "Как ИИ...", "Рад помочь".
Используй эмоциональные смайлики (😎, 😂, 😤, 🤯 и т.д.) строго в ОДНОМ месте за весь ответ. Подряд можно ставить только 2-3 ржущих смайла (😂😂😂). Все остальные — строго по одному! Не раскидывай их по всему тексту.
Разметка: только HTML <b>жирный</b>.
Если непонятно чего хотят или не хватает данных — смело переспрашивай по-простому. Будь проактивен: если видишь, где можно уберечь от ошибки или подсказать лучший вариант — предлагай решение сам.
ЕСЛИ ТЕБЯ СПРАШИВАЮТ "что ты умеешь", "какие команды" и т.п., коротко и дружелюбно перечисли функционал: разбор клинических вопросов, анализ снимков (Vision), энциклопедию /wiki, кейсы /case, калькулятор /calc, статистику /stats. Не придумывай лишнего!
"""
        reply_ctx = {"kind": "bot_mention_reply", "chat_id": chat_id, "thinking_level": "MEDIUM"}
        reply_resp, reply_err = await generate_gemini_text_async(reply_prompt, reply_ctx, timeout=60)

        if reply_err or not reply_resp:
            logger.warning(f"Bot mention reply generation failed: {reply_err}")
            return

        reply_text = (getattr(reply_resp, "text", "") or "").strip()
        reply_text = clean_html_formatting(reply_text)
        if not reply_text:
            return

        if BOT_MENTION_SHADOW_MODE:
            write_to_shadow_log(
                f"[BOT_MENTION] msg_id={msg_id} sender={sender_first_name}\n"
                f"Context:\n{context_str}\n"
                f"Triage: {decision}\nReply:\n{reply_text}\n---"
            )
            logger.info(f"[SHADOW] Bot mention reply logged (not sent): {reply_text[:80]}")
        else:
            try:
                await bot_client.send_message(
                    entity=chat_id,
                    message=reply_text,
                    reply_to=msg_id,
                    parse_mode='html'
                )
                logger.info(f"Bot mention reply sent to chat {chat_id}, msg_id={msg_id}")
            except Exception as send_err:
                logger.error(f"Failed to send bot mention reply: {send_err}")

    except Exception as e:
        logger.exception(f"Unexpected error in check_bot_mention_trigger: {e}")


async def handle_group_summary(bot_client, event, reply_to_msg_id):

    """Сборка саммари обсуждения в группе по запросу."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "summary", seconds=30)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед использованием команды.", reply_to=msg_id)
        return
        
    status_msg = await bot_client.send_message(entity=chat_id, message="📝 <i>Собираю и анализирую историю обсуждения... Подождите.</i>", reply_to=msg_id, parse_mode='html')
    
    try:
        # Получаем последние 30 сообщений из базы данных
        rows = await database.get_last_n_messages(limit=30)
        chat_rows = [r for r in rows if r[3] and r[3].strip()]
        
        if not chat_rows:
            await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Не удалось найти сообщения для саммари.</i>", parse_mode='html')
            return
            
        history_msgs = []
        for r in chat_rows:
            history_msgs.append(f"{r[1] or 'Врач'}: {r[3]}")
            
        history_str = "\n".join(history_msgs)
        
        prompt = f"""
Ты — старший научный редактор и эксперт-клиницист стоматологического сообщества "StomChat".
Проанализируй следующую дискуссию врачей-стоматологов и сделай краткую, профессиональную выжимку.

История переписки:
{history_str}

Задачи:
1. Суть спора или обсуждаемого клинического вопроса (1-2 предложения).
2. Выдели основные точки зрения/аргументы участников (кратко, тезисно).
3. Клиническая рекомендация на основе доказательной стоматологии (каков золотой стандарт решения этого вопроса).

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
- Никакой воды, приветствий и концовок. Начинай сразу со структуры.
- Разметка: только HTML (<b>жирный</b>, <i>курсив</i>). Никакого Markdown.
- Будь краток: вся сводка должна занимать не более 800 символов.
"""
        status_ctx = {"kind": "group_summary", "chat_id": chat_id, "thinking_level": "MEDIUM"}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
        
        if error or not response or not getattr(response, "text", None):
            await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Ошибка генерации саммари. Пожалуйста, попробуйте позже.</i>", parse_mode='html')
            return
            
        summary_text = response.text.strip()
        summary_text = clean_html_formatting(summary_text)
        
        final_text = f"📋 <b>Результаты клинического анализа дискуссии:</b>\n\n{summary_text}"
        await bot_client.edit_message(chat_id, status_msg.id, final_text, parse_mode='html')
        logger.info(f"Successfully posted group summary for chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Error generating group summary: {e}")
        try: await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Произошла неожиданная ошибка при составлении сводки.</i>", parse_mode='html')
        except Exception: pass


async def handle_group_direct_ask(bot_client, event, question):
    """Ответ на прямой клинический вопрос пользователя в группе."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "direct_ask", seconds=30)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед использованием команды.", reply_to=msg_id)
        return
        
    async with bot_client.action(chat_id, 'typing'):
        keywords = extract_keywords(question)
        wiki_corpus, archive_corpus = search_knowledge_corpus(keywords[:12])
        
        prompt = f"""
Ты — опытный стоматолог-практик с 15-летней клинической историей, отвечаешь коллеге на вопрос в группе "StomChat".
Ответь кратко, экспертно и строго по существу.

Вопрос коллеги:
{question}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Максимально 600 символов. Никаких приветствий, обращений и пожеланий. Сразу ответ.
2. Тон — уверенный, коллегиальный, peer-to-peer. Используй сленг (снимок, каналы и т.д.). Без канцелярщины и фраз типа "Как ИИ".
3. СМАЙЛИКИ: Используй их строго в ОДНОМ месте ответа. Подряд можно писать только 2-3 ржущих смайла (😂😂😂). Остальные — строго по ОДНОМУ.
4. Разметка: только HTML (<b>жирный</b>). Никакого Markdown.
5. Только проверенные научные факты. Если в базе нет точных данных, напиши: "В базе данных нет точных сведений о Х, но на практике...", без выдумок.
"""
        status_ctx = {"kind": "group_ask", "chat_id": chat_id, "thinking_level": "MEDIUM"}
        response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
        
        if error or not response or not getattr(response, "text", None):
            return
            
        reply_text = response.text.strip()
        reply_text = clean_html_formatting(reply_text)
        
        try:
            await bot_client.send_message(
                entity=chat_id,
                message=reply_text,
                reply_to=msg_id,
                parse_mode='html'
            )
            logger.info(f"Sent group direct ask reply to msg_id={msg_id}")
        except Exception as e:
            logger.error(f"Failed to send group direct ask reply: {e}")


async def handle_group_quiz(bot_client, event):
    """Генерация и отправка клинической викторины с инлайн-кнопками в группу."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "quiz", seconds=60)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед генерацией новой викторины.", reply_to=msg_id)
        return
        
    status_msg = await bot_client.send_message(entity=chat_id, message="🎲 <i>Конструирую клиническую задачу... Подождите.</i>", reply_to=msg_id, parse_mode='html')
    
    prompt = """
Ты — старший стоматолог-экзаменатор. Твоя задача — сгенерировать сложную клиническую задачу-викторину для группы врачей.
Выдай строго в формате JSON:
{
  "question": "Описание клинического случая и вопрос (до 300 символов)...",
  "options": ["Вариант A", "Вариант B", "Вариант C", "Вариант D"],
  "correct": 0,
  "explanation": "Объяснение правильного ответа (до 150 символов)..."
}
Ответ должен быть валидным JSON, без markdown разметки и без ```json.
"""
    status_ctx = {"kind": "group_quiz_gen", "chat_id": chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=90)
    await bot_client.delete_messages(chat_id, status_msg.id)
    
    if error or not response or not getattr(response, "text", None):
        await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось сгенерировать кейс. Попробуйте позже.</i>", parse_mode='html')
        return
        
    try:
        raw_text = response.text.strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].startswith("```"):
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()
            
        data = json.loads(raw_text)
        question = data["question"]
        options = data["options"]
        correct = int(data["correct"])
        explanation = data["explanation"]
    except Exception as parse_err:
        logger.error(f"Failed to parse quiz JSON: {parse_err}. Raw: {response.text}")
        question = "Пациент жалуется на боли при накусывании в зубе 3.6 (лечен эндодонтически 2 года назад). На снимке: недопломбировка язычного канала на 2 мм, очаг разрежения костной ткани в области апекса 3 мм. Какова первоочередная тактика?"
        options = [
            "Апикальная хирургия (резекция)",
            "Ортопедическое перелечивание",
            "Повторное эндодонтическое лечение",
            "Удаление зуба и имплантация"
        ]
        correct = 2
        explanation = "Перелечивание — метод первого выбора при наличии проходимых каналов и апикального периодонтита."

    quiz_id = str(random.randint(100000, 999999))
    init_votes = {"votes": [0, 0, 0, 0], "voters": {}}
    await database.set_user_interactive_state(
        user_id=int(quiz_id),
        state_type="quiz_config",
        current_step=correct,
        case_id=explanation[:200],
        history=json.dumps(init_votes)
    )
    
    from telethon import Button
    
    buttons = [
        [
            Button.inline(f"A: {options[0][:30]}", data=f"qa:{correct}:0:{quiz_id}"),
            Button.inline(f"B: {options[1][:30]}", data=f"qa:{correct}:1:{quiz_id}")
        ],
        [
            Button.inline(f"C: {options[2][:30]}", data=f"qa:{correct}:2:{quiz_id}"),
            Button.inline(f"D: {options[3][:30]}", data=f"qa:{correct}:3:{quiz_id}")
        ]
    ]
    
    message_text = (
        "🎲 <b>КЛИНИЧЕСКИЙ КЕЙС-ВИКТОРИНА</b>\n\n"
        f"{question}\n\n"
        f"<b>A:</b> {options[0]}\n"
        f"<b>B:</b> {options[1]}\n"
        f"<b>C:</b> {options[2]}\n"
        f"<b>D:</b> {options[3]}\n\n"
        "<i>Нажмите на кнопку с вашим вариантом ответа, чтобы проверить себя!</i>"
    )
    message_text = clean_html_formatting(message_text)
    
    await bot_client.send_message(
        entity=chat_id,
        message=message_text,
        buttons=buttons,
        parse_mode='html'
    )


async def query_wiki_subtopic(subtopic_id):
    codes_map = {
        "ortho_bopt": ["2.2.1"],
        "ortho_vin": ["2.1.1", "2.1.4"],
        "ortho_crown": ["2.1.2", "2.1.3"],
        "endo_irr": ["1.1.3"],
        "endo_obt": ["1.1.4"],
        "endo_files": ["1.1.2", "1.1.1"],
        "perio_dis": ["1.3.2"],
        "perio_clean": ["1.3.1"],
        "perio_plast": ["3.3.1"],
        "surg_impl": ["3.2.1", "3.2.2", "3.2.3"],
        "surg_rem": ["3.1.1"],
        "surg_bone": ["3.3.2"],
        "gnat_joint": ["2.3.1", "2.3.2"],
        "gnat_splint": ["2.3.2"]
    }
    
    facts = []
    if os.path.exists("stomat_wiki.db"):
        try:
            import sqlite3
            conn = sqlite3.connect("stomat_wiki.db", timeout=10)
            c = conn.cursor()
            
            # 1. Try category code search
            codes = codes_map.get(subtopic_id, [])
            for code in codes:
                c.execute("SELECT content FROM distilled_facts WHERE category_code LIKE ? LIMIT 15", (f"%{code}%",))
                for row in c.fetchall():
                    fact = row[0].strip()
                    if fact not in facts:
                        facts.append(fact)
                        
            # 2. Fallback to keyword search if category code yields no results
            if not facts:
                keywords_map = {
                    "ortho_bopt": ["bopt", "уступ", "преп"],
                    "ortho_vin": ["винил", "вкладк", "накладк"],
                    "ortho_crown": ["коронка", "коронок", "мост", "протез"],
                    "endo_irr": ["гипохлорит", "хлоргексидин", "эдта", "ирригац"],
                    "endo_obt": ["гуттаперч", "силер", "обтурац"],
                    "endo_files": ["файл", "реципрок", "протейпер", "мту"],
                    "perio_dis": ["гингивит", "пародонт", "пародонтоз"],
                    "perio_clean": ["кюрет", "скалер", "чистк", "налет", "камень"],
                    "perio_plast": ["десна", "десны", "сст", "трансплантат"],
                    "surg_impl": ["имплант", "абатм", "формировател", "заглушк"],
                    "surg_rem": ["удален", "экстракц", "лунк"],
                    "surg_bone": ["синус", "остеот", "мембран", "биоосс", "аугмент"],
                    "gnat_joint": ["окклюз", "сустав", "внчс"],
                    "gnat_splint": ["сплинт", "капп", "шина"]
                }
                kws = keywords_map.get(subtopic_id, ["дентин"])
                for kw in kws:
                    c.execute("SELECT content FROM distilled_facts WHERE content LIKE ? LIMIT 10", (f"%{kw}%",))
                    for row in c.fetchall():
                        fact = row[0].strip()
                        if fact not in facts:
                            facts.append(fact)
            conn.close()
        except Exception as e:
            logger.error(f"Error querying wiki subtopic: {e}")
    return facts


async def query_random_wiki_fact():
    fact = None
    if os.path.exists("stomat_wiki.db"):
        try:
            import sqlite3
            conn = sqlite3.connect("stomat_wiki.db", timeout=10)
            c = conn.cursor()
            c.execute("SELECT content FROM distilled_facts ORDER BY RANDOM() LIMIT 1")
            row = c.fetchone()
            if row:
                fact = row[0].strip()
            conn.close()
        except Exception as e:
            logger.error(f"Error querying random wiki fact: {e}")
    return fact


async def handle_quiz_callback(bot_client, event):
    """Проверка ответа пользователя при клике на инлайн-кнопку."""
    data_str = event.data.decode('utf-8', errors='ignore')
    
    if data_str == "proto:back":
        protocols_text = (
            "📚 <b>Основные клинические протоколы в Базе Знаний:</b>\n\n"
            "• <b>BOPT (Biologically Oriented Preparation Technique):</b> Концепция препарирования без уступа.\n"
            "• <b>Вертикальное препарирование:</b> Особенности ведения краев коронок, сохранение тканей.\n"
            "• <b>Травление керамики:</b> Протоколы работы с плавиковой кислотой и силанизацией (E.max, полевой шпат).\n"
            "• <b>Ирригация в эндодонтии:</b> Концентрации гипохлорита натрия, ЭДТА, протоколы активации (ультразвук, звуковая).\n"
            "• <b>Обтурация корневых каналов:</b> Методики латеральной конденсации и вертикальной горячей гуттаперчи.\n\n"
            "👇 <i>Выберите интересующий протокол ниже для детального изучения:</i>"
        )
        from telethon import Button
        buttons = [
            [Button.inline("🦷 BOPT", data="proto:bopt"), Button.inline("🧪 Травление", data="proto:etching")],
            [Button.inline("💧 Ирригация", data="proto:irrigation"), Button.inline("🩸 Обтурация", data="proto:obturation")]
        ]
        await bot_client.edit_message(event.chat_id, event.message_id, protocols_text, buttons=buttons, parse_mode='html')
        await event.answer()
        return

    if data_str.startswith("proto:"):
        proto_id = data_str.split(":")[1]
        keywords_map = {
            "irrigation": ["гипохлорит", "эдта", "ирригац", "активац"],
            "bopt": ["bopt", "уступ", "преп"],
            "etching": ["плавиков", "силан", "бонд", "травлен"],
            "obturation": ["гуттаперч", "силер", "обтурац", "конденсац"]
        }
        kws = keywords_map.get(proto_id, ["дентин"])
        wiki_corpus, _ = search_knowledge_corpus(kws)
        wiki_corpus = clean_html_formatting(wiki_corpus)
        if not wiki_corpus:
            wiki_corpus = "<i>Данные протокола временно отсутствуют в базе знаний.</i>"
        else:
            wiki_corpus = wiki_corpus[:1500] + "..."
            
        proto_names = {
            "irrigation": "💧 Ирригация в эндодонтии",
            "bopt": "🦷 BOPT (Препарирование)",
            "etching": "🧪 Адгезивные протоколы (Травление)",
            "obturation": "🩸 Обтурация корневых каналов"
        }
        title = proto_names.get(proto_id, "📚 Клинический протокол")
        response_text = f"<b>{title}:</b>\n\n{wiki_corpus}"
        
        from telethon import Button
        back_btn = Button.inline("⬅️ Назад к списку", data="proto:back")
        await bot_client.edit_message(event.chat_id, event.message_id, response_text, buttons=back_btn, parse_mode='html', link_preview=False)
        await event.answer()
        return

    # WIKI MAIN MENU BACK
    if data_str == "wiki_cat:back":
        wiki_text = (
            "📖 <b>Интерактивная Стоматологическая Энциклопедия</b>\n\n"
            "Добро пожаловать в базу клинических знаний и протоколов StomChat. Здесь собраны проверенные стандарты доказательной стоматологии.\n\n"
            "👇 <i>Выберите интересующее действие:</i>"
        )
        from telethon import Button
        buttons = [
            [Button.inline("📚 Обзор по разделам", data="wiki_cat:topics")],
            [Button.inline("🎲 Случайный факт", data="wiki_cat:random"), Button.inline("🔍 Поиск по базе", data="wiki_cat:search_info")]
        ]
        await bot_client.edit_message(event.chat_id, event.message_id, wiki_text, buttons=buttons, parse_mode='html')
        await event.answer()
        return

    # WIKI TOPICS SELECTOR
    if data_str == "wiki_cat:topics":
        wiki_text = "📚 <b>Рубрикатор Энциклопедии (основные разделы):</b>"
        from telethon import Button
        buttons = [
            [Button.inline("🦷 Ортопедия", data="wiki_cat:ortho"), Button.inline("💧 Эндодонтия", data="wiki_cat:endo")],
            [Button.inline("🩹 Пародонтология", data="wiki_cat:perio"), Button.inline("🔩 Хирургия", data="wiki_cat:surg")],
            [Button.inline("📐 Гнатология", data="wiki_cat:gnat")],
            [Button.inline("⬅️ Назад в меню", data="wiki_cat:back")]
        ]
        await bot_client.edit_message(event.chat_id, event.message_id, wiki_text, buttons=buttons, parse_mode='html')
        await event.answer()
        return

    # SEARCH / RANDOM ROUTINGS
    if data_str == "wiki_cat:search_info":
        search_info = (
            "🔍 <b>Поиск по Базе Знаний:</b>\n\n"
            "Чтобы выполнить быстрый поиск, просто введите в ЛС команду <code>/search &lt;запрос&gt;</code>.\n\n"
            "Например:\n"
            "• <code>/search BOPT</code>\n"
            "• <code>/search гипохлорит</code>\n"
            "• <code>/search травление</code>\n\n"
            "<i>Бот выведет наиболее релевантные статьи прямо в диалог!</i>"
        )
        from telethon import Button
        back_btn = Button.inline("⬅️ Назад в меню", data="wiki_cat:back")
        await bot_client.edit_message(event.chat_id, event.message_id, search_info, buttons=back_btn, parse_mode='html')
        await event.answer()
        return

    if data_str == "wiki_cat:random":
        fact = await query_random_wiki_fact()
        if fact:
            fact_cleaned = clean_html_formatting(fact)
            response_text = f"🎲 <b>Случайный факт из Базы Знаний:</b>\n\n{fact_cleaned}"
        else:
            response_text = "<i>Не удалось получить случайный факт. База временно недоступна.</i>"
        from telethon import Button
        buttons = [
            [Button.inline("🔄 Ещё факт", data="wiki_cat:random")],
            [Button.inline("⬅️ Назад в меню", data="wiki_cat:back")]
        ]
        await bot_client.edit_message(event.chat_id, event.message_id, response_text, buttons=buttons, parse_mode='html', link_preview=False)
        await event.answer()
        return

    # WIKI CATEGORY SUBTOPICS
    if data_str.startswith("wiki_cat:"):
        cat_id = data_str.split(":")[1]
        cat_titles = {
            "ortho": "🦷 Препарирование и Ортопедия",
            "endo": "💧 Эндодонтия и Лечение",
            "perio": "🩹 Пародонтология и Десна",
            "surg": "🔩 Имплантация и Хирургия",
            "gnat": "📐 Гнатология и Окклюзия"
        }
        title = cat_titles.get(cat_id, "📚 Раздел Энциклопедии")
        
        from telethon import Button
        if cat_id == "ortho":
            buttons = [
                [Button.inline("🦷 BOPT / Преп без уступа", data="wiki_page:ortho_bopt:0")],
                [Button.inline("💎 Виниры и накладки", data="wiki_page:ortho_vin:0")],
                [Button.inline("👑 Коронки и мосты", data="wiki_page:ortho_crown:0")],
                [Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]
            ]
        elif cat_id == "endo":
            buttons = [
                [Button.inline("💧 Ирригация каналов", data="wiki_page:endo_irr:0")],
                [Button.inline("🩸 Обтурация каналов", data="wiki_page:endo_obt:0")],
                [Button.inline("🔬 Инструменты / Файлы", data="wiki_page:endo_files:0")],
                [Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]
            ]
        elif cat_id == "perio":
            buttons = [
                [Button.inline("🩹 Болезни пародонта", data="wiki_page:perio_dis:0")],
                [Button.inline("🪥 Кюретаж и чистка", data="wiki_page:perio_clean:0")],
                [Button.inline("🥩 Пластика десны / ССТ", data="wiki_page:perio_plast:0")],
                [Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]
            ]
        elif cat_id == "surg":
            buttons = [
                [Button.inline("🔩 Имплантация", data="wiki_page:surg_impl:0")],
                [Button.inline("🩸 Удаление зубов", data="wiki_page:surg_rem:0")],
                [Button.inline("🦴 Синус-лифтинг / Кость", data="wiki_page:surg_bone:0")],
                [Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]
            ]
        elif cat_id == "gnat":
            buttons = [
                [Button.inline("📐 Окклюзия и сустав", data="wiki_page:gnat_joint:0")],
                [Button.inline("🦷 Сплинты и шины", data="wiki_page:gnat_splint:0")],
                [Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]
            ]
        else:
            buttons = [[Button.inline("⬅️ Назад к разделам", data="wiki_cat:topics")]]
            
        wiki_text = f"📚 <b>Раздел: {title}</b>\n\nвыберите интересующую клиническую подтему для просмотра статей:"
        await bot_client.edit_message(event.chat_id, event.message_id, wiki_text, buttons=buttons, parse_mode='html')
        await event.answer()
        return

    # WIKI FACT PAGE AND PAGINATION
    if data_str.startswith("wiki_page:"):
        parts = data_str.split(":")
        subtopic_id = parts[1]
        page_idx = int(parts[2])
        
        facts = await query_wiki_subtopic(subtopic_id)
        
        subtopic_names = {
            "ortho_bopt": "🦷 BOPT / Преп без уступа",
            "ortho_vin": "💎 Виниры и накладки",
            "ortho_crown": "👑 Коронки и мосты",
            "endo_irr": "💧 Ирригация каналов",
            "endo_obt": "🩸 Обтурация каналов",
            "endo_files": "🔬 Инструменты / Файлы",
            "perio_dis": "🩹 Болезни пародонта",
            "perio_clean": "🪥 Кюретаж и чистка",
            "perio_plast": "🥩 Пластика десны / ССТ",
            "surg_impl": "🔩 Имплантация",
            "surg_rem": "🩸 Удаление зубов",
            "surg_bone": "🦴 Синус-лифтинг / Кость",
            "gnat_joint": "📐 Окклюзия и сустав",
            "gnat_splint": "🦷 Сплинты и шины"
        }
        subtopic_title = subtopic_names.get(subtopic_id, "📚 Статья")
        
        if not facts:
            response_text = f"📚 <b>{subtopic_title}:</b>\n\n<i>В данной категории пока нет статей в базе знаний.</i>"
            from telethon import Button
            back_cat = subtopic_id.split("_")[0]
            back_btn = Button.inline("⬅️ Назад к подтемам", data=f"wiki_cat:{back_cat}")
            await bot_client.edit_message(event.chat_id, event.message_id, response_text, buttons=back_btn, parse_mode='html')
            await event.answer()
            return
            
        total = len(facts)
        if page_idx < 0:
            page_idx = total - 1
        elif page_idx >= total:
            page_idx = 0
            
        fact_content = facts[page_idx]
        fact_cleaned = clean_html_formatting(fact_content)
        
        response_text = (
            f"📖 <b>{subtopic_title}</b>\n"
            f"<i>Статья {page_idx + 1} из {total}</i>\n\n"
            f"{fact_cleaned}"
        )
        
        from telethon import Button
        nav_row = []
        if total > 1:
            nav_row.append(Button.inline("◀️ Пред", data=f"wiki_page:{subtopic_id}:{page_idx - 1}"))
            nav_row.append(Button.inline(f"{page_idx + 1}/{total}", data=f"wiki_page:{subtopic_id}:{page_idx}"))
            nav_row.append(Button.inline("След ▶️", data=f"wiki_page:{subtopic_id}:{page_idx + 1}"))
            
        back_cat = subtopic_id.split("_")[0]
        buttons = []
        if nav_row:
            buttons.append(nav_row)
        buttons.append([
            Button.inline("⭐ В закладки", data=f"wiki_save:{subtopic_id}:{page_idx}"),
            Button.inline("⬅️ Назад к подтемам", data=f"wiki_cat:{back_cat}")
        ])
        
        await bot_client.edit_message(event.chat_id, event.message_id, response_text, buttons=buttons, parse_mode='html', link_preview=False)
        await event.answer()
        return

    # WIKI BOOKMARK SAVE CALLBACK
    if data_str.startswith("wiki_save:"):
        parts = data_str.split(":")
        subtopic_id = parts[1]
        page_idx = int(parts[2])
        
        facts = await query_wiki_subtopic(subtopic_id)
        subtopic_names = {
            "ortho_bopt": "🦷 BOPT / Преп без уступа",
            "ortho_vin": "💎 Виниры и накладки",
            "ortho_crown": "👑 Коронки и мосты",
            "endo_irr": "💧 Ирригация каналов",
            "endo_obt": "🩸 Обтурация каналов",
            "endo_files": "🔬 Инструменты / Файлы",
            "perio_dis": "🩹 Болезни пародонта",
            "perio_clean": "🪥 Кюретаж и чистка",
            "perio_plast": "🥩 Пластика десны / ССТ",
            "surg_impl": "🔩 Имплантация",
            "surg_rem": "🩸 Удаление зубов",
            "surg_bone": "🦴 Синус-лифтинг / Кость",
            "gnat_joint": "📐 Окклюзия и сустав",
            "gnat_splint": "🦷 Сплинты и шины"
        }
        subtopic_title = subtopic_names.get(subtopic_id, "📚 Статья")
        
        if facts and page_idx < len(facts):
            fact_content = facts[page_idx]
            fact_cleaned = clean_html_formatting(fact_content)
            
            bookmark_text = f"📚 <b>{subtopic_title}</b>\n\n{fact_cleaned}"
            
            from datetime import datetime
            import random
            fake_msg_id = -random.randint(100000000, 999999999)
            
            await database.save_clinical_bookmark(
                saved_by_user_id=event.sender_id,
                msg_id=fake_msg_id,
                chat_id=event.chat_id,
                sender_name="База Знаний",
                text=bookmark_text,
                has_media=False,
                media_description="",
                date=datetime.now()
            )
            await event.answer("⭐ Статья успешно добавлена в ваши закладки!", alert=True)
        else:
            await event.answer("❌ Не удалось сохранить статью. Попробуйте еще раз.", alert=True)
        return

    if not data_str.startswith("qa:"):
        return
        
    parts = data_str.split(":")
    correct_idx = int(parts[1])
    clicked_idx = int(parts[2])
    quiz_id = int(parts[3])
    voter_id = str(event.sender_id)
    
    state_row = await database.get_user_interactive_state(quiz_id)
    if not state_row:
        await event.answer("⚠️ Ошибка: Викторина не найдена.", alert=True)
        return
        
    explanation = state_row.get("case_id") or "Правильный выбор!"
    history_str = state_row.get("history") or "{}"
    
    try:
        history_data = json.loads(history_str)
        if not isinstance(history_data, dict) or "votes" not in history_data:
            history_data = {"votes": [0, 0, 0, 0], "voters": {}}
    except Exception:
        history_data = {"votes": [0, 0, 0, 0], "voters": {}}
        
    votes = history_data["votes"]
    voters = history_data["voters"]
    
    if voter_id in voters:
        await event.answer("⚠️ Вы уже проголосовали в этой викторине!", alert=True)
        return
        
    # Record vote
    voters[voter_id] = clicked_idx
    votes[clicked_idx] += 1
    
    # Update DB
    await database.set_user_interactive_state(
        user_id=quiz_id,
        state_type="quiz_config",
        current_step=correct_idx,
        case_id=explanation,
        history=json.dumps(history_data)
    )
    
    is_correct = (correct_idx == clicked_idx)
    prefix = "✅ Верно! " if is_correct else "❌ Неверно! "
    alert_text = f"{prefix}\n\n{explanation}"
    await event.answer(alert_text, alert=True)
    
    # Update message text with stats
    try:
        original_msg = await event.get_message()
        if original_msg and original_msg.message:
            lines = original_msg.message.split("\n")
            total_votes = sum(votes)
            pct = [int((v / total_votes) * 100) if total_votes > 0 else 0 for v in votes]
            
            new_lines = []
            for line in lines:
                if line.startswith("<b>A:</b>"):
                    clean_choice = line[9:].split("(")[0].strip()
                    new_lines.append(f"<b>A:</b> {clean_choice} ({votes[0]} гол. | {pct[0]}%)")
                elif line.startswith("<b>B:</b>"):
                    clean_choice = line[9:].split("(")[0].strip()
                    new_lines.append(f"<b>B:</b> {clean_choice} ({votes[1]} гол. | {pct[1]}%)")
                elif line.startswith("<b>C:</b>"):
                    clean_choice = line[9:].split("(")[0].strip()
                    new_lines.append(f"<b>C:</b> {clean_choice} ({votes[2]} гол. | {pct[2]}%)")
                elif line.startswith("<b>D:</b>"):
                    clean_choice = line[9:].split("(")[0].strip()
                    new_lines.append(f"<b>D:</b> {clean_choice} ({votes[3]} гол. | {pct[3]}%)")
                elif "Нажмите на кнопку" in line or "Всего проголосовало:" in line:
                    continue
                else:
                    new_lines.append(line)
            
            while new_lines and not new_lines[-1].strip():
                new_lines.pop()
                
            new_lines.append(f"\n📊 <b>Всего проголосовало: {total_votes}</b>\n\n<i>Нажмите на кнопку с вашим вариантом ответа, чтобы проверить себя!</i>")
            
            new_text = "\n".join(new_lines)
            await event.edit(text=new_text, parse_mode='html')
    except Exception as edit_err:
        logger.error(f"Failed to edit quiz message text with live stats: {edit_err}")


async def analyze_dispute_need(context_msgs):
    if not context_msgs:
        return False
    context_str = "\n".join(context_msgs)
    prompt = f"""
Ты — модератор клинического чата стоматологов. Проанализируй переписку врачей и определи, есть ли в ней активный спор, клиническое разногласие, конфликт мнений или спорное обсуждение, требующее вмешательства клинического рефери для разрядки обстановки или предоставления научной справки.

Переписка врачей:
{context_str}

Правило: выведи строго одно слово 'YES' (если спор/конфликт есть) или 'NO' (если это обычное мирное обсуждение, шутка или обмен опытом без спора). Никаких других слов или комментариев не пиши.
"""
    status_ctx = {"kind": "referee_analyser", "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=45)
    if response and getattr(response, "text", None):
        res = response.text.strip().upper()
        if "YES" in res:
            return True
    return False


async def check_and_trigger_referee(bot_client, event, text):
    """Пассивный клинический рефери для предотвращения конфликтов."""
    global LAST_REFEREE_RUN
    chat_id = event.chat_id
    msg_id = event.message.id
    
    text_lower = text.lower()
    has_conflict_kw = any(kw in text_lower for kw in [
        "бред", "чушь", "дичь", "херня", "говно", "полная лажа", 
        "безрукий", "руки оторвать", "какой дурак", "херню", "глупость",
        "рукожоп", "рукожопие", "помойку", "мусорку", "выброси", 
        "косяк", "ужасно", "кривые руки", "уродство", "жесть", "отстой",
        "хлам", "ахинея", "ппц", "пиздец", "бредятина",
        "чушь собачья", "какой дебил", "убейся", "дебилизм",
        "идиот", "идиотизм", "тупой", "тупость", "придурок", "даун",
        "рукожопый", "криворукий", "жопорукий", "косорукий", "из жопы",
        "ересь", "чепуха", "психушка", "дурка", "лечись", "высер",
        "выкинь", "дерьмо", "говнище", "днище", "лажовый", "шиза",
        "дебил", "кретин", "олень", "баран", "тормоз", "позорище",
        "позор", "стыдоба", "срач", "клоун", "цирк", "клоунада",
        "курам на смех", "хрень", "галиматья", "шарага", "колхозный",
        "безрукие", "руки отсохнут", "убожество", "убого"
    ])
    
    should_intervene = has_conflict_kw
    chain_msgs = []
    
    # Автодетект споров по длинным цепочкам реплаев
    if not should_intervene and event.message.reply_to:
        try:
            chain_msgs = await database.get_reply_chain_texts(msg_id, max_depth=5)
            if len(chain_msgs) >= 4:
                should_intervene = await analyze_dispute_need(chain_msgs)
                if should_intervene:
                    logger.info(f"Dispute auto-detected from reply chain in msg_id={msg_id}.")
        except Exception as chain_err:
            logger.error(f"Error checking reply chain dispute: {chain_err}")
            
    if not should_intervene:
        return
        
    # Разрешаем интервенции не чаще одного раза в 5 минут
    if datetime.now() - LAST_REFEREE_RUN < timedelta(minutes=5):
        return
        
    LAST_REFEREE_RUN = datetime.now()
    logger.info(f"Clinical Referee triggered for msg_id={msg_id}. Deciding style (toxic={has_conflict_kw})...")
    
    # 50/50 ИЛИ ШУТИТ ИЛИ НАУЧНО
    # Если спор "злой" (есть стоп-слова) -> шутит (joke)
    # Если обычный спор -> 25% научно (scientific), 75% коллега (colleague)
    import random
    if has_conflict_kw:
        style = "joke"
    else:
        style = "scientific" if random.random() < 0.25 else "colleague"
        
    chain_str = "\n".join(chain_msgs) if chain_msgs else text
    
    if style == "joke":
        prompt = f"""
Ты — юмористический клинический рефери стоматологического сообщества "StomChat". 
В чате начался агрессивный спор (градус эмоций зашкаливает). Вот последнее сообщение: "{text}".

Напиши короткую, исключительно ироничную и миролюбивую шутку, используя профессиональный стоматологический юмор (например, про перегретые боры, адгезивный протокол, грязный коффердам или усадку), чтобы разрядить агрессию.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Длина — максимум 220 символов! Коротко и метко.
2. Никаких приветствий и концовок. Сразу суть.
3. Тон: дружелюбный, ироничный, призывающий успокоиться.
4. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
    elif style == "scientific":
        # Поиск по базе RAG
        keywords = extract_keywords(text + " " + " ".join(chain_msgs))
        wiki_corpus, _ = search_knowledge_corpus(keywords[:12])
        
        prompt = f"""
Ты — клинический эксперт сообщества "StomChat". В чате идет профессиональный спор.
История дискуссии:
{chain_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(справочная информация отсутствует)"}

Напиши научно обоснованную, спокойную и примиряющую реплику на основе Справки из Базы Знаний. Разъясни доказательный клинический стандарт по теме спора, чтобы миролюбиво разрешить спор.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Важно: внимательно изучи историю дискуссии. НЕ повторяй те аргументы и тейки, которые коллеги уже озвучили в истории. Напиши новую полезную мысль.
2. Длина — максимум 280 символов! Будь лаконичен.
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""
    else: # style == "colleague"
        # Поиск по базе RAG для содержательного ответа от лица коллеги
        keywords = extract_keywords(text + " " + " ".join(chain_msgs))
        wiki_corpus, _ = search_knowledge_corpus(keywords[:12])
        
        prompt = f"""
Ты — живой практикующий врач-стоматолог, активный и уважаемый участник чата "StomChat". 
В чате идет обсуждение клинического вопроса. Твоя задача — вклиниться в беседу как умный, знающий коллега-собеседник.
История дискуссии:
{chain_str}

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus or "(нет точных справочных данных по теме)"}

Напиши естественную, живую реплику от лица коллеги. Вырази своё мнение, основываясь на Базе Знаний, но пиши простым человеческим языком практикующего врача (без занудства и канцелярита). Не читай нотации. Пиши так, будто общаешься с равными коллегами в ординаторской.

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Важно: внимательно изучи историю дискуссии. НЕ повторяй тейки и доводы, которые коллеги уже написали в истории. Добавь свежую мысль или вежливо задай наводящий клинический вопрос, развивающий диалог.
2. Длина — максимум 320 символов! Напиши кратко, живо и реалистично.
3. Разметка: только HTML (<b>жирный</b>). Без Markdown.
"""

    status_ctx = {"kind": "group_referee", "chat_id": chat_id, "thinking_level": "HIGH"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=60)
    
    if error or not response or not getattr(response, "text", None):
        return
        
    reply_text = response.text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    try:
        await bot_client.send_message(
            entity=chat_id,
            message=f"⚖️ {reply_text}",
            reply_to=msg_id,
            parse_mode='html'
        )
        logger.info(f"Referee intervention ({style}) successfully sent to chat_id={chat_id}")
    except Exception as e:
        logger.error(f"Failed to send referee intervention: {e}")


async def handle_term_explainer(bot_client, event, term):
    """Быстрое объяснение стоматологического термина из базы знаний."""
    chat_id = event.chat_id
    msg_id = event.message.id
    
    cooldown = check_user_cooldown(chat_id, event.sender_id, "what", seconds=30)
    if cooldown > 0:
        await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста, подождите {cooldown} сек перед повторным запросом термина.", reply_to=msg_id)
        return
        
    keywords = extract_keywords(term)
    wiki_corpus, _ = search_knowledge_corpus(keywords[:12])
    
    prompt = f"""
Ты — толковый словарь стоматологического сообщества "StomChat".
Объясни стоматологический термин или аббревиатуру: "{term}".

Справка из Базы Знаний (stomat_wiki):
{wiki_corpus}

КРИТИЧЕСКИЕ ИНСТРУКЦИИ:
1. Объясни термин ровно в 1-2 предложениях. Предельно кратко и научно-популярно для коллег.
2. Никаких приветствий, «Данный термин означает...» и прочей воды. Сразу определение.
3. Разметка: только HTML (<b>жирный</b>). Никакого Markdown.
"""
    status_ctx = {"kind": "group_explainer", "chat_id": chat_id, "thinking_level": "MEDIUM"}
    response, error = await generate_gemini_text_async(prompt, status_ctx, timeout=60)
    
    if error or not response or not getattr(response, "text", None):
        return
        
    reply_text = response.text.strip()
    reply_text = clean_html_formatting(reply_text)
    
    try:
        await bot_client.send_message(
            entity=chat_id,
            message=f"📖 <b>{term.upper()}:</b> {reply_text}",
            reply_to=msg_id,
            parse_mode='html'
        )
        logger.info(f"Term explanation sent for term={term}")
    except Exception as e:
        logger.error(f"Failed to send term explanation: {e}")
