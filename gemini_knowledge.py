from google import genai
from google.genai import types
import config
import json
import random
import logging
import re
import time

logger = logging.getLogger(__name__)

# Схема ответа для JSON-режима. Смысл не в аккуратности, а в том, что при ней
# модель ФИЗИЧЕСКИ не может отдать `s: ["MSG_2421"]` — ярлык не влезает в
# INTEGER. Замер по боевой вики: ярлыки оставили 353 факта (2 073 битых токена)
# без единой открываемой ссылки на исходное сообщение. Врач читает статью и не
# может проверить, из какого обсуждения она собрана.
# Строковый `c` по той же причине: список в поле кода превращался в склейку
# '2.3.2, 2.3.1, 2.3.3', и такой код не находит ни один экспорт savdel.py —
# статья есть, а в файл к врачу не попадает. Таких записей 12 667 из 12 784.
_FACTS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "facts": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "c": {"type": "STRING"},
                    "f": {"type": "STRING"},
                    "s": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "case": {"type": "BOOLEAN"},
                },
                "required": ["c", "f", "s"],
            },
        }
    },
    "required": ["facts"],
}

# Модели с поддержкой response_mime_type идут ПЕРВЫМИ: до этой правки в списке
# была одна Gemma, ветка `if not is_gemma` была мертва, и JSON-режим не
# включался ни разу за весь прогон архива.
# Gemma осталась последней и без JSON-режима: она единственная проверенно
# рабочая, и если id выше окажутся недоступны, поведение вырождается в прежнее
# плюс строка в журнале — но не в тишину.
_JSON_MODELS_DEFAULT = ["gemini-2.0-flash", "gemini-2.5-flash"]
_LAST_RESORT_MODEL = "models/gemma-3-27b-it"

# Дедлайн на ВЕСЬ вызов. Родительского дедлайна нет: distiller.process_batch
# зовёт эту функцию через run_in_executor без таймаута. До правки худший случай
# был 10 ключей x 10 с сна на 429 = ~100 с при одной модели; цепочка из трёх
# моделей без ограничения дала бы ~300 с, и сито стояло бы втрое дольше на
# каждой пачке архива.
_CALL_DEADLINE_SECONDS = 120

# Резерв времени под ПОСЛЕДНЮЮ модель каскада. Замер без него: новые JSON-модели
# отдают 429 на всех 10 ключах, сон 10 с съедает все 120 с, и до Gemma —
# единственной проверенно рабочей модели — очередь не доходит ни разу. При
# латентности запроса 2 с пачка уходит в отказ, хотя Gemma ответила бы. То есть
# врач не получает статью не потому, что рабочей модели нет, а потому что её
# место в очереди выел сон на чужой исчерпанной квоте.
_LAST_RESORT_RESERVE_SECONDS = 25


def _models_to_try():
    """Цепочка моделей: сначала с JSON-режимом, Gemma последней."""
    configured = getattr(config, "GEMINI_KNOWLEDGE_MODELS", None)
    if isinstance(configured, str):
        configured = [configured]
    ordered, seen = [], set()
    for model_id in list(configured or _JSON_MODELS_DEFAULT) + [_LAST_RESORT_MODEL]:
        model_id = str(model_id or "").strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            ordered.append(model_id)
    return ordered


def _reasons_summary(rejections):
    """
    Причины отказа без повторов, с кратностью.

    Нужно потому, что 10 ключей x 3 модели дают до 30 одинаковых «лимит 429», и
    при простой обрезке списка они вытесняют из строки журнала единственное
    упоминание снятой с обслуживания модели — то есть настоящую причину, по
    которой вики перестала расти.
    """
    counts = {}
    for reason in rejections:
        counts[reason] = counts.get(reason, 0) + 1
    parts = [f"{reason} x{n}" if n > 1 else reason for reason, n in counts.items()]
    return "; ".join(parts[:10]) or "нет данных"


def _finish_reason(response):
    """Причина остановки генерации, терпимо к форме (enum/строка/нет поля)."""
    try:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""
        reason = getattr(candidates[0], "finish_reason", None)
        if reason is None:
            return ""
        return str(getattr(reason, "name", reason) or "")
    except Exception:
        return ""


def validate_fact_payload(text):
    """
    Судит ответ модели ДО того, как он уйдёт в базу. Возвращает (вердикт, причина).

    "ok"       — разобрано, поля пригодны;
    "unparsed" — как JSON не собралось. Такой ответ НЕ отвергается насовсем:
                 distiller.parse_facts умеет вынуть целые объекты фактов из
                 обрезанного ответа, и рубить это — значит терять пачку фактов,
                 из которой первые восемь были целыми;
    "contract" — собралось, но поля непригодны: провенанс ярлыками или код
                 категории списком. Именно такой ответ разбирался штатно и молча
                 уходил в вики — 353 факта без провенанса появились так.
    """
    raw = str(text or "")
    stripped = re.sub(r"```(?:json)?\s*", "", raw).strip()
    try:
        data = json.loads(stripped)
    except Exception as e:
        return "unparsed", f"как JSON не собралось ({str(e)[:60]})"

    facts = None
    if isinstance(data, list):
        facts = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        found = data.get("facts")
        if found is None:
            # Модель могла назвать ключ иначе — ищем первый список словарей.
            for value in data.values():
                if isinstance(value, list) and any(isinstance(x, dict) for x in value):
                    found = value
                    break
        if isinstance(found, list):
            facts = [x for x in found if isinstance(x, dict)]
    if facts is None:
        return "unparsed", "в ответе нет списка фактов"

    bad = []
    for pos, fact in enumerate(facts, 1):
        if not str(fact.get("f") or fact.get("content") or "").strip():
            bad.append(f"факт #{pos}: пустой текст статьи")

        code = fact.get("c")
        if isinstance(code, (list, tuple, set)):
            bad.append(f"факт #{pos}: код категории пришёл списком {list(code)[:4]}")
        elif "," in str(code or ""):
            bad.append(f"факт #{pos}: в коде категории склейка {str(code)[:40]!r}")

        source = fact.get("s")
        items = source if isinstance(source, (list, tuple, set)) else [source]
        for item in items:
            # isdigit() пропускает '²' и int() на нём падает, поэтому проверка
            # строго по ASCII-цифрам.
            if isinstance(item, bool) or item is None:
                bad.append(f"факт #{pos}: ссылка провенанса не число: {item!r}")
            elif isinstance(item, int):
                continue
            elif isinstance(item, str) and re.fullmatch(r"[0-9]+", item.strip()):
                continue
            else:
                bad.append(f"факт #{pos}: ссылка провенанса не число: {str(item)[:24]!r}")

    if bad:
        return "contract", "; ".join(bad[:6]) + (f" (и ещё {len(bad) - 6})" if len(bad) > 6 else "")
    return "ok", ""


def generate_fact_json(prompt):
    """
    Генерирует JSON фактов. Каскад: модели с JSON-режимом, Gemma последней.

    Непригодный ответ больше не возвращается молча. Ответ с провенансом-ярлыками
    или склеенным кодом отвергается совсем: цена отказа — повтор пачки и, после
    MAX_BATCH_ATTEMPTS, строка ПОТЕРЯ с точным диапазоном msg_id в журнале; цена
    молчаливой записи — статья в вики, у которой врач не может открыть ни одного
    исходного сообщения. Вторая цена выше.
    """
    if not config.GOOGLE_KEYS:
        logger.error("No Google API keys found.")
        return None

    keys = list(config.GOOGLE_KEYS)
    random.shuffle(keys)

    deadline = time.monotonic() + _CALL_DEADLINE_SECONDS
    rejections = []
    salvageable = None  # лучший непригодный-но-спасаемый ответ

    models = _models_to_try()
    for position, model_id in enumerate(models):
        # Ранние модели не имеют права съесть весь бюджет: последней в каскаде
        # всегда остаётся окно, иначе она не получает ни одной попытки.
        # Вложенность соблюдена: мягкий дедлайн строго меньше общего.
        is_last = position == len(models) - 1
        model_deadline = deadline if is_last else deadline - _LAST_RESORT_RESERVE_SECONDS
        for api_key in keys:
            if time.monotonic() > model_deadline:
                logger.error(
                    "Дедлайн %d с на синтез исчерпан на модели %s — дальше не пробую, "
                    "иначе сито стоит на одной пачке вместо того, чтобы двигать архив",
                    _CALL_DEADLINE_SECONDS, model_id,
                )
                rejections.append(f"{model_id}: дедлайн вызова исчерпан")
                break
            try:
                client = genai.Client(api_key=api_key)

                # ДИНАМИЧЕСКИЙ КОНФИГ
                # Gemma не поддерживает response_mime_type, Gemini - поддерживает.
                is_gemma = "gemma" in model_id

                config_params = {
                    "temperature": 0.0,
                    "max_output_tokens": 8192,
                    "safety_settings": [
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE")
                    ]
                }

                if not is_gemma:
                    config_params["response_mime_type"] = "application/json"
                    config_params["response_schema"] = _FACTS_SCHEMA

                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                    config=types.GenerateContentConfig(**config_params)
                )

                text = getattr(response, "text", None) if response else None
                reason = _finish_reason(response)

                if not text:
                    # Раньше пустой кандидат просто уходил на следующий ключ, и
                    # отказ по фильтрам выглядел как «модель промолчала».
                    logger.error(
                        "Модель %s вернула пустой ответ (finish_reason=%s): пачка реплик "
                        "не даст ни одного факта, и без этой строки в журнале причина неизвестна",
                        model_id, reason or "неизвестен",
                    )
                    rejections.append(f"{model_id}: пустой ответ (finish_reason={reason or 'неизвестен'})")
                    continue

                verdict, why = validate_fact_payload(text)

                if verdict == "ok":
                    if reason and reason.upper() not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
                        logger.error(
                            "Модель %s остановилась по %s: ответ разобрался, но он неполный — "
                            "часть фактов этой пачки в вики не попадёт",
                            model_id, reason,
                        )
                    return text

                if verdict == "unparsed":
                    logger.warning(
                        "Модель %s: %s (finish_reason=%s). Пробую следующего кандидата; если лучше "
                        "никто не ответит, отдам этот ответ на спасение целых фактов",
                        model_id, why, reason or "неизвестен",
                    )
                    if salvageable is None:
                        salvageable = text
                    rejections.append(f"{model_id}: {why}")
                    continue

                # verdict == "contract"
                logger.error(
                    "ОТКАЗ от ответа %s: %s. Такой ответ разбирается штатно и именно поэтому "
                    "молча портил вики — записываю пачку как отказ, а не как факты без провенанса",
                    model_id, why,
                )
                rejections.append(f"{model_id}: контракт нарушен — {why}")
                continue

            except Exception as e:
                err = str(e).lower()

                if "429" in err or "resource_exhausted" in err:
                    # ВАЖНО: Делаем паузу, чтобы не долбить API
                    if time.monotonic() + 10 > model_deadline:
                        logger.error(
                            "Лимит на %s и до дедлайна меньше 10 с — сон отменён, пачка уйдёт в отказ",
                            model_id,
                        )
                        rejections.append(f"{model_id}: лимит 429, дедлайн не даёт ждать")
                        break
                    logger.warning(f"Rate limit for {model_id}. Sleeping 10s...")
                    # Причину надо записать ИМЕННО здесь: без неё итоговая строка
                    # отказа говорила «Причины: нет данных» на самом частом отказе
                    # (33 строки лимита в distiller.log), и оператор не мог
                    # отличить исчерпанную квоту от сломанной модели.
                    rejections.append(f"{model_id}: лимит 429")
                    time.sleep(10)
                    continue

                if "400" in err and "json mode" in err:
                    # Это страховка, если логика is_gemma не сработала
                    logger.error(
                        "Модель %s отвергла JSON-режим: перехожу к следующей, иначе те же "
                        "10 ключей сгорят на одной и той же ошибке", model_id,
                    )
                    rejections.append(f"{model_id}: JSON-режим не поддержан")
                    break

                if "404" in err:
                    # Раньше 404 не логировался ВООБЩЕ. При одной модели в списке
                    # это значит: синтез знаний умер целиком, вики перестала
                    # расти, и в журнале об этом ни строки.
                    logger.error(
                        "Модель %s недоступна (404) — снята с обслуживания или id опечатан; "
                        "перехожу к следующей", model_id,
                    )
                    rejections.append(f"{model_id}: 404, модель недоступна")
                    break

                logger.error(f"Error {model_id}: {err[:100]}")
                rejections.append(f"{model_id}: {err[:60]}")
                continue

    if salvageable is not None:
        logger.error(
            "Ни один кандидат не дал пригодный JSON. Отдаю неразобранный ответ на спасение "
            "целых фактов. Причины: %s", "; ".join(rejections[:8]) or "нет данных",
        )
        return salvageable

    logger.error(
        "СИНТЕЗ НЕ СОСТОЯЛСЯ: ни одна модель не дала пригодный ответ, из этой пачки реплик "
        "в вики не попадёт ничего. Причины: %s", "; ".join(rejections[:8]) or "нет данных",
    )
    return None
