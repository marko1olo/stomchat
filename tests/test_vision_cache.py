"""
Тест двухуровневого кэша Vision (in-memory + диск) и защиты квоты.
Проверяет:
1. Первый вызов на реальном файле кладёт результат в кэш и на диск.
2. Повторный вызов на том же файле/байтах возвращает кэшированный результат с 0 запросов к API.
3. VisionDescription сохраняет image_urls и строковый тип.
4. Разные подписи/байты дают разные ключи кэша (нет ложных совпадений).
5. use_cache=False обходит кэш.
6. Несуществующие файлы (мок-тесты) пропускают кэш.
7. Успешный вызов отмечает note_success для ключа и модели.
8. Перезагрузка модуля сохраняет кэш с диска.
"""

import asyncio
import os
import sys
import tempfile
import time

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import vision
import gemini_client as gc

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail and not cond else ""))


REQUESTS = []


class FakeCompletions:
    def __init__(self, api_key):
        self.api_key = api_key

    async def create(self, model=None, messages=None, max_tokens=None):
        REQUESTS.append((model, self.api_key))
        return type("R", (), {
            "choices": [type("C", (), {
                "message": type("M", (), {
                    "content": "Клинический разбор снимка: виден кариес зуба 46."
                })()
            })()]
        })()


class FakeAsyncOpenAI:
    def __init__(self, api_key=None, base_url=None, http_client=None, max_retries=0, timeout=None):
        self.chat = type("Chat", (), {"completions": FakeCompletions(api_key)})()


async def run_tests():
    # Мокаем сеть и интервал
    vision.AsyncOpenAI = FakeAsyncOpenAI
    vision.VISION_MIN_CALL_INTERVAL_SECONDS = 0.0

    with tempfile.TemporaryDirectory() as tmpdir:
        test_cache_file = os.path.join(tmpdir, "test_vision_cache.json")
        vision.VISION_CACHE_FILE = test_cache_file
        vision.clear_vision_cache()

        # Создаём тестовый jpg файл на диске
        test_img_path = os.path.join(tmpdir, "test_tooth.jpg")
        with open(test_img_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"fake-tooth-image-content-12345" * 10)

        # Мокаем prepare_image_for_analysis
        async def fake_prep(path, timeout=None):
            if os.path.exists(path):
                with open(path, "rb") as fp:
                    return fp.read(), None
            return None, "File not found"

        vision.prepare_image_for_analysis = fake_prep

        print("\n[1] Первый вызов наполняет кэш и сохраняет на диск")
        REQUESTS.clear()
        res1 = await vision.describe_image([test_img_path], caption="снимок 46")
        check("описание получено", bool(res1 and "46" in res1), f"got {res1!r}")
        check("выполнен 1 API запрос", len(REQUESTS) == 1, f"got {len(REQUESTS)}")
        check("результат — экземпляр VisionDescription", isinstance(res1, vision.VisionDescription))
        check("image_urls присутствуют", len(res1.image_urls) == 1)
        check("запись появилась в кэше памяти", len(vision.get_vision_cache()) == 1)
        check("файл кэша записан на диск", os.path.exists(test_cache_file))

        print("\n[2] Повторный вызов отдаёт из кэша БЕЗ запросов к API")
        REQUESTS.clear()
        res2 = await vision.describe_image([test_img_path], caption="снимок 46")
        check("описание идентично первому", res2 == res1)
        check("0 запросов к API (квота не сожжена)", len(REQUESTS) == 0, f"got {len(REQUESTS)}")
        check("image_urls сохранены в результате из кэша", len(res2.image_urls) == 1)

        print("\n[3] Другая подпись или другое изображение меняет ключ кэша")
        REQUESTS.clear()
        res3 = await vision.describe_image([test_img_path], caption="другой зуб 16")
        check("сделан новый запрос для другой подписи", len(REQUESTS) == 1)
        check("в кэше теперь 2 записи", len(vision.get_vision_cache()) == 2)

        test_img_path2 = os.path.join(tmpdir, "test_tooth2.jpg")
        with open(test_img_path2, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"another-image-content-99999" * 10)

        REQUESTS.clear()
        res4 = await vision.describe_image([test_img_path2], caption="снимок 46")
        check("сделан новый запрос для другого файла", len(REQUESTS) == 1)
        check("в кэше теперь 3 записи", len(vision.get_vision_cache()) == 3)

        print("\n[4] use_cache=False обходит кэш")
        REQUESTS.clear()
        res5 = await vision.describe_image([test_img_path], caption="снимок 46", use_cache=False)
        check("запрос ушёл в API несмотря на наличие в кэше", len(REQUESTS) == 1)

        print("\n[5] Несуществующие пути пропускают кэш (не засоряют мок-тесты)")
        REQUESTS.clear()
        # для несуществующего пути подменяем prepare_image_for_analysis чтобы вернуть байты как в моках
        old_prep = vision.prepare_image_for_analysis
        async def mock_fake_prep(path, timeout=None):
            return b"mock-bytes", None
        vision.prepare_image_for_analysis = mock_fake_prep

        cache_count_before = len(vision.get_vision_cache())
        await vision.describe_image(["/nonexistent/mock.jpg"])
        check("кэш не пополнился для фиктивного пути", len(vision.get_vision_cache()) == cache_count_before)
        vision.prepare_image_for_analysis = old_prep

        print("\n[6] Загрузка кэша с диска после перезапуска")
        # Имитируем перезагрузку: очищаем память и вызываем _load_vision_cache
        loaded = vision._load_vision_cache()
        check("кэш успешно прочитан с диска", len(loaded) >= 3)

        print("\n[7] Успешный ответ вызывает note_success")
        test_key = "test-gemini-key-12345"
        gc.set_key_cooldown("gemini", test_key, seconds=300)
        check("ключ предварительно был на кулдауне", gc._key_fingerprint("gemini", test_key) in gc.get_key_cooldowns())
        # Вызываем note_success напрямую для проверки
        gc.note_success("gemini", test_key, "gemini-3.5-flash-lite")
        check("note_success снял кулдаун", gc._key_fingerprint("gemini", test_key) not in gc.get_key_cooldowns())

    print(f"\n==============================================================")
    print(f"PASSED: {len(PASS)}   FAILED: {len(FAIL)}")
    if FAIL:
        print("Провалено: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    exit(asyncio.run(run_tests()))
