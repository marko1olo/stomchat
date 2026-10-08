"""
Ручная публикация ежедневного опроса в оба чата (прод + тест).
Запускать: python post_poll_now.py
"""
import asyncio
import json
import logging
import sys

sys.path.insert(0, r'c:\Users\danat\Desktop\stomchat')

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('post_poll_now')

import config
import poll_engine
import poll_storage
from telethon import TelegramClient

TARGETS = [
    {'chat_id': -1001820467444, 'topic_id': None},   # прод
    {'chat_id': -1003735006121, 'topic_id': 26},      # тест
]


async def main():
    bot_token = config.BOT_TOKEN
    api_id    = config.API_ID
    api_hash  = config.API_HASH

    client = TelegramClient('bot_post_poll_now', api_id, api_hash)
    await client.start(bot_token=bot_token)
    logger.info("Bot client started")

    # generate_poll() сам подтягивает blocking_tools.generate_gemini_text_async
    logger.info("Generating poll (max 3 LLM attempts)...")
    payload, _ = await poll_engine.generate_poll(
        chat_context=None,
        force_type=None,
        is_anonymous=True,
    )
    logger.info("Poll generated: source=%s type=%s", payload.source, payload.poll_type.value)
    logger.info("Topic: %s", payload.topic)
    logger.info("Q: %s", payload.question)
    for i, o in enumerate(payload.options):
        logger.info("  [%d] %s", i, o)

    for target in TARGETS:
        tgt_chat  = target['chat_id']
        tgt_topic = target.get('topic_id')

        if await poll_storage.has_poll_today(tgt_chat):
            logger.warning("Poll already exists today in chat %s — skipping", tgt_chat)
            continue

        # Пересобираем медиа-объект (InputMediaPoll нельзя переиспользовать между чатами)
        media_to_send = poll_engine.build_poll_media(
            question=payload.question,
            options=payload.options,
            poll_type=payload.poll_type.value,
            correct_idx=payload.correct_option_id if payload.correct_option_id is not None else 0,
            explanation=payload.explanation_brief,
            is_anonymous=True,
        )

        case_msg_id = None
        if payload.case_intro:
            intro_msg = await client.send_message(
                entity=tgt_chat,
                message=payload.case_intro,
                reply_to=tgt_topic,
                parse_mode='html',
            )
            if intro_msg and hasattr(intro_msg, 'id'):
                case_msg_id = intro_msg.id
            logger.info("  Sent case_intro to %s (msg_id=%s)", tgt_chat, case_msg_id)

        poll_reply_to = case_msg_id if case_msg_id else tgt_topic
        poll_msg = await client.send_message(
            entity=tgt_chat,
            file=media_to_send,
            reply_to=poll_reply_to,
        )

        if poll_msg and hasattr(poll_msg, 'media') and hasattr(poll_msg.media, 'poll'):
            poll_id = poll_msg.media.poll.id
            await poll_storage.save_poll(
                id=poll_id,
                chat_id=tgt_chat,
                case_msg_id=case_msg_id,
                poll_msg_id=poll_msg.id,
                poll_type=payload.poll_type.value,
                topic=payload.topic,
                question=payload.question,
                options_json=json.dumps(payload.options, ensure_ascii=False),
                correct_option_id=payload.correct_option_id,
                explanation_brief=payload.explanation_brief,
                explanation_deep=payload.explanation_deep,
                case_intro=payload.case_intro,
                discussion_seed_question=payload.discussion_seed_question,
            )
            logger.info("✅ Poll #%s sent to chat=%s (msg_id=%s)", poll_id, tgt_chat, poll_msg.id)
        else:
            logger.error("No poll media in response for chat %s", tgt_chat)

    await client.disconnect()
    logger.info("Done.")


if __name__ == '__main__':
    asyncio.run(main())
