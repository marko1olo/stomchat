# -*- coding: utf-8 -*-
import sys
import codecs
sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())

import config
import time
from groq import Groq

models = [
    'llama-3.3-70b-versatile',
    'meta-llama/llama-4-scout-17b-16e-instruct',
    'openai/gpt-oss-120b'
]

tasks = [
    {
        'name': '1. Simple Logic (Math & Trick)',
        'prompt': 'У меня было 3 яблока. Я съел одно, а потом купил еще половину от того, что осталось. Сколько яблок у меня теперь? Ответь коротко и объясни логику.'
    },
    {
        'name': '2. Creative / Medical',
        'prompt': 'Придумай смешной, но клинически достоверный стишок про неудачное удаление зуба мудрости (8-ки) с перфорацией пазухи. На русском языке, 4 строки.'
    },
    {
        'name': '3. Large Instruction (Formatting)',
        'prompt': 'Ты строгий редактор. Перепиши этот текст так, чтобы он звучал максимально официально, сухо и бюрократично, используя сложные деепричастные обороты: "Вчера зуб заболел, я пошел к врачу, он сказал надо удалять нерв и ставить коронку."'
    }
]

client = Groq(api_key=config.GROQ_KEYS[0])

print('# Model Benchmark Results\n')

for model in models:
    print(f'## Model: {model}\n')
    for task in tasks:
        print(f'**Task: {task["name"]}**')
        try:
            start = time.time()
            completion = client.chat.completions.create(
                model=model,
                messages=[{'role': 'user', 'content': task['prompt']}],
                temperature=0.3,
                max_tokens=500,
            )
            elapsed = time.time() - start
            text = completion.choices[0].message.content.strip().replace('\n', ' ')
            print(f'- Time: {elapsed:.2f}s')
            print(f'- Output: {text[:200]}...\n')
        except Exception as e:
            print(f'- FAILED: {e}\n')
    time.sleep(2)
