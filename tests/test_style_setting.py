"""
Настройка /style: доходит ли выбор врача до промпта.

Что было не так:
  * в общем чате учитывался ровно один стиль. Ветка была написана как
    `if selected_style == "clinical_dry"`, и врач, выбравший «Ироничный
    циник 💀», не получал ничего: настройка молча не работала. Причём код
    этой ветки лежал в файле ДВАЖДЫ, в двух местах сборки промпта;
  * в ЛС тот же выбор учитывался через STYLE_PROMPTS. То есть при одной
    настройке бот вёл себя в группе и в ЛС по-разному;
  * обработчик кнопки клал в базу любую строку из callback-данных. Их шлёт
    клиент, а не наше сообщение, поэтому туда можно было записать что угодно
    и оно осталось бы стилем пользователя навсегда;
  * подтверждение обещало «все последующие ответы в ЛС» — после правки стиль
    работает и в группе.

Работает на копии базы во временном каталоге; боевая не открывается.

Запуск: python test_style_setting.py
"""
import asyncio
import io
import os
import sys
import tempfile

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import config
import runtime_guard

_TMPDIR = tempfile.mkdtemp(prefix="stomchat_style_")
config.DB_PATH = os.path.join(_TMPDIR, "test_style.db")
runtime_guard.SUMMARY_STATUS_PATH = os.path.join(_TMPDIR, "bot_summary_status.json")

import database  # noqa: E402
import assistant  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


SOURCE = io.open("assistant.py", encoding="utf-8").read()
CODE = "\n".join(l for l in SOURCE.split("\n") if not l.lstrip().startswith("#"))

KNOWN = ("colleague_friendly", "clinical_dry", "humor_cynic")


async def run():
    await database.init_db()

    print("\n[1] Каждый стиль из меню даёт свою вставку в промпт группы")
    blocks = {s: assistant.style_instruction_block(s) for s in KNOWN}
    check("«сухие факты» дают вставку", blocks["clinical_dry"].strip() != "")
    check("«ироничный циник» дают вставку", blocks["humor_cynic"].strip() != "",
          "настройка не доходит до промпта")
    check("стиль по умолчанию вставки не добавляет", blocks["colleague_friendly"] == "",
          "основной тон и так задан правилами промпта")
    check("вставки разных стилей различаются",
          blocks["clinical_dry"] != blocks["humor_cynic"])
    check("у циника в тексте есть ирония",
          "иронич" in blocks["humor_cynic"].lower() or "цинизм" in blocks["humor_cynic"].lower(),
          blocks["humor_cynic"][:70])
    check("сухой стиль запрещает смайлы",
          "смайл" in blocks["clinical_dry"].lower())
    check("неизвестный стиль вставки не даёт", assistant.style_instruction_block("нет_такого") == "")
    check("пустое значение не роняет", assistant.style_instruction_block(None) == "")

    print("\n[2] Ветка на один стиль в файле не осталась")
    # Сравнение с clinical_dry законно ровно в одном месте — внутри общего
    # помощника, где у этого стиля свой, более жёсткий текст. Если оно снова
    # появится в сборке промпта, остальные стили опять будут игнорироваться.
    helper = CODE.split("def style_instruction_block", 1)[1].split("\nAD_HINTS", 1)[0]
    outside = CODE.replace(helper, "")
    check("в сборке промпта сравнения со стилем нет",
          'selected_style == "clinical_dry"' not in outside,
          "остатки прежней ветки: остальные стили снова будут игнорироваться")
    check("внутри помощника оно одно",
          helper.count('selected_style == "clinical_dry"') == 1,
          f"got {helper.count('selected_style == chr(34)clinical_dry')}")
    check("сборка промпта зовёт общий помощник",
          CODE.count("style_instruction_block(selected_style)") >= 2,
          f"мест: {CODE.count('style_instruction_block(selected_style)')}, а групповых путей два")

    print("\n[3] Выбор сохраняется и читается обратно")
    uid = 555000111
    for style in KNOWN:
        await database.set_user_style(uid, style)
        profile = await database.get_user_profile(uid)
        check(f"«{style}» сохранён и прочитан", profile.get("selected_style") == style,
              f"got {profile.get('selected_style')}")
    profile = await database.get_user_profile(999000222)
    check("у нового врача стиль по умолчанию",
          profile.get("selected_style") == assistant.DEFAULT_STYLE, f"got {profile}")

    print("\n[4] Смена стиля не затирает портрет")
    await database.set_user_style(uid, "clinical_dry")
    await database.set_user_portrait(uid, "Ортопед, работает с цирконием.", 4242)
    await database.set_user_style(uid, "humor_cynic")
    profile = await database.get_user_profile(uid)
    check("портрет пережил смену стиля",
          profile.get("profile_portrait") == "Ортопед, работает с цирконием.",
          f"got {profile.get('profile_portrait')!r}")
    check("стиль всё же сменился", profile.get("selected_style") == "humor_cynic")
    check("отметка разбора сохранена", profile.get("last_analyzed_msg_id") == 4242,
          f"got {profile.get('last_analyzed_msg_id')}")

    print("\n[5] Кнопка проверяет значение перед записью в базу")
    handler = CODE.split('if data_str.startswith("style:")', 1)[1].split("if data_str ==", 1)[0]
    check("есть сверка со списком известных стилей", "style not in STYLE_PROMPTS" in handler,
          "в базу ляжет любая строка из callback-данных")
    check("неизвестное значение не сохраняется",
          handler.find("style not in STYLE_PROMPTS") < handler.find("set_user_style"),
          "проверка стоит после записи")
    check("врачу показывают отказ", "event.answer(" in handler)

    print("\n[6] Все стили из меню имеют промпт")
    buttons = [l for l in SOURCE.split("\n") if "KeyboardButtonCallback" in l and b"style:".decode() in l]
    check("кнопок ровно три", len(buttons) == 3, f"got {len(buttons)}")
    for line in buttons:
        code = line.split(b'style:'.decode(), 1)[1].split('"')[0].strip()
        check(f"у кнопки «{code}» есть промпт", code in assistant.STYLE_PROMPTS,
              "кнопка есть, а стиля нет")
    for style in assistant.STYLE_PROMPTS:
        check(f"стиль «{style}» доступен из меню",
              any(style in l for l in buttons), "промпт есть, а кнопки нет")

    print("\n[7] Подтверждение не обещает лишнего")
    confirm = CODE.split("Стиль общения успешно изменен", 1)[1][:400]
    check("не сказано, что стиль только для ЛС",
          "только в ЛС" not in confirm and "ответы в ЛС будут" not in confirm,
          "текст обещает применение лишь в личных сообщениях")
    check("сказано про общий чат", "общем чате" in confirm, confirm[:90])


asyncio.run(run())

import shutil  # noqa: E402
shutil.rmtree(_TMPDIR, ignore_errors=True)

print(f"\n{'='*62}\nPASSED: {len(PASS)}   FAILED: {len(FAIL)}")
if FAIL:
    print("Провалено: " + ", ".join(FAIL))
sys.exit(1 if FAIL else 0)
