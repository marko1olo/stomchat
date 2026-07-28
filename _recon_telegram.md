# Разведка: обращение с Telegram API (telethon)

Область: main.py, assistant.py, summarizer.py. Только чтение, правок не делал.
telethon **1.42.0**, `C:\Users\Admin\AppData\Local\Programs\Python\Python313\Lib\site-packages\telethon`.

## Базовый замер механики (нужен для находок 1, 2, 4)

Клиенты, main.py:794-816:
```
client     = TelegramClient(..., timeout=30, request_retries=10, connection_retries=1000, retry_delay=5)
bot_client = TelegramClient(..., timeout=30, request_retries=10, connection_retries=1000, retry_delay=5)
```
`flood_sleep_threshold` НЕ задан -> дефолт **60** (замер:
`inspect.signature(TelegramBaseClient.__init__)` -> `flood_sleep_threshold = 60`).

telethon `client/users.py:105-124`:
- FloodWait `seconds <= 60` -> `await asyncio.sleep(e.seconds)` и **повтор** внутри
  `for attempt in retry_range(self._request_retries)` = 10 попыток.
  **Худший случай: 10 x 60 = 600 с сна внутри ОДНОГО `send_message`.**
- FloodWait `seconds > 60` -> `raise`, наружу летит `FloodWaitError`.
- `users.py:45-57`: перед отправкой проверяется `self._flood_waited_requests[r.CONSTRUCTOR_ID]`.
  Ключ — **тип запроса, а не чат**. Один FloodWait на `SendMessageRequest` роняет
  `FloodWaitError` на ЛЮБОЙ следующий `send_message` в любой чат до конца окна.

Замер обработки FloodWait в области:
```
grep -i flood main.py assistant.py summarizer.py
main.py:752       # ...только комментарий
main.py:757       # ...только комментарий
assistant.py:4991 # ...только комментарий
```
**Ни одного `except FloodWaitError` во всех трёх файлах**; импорта `telethon.errors`
там нет вообще. Единственный обработчик в репозитории — `deppd.py:7,138` (утилита,
не в рантайме бота).

---

## Находка 1. assistant.py: ~100 вызовов Telegram и ровно ОДИН таймаут на весь файл

**Замер.** `grep -n "wait_for" assistant.py` -> **одна строка**, `assistant.py:3079`,
и это `download_media`, не отправка. При 5194 строках и ~100 вызовах
`bot_client.send_message / edit_message / delete_messages / get_messages / get_me /
get_permissions`, `event.reply`, `event.edit`.

Для сравнения: main.py — 40+ `asyncio.wait_for`; summarizer.py — все отправки идут
через `_send_message_once` (summarizer.py:188) с `timeout=TELEGRAM_SEND_TIMEOUT_SECONDS=90`.
Прикрыт не прикрыт только assistant.py.

**Сценарий отказа.** Врач в ЛС жмёт `/quiz`. assistant.py:2747:
```python
status_msg = await bot_client.send_message(entity=chat_id, message="🎲 <i>Генерирую клиническую викторину для вас... Подождите.</i>", parse_mode='html')
```
Telegram отвечает `FLOOD_WAIT_45`. 45 <= 60, значит telethon спит 45 с и повторяет,
до 10 раз — **до 600 с внутри этой одной строки, без возможности прерывания**. Врач
видит зависший бот; кулдаун `/quiz` — 60 с (assistant.py:3693), уже истёк, повторное
нажатие заводит второй такой же висящий вызов.

Особенно дорого там, где отправка идёт ПОСЛЕ оплаченной генерации: assistant.py:2770,
2803, 2878, 2921, 2974, 3329, 3641 — LLM-ответ уже получен и лежит в памяти,
а отдать его некуда, и при падении он теряется целиком.

**Правка.** Один хелпер отправки/правки в assistant.py с
`asyncio.wait_for(..., timeout=90)` и явным `except FloodWaitError`, вместо прямых
вызовов `bot_client.*`.

---

## Находка 2. FloodWait записывается пользователю как отказ доставки и глушит живого врача навсегда

**Место.** assistant.py:5157-5188, `check_and_send_group_activity_pings`.

```python
sample_size = max(1, int(math.ceil(len(candidates) * 0.20)))
targets = random.sample(candidates, min(sample_size, len(candidates)))
for uid in targets:
    ...
    await bot_client.send_message(entity=uid, message=msg, parse_mode='html', link_preview=True)
    ...
    except Exception as send_err:
        failures = pings.get(str(uid), {}).get("ping_failures", 0) + 1
        commit_pm_ping(uid, last_group_ping=datetime.now().isoformat(), ping_failures=failures)
```

**Замеры.** В цикле **нет ни `asyncio.sleep`, ни потолка на `len(targets)`**.
`MAX_PINGS_PER_CYCLE = 5` (assistant.py:4854) применяется ТОЛЬКО к ЛС-пингам
(проверка на assistant.py:4920); у групповых пингов такой проверки нет. Размер
пачки — 20% от всех, кто писал в ЛС за 30 дней (`database.get_active_pm_users(days_limit=30)`,
database.py:997), то есть растёт вместе с аудиторией без ограничения.

**Сценарий отказа.** 300 активных в ЛС -> `targets` = 60 отправок разным peer
подряд без паузы. Telegram отвечает FloodWait (штатная реакция на рассылку).
Складываются два эффекта:
1. `except Exception` **не различает** FloodWait и `UserIsBlockedError`. Каждому
   недоставленному ставится `ping_failures += 1` и `last_group_ping = now`.
2. `_flood_waited_requests` кэшируется по `CONSTRUCTOR_ID` `SendMessageRequest`
   (см. базовый замер), поэтому после первого FloodWait > 60 с **весь остаток
   пачки падает мгновенно** — 30-50 пользователей получают +1 отказ за один заход.

Три таких захода -> `ping_failures >= MAX_PING_FAILURES` (=3, assistant.py:4855)
-> assistant.py:5051 `if user_info.get("ping_failures", 0) >= MAX_PING_FAILURES: continue`.
**Живой врач, который бота не блокировал, исключается из приглашений в чат навсегда.**
Выхода нет: `ping_failures` сбрасывается в 0 только на успешной отправке
(assistant.py:5167), а успешных отправок больше не будет — его уже не выбирают
в `candidates`. Тот же механизм в ЛС-пингах: assistant.py:4991-4999 — комментарий
там прямо перечисляет FloodWait среди причин и всё равно считает его отказом юзера.

**Правка.** Ловить `FloodWaitError` отдельно: не увеличивать `ping_failures`,
прервать пачку (`break`), плюс `await asyncio.sleep(1)` между отправками и потолок
на `len(targets)` по образцу `MAX_PINGS_PER_CYCLE`.

---

## Находка 3. Разрешение сущностей: до 14 round-trip'ов на одно сообщение, кэша нет, один вызов буквально дублирован

**Замер round-trip'ов на ОДНО сообщение группы, отвечающее боту:**

| # | file:line | вызов | что уходит в Telegram |
|---|---|---|---|
| 1 | main.py:1327 | `event.get_sender()` | `users.GetUsers` (если `_sender` None/min) |
| 2 | assistant.py:1027 | `event.client.get_messages(event.chat_id, ids=reply_to_msg_id)` | `channels.GetMessages` |
| 3 | assistant.py:1300 | `event.client.get_messages(event.chat_id, ids=reply_to_msg_id)` | `channels.GetMessages` — **тот же чат, тот же id** |
| 4..8 | assistant.py:1342 | `event.client.get_messages(...)` в `for _ in range(6)` | до 5 x `channels.GetMessages` |
| 9..14 | assistant.py:1325 | `await curr.get_sender()` в том же цикле | до 6 x `users.GetUsers` |

Пункты 2 и 3 — **гарантированный дубль**: `check_and_trigger_assistant`
(assistant.py:1243) на строке 1252 вызывает `check_and_apply_silence(...)`, которая
внутри (1027) достаёт `parent_msg` ровно за тем же, зачем сам
`check_and_trigger_assistant` достаёт его повторно на 1300 — сверить
`parent_msg.sender_id == BOT_ID`. Результат первого вызова выбрасывается.

**Кэша нет ни на одном уровне** (проверено по исходникам telethon):
- у telethon нет кэша сообщений — `get_messages` всегда идёт по сети;
- `get_entity` на `InputPeerUser` всегда идёт по сети:
  `client/users.py:319` `tmp.extend(await self(functions.users.GetUsersRequest(curr)))`;
- `get_sender` пропускает сеть только если `message._sender` заполнен и не `min`
  (`tl/custom/sendergetter.py:49-58`); в супергруппах `min`-юзеры — норма.

Что бот умеет кэшировать в другом месте: `sync_history` (main.py:1892-1896) держит
`sender_cache = {}` с комментарием «Автора спрашиваем один раз на догоняющий проход»
и цифрой 749 уникальных отправителей. В живом обработчике этого нет.

**Сценарий отказа.** Ожившая ветка (5-6 сообщений подряд в ответ боту) даёт до ~14
запросов на каждое сообщение вместо ~3. Это ровно тот профиль, на который Telegram
отвечает FloodWait, — а по находке 1 его никто не поймает, по находке 2 он спишется
на пользователей, по находке 4 его не будет видно в логе. Ни один вызов из строк
2-14 таймаута не имеет (все в assistant.py).

**Правка.** `check_and_apply_silence` должна принимать уже загруженный `parent_msg`
аргументом (или возвращать его наружу), а не тянуть повторно; в цикле цепочки
использовать свойство `curr.sender` вместо `await curr.get_sender()`.

---

## Находка 4 (ответ на пункт 5 задания). FloodWait физически не может попасть в bot.log — логгер telethon заглушён до ERROR

**Место.** runtime_guard.py:79, последняя строка настройки логирования:
```python
logging.getLogger("telethon").setLevel(logging.ERROR)
```
При этом telethon сообщает о сне на FloodWait через
`self._log[__name__].info(*_fmt_flood(e.seconds, request))` — `client/users.py:122`,
уровень **INFO** логгера `telethon.client.users`. Уровень ERROR его отсекает.

**Следствие, проверяемое по логам.** Сон до 600 с внутри `send_message` (находка 1)
**не оставляет в bot.log ни одной строки**. Находка 1 не просто опасна — она
недиагностируемая: врач видит зависший бот, в журнале за это время пусто.

**Что в логах при этом ЕСТЬ по flood (то есть замер, а не догадка):**
```
bot.log.1 (109798 строк, 2026-03..05):
  61717:  2026-03-28 15:17:59,071 - telethon.network.mtprotosender - WARNING -
          Server indicated flood error at transport level: Invalid response buffer (HTTP code 429)
  70263:  2026-03-28 15:18:04,346 - (то же)
  100072: 2026-05-03 17:28:04,199 - (то же)
bot.log (12251 строка, до 2026-07-28 10:00): flood-строк ноль.
```
Три события; два с интервалом 5 секунд, то есть повтор сразу упёрся в тот же лимит.
Прошли они только потому, что уровень WARNING. **Задержек в секундах в логах нет
нигде — не потому, что FloodWait не было, а потому что строка с секундами
подавлена.** Ответ на пункт 5 задания: реальные flood-события подтверждены на
транспортном уровне, а RPC-уровень с секундами замерить по этим логам невозможно
в принципе, пока стоит `setLevel(ERROR)`.

**Правка.** `logging.getLogger("telethon.client.users").setLevel(logging.INFO)`
одной строкой рядом с runtime_guard.py:79 — иначе FloodWait не будет видно никогда.

---

## Находка 5. `get_me()` на горячем пути медиа: 2 сетевых запроса вместо готовых глобалов, и при их отказе бот молча игнорирует прямое обращение

**Место.** assistant.py:1774-1789, внутри `check_and_trigger_assistant_media`
(assistant.py:1739) — путь обработки клинического снимка, то есть основной сценарий бота.

```python
    if getattr(message, 'reply_to_msg_id', None):
        try:
            parent = await bot_client.get_messages(message.chat_id, ids=message.reply_to_msg_id)
            if parent and parent.sender_id == (await bot_client.get_me()).id:
                is_direct_reply = True
        except Exception:
            pass

    is_mentioned = False
    if text:
        try:
            bot_info = await bot_client.get_me()
            if bot_info and bot_info.username and bot_info.username.lower() in text.lower():
                is_mentioned = True
        except Exception:
            pass
```

**Замер.** `get_me()` **всегда** идёт в сеть. telethon `client/users.py:166-176`:
кэш-ветка `if input_peer and self._mb_entity_cache.self_id` требует `input_peer=True`;
здесь вызов без аргументов, поэтому исполняется
`me = (await self(functions.users.GetUsersRequest([types.InputUserSelf()])))[0]`.
Два вызова в 12 строках = **2 лишних `users.GetUsers` на каждое медиасообщение**,
плюс `get_messages` на 1777. Все три — без таймаута (находка 1).

При этом ответы уже лежат в памяти процесса: `resolve_bot_identity` (assistant.py:189-206)
заполняет глобалы `BOT_ID` и `BOT_USERNAME` один раз на старте, и остальной код
ими и пользуется (assistant.py:1301, 1320, 1778 — сравните: рядом, в
`check_and_trigger_assistant`, используется именно `BOT_ID`).

**Сценарий отказа (важнее лишнего трафика).** Врач отвечает на сообщение бота
рентгеновским снимком. `get_me()` спотыкается — FloodWait, таймаут соединения,
что угодно. `except Exception: pass` -> `is_direct_reply` остаётся `False`,
`is_mentioned` остаётся `False` -> `is_passive = True` (assistant.py:1792) ->
включается двухчасовой кулдаун пассивных срабатываний (assistant.py:1794-1797)
-> `return`. **Прямое обращение врача с клиническим снимком отбрасывается молча,
без единой строки в логе** (оба `except` — пустые `pass`, а сам сбой telethon
подавлен по находке 4). Ветка обычной пассивной обработки при этом ещё и уводит
логику от нужной: сообщение считается «фоновым», хотя это адресный вопрос.

**Правка.** Заменить оба `await bot_client.get_me()` на глобалы `BOT_ID` и
`BOT_USERNAME`; если они пусты — `await resolve_bot_identity(bot_client)` один раз,
как это уже делается на assistant.py:1004-1006.

---

## Опровергнутые гипотезы (проверил, находкой не является)

1. **summarizer.py без таймаутов** — неверно, это самый аккуратный файл из трёх.
   Все отправки идут через `_send_message_once` (summarizer.py:188):
   `asyncio.wait_for(..., timeout=90)`, защита от дубля через
   `_find_recent_matching_message`, восстановление после таймаута повторным
   сканированием последних 20 сообщений, деградация в plain text на ошибке разбора
   HTML. `pin_message` — через `_pin_message_safely` (summarizer.py:240) с
   `timeout=30` и глушением ошибок.

2. **`edit_message` в inline-кнопках падает и оставляет крутящийся спиннер** —
   уже закрыто, и я ошибся на первом проходе. Все 11 `edit_message` из
   assistant.py:4177-4364 и `event.edit` (4488) лежат внутри
   `handle_quiz_callback` (assistant.py:4149-4492), а единственный её вызов —
   main.py:1789-1804, где есть и глушение `MessageNotModifiedError`
   (`if "MessageNotModifiedError" in type(e).__name__ or "Content of the message
   was not modified" in str(e): pass`), и `finally: if not answered: await
   event.answer()` с комментарием про спиннер. Остаётся косметика: на прочей
   ошибке правки (сообщение слишком старое) врач видит, что меню не изменилось,
   без объяснения — но спиннер снимается и обработчик не падает.

3. **`get_sender()` роняет обработчик сообщения** — уже исправлено
   (main.py:1325-1330: `try/except` вокруг `wait_for`, дальше `sender_name = "Unknown"`).
   Историческое доказательство, что это происходило реально, в bot.log:712/745/778
   (2026-05-18 22:59:45-46, три падения за 0.5 с): `get_sender -> get_entity ->
   users.GetUsersRequest -> CancelledError -> TimeoutError`, старая ревизия
   main.py:278, обработчик падал целиком и сообщение не сохранялось. Код другой,
   находкой не считаю — но это прямой замер, что разрешение сущностей на этом боте
   реально зависает, а по находке 3 их за одну обработку до семи.

4. **Цикл пингов может зависнуть навсегда** — уже исправлено. main.py:768-786
   оборачивает оба прохода в `asyncio.wait_for(timeout=PING_PHASE_TIMEOUT_SECONDS=1200)`,
   и комментарий main.py:751-758 прямо считает бюджет с учётом сна на FloodWait.
   Дыра осталась не в цикле, а внутри него — находки 1 и 2.

5. **FloodWait при рассылке дайджеста заставляет перегенерировать его каждые
   10 минут** — уже исправлено. `daily_cache_text` переживает круг цикла
   (main.py:570-572, 619-623), `sent_targets` персистится через
   `load_sent_targets`/`mark_target_delivered`, каждая цель в своём
   `try/except`. Повтор отправки из кэша стоит один `get_messages` + один
   `send_message` раз в 10 минут — приемлемо.

6. **`/search` и `/bookmarks` могут превысить лимит сообщения Telegram (4096)** —
   не подтвердилось на замере. `send_message_chunks_async` (assistant.py:174,
   `html_safe.split_html`, лимит 4000) вызывается ровно из одного места
   (assistant.py:3341), все прочие отправки не нарезаются, но фактические длины
   в лимит укладываются:
   - `stomat_wiki.db`, 12784 факта: длина `content` min/медиана/среднее/max =
     38 / 236 / 260 / 5477, p95 = 402, p99 = 537.
   - Прогон настоящего кода `/search` (`extract_keywords` -> `like_any_case` ->
     `LIMIT 5` на ключ -> дедуп -> `[:8]` -> `\n\n`) на семи реальных запросах
     («BOPT», «имплант», «фиксация коронок циркония», «обтурация каналов
     гуттаперча», «анестезия мандибулярная артикаин», «уступ препарирование
     десна керамика», «гипохлорит»): 1425-2783 символов, максимум **2783**.
   - `/bookmarks`: `per_page = 10`, `BOOKMARK_SNIPPET_CHARS = 80` (assistant.py:2029),
     имя обрезано до 64 -> худшая страница ~3810 символов. Под лимитом, но запас
     всего ~290 символов; при этом `html.escape` может раздуть 80 символов до 400,
     так что теоретический выход за лимит существует. Комментарий на
     assistant.py:2846-2850 говорит, что угловая скобка встречается в одном
     сообщении из 30 082 — то есть реалистичным драйвером это не является.
     Не находка, но самое узкое место из проверенных.

7. **`patched_send_message` (main.py:821-838) не покрывает
   `send_file`/`event.reply`/`event.edit`** — верно как факт, но это учёт своих
   сообщений для `/wipe`, а не обращение с Telegram API. Вне области, не разворачивал.
