if __name__ != "__main__":
    # Perechen modeley zaprashivaetsya na urovne modulya, to est prostoy import
    # bet po realnomu API Google. Pryamoy zapusk `python delist.py` rabotaet.
    raise ImportError("delist.py - instrument, ne modul: zapuskat napryamuyu")

import config
from google import genai

client = genai.Client(api_key=config.GOOGLE_KEYS[0])
print("--- ДОСТУПНЫЕ МОДЕЛИ И ИХ ID ---")
for m in client.models.list():
    if 'generateContent' in m.supported_actions:
        print(f"ID: {m.name}")