# -*- coding: utf-8 -*-
raise SystemExit(
    "patch_assistant_v2.py - odnorazovyy migrator, USTAREL i LOMAET bota.\n"
    "To zhe samoe, chto i patch_assistant.py: perepisyvaet assistant.py pryamo\n"
    "na urovne modulya, sledovatelno srabatyvaet ot prostogo importa. Obraztsy\n"
    "dlya replace v tekushchem kode ne sovpadayut, i zamena popadaet ne tuda.\n"
    "Te pravki uzhe v istorii git. Udalenie fayla - reshenie vladeltsa repozitoriya."
)

import re

with open('assistant.py', 'r', encoding='utf-8') as f:
    code = f.read()

# PATCH 1: Media assistant deep reply chain and IGNORE prompt
media_patch_target = '''        if getattr(message, 'reply_to_msg_id', None):
            try:
                parent = await bot_client.get_messages(message.chat_id, ids=message.reply_to_msg_id)
                if parent and parent.sender_id == (await bot_client.get_me()).id:
                    is_direct_reply = True
            except Exception:
                pass'''

media_patch_replacement = '''        context_msgs = []
        if getattr(message, 'reply_to_msg_id', None):
            try:
                parent = await bot_client.get_messages(message.chat_id, ids=message.reply_to_msg_id)
                if parent and parent.sender_id == (await bot_client.get_me()).id:
                    is_direct_reply = True
            except Exception:
                pass
            
            # ВЫТЯГИВАЕМ ВСЮ ЦЕПОЧКУ (ГЛУБОКИЙ КОНТЕКСТ ДЛЯ МЕДИА)
            try:
                chain = await database.get_reply_chain_texts(message.reply_to_msg_id, max_depth=7)
                if chain:
                    context_msgs = chain[::-1] # Разворачиваем в хронологическом порядке
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Failed to fetch media reply chain: {e}")
                
        context_str = "\\n".join(context_msgs) if context_msgs else "Нет истории переписки."
        
        ignore_instruction = "ЕСЛИ изображение явно не стоматологическое (мем, еда, бытовая сцена) — верни одно слово: IGNORE"
        ignore_instruction_else = "ЕСЛИ юмор здесь неуместен (например, обсуждается серьёзная тема) — верни одно слово: IGNORE"
        
        if is_direct_reply:
            ignore_instruction = "ЕСЛИ пользователь просто благодарит тебя или закрывает тему — НЕ МОЛЧИ (не пиши IGNORE), а вежливо и грамотно заверши диалог (например, 'Всегда пожалуйста!', 'Обращайтесь!'). Отвечать IGNORE при прямом обращении запрещено."
            ignore_instruction_else = ignore_instruction'''

code = code.replace(media_patch_target, media_patch_replacement)

# Inject context_str into media prompt
code = code.replace(
    'Подпись пользователя к изображению (если есть):\n{caption_text}',
    'История диалога (цепочка ответов):\n{context_str}\n\nПодпись пользователя к изображению (если есть):\n{caption_text}'
)
code = code.replace(
    'Подпись (если есть):\n{caption_text}',
    'История диалога (цепочка ответов):\n{context_str}\n\nПодпись (если есть):\n{caption_text}'
)

# Replace hardcoded IGNORE instruction in media prompt
code = code.replace(
    'ЕСЛИ изображение явно не стоматологическое (мем, еда, бытовая сцена) — верни одно слово: IGNORE',
    '{ignore_instruction}'
)
code = code.replace(
    'ЕСЛИ юмор здесь неуместен (например, обсуждается серьёзная тема) — верни одно слово: IGNORE',
    '{ignore_instruction_else}'
)

# PATCH 2: Text assistant deep reply chain
text_patch_target = '''                # If the triggering message is a reply, prepend the parent chain for full context
                if reply_to_msg_id:
                    try:
                        thread_rows = await query_db_async(
                            "SELECT sender_name, text, msg_id, reply_to_msg_id FROM messages WHERE msg_id = ? OR reply_to_msg_id = ? ORDER BY date ASC",
                            (reply_to_msg_id, reply_to_msg_id)
                        )
                        thread_msgs = []
                        for tr in thread_rows:
                            sender = tr[0]
                            text_val = tr[1] or ""
                            thread_msgs.append(f"{sender}: {text_val}")
                            
                        if thread_msgs:
                            # Merge: thread first, then recent context (deduplicated)
                            seen = set(thread_msgs)
                            extra = [m for m in context_msgs if m not in seen]
                            context_msgs = thread_msgs + extra
                    except Exception as thread_err:
                        logger.warning(f"Failed to fetch reply thread for passive context: {thread_err}")'''

text_patch_replacement = '''                # If the triggering message is a reply, prepend the ENTIRE parent chain for full context (max depth 7)
                if reply_to_msg_id:
                    try:
                        chain = await database.get_reply_chain_texts(reply_to_msg_id, max_depth=7)
                        if chain:
                            thread_msgs = chain[::-1] # Chronological order
                            seen = set(thread_msgs)
                            extra = [m for m in context_msgs if m not in seen]
                            context_msgs = thread_msgs + extra
                    except Exception as thread_err:
                        logger.warning(f"Failed to fetch deep reply thread for passive context: {thread_err}")'''

code = code.replace(text_patch_target, text_patch_replacement)

# PATCH 3: Text assistant IGNORE prompt dynamic behavior
ignore_var_injection = '''    # BUILD PROMPT
    ignore_instruction = "ЕСЛИ тема чата — чистый флуд, приветствия, погода, политика, оффтоп без связи со стоматологией или медициной — верни ровно одно слово: IGNORE"
    if is_dialogue:
        ignore_instruction = "ЕСЛИ пользователь просто благодарит тебя, соглашается или тема исчерпана — НЕ МОЛЧИ (не пиши IGNORE), а вежливо и грамотно заверши диалог (например, 'Всегда пожалуйста!', 'Обращайтесь!'). Отвечать IGNORE при прямом обращении запрещено."
    
    prompt = f"""'''

code = code.replace('    # BUILD PROMPT\n    if is_dialogue:\n        prompt = f"""', ignore_var_injection.replace('prompt = f"""', 'if is_dialogue:\n        prompt = f"""'))
code = code.replace('    # BUILD PROMPT\n    prompt = f"""', ignore_var_injection)

code = code.replace(
    'ЕСЛИ тема чата — чистый флуд, приветствия, погода, политика, оффтоп без связи со стоматологией или медициной — верни ровно одно слово: IGNORE',
    '{ignore_instruction}'
)

with open('assistant.py', 'w', encoding='utf-8') as f:
    f.write(code)

print("Patch complete!")
