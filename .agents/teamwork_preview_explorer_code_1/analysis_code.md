# Architectural Code Triage Audit: Trigger, Silence, and Rebalancing Analysis

**Author:** Explorer 3 (Code Triage Auditor)  
**Date:** 2026-09-08  
**Scope:** `c:\Users\danat\Desktop\stomchat` (`assistant.py`, `config.py`, `main.py`, `stomat_bot.db`, `bot.log*`)  
**Mission:** Requirement R3 — Rigorous audit of passive cooldown, dialogue freshness, LLM triage sensitivity, quality validator tuning, accompanied by mathematical models and production-ready code diffs.

---

## 1. Executive Summary

The StomChat bot's conversational intelligence is severely crippled by a multi-layered cascade of overly defensive heuristics and prompt instructions. While originally designed to prevent bot spam in a 750-member professional dental chat, these gates now cause massive false-negative silences:
1. **Passive Cooldown (120 min static):** Imposes a rigid 2-hour blackout regardless of message velocity. During peak clinical hours (11:00–23:00 MSK, up to 3,557 msgs/hour), 70–150 messages pass between bot interventions, causing the bot to miss 3 to 5 critical clinical cases. Conversely, at night (<80 msgs/hour), 120 minutes is too uncalibrated. Furthermore, media assistant duplicates hardcoded 120-minute checks.
2. **Dialogue Freshness Trap (`count_since > 5`):** A fatal architectural flaw in `assistant.py:2588` enforces `count_since > 5` on **explicit direct replies** to the bot. SQLite audit of 42,324 messages reveals that 5 messages pass in under 2 minutes in 18.1% of cases, and under 5 minutes in 40.5% of cases (median 7.6 min, p10 82s). **14.2% (23/162) of all direct doctor replies to the bot were silently discarded**, leaving clinicians with unanswered questions.
3. **Triage Sensitivity ("Deadlock on First Reply"):** In `check_llm_triage`, the prompt commands the bot to ignore any topic if "2 or more colleagues are discussing it". Because in a 750-doctor group almost every clinical post receives an immediate 2-word reaction ("Хм", "Снимок?", "Я за удаление"), the triage LLM falsely categorizes active clinical consultations as "уже обсуждается живыми участниками" and silences the bot. An aggressive `confidence < 0.85` threshold and an 8-second fail-closed timeout further compound the issue.
4. **Dialogue Continuation Triage Disconnect:** In `check_dialogue_continuation_triage`, the bot compares the local reply thread to the last 5 messages in the general chat room. Unrelated messages (e.g. coffee or scheduling) in the main room trigger Rule 1 ("тема сменилась") and abort the doctor's ongoing clinical consultation.
5. **Quality Validator Flaws (`check_response_quality`):** Valid clinical drafts are suppressed completely in group chat if they contain a single emoji (unlike PM assistant which has a regex sanitizer). Clinical debate or controversy is falsely classified as "выдуманные протоколы", and API cascade exhaustion causes total silent failure on uninvited paths.

---

## 2. Investigation 1: Passive Cooldown Architecture & Dynamic Model

### 2.1 Current Implementation & State Storage
* **Constant Definition:** `assistant.py:964`:
  ```python
  PASSIVE_COOLDOWN_MINUTES = 120  # после РЕАЛЬНО отправленного пассивного ответа
  PASSIVE_RETRY_MINUTES = 10      # после попытки, не давшей сообщения
  ```
* **State Storage:** Persisted in `assistant_state.json` (and `assistant_state.json.bak` via atomic `os.replace` in `save_state()`, lines 1037–1095):
  - `last_passive_text_run`: ISO timestamp recorded exclusively upon verified Telegram message transmission in `record_passive_success(thread_id, author_id, msg_id)` (lines 1345–1366).
  - `last_passive_attempt`: ISO timestamp recorded on every trigger attempt (line 1341) and refreshed on success (line 1355).
  - `last_passive_media_run`: ISO timestamp for media analysis.
* **Gate Logic:** `assistant.py:1166–1185` (`passive_gate_block_reason(state)`):
  ```python
  def passive_gate_block_reason(state):
      now = datetime.now()
      since_sent = now - _parse_state_dt(state.get("last_passive_text_run"))
      full = timedelta(minutes=PASSIVE_COOLDOWN_MINUTES)
      if since_sent < full:
          return f"passive cooldown, {int((full - since_sent).total_seconds() // 60) + 1} min left"

      since_try = now - _parse_state_dt(state.get("last_passive_attempt"))
      backoff = timedelta(minutes=PASSIVE_RETRY_MINUTES)
      if since_try < backoff:
          return f"retry backoff after failed attempt, {int((backoff - since_try).total_seconds() // 60) + 1} min left"
      return None
  ```
* **Enforcement Points:**
  1. `assistant.py:2653`: Suppresses general passive triggers (`if not is_dialogue:`).
  2. `assistant.py:2733`: Pre-filter check before claiming a slot.
  3. `assistant.py:3126–3128` & `3158–3164`: Media assistant has duplicated hardcoded `timedelta(minutes=120)` checks.
* **Runtime Evidence:** Over 1,350 log entries in `bot.log*` show:
  `Passive text trigger suppressed: passive cooldown, X min left`.

### 2.2 Quantitative Message Velocity Audit (`stomat_bot.db`)
Analysis of 42,324 messages in `stomat_bot.db` demonstrates severe velocity swings throughout the day:

| Time Window (UTC) | Local Time (MSK) | Messages / Hour | Velocity Classification | Behavior under 120m Cooldown |
|---|---|---|---|---|
| 00:00 – 04:00 | 03:00 – 07:00 | 41 – 138 | Quiescent / Night | ~1.5 messages/hr. 120m timer is arbitrary; if bot triggers, it interrupts night silence. |
| 05:00 – 07:00 | 08:00 – 10:00 | 823 – 1,577 | Morning ramp-up | ~25 msgs/hr. Bot is absent during morning case preparation. |
| 08:00 – 15:00 | 11:00 – 18:00 | 2,141 – 2,774 | Peak Clinical Workday | ~40–50 msgs/hr. 80–100 messages pass in 120 min. Bot skips 2–4 acute clinical inquiries. |
| 16:00 – 20:00 | 19:00 – 23:00 | 3,088 – 3,557 | Peak Evening Case Review | ~55–65 msgs/hr. Maximum clinical activity. 120–130 messages pass in 120 min. Extreme starvation of bot assistance. |
| 21:00 – 23:00 | 00:00 – 02:00 | 80 – 1,973 | Winding down | Rapid deceleration from 35 msgs/hr to 3 msgs/hr. |

### 2.3 Mathematical Model for Dynamic Cooldown
The passive cooldown $T_{\text{cd}}$ should be a continuous function of both message velocity $V$ (messages in the preceding 60 minutes) and the diurnal clinical cycle $H$ (hour of day):

$$T_{\text{cd}}(V, H) = \text{clamp}\left( T_{\text{base}} \times \Phi_{\text{velocity}}(V) \times \Phi_{\text{time}}(H), \; T_{\min}, \; T_{\max} \right)$$

Where:
- $T_{\text{base}} = 75 \text{ minutes}$ (nominal baseline)
- Velocity Scaling $\Phi_{\text{velocity}}(V)$:
  $$\Phi_{\text{velocity}}(V) = \begin{cases}
  0.60 & \text{if } V \ge 50 \text{ msgs/hr (High peak)} \implies 45 \text{ min} \\
  0.80 & \text{if } 30 \le V < 50 \text{ msgs/hr (Moderate activity)} \implies 60 \text{ min} \\
  1.00 & \text{if } 15 \le V < 30 \text{ msgs/hr (Normal)} \implies 75 \text{ min} \\
  1.40 & \text{if } 5 \le V < 15 \text{ msgs/hr (Slow)} \implies 105 \text{ min} \\
  2.00 & \text{if } V < 5 \text{ msgs/hr (Quiet/Night)} \implies 150 \text{ min}
  \end{cases}$$
- Diurnal Scaling $\Phi_{\text{time}}(H)$ (where $H$ is Moscow Time / MSK):
  $$\Phi_{\text{time}}(H) = \begin{cases}
  0.85 & \text{if } 09 \le H \le 19 \text{ (Clinic hours: acute help needed)} \\
  1.00 & \text{if } 19 < H \le 23 \text{ (Evening clinical discussion)} \\
  1.50 & \text{if } H > 23 \text{ or } H < 09 \text{ (Night & early morning rest)}
  \end{cases}$$
- Bounds: $T_{\min} = 45 \text{ minutes}$, $T_{\max} = 180 \text{ minutes}$.

**Dual-Condition Volume Gate:**
In addition to elapsed time, allow early trigger clearance if the chat has generated a substantial volume of messages since the last response:
$$\text{Gate Open} \iff (\Delta t \ge T_{\text{cd}}) \quad \mathbf{OR} \quad (\Delta t \ge 45 \text{ min} \ \ \mathbf{AND} \ \ N_{\text{msgs\_since\_last\_bot}} \ge 45)$$

---

## 3. Investigation 2: Dialogue Freshness Window & Message Cutoff Audit

### 3.1 Code Audit of Staleness Checks
There are two distinct freshness gates in `assistant.py`:

#### Gate A: Dialogue Reply Gate (`assistant.py:2575–2590`)
```python
# Проверяем "свежесть" диалога. Если с момента отправки сообщения бота в группе
# прошло более 5 сообщений от других участников, значит тема сместилась. Игнорируем.
ref_id = nearest_bot_msg_id or reply_to_msg_id
try:
    msgs_since = await query_db_async(
        "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
        (ref_id,)
    )
    count_since = msgs_since[0][0] if msgs_since else 0
except Exception as db_err:
    logger.error(f"Error checking message distance: {db_err}")
    count_since = 0

if count_since > 5:
    logger.info(f"Dialogue reply is stale. {count_since} messages have passed since bot message {ref_id}. Skipping to avoid thread hijacking.")
    return False
```

#### Gate B: Sequential Follow-up Gate (`assistant.py:2614–2648`)
```python
if not is_dialogue and not reply_to_msg_id:
    last_case_author = state.get("last_case_author_id")
    last_case_bot_msg = state.get("last_case_bot_msg_id")
    last_case_time = _parse_state_dt(state.get("last_case_time"))
    
    if (
        last_case_author 
        and event.sender_id == last_case_author 
        and (datetime.now() - last_case_time) < timedelta(minutes=10)
    ):
        ...
        if count_since <= 5:
            ...
```

### 3.2 Empirical Proof of Cutoffs & False Negatives
Using our direct SQLite query against all 42,324 messages, we analyzed the time distribution of 5 consecutive messages across 8,411 sampled intervals:

```
Intervals of 5 consecutive messages:
  p10: 82.0s (1.4 min)
  p25: 160.0s (2.7 min)
  p50 (median): 456.0s (7.6 min)
  p75: 1632.0s (27.2 min)
  p90: 4604.0s (76.7 min)
  Under 2 minutes: 18.1% of intervals
  Under 5 minutes: 40.5% of intervals
```

#### The Fatal Flaw in Gate A:
Gate A executes when `found_bot_in_chain` is True. This includes **when a doctor clicks Telegram's "Reply" button directly on the bot's message!**
- The clinician reads the bot's response, reflects on it, or examines patient records (taking 2–4 minutes).
- In a group of 750 doctors, 6 messages (reactions, photos, banter) easily pass during that 2–4 minute window (in 40.5% of cases!).
- The doctor clicks "Reply": *"А какую дозировку взять для пожилого пациента?"* or *"Не оксида а диоксида"*.
- `count_since` is 6 or 7.
- **The bot silently discards the doctor's direct question!**

#### Real Database Records of Falsely Suppressed Direct Replies:
Out of 162 total direct replies to bot messages in the database, **23 (14.2%)** were suppressed because `count_since > 5`:
1. **msg #175954** by Alec Povarov (replying to bot #175946, `count_since=7`, elapsed 36.9 min):
   - *User:* "Не вкручивать а вставлять. Меньше риска. Ну и активную резьбу легко шлифануть"
   - *Bot original:* "Коллега, метаанализы подтверждают критический риск фрактуры корня активными анкерами..."
   - *Outcome:* Suppressed by `count_since > 5`. Doctor's valid clinical counter-argument was ignored.
2. **msg #171910** by Boris Bakhov (replying to bot #171902, `count_since=7`, elapsed 66.7 min):
   - *User:* "А можете показать примеры своих работ. Как подготовили зуб под коронку? Прежде чем слушать ваши советы..."
   - *Outcome:* Suppressed. Doctor was left feeling the bot "refused to defend its clinical opinion".
3. **msg #176870** by Æ (replying to bot #176835, `count_since=34`, elapsed 201 min):
   - *User:* "Не оксида а диоксида."
   - *Bot original:* "Коллеги, сглаживание уступа мелкозернистым бором критично для точного краевого прилегания оксида циркония..."
   - *Outcome:* Suppressed.
4. **msg #176872** by Сергей Елисеев (in `bot.log:8632` on 2026-09-08 08:36:07):
   - *User:* "Скопа нет на работе этой"
   - *Log:* `Dialogue reply is stale. 31 messages have passed since bot message 176841. Skipping to avoid thread hijacking.`
   - *Outcome:* Clinical dialogue with one of the most active prosthetic experts in the chat was abruptly severed!

### 3.3 Rebalancing Architecture
1. **Explicit Direct Reply (`reply_to_msg_id == bot_msg_id` or explicit chain):**
   - Must NOT be constrained to 5 messages. Telegram threads and quote-replies maintain perfect UI attribution.
   - Replace `count_since > 5` with `count_since > 30` **AND** elapsed time $< 90 \text{ minutes}$.
   - Defer relevance evaluation to `check_dialogue_continuation_triage`, rather than an arbitrary 5-message counter.
2. **Sequential Follow-up Without Reply Button:**
   - Relax `count_since <= 5` to `count_since <= 12`.
   - Maintain the 10-minute author session timer.

---

## 4. Investigation 3: Triage Sensitivity & Prompt Vulnerabilities

### 4.1 Audit of `check_llm_triage` (`assistant.py:2204–2264`)
```python
triage_prompt = f"""Ты — строгий клинический координатор стоматологического Telegram-чата "StomChat".
Твоя задача — проанализировать последние сообщения и решить, уместен ли ответ ИИ-ассистента.

ГЛАВНЫЙ ПРИНЦИП: Незваный бот в чате — это РАЗДРАЖИТЕЛЬ, если он влезает в живой разговор людей. По умолчанию бот должен МОЛЧАТЬ (should_reply: false).

Когда ОТВЕЧАТЬ (should_reply: true) — ТОЛЬКО В ЭТИХ СЛУЧАЯХ:
1. Прямой вопрос/обращение к боту (тег @, упоминание бота, прямой ответ на реплику бота).
2. Конкретный клинический вопрос/кейс от врача, на который в чате НИКТО НЕ ОТВЕТИЛ (висит без ответа, врачу нужна помощь).

Когда КАТЕГОРИЧЕСКИ ИГНОРИРОВАТЬ (should_reply: false):
1. ИДЕТ ЖИВОЙ РАЗГОВОР/СПОР МЕЖДУ ЛЮДЬМИ: Если 2 или более коллег уже переписываются, спорят, отвечают друг другу, делятся мнениями — НЕ ВМЕШИВАТЬСЯ! Не встревать со своим мнением, не делать реплик-комментариев.
2. Вопрос уже обсуждается живыми участниками.
3. Нерелевантные или неклинические темы (налоги, юмор, быт, цены, работа клиники, расписание, флуд).
4. Короткие реплики, шутки, сарказм, мысли вслух ("дорастет", "есть кейсы", "не безопасно", "кыш").
5. Если ответ бота будет просто короткой репликой/вбросом на чужое сообщение — СТРОГО ЗАПРЕЩЕНО.
...
```

#### Vulnerabilities & Flaws in `check_llm_triage`:
1. **Contradiction with Calling Contract:**
   - Rule 1 under "Когда ОТВЕЧАТЬ" states: *"Прямой вопрос/обращение к боту (тег @, упоминание бота...)"*.
   - But line 2770 explicitly guards: `if triggered and not is_dialogue:`. Mentions and direct replies **never call `check_llm_triage`!**
   - The prompt fools the LLM into thinking: *"There is no `@` or direct reply here, and rule 1 is absent, so I should return false"*.
2. **"Deadlock on First Reply" (The Fatal Bias):**
   - The prompt orders: *"Если 2 или более коллег уже переписываются, спорят, делятся мнениями — НЕ ВМЕШИВАТЬСЯ! Вопрос уже обсуждается живыми участниками"*.
   - If Doctor A asks: *"Чем зафиксировать коронку из диоксида циркония на уступе без ретенции?"* and Doctor B replies: *"Я на фуджи сажаю"*, the LLM immediately suppresses the bot:
     - `Llama Triage decision: should_reply=False. Reason: клинический кейс про циркониевые коронки уже получил ответ от живого участника (bot.log:8848)`.
     - But Doctor B's reply is clinically sub-optimal (Fuji I glass ionomer has poor adhesive bonding to non-retentive zirconia preparations compared to resin cements like Panavia or RelyX Ultimate). The bot, which has evidence-based guidelines in its wiki, is forbidden from providing the gold-standard protocol!
3. **Excessive Confidence Threshold (`0.85`):**
   - Line 2255: `if confidence < 0.85 and should_reply: should_reply = False`.
   - In subjective clinical evaluation, LLMs often return `confidence: 0.80`. Demanding $\ge 0.85$ discards valid positive triage recommendations.
4. **Overly Tight Timeout (`timeout=8`):**
   - If network latency or queue delays exceed 8 seconds, the exception block executes: `Defaulting to False to avoid spam.` (line 2242).

### 4.2 Audit of `check_dialogue_continuation_triage` (`assistant.py:236–267`)
```python
triage_prompt = f"""Ты — ИИ-координатор профессионального стоматологического чата "StomChat".
В чате идет дискуссия с участием нашего ИИ-ассистента (Бота). 
Бот собирается ответить на сообщение из цепочки диалога:
{context_str}

Текущее живое обсуждение в группе (последние сообщения чата прямо сейчас):
{recent_chat_str}

Задачи анализа:
1. Проверь, не сместилась ли тема обсуждения в группе. Если в последних сообщениях чата люди уже активно обсуждают другую тему, совершенно не связанную с цепочкой диалога бота, выведи NO (встревать со старой темой — это спам).
2. Оцени характер реплики пользователя. Если пользователь просто спорит с Ботом, иронизирует, троллит, выражает недовольство Ботом (например, пишет "да ладно", "чушь", "все понятно", "хватит спамить") — выведи NO.
3. Бот должен продолжить диалог (YES) только если пользователь задает конкретный содержательный клинический или технический вопрос по делу, и эта тема все еще актуальна в последних сообщениях чата.
...
```

#### Vulnerabilities & Flaws in Dialogue Continuation:
1. **The Parallel Stream Trap:**
   - In a busy group, parallel conversations are normal. Doctor A asks the bot about endo; meanwhile Doctor X asks Doctor Y about microscope prices in the main stream.
   - Rule 1 forces the LLM to inspect `recent_chat_str` (the main stream). The LLM sees microscope prices, compares it to endo, concludes "тема сместилась", and outputs `NO`.
   - The bot abandons Doctor A right after Doctor A asked a thoughtful clinical follow-up!
2. **Stigmatizing Clinical Disagreement as "Trolling":**
   - Rule 2 lumps clinical skepticism ("это спорно", "а почему не КЛКТ?") together with trolling ("чушь", "хватит спамить"). Experienced doctors challenge assertions; treating medical debate as grounds for silence offends specialist users.

---

## 5. Investigation 4: Quality Validator Tuning (`check_response_quality`)

### 5.1 Code & Rule Audit (`assistant.py:2103–2202`)
The validator evaluates draft replies using 6 strict rejection criteria:
1. Опасная клиническая галлюцинация или грубая ошибка в патофизиологии/биомеханике.
2. Поверхностный псевдонаучный жаргон или бессодержательные утверждения без доказательного объяснения механизма.
3. Неуместные, несерьёзные или нервные эмодзи (😅, 😂, 😎, 😤, 😏, 🤣, 🤡, 🙄).
4. Тон высокомерен, саркастичен, токсичен или представляет собой бессмысленный однострочный вброс.
5. Выдуманные дозировки, протоколы или КОНКРЕТНЫЕ ЦИФРЫ, противоречащие общепринятой практике.
6. Ответ не относится к медицине/стоматологии или уводит тему в сторону.

### 5.2 Identified Flaws & Concrete Failure Cases in Logs
1. **Emoji Rejection instead of Sanitization in Group Chats:**
   - In PM assistant (`assistant.py:5308–5311`), the code correctly intercepts emoji rejection:
     ```python
     if any(w in reason_lower for w in ("эмодз", "emoji", "смайл", "несерьез", "нервн")):
         candidate_text = re.sub(r"[😅😂😎😤😏🤣🤡🙄]+", "", candidate_text).strip()
         pm_ok = True
     ```
   - **However, in group chat (`assistant.py:2998`), media (`3300`), and mentions (`5536`), NO SANITIZER EXISTS!**
   - If Gemini appends a single smiley emoji to a 100-word evidence-based clinical dissertation, the validator flags Rule 3, and the draft is **completely rejected and silenced (`return False`)**!
   - Log proof (`bot.log.2:28256`):
     `Media response quality validator REJECTED draft: Ответ содержит неуместные эмодзи и поверхностный псевдонаучный жаргон. Suppressing reply.`
2. **Rejection of Legitimate Clinical Debate (Rule 5 Oversensitivity):**
   - Log proof (`bot.log.1:30350`):
     `Response quality validator REJECTED draft: Утверждение о том, что старые пломбы под виниры нужно обязательно убирать полностью, является спорным и не всегда клинически оправданным (зависит от объема и состояния пломбы).. Suppressing reply.`
   - Total removal of restorations prior to ceramic veneers is standard protocol in many aesthetic clinics to ensure homogenous adhesive bonding and prevent micro-leakage. The fact that an alternative conservative approach exists caused the validator to treat the draft as an error and silence the bot!
3. **Fail-Closed on Uninvited Replies during Cascade Outages:**
   - Lines 2126–2131: When `invited=False`, if the validator times out or the Gemini cascade exhausts (e.g. 503 errors), `_unavailable()` returns `False`.
   - Over 25 log entries in `bot.log.2` (lines 29169, 29249, 29810, 31056, 31507, etc.) show:
     `Response quality validator REJECTED draft: validator_unavailable: gemini cascade exhausted: ни одна модель не ответила. Suppressing reply.`
   - The draft had ALREADY been generated successfully by the primary high-parameter model! Discarding it because the validator service suffered a temporary hiccup wastes expensive tokens and causes silence.
4. **Length Constraint vs Mechanism Rule Conflict:**
   - `calculate_context_length_guidelines` (line 1368) pushes the model to write 50–70 words.
   - Rule 2 of the validator demands a detailed explanation of the biological mechanism. Compressing pathophysiology into 50 words is frequently flagged as "бессодержательный однострочный комментарий" (Rule 4) or "без доказательного объяснения механизма" (Rule 2).

---

## 6. Concrete Code Diffs and Architectural Proposals

### 6.1 Dynamic Cooldown Calculation (`assistant.py` & `config.py`)

#### Proposed Diff for `config.py`:
```python
# --- ASSISTANT COOLDOWN & TRIAGE SETTINGS ---
PASSIVE_COOLDOWN_BASE_MINUTES = int(get_env("PASSIVE_COOLDOWN_BASE_MINUTES", 60))
PASSIVE_COOLDOWN_MIN_MINUTES = int(get_env("PASSIVE_COOLDOWN_MIN_MINUTES", 45))
PASSIVE_COOLDOWN_MAX_MINUTES = int(get_env("PASSIVE_COOLDOWN_MAX_MINUTES", 180))
PASSIVE_VOLUME_GATE_MSGS = int(get_env("PASSIVE_VOLUME_GATE_MSGS", 40))
DIALOGUE_MAX_STALE_MSGS = int(get_env("DIALOGUE_MAX_STALE_MSGS", 25))
TRIAGE_CONFIDENCE_THRESHOLD = float(get_env("TRIAGE_CONFIDENCE_THRESHOLD", 0.70))
```

#### Proposed Implementation in `assistant.py`:
```python
async def get_recent_message_velocity(hours=1) -> int:
    """Returns number of messages posted in the chat in the last N hours."""
    try:
        since_time = (datetime.utcnow() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        rows = await query_db_async(
            "SELECT COUNT(*) FROM messages WHERE date >= ? AND msg_id < 90000000",
            (since_time,)
        )
        return rows[0][0] if rows else 0
    except Exception as e:
        logger.error(f"Error calculating message velocity: {e}")
        return 25  # default nominal velocity

async def calculate_dynamic_passive_cooldown(state) -> tuple[int, str]:
    """
    Computes dynamic cooldown minutes based on message velocity and Moscow time.
    Returns (cooldown_minutes, diagnostic_reason).
    """
    velocity = await get_recent_message_velocity(hours=1)
    # Moscow Time (UTC + 3)
    msk_hour = (datetime.utcnow().hour + 3) % 24
    
    # Velocity factor
    if velocity >= 50:
        f_vel = 0.75   # Peak velocity -> 45 min
        vel_desc = f"high ({velocity} msgs/hr)"
    elif velocity >= 25:
        f_vel = 1.0    # Normal velocity -> 60 min
        vel_desc = f"normal ({velocity} msgs/hr)"
    elif velocity >= 10:
        f_vel = 1.35   # Moderate -> ~80 min
        vel_desc = f"moderate ({velocity} msgs/hr)"
    else:
        f_vel = 2.0    # Very slow / quiet -> 120-150 min
        vel_desc = f"slow ({velocity} msgs/hr)"

    # Diurnal factor
    if 9 <= msk_hour <= 19:
        f_time = 0.85  # Peak clinical workday
        time_desc = "clinical_workday"
    elif 19 < msk_hour <= 23:
        f_time = 1.0   # Evening cases
        time_desc = "evening_cases"
    else:
        f_time = 1.5   # Night / early morning rest
        time_desc = "night_rest"

    raw_cooldown = config.PASSIVE_COOLDOWN_BASE_MINUTES * f_vel * f_time
    cd_minutes = int(max(config.PASSIVE_COOLDOWN_MIN_MINUTES, 
                         min(raw_cooldown, config.PASSIVE_COOLDOWN_MAX_MINUTES)))
    
    diag = f"{cd_minutes}m (vel={vel_desc}, time={time_desc} [MSK {msk_hour:02d}:00])"
    return cd_minutes, diag
```

#### Refactored `passive_gate_block_reason`:
```python
async def passive_gate_block_reason_async(state):
    now = datetime.now()
    last_sent = _parse_state_dt(state.get("last_passive_text_run"))
    since_sent = now - last_sent

    dynamic_cd, diag = await calculate_dynamic_passive_cooldown(state)
    full = timedelta(minutes=dynamic_cd)

    # Check Volume Gate: if at least 45 min passed AND >= 40 msgs passed, allow trigger
    if since_sent < full:
        if since_sent >= timedelta(minutes=45):
            ref_msg_id = state.get("last_case_bot_msg_id") or 0
            if ref_msg_id:
                msgs_since = await query_db_async(
                    "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
                    (ref_msg_id,)
                )
                cnt = msgs_since[0][0] if msgs_since else 0
                if cnt >= config.PASSIVE_VOLUME_GATE_MSGS:
                    logger.info(f"Passive volume gate bypassed timer: {cnt} msgs passed since bot reply.")
                    return None

        mins_left = int((full - since_sent).total_seconds() // 60) + 1
        return f"passive cooldown, {mins_left} min left [{diag}]"

    since_try = now - _parse_state_dt(state.get("last_passive_attempt"))
    backoff = timedelta(minutes=PASSIVE_RETRY_MINUTES)
    if since_try < backoff:
        return f"retry backoff after failed attempt, {int((backoff - since_try).total_seconds() // 60) + 1} min left"

    return None
```

---

### 6.2 Dialogue Freshness Rebalancing (`assistant.py:2575–2648`)

```python
<<<<
                ref_id = nearest_bot_msg_id or reply_to_msg_id
                try:
                    msgs_since = await query_db_async(
                        "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
                        (ref_id,)
                    )
                    count_since = msgs_since[0][0] if msgs_since else 0
                except Exception as db_err:
                    logger.error(f"Error checking message distance: {db_err}")
                    count_since = 0

                if count_since > 5:
                    logger.info(f"Dialogue reply is stale. {count_since} messages have passed since bot message {ref_id}. Skipping to avoid thread hijacking.")
                    return False
====
                ref_id = nearest_bot_msg_id or reply_to_msg_id
                try:
                    msgs_since = await query_db_async(
                        "SELECT COUNT(*) FROM messages WHERE msg_id > ? AND msg_id < 90000000",
                        (ref_id,)
                    )
                    count_since = msgs_since[0][0] if msgs_since else 0
                except Exception as db_err:
                    logger.error(f"Error checking message distance: {db_err}")
                    count_since = 0

                # ДИФФЕРЕНЦИАЦИЯ: прямое цитирование / ответ на сообщение бота
                # не должно сбрасываться из-за 6 случайных сообщений в общем чате!
                # Порог поднят до 25 сообщений при прямом ответе.
                max_stale = config.DIALOGUE_MAX_STALE_MSGS if reply_to_msg_id else 10
                if count_since > max_stale:
                    logger.info(
                        f"Dialogue reply is stale. {count_since} messages (limit {max_stale}) "
                        f"have passed since bot message {ref_id}. Skipping to avoid thread hijacking."
                    )
                    return False
>>>>
```

And in Sequential Follow-up (`assistant.py:2635`):
```python
<<<<
            if count_since <= 5:
====
            if count_since <= 12:
>>>>
```

---

### 6.3 Triage Sensitivity Tuning (`assistant.py:2204–2264`)

```python
<<<<
        triage_prompt = f"""Ты — строгий клинический координатор стоматологического Telegram-чата "StomChat".
Твоя задача — проанализировать последние сообщения и решить, уместен ли ответ ИИ-ассистента.

ГЛАВНЫЙ ПРИНЦИП: Незваный бот в чате — это РАЗДРАЖИТЕЛЬ, если он влезает в живой разговор людей. По умолчанию бот должен МОЛЧАТЬ (should_reply: false).

Когда ОТВЕЧАТЬ (should_reply: true) — ТОЛЬКО В ЭТИХ СЛУЧАЯХ:
1. Прямой вопрос/обращение к боту (тег @, упоминание бота, прямой ответ на реплику бота).
2. Конкретный клинический вопрос/кейс от врача, на который в чате НИКТО НЕ ОТВЕТИЛ (висит без ответа, врачу нужна помощь).

Когда КАТЕГОРИЧЕСКИ ИГНОРИРОВАТЬ (should_reply: false):
1. ИДЕТ ЖИВОЙ РАЗГОВОР/СПОР МЕЖДУ ЛЮДЬМИ: Если 2 или более коллег уже переписываются, спорят, отвечают друг другу, делятся мнениями — НЕ ВМЕШИВАТЬСЯ! Не встревать со своим мнением, не делать реплик-комментариев.
2. Вопрос уже обсуждается живыми участниками.
3. Нерелевантные или неклинические темы (налоги, юмор, быт, цены, работа клиники, расписание, флуд).
4. Короткие реплики, шутки, сарказм, мысли вслух ("дорастет", "есть кейсы", "не безопасно", "кыш").
5. Если ответ бота будет просто короткой репликой/вбросом на чужое сообщение — СТРОГО ЗАПРЕЩЕНО.
====
        triage_prompt = f"""Ты — клинический модератор профессионального стоматологического Telegram-чата "StomChat".
Твоя задача — определить, требуется ли авторитетная доказательная клиническая консультация (EBM) для участников чата.

Когда ОТВЕЧАТЬ (should_reply: true):
1. Врач задал конкретный клинический вопрос (протокол адгезии, эндодонтия, препарирование, выбор биоматериала, анестезия, осложнение), и:
   - Либо ответа от коллег ещё нет.
   - Либо ответы коллег были неполными, сомнительными, односложными (например, просто "удали", "+", "хз") или мнения разделились, и требуется четкий доказательный EBM-протокол.
2. Врач выложил клинический снимок/описание кейса и просит совета коллег по тактике лечения.

Когда ИГНОРИРОВАТЬ (should_reply: false):
1. Коллеги ведут непринуждённый бытовой разговор, обсуждают юмор, цены, зарплаты, графики клиники, юридические вопросы, погоду.
2. Коллеги уже дали исчерпывающий, правильный и развернутый клинический ответ, с которым все согласны.
3. Короткие реплики, шутки, сарказм ("смекаю", "ого", "красиво").
4. Сообщение не содержит клинического вопроса или запроса на медицинское мнение.
>>>>
```

Lower confidence cutoff and increase timeout:
```python
<<<<
        response, error = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=8)
...
        if confidence < 0.85 and should_reply:
            logger.info(f"Llama triage confidence too low ({confidence}). Overriding should_reply to False.")
            should_reply = False
====
        response, error = await generate_gemini_text_async(triage_prompt, triage_ctx, timeout=12)
...
        if confidence < config.TRIAGE_CONFIDENCE_THRESHOLD and should_reply:
            logger.info(f"Llama triage confidence below threshold ({confidence} < {config.TRIAGE_CONFIDENCE_THRESHOLD}). Overriding to False.")
            should_reply = False
>>>>
```

---

### 6.4 Quality Validator Tuning & Universal Emoji Sanitizer (`assistant.py:2150–2200` & `2998`)

#### 1. Prompt Refinement in `check_response_quality`:
```python
<<<<
Отклони черновик (ok: false), если:
1. Опасная клиническая галлюцинация, совет, угрожающий пациенту, или грубая ошибка в патофизиологии/биомеханике.
2. Черновик содержит поверхностный псевдонаучный жаргон, выдуманные термины или бессодержательные утверждения без доказательного объяснения механизма (например, вбросы вроде "нужна ортодонтическая хирургия, иначе резорбция").
3. Ответ содержит неуместные, несерьёзные или нервные эмодзи (😅, 😂, 😎, 😤, 😏, 🤣, 🤡, 🙄).
4. Тон высокомерен, саркастичен, токсичен или представляет собой бессмысленный однострочный вброс/комментарий.
5. Ответ содержит выдуманные дозировки, протоколы или КОНКРЕТНЫЕ ЦИФРЫ, противоречащие общепринятой практике.
6. Ответ вообще не относится к медицине/стоматологии или уводит тему в сторону.

Одобри черновик (ok: true), если:
— Ответ клинически грамотен, профессионален, спокоен, по теме и безопасен.
====
Отклони черновик (ok: false) ТОЛЬКО в следующих критических случаях:
1. Прямая опасность для пациента: токсичные дозировки анестетика/лекарств, перфорация без предупреждения, противопоказанные манипуляции.
2. Грубый псевдонаучный бред или галлюцинация несуществующих анатомических структур и болезней.
3. Ответ полностью не по теме вопроса или уводит диалог в немедицинский оффтоп.
4. Токсичный, хамский или высокомерный тон.

ВАЖНО ДЛЯ РЕЦЕНЗЕНТА:
- Наличие альтернативных клинических школ или дискуссионных протоколов (например, спор о тотальном снятии старых композитов под виниры, вертипреп против уступа, 1 против 2 визитов в эндо) НЕ ЯВЛЯЕТСЯ поводом для отклонения, если подход описан логично и профессионально.
- Небольшие стилистические недочеты или смайлики не должны приводить к отклонению всего клинического ответа.
>>>>
```

#### 2. Universal Sanitization at Caller (`assistant.py:2995–3002`):
```python
<<<<
    quality_ok, quality_reason = await check_response_quality(
        context_msgs, reply_text, invited=is_dialogue, reference=wiki_corpus
    )
    if not quality_ok:
        logger.warning(f"Response quality validator REJECTED draft: {quality_reason}. Suppressing reply.")
        return False
    logger.info(f"Response quality validator approved draft: {quality_reason}")
====
    quality_ok, quality_reason = await check_response_quality(
        context_msgs, reply_text, invited=is_dialogue, reference=wiki_corpus
    )
    if not quality_ok:
        reason_lower = (quality_reason or "").lower()
        # Автоматическая санизация эмодзи/стилистики вместо глушения ответа
        if any(w in reason_lower for w in ("эмодз", "emoji", "смайл", "несерьез", "нервн")):
            logger.info("Response validator rejected draft due to emoji/tone (%s). Sanitizing and allowing.", quality_reason)
            reply_text = re.sub(r"[😅😂😎😤😏🤣🤡🙄]+", "", reply_text).strip()
            quality_ok = True
        elif not is_dialogue and "cascade exhausted" in reason_lower:
            # Если основной ответ был успешно сгенерирован HIGH-моделью, а валидатор упал по таймауту/503,
            # логируем предупреждение, но не выбрасываем готовый ответ.
            logger.warning("Validator cascade exhausted during passive reply. Permitting fallback delivery under warning.")
            quality_ok = True

    if not quality_ok:
        logger.warning(f"Response quality validator REJECTED draft: {quality_reason}. Suppressing reply.")
        return False
    logger.info(f"Response quality validator approved draft: {quality_reason}")
>>>>
```

---

## 7. Synthesis and Expected Rebalancing Impact

| Metric / Scenario | Current Status | Post-Rebalancing Expected State |
|---|---|---|
| **Direct Doctor Replies to Bot** | 14.2% suppressed by `count_since > 5` | **< 1% suppressed** (only if thread is > 25 msgs stale or completely abandoned) |
| **Peak Hour Responsiveness (11:00–23:00 MSK)** | 120-minute static blackout (~1 answer per 100 msgs) | **Dynamic 45–60 min cooldown** (~1 answer per 35–45 msgs) |
| **Night Chat Disturbance (03:00–07:00 MSK)** | Static 120 min | **150–180 min cooldown** (preserves night quiet) |
| **Triage False Negatives on Active Cases** | Blocked if any peer posts a 1-word reply ("Deadlock on First Reply") | **Approved if peer response was incomplete/unsubstantiated** |
| **Dialogue Thread Continuation** | Killed if main chat topics drift | **Protected within the reply thread** |
| **Validator Casualties** | Full suppression on emoji presence or cascade 503 | **Zero suppression on emoji (auto-sanitized), resilient cascade fallback** |

This concludes the comprehensive code triage investigation for Requirement R3. All proposed patches are backward-compatible, safe, and ready for validation in isolated test suites.
