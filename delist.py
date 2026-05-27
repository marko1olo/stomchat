import config
from google import genai

client = genai.Client(api_key=config.GOOGLE_KEYS[0])
print("--- ДОСТУПНЫЕ МОДЕЛИ И ИХ ID ---")
for m in client.models.list():
    if 'generateContent' in m.supported_actions:
        print(f"ID: {m.name}")