# Правка обращения с Telegram API (по `_recon_telegram.md`)

Владение файлами в этой работе: созданы `tg_safety.py` и `test_tg_safety.py`. Ни один
существующий `.py` не тронут — всё остальное здесь лежит как точные патчи для ведущего.

## ВНИМАНИЕ: боевые файлы менялись ПОСРЕДИ этой работы

Замерено по времени изменения: `assistant.py` — 2026-07-29 07:40:46 (382307 -> 397193 байт),
`runtime_guard.py` — 07:55:14, затем ещё раз 08:04 (10950 -> 21066), `main.py` — 07:57:40,
затем ещё раз 08:04 (124545 -> 155544). Файлы правит кто-то другой прямо сейчас.

Нумерация строк в `_recon_telegram.md` уже недействительна, и приведённая ниже тоже
устареет при следующей правке. Поэтому каждый патч даёт **точный текст «до»** — по нему
место находится однозначно, независимо от сдвига номеров. Номера строк ниже — замер на
07:58, кроме перепроверенных на 08:05 якорей патчей: `runtime_guard.py:132` (заглушение
telethon на месте), `main.py:830-831` и `main.py:843-844` (оба клиента с
`timeout=30, request_retries=10`), `flood_sleep_threshold` по-прежнему нигде не задан,
`tg_safety` нигде не подключён.

Проверено на текущих файлах: все находки разведки в силе.

| Факт | Где сейчас | Состояние |
|---|---|---|
| `logging.getLogger("telethon").setLevel(logging.ERROR)` | `runtime_guard.py:132` | на месте |
| клиенты с `timeout=30, request_retries=10, connection_retries=1000, retry_delay=5` | `main.py:826` (`client`), `main.py:839` (`bot_client`) | на месте |
| `flood_sleep_threshold` задан где-либо | — | **нет**, значит 60 |
| `except FloodWaitError` в main/assistant/summarizer | — | **ноль** |
| `asyncio.wait_for` в `assistant.py` | 2 штуки, оба `download_media` | ни одной отправки |
| вызовов telethon в `main.py` / `assistant.py` | 29 / **122** | |
| `tg_safety` подключён где-либо | — | нет, adoption не начат |

---

## 1. Замер по журналам: что Telegram РЕАЛЬНО делал с этим ботом

Искал по `bot.log`, `bot.log.1`, `bot_test.log`, `distiller.log`, `bot_supervisor.log`.
`stomat_bot.db` в замерах не участвует — это чужой устаревший снимок.

Точные шаблоны (регистронезависимо): `floodwait`, `flood_wait`, `FLOOD_WAIT`, `flood error`,
`ChatWriteForbidden`, `MsgIdInvalid`, `MESSAGE_ID_INVALID`, `SlowModeWait`, `SLOWMODE_WAIT`,
`UserIsBlocked`, `PeerIdInvalid`, `PeerFlood`, `AuthKey`, `ServerError`, `RPCError`, `rpc_error`,
`TimeoutError`, `CancelledError`, `Disconnect`, `disconnected`, `Connection.*failed`,
`ConnectionResetError`, `ServerDisconnected`, `OSError`, `Server closed the connection`,
`at connecting failed`, `Security error`, `Telegram is having internal issues`.

| Класс отказа | `bot.log` (12251 стр., 2026-05-18 → 2026-07-28) | `bot.log.1` (109798 стр., 2026-01-31 → 2026-05-13) |
|---|---|---|
| `Server closed the connection` | **181** | **51542** |
| `Attempt N at connecting failed` | **26** | **2447** (1187 `ConnectionAbortedError`, 1171 `TimeoutError`, ост. `OSError`) |
| `Security error … wrong session ID` | 0 | **423** |
| `Telegram is having internal issues` (RPC-уровень) | 0 | **51** |
| — `ServerError: RPCError -500: No workers running (caused by GetUsersRequest)` | 0 | **9** |
| — `RpcCallFailError` (наследник `ServerError`) | 0 | **20** |
| — `PersistentTimestampOutdatedError` (наследник `ServerError`) | 0 | **20** |
| — `TimeoutError: Timeout while fetching data` | 0 | **1** |
| — `HistoryGetFailedError` | 0 | **1** |
| flood (транспортный уровень, HTTP 429) | **0** | **3** |
| FloodWait с секундами (RPC-уровень) | **0** | **0** |
| `ChatWriteForbidden` | **0** | **0** |
| `MsgIdInvalid` / `MESSAGE_ID_INVALID` | **0** | **0** |
| `SlowModeWait` / `SLOWMODE_WAIT` | **0** | **0** |
| `UserIsBlocked` / `PeerIdInvalid` / `PeerFlood` | **0** | **0** |
| `AuthKey*` / `SESSION_REVOKED` / `Unauthorized` | **0** | **0** |
| прикладной `TimeoutError` на вызове telethon (трассировки) | **8**: `get_sender` в обработчике сообщения ×3 (2026-05-18 22:59:45, старая ревизия `main.py:278`), `client.start()` ×5 (`main.py:908`) | — |

Три flood-события целиком:

```
bot.log.1:61717  2026-03-28 15:17:59,071 - telethon.network.mtprotosender - WARNING -
                 Server indicated flood error at transport level: Invalid response buffer (HTTP code 429)
bot.log.1:70263  2026-03-28 15:18:04,346 - (то же, через 5 с: повтор сразу упёрся в тот же лимит)
bot.log.1:100072 2026-05-03 17:28:04,199 - (то же)
```

Девять строк `ServerError` целиком уложились в 2 миллисекунды (bot.log.1:61625-61633,
2026-03-28 15:17:51,791-793) — все девять на `GetUsersRequest`, то есть на разрешении
сущностей, которое по находке 3 разведки выполняется до 14 раз на одно сообщение.

### Что из этого следует для приоритетов

1. **Доминирующий отказ здесь — не flood, а обрыв соединения.** 51542 + 181 обрыв и 2473
   провалившиеся попытки соединения против 3 flood-событий. Класс «повторяемый транспортный
   сбой» здесь основной, а не теоретический, и именно он подвешивает вызов надолго:
   у клиентов `connection_retries=1000, retry_delay=5`, то есть восстановление соединения
   внутри одного `send_message` практически не ограничено по времени.
2. **RPC-уровень FloodWait по этим журналам замерить НЕЛЬЗЯ.** `runtime_guard.py:132` глушит
   логгер `telethon` до ERROR, а telethon сообщает о сне на FloodWait через
   `self._log[__name__].info(...)` в `client/users.py` — уровень INFO логгера
   `telethon.client.users`. 51 строка этого логгера в `bot.log.1` — WARNING старой ревизии,
   до заглушения; в `bot.log` строк этого логгера **ноль**. Отсутствие FloodWait в журнале
   не является доказательством отсутствия FloodWait.
3. **`ServerError` подтверждён замером** — 49 из 51 строки «internal issues» это `ServerError`
   и наследники. Telethon сам считает его повторяемым: ловит, пишет WARNING, делает
   `await asyncio.sleep(2)` и повторяет внутри `request_retries`.
4. **Терминальных ошибок в журналах не наблюдалось ни разу** (0 `ChatWriteForbidden`,
   0 `MsgIdInvalid`, 0 auth за 122049 строк). Различать их всё равно обязательно: повтор
   терминальной ошибки сжигает дедлайн, за который врач ждёт ответа, и всё равно ничего
   не доставляет.

### Механика telethon 1.42.0 — замер мой, не пересказ разведки

`inspect.signature(TelegramBaseClient.__init__)`: `timeout=10, request_retries=5,
connection_retries=5, retry_delay=1, flood_sleep_threshold=60`. В `main.py` переопределены
все, кроме `flood_sleep_threshold` — то есть он равен 60.

`client/users.py`, прочитано:

* FloodWait `seconds <= flood_sleep_threshold` → `self._log[__name__].info(...)` +
  `await asyncio.sleep(e.seconds)` и повтор внутри `for i in retry_range(request_retries)`.
  При `request_retries=10` это **до 600 с сна внутри ОДНОГО `await send_message(...)`**,
  без возможности прерывания и без единой строки в журнале.
* FloodWait `seconds > flood_sleep_threshold` → `raise` наружу.
* Предполётная проверка: `if r.CONSTRUCTOR_ID in self._flood_waited_requests` — ключ по
  **типу запроса, а не по чату**; при `diff > flood_sleep_threshold` поднимается
  `FloodWaitError` ещё до обращения к сети. `SlowModeWaitError` в этот кэш намеренно не
  пишется (`# SLOW_MODE_WAIT is chat-specific, not request-specific`).
* `ServerError` → WARNING «Telegram is having internal issues» + `await asyncio.sleep(2)` +
  повтор. Ровно эти 49 строк и замерены в журнале.

---

## 2. Инвентаризация: 10 худших мест

Критерий «худшее» — что теряет врач, а не частота отказа.

| # | file:line | вызов | таймаут | повтор | что получает врач при отказе |
|---|---|---|---|---|---|
| 1 | `assistant.py:3940` | `bot_client.edit_message(chat_id, status_msg.id, final_text, parse_mode='html')` | **нет** | нет | `/summary` в группе: сводка УЖЕ сгенерирована LLM и лежит в памяти, единственный путь доставки — этот `edit_message`. Отказ → `except Exception as e` → сводка потеряна целиком, генерация оплачена впустую. Зависание → «Собираю и анализирую историю обсуждения…» висит вечно |
| 2 | `assistant.py:3998`, `4019` | `bot_client.send_message(...)` в `handle_group_direct_ask` | **нет** | нет | Ответ на прямой вопрос врача в группе, LLM уже оплачен. `NO-TRY`: исключение уходит в `run_group_features` (`main.py`, `except Exception`), ответ теряется без следа для врача |
| 3 | `assistant.py:5552` | `bot_client.send_message(entity=uid, …)` в цикле по `targets` | **нет** | нет | Групповой пинг. `except Exception as send_err` не отличает FloodWait от `UserIsBlockedError` → `ping_failures += 1`. Три захода → `>= MAX_PING_FAILURES` (=3) → врач пропускается в отборе кандидатов: **живой врач, не блокировавший бота, исключён из приглашений навсегда**, потому что сброс счётчика бывает только на успешной отправке, а её больше не будет |
| 4 | `assistant.py:5366` | `bot_client.send_message(entity=chat_id, message=reply_text, …)` | **нет** | нет | ЛС-пинг, тот же механизм. Комментарий прямо над `except` сам перечисляет FloodWait среди причин и всё равно считает его виной врача |
| 5 | `assistant.py:2078` | `bot_info = await bot_client.get_me()` | **нет** | нет | Горячий путь клинического снимка. Отказ → `except Exception: pass` → `is_mentioned = False` → `is_passive = True` → двухчасовой кулдаун и `return`. **Прямое обращение врача со снимком отброшено молча, без строки в журнале** (оба `except` пустые, а сбой telethon подавлен заглушением логгера) |
| 6 | `assistant.py:2069-2070` | `get_messages(...)` и `(await bot_client.get_me()).id` | **нет** | нет | Тот же путь: `is_direct_reply` остаётся `False`. Плюс два лишних `users.GetUsers` на каждое медиасообщение при готовых глобалах `BOT_ID` / `BOT_USERNAME` |
| 7 | `assistant.py:3057` (`/quiz`), `3235` (`/case`), `2512` (симулятор), `3874` (`/summary`) | `status_msg = await bot_client.send_message(…)` | **нет** | нет | Статусное сообщение в ЛС, `NO-TRY`. Зависание держит **per-user замок** `_pm_user_lock` в `main.py` → следующее сообщение того же врача тоже встаёт в очередь навсегда. Отказ выбрасывает всю команду |
| 8 | `assistant.py:4574, 4594, 4633, 4649, 4660, 4677, 4693, 4707, 4729, 4761` | 10 × `edit_message` + 11 × `event.answer()` в `handle_quiz_callback` | **нет** | нет | Все `NO-TRY`. На `raise` спасает `finally: await event.answer()` в `main.py`. Но при **зависании** `finally` не наступает вообще: спиннер на кнопке крутится до таймаута клиента Telegram |
| 9 | `assistant.py:188` | `bot_client.send_message(entity=chat_id, message=chunk, **kwargs)` в цикле по чанкам | **нет** | нет | `send_message_chunks_async` — единственный нарезающий отправщик (`/search`). `NO-TRY`, цикл без паузы между чанками: отказ на середине оставляет половину выдачи |
| 10 | `assistant.py:1531`, `1557`, `1574` | `get_messages` ×2 (второй — в `for _ in range(6)`), `curr.get_sender()` в том же цикле | **нет** | нет | Разрешение цепочки ответов: до 14 round-trip на одно сообщение, ни одного таймаута. Именно этот профиль запросов и вызывает flood, а девять замеренных `ServerError` пришли ровно на `GetUsersRequest` |

Сопутствующее того же класса: `main.py:1978` `get_permissions` (`/wipe`, без таймаута),
`main.py:1985`/`1989` `delete_messages` там же, `main.py:1913` (подтверждение `/save`),
`assistant.py:3412` `edit_message` внутри `finally` — может подвесить сам `finally`.

### Сводный замер покрытия

| | main.py | assistant.py | summarizer.py |
|---|---|---|---|
| вызовов telethon (построчный разбор, без строк-комментариев) | 29 | **122** | — |
| `asyncio.wait_for` в файле | 35 | **2** | 6 |
| из них покрывают отправку или правку | — | **0** (оба — `download_media`) | все отправки через `_send_message_once`, `timeout=90` |
| `except FloodWaitError` | **0** | **0** | **0** |
| импорт `telethon.errors` | **нет** | **нет** | **нет** |

Единственный обработчик FloodWait в репозитории — `deppd.py`, утилита вне рантайма бота.
Полный машинный перечень всех 151 вызова — в конце файла, раздел «Приложение».

---

## 3. Что гарантирует `tg_safety.py`

1. **`timeout` — ПОЛНЫЙ бюджет операции, а не бюджет попытки.** Дедлайн считается один раз
   (`time.monotonic() + timeout`), и сон на FloodWait, откат на обрыве и каждая попытка
   вычитаются из него же. Вложенных бюджетов быть не может: внутреннего срока просто нет.
2. **FloodWait дольше остатка бюджета НЕ усыпляет.** Если `wait + MIN_RETRY_SLICE_SECONDS`
   не влезает в остаток — сна нет, отказ сразу, `reason="flood_over_budget"`, серверные
   секунды сохранены в итоге для вызывающего.
3. **Терминальное отделено от повторяемого.** Повторяются FloodWait, обрыв/таймаут
   транспорта и `ServerError` с наследниками. `ChatWriteForbidden`, `MsgIdInvalid`, auth,
   `UserIsBlocked` и **неизвестный класс** — отказ с первой попытки.
4. **Ни одного молчаливого отказа.** Каждый отказ — WARNING со строкой `tg give up op=…
   chat_id=… reason=… kind=… attempts=… elapsed=…s flood_wait=… error=…`.
5. **Вина врача отделена от вины Telegram** (`TgOutcome.user_at_fault`). FloodWait, обрыв и
   исчерпанный бюджет — не вина врача; `UserIsBlocked`, удалённый аккаунт, приватность,
   неизвестный peer — вина. Это то, чего не хватает обоим циклам пингов.
6. **Кулдаун по ключу чата, объём ограничен.** Остановка одного чата не глушит остальные —
   в отличие от кэша самого telethon, который заведён по типу запроса.
7. **Импорт безвреден.** Ни сети, ни чтения конфигурации, ни настройки логирования;
   `telethon` на импорте не подтягивается (классы разрешаются лениво и кэшируются). Без
   telethon классификация работает по именам классов — проверено отдельным процессом с
   запрещённым импортом пакета.
8. **Отмена доводится, а не превращается в отказ.** `runtime_guard.create_task` и чужие
   `wait_for` снимают работу; снятая работа обязана остаться снятой.

Честные оговорки:

* снятие по дедлайну — это `task.cancel()`, то есть для отправки «неизвестно, ушло ли
  сообщение». Где дубль недопустим, отмену обязан дополнять поиск своего сообщения, как
  уже сделано в `summarizer.py` через `_find_recent_matching_message`;
* модуль не может помешать telethon спать ВНУТРИ вызова — он лишь снимает вызов по
  дедлайну. Чтобы FloodWait доходил до нас вместо внутреннего сна до 600 с, нужен патч 2
  (`flood_sleep_threshold=0`);
* `guard` принимает функцию, возвращающую корутину, а не готовую корутину: корутину нельзя
  дождаться дважды. Передача корутины поднимает `TypeError` сразу.

---

## 4. Прогон и диверсионная проверка

```
python test_tg_safety.py     -> PASSED: 131   FAILED: 0    код выхода 0
python -c "import tg_safety" -> код выхода 0, вывода нет
python run_all_tests.py tg_safety
   ок     test_tg_safety.py                  проверок  131   14.8 с
   наборов: 1   проверок: 131   провалено: 0   время: 15 с
   охраняемые файлы целы: 19 шт, md5 совпал
```

Последняя строка — отдельное доказательство, что набор не тронул ни `assistant.py`, ни
`main.py`, ни `bot.log`, ни базы.

Диверсия: 16 умышленных поломок `tg_safety.py`, после каждой полный прогон набора,
затем восстановление из копии и сверка md5.

| Диверсия | провалено проверок | код выхода | вердикт |
|---|---|---|---|
| S1 общий бюджет превращён в бюджет ПОПЫТКИ | 2 | 1 | поймана |
| S2 FloodWait усыпляет безусловно | 13 | 1 | поймана |
| S3 неизвестная ошибка объявлена повторяемой | 2 | 1 | поймана |
| S4 список терминальных имён отключён | 1 | 1 | поймана |
| S5 отказ перестаёт писаться в журнал | 19 | 1 | поймана |
| S6 кулдаун стал общим на все чаты | 12 | 1 | поймана |
| S7 любой отказ списывается на врача | 3 | 1 | поймана |
| S8 готовая корутина принимается молча | 2 | 1 | поймана |
| S9a проверка `task.cancelled()` снята | 0 | 0 | **НЕ ПОЙМАНА — см. ниже** |
| S9b отмена снаружи глотается в `_run_bounded` | 1 | 1 | поймана |
| S10 telethon импортируется на уровне модуля | 2 | 1 | поймана |
| S11 память кулдауна не ограничена | 1 | 1 | поймана |
| S12 отсутствующие секунды подменяются выдумкой | 2 | 1 | поймана |
| S13 кулдаун не проверяется перед обращением | 2 | 1 | поймана |
| S14 транспортный сбой повторяется без потолка попыток | 3 | 1 | поймана |
| S15 успешное значение не возвращается вызывающему | 1 | 1 | поймана |

Головная диверсия S2 (безусловный сон на FloodWait) даёт ровно ту строку, ради которой
набор написан:

```
[FAIL] сна не было: вернулись быстрее, чем длится сам FloodWait -- 45.088 с при FLOOD_WAIT_45
[FAIL] сна не было: вернулись даже раньше конца своего бюджета -- 45.088 с при бюджете 0.5 с
[FAIL] серверное ожидание сохранено в итоге для вызывающего -- flood_seconds=None
```

**S9a не поймана, и это не дыра в наборе, а избыточная строка в модуле.** Снятие проверки
`if task.cancelled(): raise asyncio.CancelledError()` поведение не меняет: у снятой задачи
`Task.exception()` сам поднимает `CancelledError` (замерено отдельно), поэтому отмена
доходит наружу и без этой строки. Строка оставлена сознательно и с комментарием: полагаться
на побочный эффект чужого метода — заявка на то, что первый же рефакторинг с `try/except`
вокруг `exception()` проглотит отмену молча. Само поведение проверено двумя проверками
раздела [11], и S9b показывает, что они не слепые.

Первый проход диверсий поймал **четыре пустых проверки в наборе**; все четыре исправлены,
и в таблице выше уже итог после исправления:

1. S1 проходила набор целиком: ни в одном сценарии зависание не следовало за повторами,
   поэтому потолок на попытку никем не измерялся. Добавлена проверка, где три мгновенных
   обрыва съедают откатами 0.75 с из 1.2 с, а зависший четвёртый вызов обязан быть снят
   через остаток (~0.45 с), а не через полный бюджет. Сломанный модуль держал его 1.22 с.
2. S4 проходила: отключение списка терминальных имён маскировалось значением по умолчанию
   («неизвестное = терминальное»). Добавлен класс, попадающий в оба списка сразу, — на нём
   приоритет терминального становится измеримым.
3. S8 роняла набор до сводки вместо строки `[FAIL]`. Проба перехватывает любое исключение и
   превращает его в провал проверки.
4. S2 подвешивала набор на 300 с и он умирал, не напечатав ни одной строки: сломанный
   модуль засыпал внутри вспомогательной пробы в отдельном процессе, а у той был свой
   жёсткий срок. Срок пробы теперь превращается в провал проверки, а не в падение набора.

---

## 5. План внедрения для ведущего

Порядок важен: патч 2 меняет поведение telethon и до патчей 3-4 сделает хуже — FloodWait
начнёт доходить до `except Exception` в циклах пингов и списываться на врачей.

### Патч 0 (обязателен во всех файлах, где будет adoption)

Добавить рядом с прочими импортами модуля:

```python
import tg_safety
```

### Патч 1 — сделать FloodWait видимым. `runtime_guard.py`, функция `configure_logging`

Без него ни один следующий шаг нельзя проверить по журналу.

до:
```python
    logging.getLogger("telethon").setLevel(logging.ERROR)
```
после:
```python
    logging.getLogger("telethon").setLevel(logging.ERROR)
    # Единственная строка, где telethon сообщает, что СПИТ на FloodWait и сколько
    # секунд, идёт уровнем INFO логгера telethon.client.users (client/users.py).
    # Общее заглушение до ERROR срезало её, и сон до 600 с внутри одного
    # send_message не оставлял в журнале ничего: врач видит зависший бот, в
    # журнале за это время пусто. Замер: в bot.log (12251 строка,
    # 2026-05-18..2026-07-28) строк этого логгера ноль.
    logging.getLogger("telethon.client.users").setLevel(logging.INFO)
```

### Патч 2 — прекратить внутренний сон telethon. `main.py`, ОБА `TelegramClient(...)`

Делать ТОЛЬКО вместе с патчами 3 и 4 или после них.

до (в каждом из двух блоков):
```python
    timeout=30,
    request_retries=10,
    connection_retries=1000,
    retry_delay=5,
```
после:
```python
    timeout=30,
    request_retries=10,
    connection_retries=1000,
    retry_delay=5,
    # По умолчанию 60 (замер: inspect.signature(TelegramBaseClient.__init__)).
    # При этом значении telethon сам спит на FloodWait <= 60 с и повторяет до
    # request_retries=10 раз, то есть до 600 с внутри ОДНОГО await send_message,
    # без возможности прерывания. Ноль означает «не спи сам, отдай
    # FloodWaitError наверх» — там его ждёт tg_safety, который считает сон
    # против ОСТАТКА бюджета вызывающего.
    flood_sleep_threshold=0,
```

### Патч 3 — групповые пинги. `assistant.py`, `check_and_send_group_activity_pings`

Здесь цена самая высокая: врач теряет приглашения навсегда.

до:
```python
                try:
                    await bot_client.send_message(entity=uid, message=msg, parse_mode='html', link_preview=True)
                    # Сохраняем в историю переписки ЛС
                    await database.save_pm_message(uid, "Assistant", f"[Проактивный пинг чата]: {teaser}")
```
после:
```python
                try:
                    ping_result = await tg_safety.guard(
                        lambda: bot_client.send_message(
                            entity=uid, message=msg, parse_mode='html',
                            link_preview=True),
                        op="send_message:group_ping", chat_id=uid, timeout=90)
                    if not ping_result.ok:
                        if ping_result.flooded:
                            # FloodWait — это лимит Telegram на нашу рассылку, а
                            # не отказ врача. Счётчик отказов не двигаем: три
                            # таких захода исключали живого врача из приглашений
                            # навсегда, потому что сброс бывает только на
                            # успешной отправке, а его уже не выбирают в
                            # кандидаты. Пачку рвём — остаток упрётся в то же
                            # окно (кэш telethon заведён по типу запроса, а не
                            # по чату, поэтому падает мгновенно и целиком).
                            logger.warning(
                                "Group ping batch stopped by flood at uid=%s "
                                "(wait=%ss)", uid, ping_result.flood_seconds)
                            break
                        if ping_result.user_at_fault:
                            failures = pings.get(str(uid), {}).get("ping_failures", 0) + 1
                            commit_pm_ping(
                                uid,
                                last_group_ping=datetime.now().isoformat(),
                                ping_failures=failures,
                            )
                        continue
                    # Сохраняем в историю переписки ЛС
                    await database.save_pm_message(uid, "Assistant", f"[Проактивный пинг чата]: {teaser}")
```
Плюс в том же цикле, отдельной правкой: пауза `await asyncio.sleep(1)` после каждой
отправки и потолок на размер пачки по образцу `MAX_PINGS_PER_CYCLE = 5`. Сейчас пачка —
20 % от всех активных в ЛС за 30 дней, то есть растёт вместе с аудиторией; 300 активных
дают 60 отправок разным peer подряд без единой паузы. Это и есть штатный повод для
FloodWait, и лечить надо в первую очередь причину.

### Патч 4 — пинги в ЛС. `assistant.py`, `check_and_send_pm_pings`

до:
```python
                        try:
                            await bot_client.send_message(entity=chat_id, message=reply_text, parse_mode='html')
                            await database.save_pm_message(chat_id, "Assistant", reply_text)
```
после:
```python
                        try:
                            ping_result = await tg_safety.guard(
                                lambda: bot_client.send_message(
                                    entity=chat_id, message=reply_text,
                                    parse_mode='html'),
                                op="send_message:pm_ping", chat_id=chat_id,
                                timeout=90)
                            if not ping_result.ok:
                                # Комментарий у прежнего except сам перечислял
                                # FloodWait среди причин и всё равно считал его
                                # виной врача. Счётчик двигаем только там, где
                                # отказ действительно на стороне врача.
                                if ping_result.user_at_fault:
                                    failures = info.get("ping_failures", 0) + 1
                                    commit_pm_ping(chat_id_str, ping_failures=failures)
                                if ping_result.flooded:
                                    break
                                continue
                            await database.save_pm_message(chat_id, "Assistant", reply_text)
```

### Патч 5 — доставка готовой сводки. `assistant.py`, `handle_group_summary`

Сводка уже сгенерирована и оплачена; это единственный путь её доставки.

до:
```python
        await bot_client.edit_message(chat_id, status_msg.id, final_text, parse_mode='html')
        logger.info(f"Successfully posted group summary for chat_id={chat_id}")
```
после:
```python
        # Бюджет 90 с согласован с summarizer.TELEGRAM_SEND_TIMEOUT_SECONDS.
        # Сводка уже сгенерирована и лежит в памяти: без границы зависание этой
        # строки теряло её целиком и оставляло врачу вечное «Собираю и
        # анализирую историю обсуждения…».
        delivery = await tg_safety.edit_message(
            bot_client, chat_id, status_msg.id, final_text,
            op="edit_message:group_summary", timeout=90, parse_mode='html')
        if not delivery.ok:
            logger.error(
                "Group summary generated but NOT delivered chat_id=%s reason=%s",
                chat_id, delivery.reason)
        else:
            logger.info(f"Successfully posted group summary for chat_id={chat_id}")
```

### Патч 6 — убрать `get_me()` с горячего пути снимка. `assistant.py`, `check_and_trigger_assistant_media`

Здесь правильнее не оборачивать, а убрать сетевые вызовы совсем: ответы уже лежат в
памяти процесса (`resolve_bot_identity` заполняет `BOT_ID` и `BOT_USERNAME` на старте, и
соседний код в этом же файле пользуется именно ими).

до:
```python
    is_direct_reply = False
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
после:
```python
    # get_me() ВСЕГДА идёт в сеть: кэш-ветка telethon (client/users.py) требует
    # input_peer=True, а здесь вызов без аргументов. Два таких вызова на каждое
    # медиасообщение — и оба под пустым except: при их отказе is_direct_reply и
    # is_mentioned оставались False, сообщение уходило в пассивную ветку с
    # двухчасовым кулдауном, и прямое обращение врача с клиническим снимком
    # отбрасывалось молча, без строки в журнале. Идентичность бота уже известна
    # процессу: resolve_bot_identity заполняет её на старте.
    if not BOT_ID or not BOT_USERNAME:
        await resolve_bot_identity(bot_client)

    is_direct_reply = False
    if getattr(message, 'reply_to_msg_id', None) and BOT_ID:
        parent_res = await tg_safety.get_messages(
            bot_client, message.chat_id, ids=message.reply_to_msg_id,
            op="get_messages:media_parent", timeout=30)
        if parent_res.ok and parent_res.value and parent_res.value.sender_id == BOT_ID:
            is_direct_reply = True

    is_mentioned = bool(
        text and BOT_USERNAME and BOT_USERNAME.lower() in text.lower()
    )
```
Проверить перед применением: как именно `resolve_bot_identity` объявляет глобалы в текущей
ревизии файла (`BOT_ID` / `BOT_USERNAME`) и нужен ли внутри функции `global`.

### Патч 7 — нарезанная выдача `/search`. `assistant.py`, `send_message_chunks_async`

до:
```python
    for chunk in html_safe.split_html(text, limit=TELEGRAM_MESSAGE_LIMIT):
        await bot_client.send_message(entity=chat_id, message=chunk, **kwargs)
```
после:
```python
    for chunk in html_safe.split_html(text, limit=TELEGRAM_MESSAGE_LIMIT):
        # Без границы отказ на середине оставлял врачу половину выдачи и ни
        # строки в журнале. Прерываемся на первом же отказе: продолжать
        # нарезку в чат, который нас не принимает, смысла нет.
        part = await tg_safety.send_message(
            bot_client, chat_id, chunk, op="send_message:chunk", timeout=90,
            **kwargs)
        if not part.ok:
            return False
    return True
```
Проверить: возвращаемое значение сейчас не используется — убедиться, что добавление
`return` ничего не ломает у единственного вызывающего.

### Патч 8 — статусные сообщения и правки в ЛС (пачкой, механически)

Все `status_msg = await bot_client.send_message(...)`, `bot_client.edit_message(...)` и
`bot_client.delete_messages(...)` в `handle_private_message`, `handle_interactive_case_step`,
`handle_group_quiz`, `handle_term_explainer`, `check_and_trigger_referee` заменить на
`tg_safety.send_message / edit_message / delete_messages` с `timeout=90` и осмысленным
`op=`. Шаблон:

до:
```python
            status_msg = await bot_client.send_message(entity=chat_id, message="🎲 <i>Генерирую клиническую викторину для вас... Подождите.</i>", parse_mode='html')
```
после:
```python
            status_res = await tg_safety.send_message(
                bot_client, chat_id,
                "🎲 <i>Генерирую клиническую викторину для вас... Подождите.</i>",
                op="send_message:quiz_status", timeout=90, parse_mode='html')
            if not status_res.ok:
                return
            status_msg = status_res.value
```
Важно именно здесь: зависание этой строки держит per-user замок `_pm_user_lock`, поэтому
следующее сообщение того же врача тоже встаёт в очередь и не обрабатывается никогда.

### Порядок работ

| Очередь | Что | Почему первым |
|---|---|---|
| 1 | патч 1 (журнал) | без него ничего из следующего не проверяемо по журналу |
| 2 | патч 3 + патч 4 (пинги) | единственный дефект с необратимым последствием для врача |
| 3 | патч 3, вторая часть: пауза и потолок пачки | лечит причину flood, а не симптом |
| 4 | патч 2 (`flood_sleep_threshold=0`) | делает границу действующей; безопасен только после 2-3 |
| 5 | патч 5 (доставка сводки) | оплаченная работа, теряемая целиком |
| 6 | патч 6 (`get_me` со снимка) | молчаливое отбрасывание прямого обращения плюс минус два запроса на каждое медиа |
| 7 | патчи 7-8 | механическая замена остальных ~110 вызовов в `assistant.py` |

Отдельно, вне adoption и вне моих файлов: находка 3 разведки (дубль `get_messages` в
`check_and_apply_silence` / `check_and_trigger_assistant` и `await curr.get_sender()` в
цикле вместо свойства `curr.sender`) снижает число запросов на одно сообщение с ~14 до ~3.
Это дешевле любой обёртки: девять замеренных `ServerError` пришли именно на
`GetUsersRequest`.

---

## 6. Журнал работы

* Прочитан `_recon_telegram.md` целиком.
* Замер по журналам выполнен, счётчики в разделе 1. Механика telethon перепроверена
  независимо от разведки — расхождений нет.
* Инвентаризация выполнена машинным разбором: для каждого вызова определены объемлющая
  функция, ближайший объемлющий `try:` с его `except` и наличие `wait_for` рядом.
* Обнаружено, что боевые файлы менялись посреди работы; замеры и номера строк
  переснятыми на состояние 07:58, патчи привязаны к тексту, а не только к номерам.
* `tg_safety.py` написан.
* `test_tg_safety.py` написан: 131 проверка, 14 разделов.
* Диверсия: 16 поломок, 15 поймано, S9a разобрана как избыточная строка модуля. Первый
  проход поймал 4 пустые проверки в наборе — исправлены.

---

## Приложение. Полный машинный перечень вызовов telethon

Замер по состоянию файлов на 2026-07-29 07:58 (`assistant.py` менялся в 07:40, `main.py` в 07:57).
Столбцы: `file:line` | объемлющая функция | `asyncio.wait_for` в 4 строках выше | ближайший
объемлющий `try:` и его `except` | код. Отсутствие `WAIT_FOR` = вызов не ограничен по
времени. `NO-TRY` = исключение уходит выше объемлющей функции.

```
================ main.py
main.py:208        | get_my_id                          | -        | NO-TRY                                       | me = await client.get_me()
main.py:969        | recover_pending_media_analysis     | WAIT_FOR | try@967 except Exception:                    | client.get_messages(config.SOURCE_CHAT_ID, ids=ids),
main.py:1226       | transcribe_group_voice             | WAIT_FOR | try@1221 except asyncio.TimeoutError:        | message.download_media(file="temp_media/"),
main.py:1276       | _publish_voice_transcription       | -        | try@1272 except Exception as send_err:       | await bot_client.send_message(
main.py:1361       | _notify_voice_failure              | -        | try@1360 except Exception as send_err:       | await bot_client.send_message(
main.py:1577       | process_media_message              | WAIT_FOR | try@1575 except Exception as e:              | message.download_media(file=os.path.join(MEDIA_TEMP_DIR, "")),
main.py:1731       | handle_new_message                 | WAIT_FOR | try@1729 except Exception as exc:            | event.get_sender(),
main.py:1885       | handle_new_message                 | -        | try@1884 except Exception as bookmark_exc:   | parent_msg = await event.client.get_messages(event.chat_id, ids=reply_
main.py:1890       | handle_new_message                 | -        | try@1884 except Exception as bookmark_exc:   | p_sender = await parent_msg.get_sender()
main.py:1913       | handle_new_message                 | -        | try@1884 except Exception as bookmark_exc:   | await bot_client.send_message(
main.py:1978       | run_group_features                 | -        | try@1973 except Exception as delete_exc:     | permissions = await event.client.get_permissions(event.chat_id, event.
main.py:1985       | run_group_features                 | -        | try@1984 except Exception as e1:             | await bot_client.delete_messages(event.chat_id, [reply_to_msg_id])
main.py:1989       | run_group_features                 | -        | try@1988 except Exception as e2:             | await bot_client.delete_messages(event.chat_id, [msg_id])
main.py:2205       | handle_callback_query              | -        | try@2204 except Exception:                   | await event.answer()
main.py:2229       | handle_callback_query              | -        | try@2228 except Exception:                   | await event.answer()
main.py:2235       | dump_handler                       | -        | NO-TRY                                       | await event.edit("📦 <b>Начинаю тестовую выкачку истории...</b>", parse
main.py:2237       | dump_handler                       | -        | NO-TRY                                       | async for message in client.iter_messages(config.SOURCE_CHAT_ID, limit
main.py:2240       | dump_handler                       | -        | NO-TRY                                       | await event.edit(f"✅ Успешно прочитано {count} последних сообщений. До
main.py:2260       | get_chat_id                        | -        | NO-TRY                                       | await event.edit(text, parse_mode='HTML')
main.py:2272       | manual_test_handler                | -        | NO-TRY                                       | await event.edit(f"🧪 <b>Тест кэша (Topic: {target_topic})...</b>", par
main.py:2308       | manual_weekly_test                 | -        | NO-TRY                                       | await event.edit(f"🗞 <b>Готовлю тестовый WEEKLY за 7 дней...</b>\nTarg
main.py:2319       | manual_weekly_test                 | -        | try@2310 except Exception as e:              | await event.edit("❌ Сообщений за неделю не найдено (или база пуста).")
main.py:2322       | manual_weekly_test                 | -        | try@2310 except Exception as e:              | await event.edit(f"🗞 <b>Анализирую {len(messages)} сообщений...</b>\nП
main.py:2333       | manual_weekly_test                 | -        | try@2310 except Exception as e:              | await event.edit("❌ Ошибка генерации (вернулся None). Проверь логи.")
main.py:2337       | manual_weekly_test                 | -        | try@2310 <не найден except>                  | await event.edit(f"❌ Ошибка: {e}")
main.py:2352       | get_sender                         | -        | NO-TRY                                       | return await self.message.get_sender()
main.py:2381       | sync_history                       | -        | NO-TRY                                       | async for message in client.iter_messages(config.SOURCE_CHAT_ID, min_i
main.py:2391       | sync_history                       | WAIT_FOR | try@2389 except Exception as exc:            | message.get_sender(),
main.py:2560       | health_watchdog_task               | WAIT_FOR | try@2550 except Exception as exc:            | client.get_messages(config.SOURCE_CHAT_ID, limit=1),
================ assistant.py
assistant.py:188   | send_message_chunks_async          | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=chunk, **kwargs)
assistant.py:200   | resolve_bot_identity               | -        | try@199 except Exception as e:               | me = await bot_client.get_me()
assistant.py:1251  | check_and_apply_silence            | -        | try@1250 except Exception:                   | parent_msg = await event.client.get_messages(event.chat_id, ids=reply_
assistant.py:1275  | check_and_apply_silence            | -        | try@1274 except Exception as reply_err:      | await event.reply(apology)
assistant.py:1531  | check_and_trigger_assistant        | -        | try@1530 except Exception as e:              | parent_msg = await event.client.get_messages(event.chat_id, ids=reply_
assistant.py:1543  | check_and_trigger_assistant        | -        | try@1530 except Exception as e:              | await event.reply(apology)
assistant.py:1557  | check_and_trigger_assistant        | -        | try@1530 except Exception as e:              | sender = await curr.get_sender()
assistant.py:1574  | check_and_trigger_assistant        | -        | try@1530 except Exception as e:              | curr = await event.client.get_messages(event.chat_id, ids=curr.reply_t
assistant.py:1994  | check_and_trigger_assistant        | -        | try@1993 except Exception as e:              | await bot_client.send_message(
assistant.py:2018  | check_and_trigger_assistant        | -        | try@2017 except Exception as e:              | await bot_client.send_message(
assistant.py:2069  | check_and_trigger_assistant_media  | -        | try@2068 except Exception:                   | parent = await bot_client.get_messages(message.chat_id, ids=message.re
assistant.py:2070  | check_and_trigger_assistant_media  | -        | try@2068 except Exception:                   | if parent and parent.sender_id == (await bot_client.get_me()).id:
assistant.py:2078  | check_and_trigger_assistant_media  | -        | try@2077 except Exception:                   | bot_info = await bot_client.get_me()
assistant.py:2283  | __init__                           | -        | try@2282 except Exception as e:              | await bot_client.send_message(
assistant.py:2295  | __init__                           | -        | try@2294 except Exception as e:              | await bot_client.send_message(
assistant.py:2479  | handle_interactive_case_step       | -        | try@2465 except Exception as save_err:       | await bot_client.send_message(
assistant.py:2512  | handle_interactive_case_step       | -        | NO-TRY                                       | status_msg = await bot_client.send_message(entity=chat_id, message="⚙️
assistant.py:2562  | handle_interactive_case_step       | -        | try@2465 except Exception as save_err:       | try: await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:2566  | handle_interactive_case_step       | -        | try@2465 except Exception as save_err:       | await bot_client.send_message(entity=chat_id, message="❌ <i>Ошибка сим
assistant.py:2576  | handle_interactive_case_step       | -        | try@2465 except Exception as save_err:       | await bot_client.send_message(entity=chat_id, message=final_message, p
assistant.py:2591  | handle_interactive_case_step       | -        | try@2465 except Exception as save_err:       | await bot_client.send_message(entity=chat_id, message=reply_text, pars
assistant.py:2647  | handle_private_message             | -        | try@2646 except Exception as notice_err:     | await bot_client.send_message(
assistant.py:2682  | handle_private_message             | -        | try@2680 except Exception as opt_err:        | await bot_client.send_message(
assistant.py:2710  | handle_private_message             | -        | try@2659 <не найден except>                  | status_msg = await bot_client.send_message(entity=chat_id, message="🎤 
assistant.py:2727  | handle_private_message             | WAIT_FOR | try@2712 except Exception as audio_err:      | event.message.download_media(
assistant.py:2744  | handle_private_message             | -        | try@2712 except Exception as exp_err:        | try: await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:2761  | handle_private_message             | -        | try@2712 except Exception as exp_err:        | await bot_client.send_message(entity=chat_id, message="🎤 <i>(Тишина ил
assistant.py:2763  | handle_private_message             | -        | try@2712 except Exception as exp_err:        | await bot_client.send_message(entity=chat_id, message=f"🎤 <b>Распознан
assistant.py:2765  | handle_private_message             | -        | try@2712 except Exception as exp_err:        | await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось
assistant.py:2783  | handle_private_message             | -        | try@2776 except Exception as exp_err:        | await bot_client.send_message(
assistant.py:2796  | handle_private_message             | -        | try@2659 <не найден except>                  | await bot_client.send_message(entity=chat_id, message="⏹️ <i>Активный 
assistant.py:2801  | handle_private_message             | -        | try@2776 <не найден except>                  | await bot_client.send_message(entity=chat_id, message="⏹️ <i>Интеракти
assistant.py:2803  | handle_private_message             | -        | try@2776 <не найден except>                  | await bot_client.send_message(entity=chat_id, message="ℹ️ <i>У вас нет
assistant.py:2821  | handle_private_message             | -        | try@2776 <не найден except>                  | await bot_client.send_message(
assistant.py:2842  | handle_private_message             | -        | try@2836 except Exception as auth_err:       | permissions = await bot_client.get_permissions(config.SOURCE_CHAT_ID, 
assistant.py:2868  | handle_private_message             | -        | try@2836 <не найден except>                  | await bot_client.send_message(entity=chat_id, message="⛔ <i>Целевой ча
assistant.py:2873  | handle_private_message             | -        | try@2836 <не найден except>                  | await bot_client.send_message(entity=chat_id, message="🤷‍♂️ <i>Не найд
assistant.py:2884  | handle_private_message             | -        | try@2883 except Exception as del_err:        | await bot_client.delete_messages(c_id, msg_ids)
assistant.py:2891  | handle_private_message             | -        | try@2776 <не найден except>                  | await bot_client.send_message(
assistant.py:2897  | handle_private_message             | -        | try@2776 <не найден except>                  | await bot_client.send_message(entity=chat_id, message="⛔ <i>У вас нет 
assistant.py:2935  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=greeting, button
assistant.py:2961  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=style_welcome, b
assistant.py:2983  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=help_text, parse
assistant.py:3004  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=protocols_text, 
assistant.py:3020  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=wiki_text, butto
assistant.py:3053  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=calc_text, parse
assistant.py:3057  | handle_private_message             | -        | NO-TRY                                       | status_msg = await bot_client.send_message(entity=chat_id, message="🎲 
assistant.py:3074  | handle_private_message             | -        | NO-TRY                                       | await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:3076  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось
assistant.py:3080  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"🎲 <b>Клиническ
assistant.py:3096  | handle_private_message             | -        | NO-TRY                                       | status_msg = await bot_client.send_message(
assistant.py:3104  | handle_private_message             | -        | try@3103 except Exception:                   | await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:3113  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=stats_text, pars
assistant.py:3135  | handle_private_message             | -        | try@3103 except Exception as e:              | await bot_client.send_message(entity=chat_id, message=f"🔍 В ваших закл
assistant.py:3137  | handle_private_message             | -        | try@3103 except Exception as e:              | await bot_client.send_message(entity=chat_id, message="📌 <b>У вас пока
assistant.py:3145  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"⚠️ Страница {p
assistant.py:3188  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=text_out, parse_
assistant.py:3194  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message="🔍 <b>Пожалуйста
assistant.py:3227  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"🔍 По запросу «
assistant.py:3231  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=search_out, pars
assistant.py:3235  | handle_private_message             | -        | NO-TRY                                       | status_msg = await bot_client.send_message(entity=chat_id, message="🎮 
assistant.py:3260  | handle_private_message             | -        | NO-TRY                                       | await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:3262  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось
assistant.py:3284  | handle_private_message             | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=case_welcome, pa
assistant.py:3371  | handle_private_message             | -        | try@3309 <не найден except>                  | await bot_client.send_message(
assistant.py:3383  | handle_private_message             | -        | try@3381 except Exception as e:              | status_msg = await bot_client.send_message(entity=chat_id, message="📥 
assistant.py:3390  | handle_private_message             | WAIT_FOR | try@3381 except Exception as e:              | event.message.download_media(file=f"temp_media/{event.message.id}_"),
assistant.py:3407  | handle_private_message             | -        | try@3381 except Exception as e:              | await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:3412  | handle_private_message             | -        | try@3381 finally:                            | await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Не удалось
assistant.py:3447  | handle_private_message             | -        | try@3381 <не найден except>                  | await bot_client.send_message(
assistant.py:3639  | _bg_portrait                       | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message="❌ <i>Ошибка ген
assistant.py:3672  | _bg_portrait                       | -        | NO-TRY                                       | await bot_client.send_message(
assistant.py:3847  | check_bot_mention_trigger          | -        | try@3846 except Exception as send_err:       | await bot_client.send_message(
assistant.py:3871  | handle_group_summary               | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста,
assistant.py:3874  | handle_group_summary               | -        | NO-TRY                                       | status_msg = await bot_client.send_message(entity=chat_id, message="📝 
assistant.py:3898  | handle_group_summary               | -        | try@3876 except Exception as e:              | await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Не удалось
assistant.py:3929  | handle_group_summary               | -        | try@3876 except Exception as e:              | await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Ошибка ген
assistant.py:3940  | handle_group_summary               | -        | try@3876 except Exception as e:              | await bot_client.edit_message(chat_id, status_msg.id, final_text, pars
assistant.py:3944  | handle_group_summary               | -        | try@3876 except Exception as parse_err:      | try: await bot_client.edit_message(chat_id, status_msg.id, "❌ <i>Произ
assistant.py:3955  | handle_group_direct_ask            | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста,
assistant.py:3998  | handle_group_direct_ask            | -        | NO-TRY                                       | await bot_client.send_message(
assistant.py:4019  | handle_group_direct_ask            | -        | NO-TRY                                       | await bot_client.send_message(
assistant.py:4037  | handle_group_direct_ask            | -        | try@4036 except Exception as e:              | await bot_client.send_message(
assistant.py:4090  | handle_group_quiz                  | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста,
assistant.py:4093  | handle_group_quiz                  | -        | NO-TRY                                       | status_msg = await bot_client.send_message(entity=chat_id, message="🎲 
assistant.py:4108  | handle_group_quiz                  | -        | NO-TRY                                       | await bot_client.delete_messages(chat_id, status_msg.id)
assistant.py:4111  | handle_group_quiz                  | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message="❌ <i>Не удалось
assistant.py:4194  | handle_group_quiz                  | -        | NO-TRY                                       | await bot_client.send_message(
assistant.py:4562  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer("Неизвестный стиль", alert=True)
assistant.py:4574  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, confirm
assistant.py:4575  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4594  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, protoco
assistant.py:4595  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4633  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, respons
assistant.py:4634  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4649  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, wiki_te
assistant.py:4650  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4660  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, wiki_te
assistant.py:4661  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4677  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, search_
assistant.py:4678  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4693  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, respons
assistant.py:4694  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4707  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, wiki_te
assistant.py:4708  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4729  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, respons
assistant.py:4730  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4761  | handle_quiz_callback               | -        | NO-TRY                                       | await bot_client.edit_message(event.chat_id, event.message_id, respons
assistant.py:4762  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer()
assistant.py:4798  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer("⭐ Статья успешно добавлена в ваши закладки!", aler
assistant.py:4800  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer("❌ Не удалось сохранить статью. Попробуйте еще раз.
assistant.py:4814  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer("⚠️ Ошибка: Викторина не найдена.", alert=True)
assistant.py:4831  | handle_quiz_callback               | -        | try@4820 except Exception as edit_err:       | await event.answer("⚠️ Вы уже проголосовали в этой викторине!", alert=
assistant.py:4850  | handle_quiz_callback               | -        | NO-TRY                                       | await event.answer(alert_text, alert=True)
assistant.py:4885  | handle_quiz_callback               | -        | try@4853 except Exception as edit_err:       | await event.edit(text=new_text, parse_mode='html')
assistant.py:5160  | check_and_trigger_referee          | -        | try@5159 except Exception as e:              | await bot_client.send_message(
assistant.py:5178  | handle_term_explainer              | -        | NO-TRY                                       | await bot_client.send_message(entity=chat_id, message=f"⚠️ Пожалуйста,
assistant.py:5185  | handle_term_explainer              | -        | NO-TRY                                       | await bot_client.send_message(
assistant.py:5218  | handle_term_explainer              | -        | NO-TRY                                       | await bot_client.send_message(
assistant.py:5231  | handle_term_explainer              | -        | try@5230 except Exception as e:              | await bot_client.send_message(
assistant.py:5366  | check_and_send_pm_pings            | -        | try@5365 except ValueError as ve:            | await bot_client.send_message(entity=chat_id, message=reply_text, pars
assistant.py:5552  | check_and_send_group_activity_ping | -        | try@5551 except ValueError as ve:            | await bot_client.send_message(entity=uid, message=msg, parse_mode='htm

ИТОГО вызовов telethon: 151
```
