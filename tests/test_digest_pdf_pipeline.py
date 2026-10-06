import asyncio
import os
import sys

sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")
from digest_pdf import generate_digest_pdf, render_pdf_first_page_preview

async def test_pdf_generation():
    test_html = """
    <p><b>0. 📚 ТЕОРЕТИЧЕСКИЙ СПРАВОЧНИК</b></p>
    <p>Адгезивные протоколы 4-го и 7-го поколений: сравнительный анализ гибридного слоя. При технике тотального протравливания (Total-Etch) 37% ортофосфорная кислота наносится на эмаль на 15-30 секунд, на дентин — максимум на 10-15 секунд во избежание коллапса коллагеновой фибриллярной сети.</p>
    
    <p><b>1. 🔥 ТЕМА ДНЯ: ВЕРТИПРЕП VS УСТУПНОЕ ПРЕПАРИРОВАНИЕ</b></p>
    <p>В чате развернулась профессиональная дискуссия касательно препарирования без уступа (Vertiprep / BOPT) в области жевательной группы зубов. Доктор Иванов представил анатомические обоснования сохранения коронкового феррула при дефиците твердых тканей.</p>
    
    <p><b>2. 🦷 КЛИНИЧЕСКИЕ КЕЙСЫ</b></p>
    <p><b>▶️ СИТУАЦИЯ:</b> Пациент 38 лет обратился с жалобами на боли при накусывании в области зуба 4.6. На прицельном рентгеновском снимке визуализируется периапикальный очаг деструкции у дистального корня, ранее лечен резорцин-формалиновым методом.</p>
    
    <figure>
        <img src="https://iili.io/no8eVkb.jpg" alt="Clinical Case">
        <figcaption>Клинический снимок #177776: Деструкция периапикальных тканей зуба 4.6 до повторного эндодонтического лечения</figcaption>
    </figure>
    
    <p><b>ЧТО СДЕЛАЛИ:</b> Проведена ультразвуковая распломбировка устьев с ирригацией 3% NaOCl и активацией по протоколу EDDY. Механическая обработка доведена до размера 35/.04.</p>
    <p><b>ЛОГИКА ЛЕЧЕНИЯ:</b> Сохранение собственной связки и отказ от немедленной резекции верхушки корня.</p>
    <p><b>ВЫВОД:</b> Консервативная эндодонтия под операционным микроскопом позволяет сохранить зубы даже при выраженной деструкции.</p>
    
    <p><b>3. 💰 РЫНОК И МАТЕРИАЛЫ</b></p>
    <p>Обсудили цены на композиты премиум-класса (Estelite Asteria, Enamel Plus) и наличие оригинальных бондов с мономером 10-MDP (Clearfil SE Protect). Средняя стоимость шприца Asteria в закупке составляет от 4 800 до 5 600 рублей.</p>
    
    <p><b>🌟 ЭКСПЕРТЫ ВЫПУСКА</b></p>
    <p><b>Доктор Смирнов</b> (эндодонтист, микроскопист) — за подробный протокол ультразвуковой дезобтурации анатомически сложных перешейков.</p>
    """

    print("Generating Clinical Digest PDF...")
    pdf_path = await generate_digest_pdf(
        html_content=test_html,
        title="Клинический Вестник StomChat",
        subtitle="Еженедельный клинический дайджест профессионального сообщества",
        msg_count=420,
        date_str="17.09.2026",
    )

    if not pdf_path or not os.path.exists(pdf_path):
        print("FAILED: PDF was not created.")
        return False

    file_size = os.path.getsize(pdf_path)
    print(f"SUCCESS: PDF created at {pdf_path} (size={file_size} bytes)")

    # Render first page preview to PNG
    preview_png = os.path.join(os.path.dirname(pdf_path), "preview_page_1.png")
    ok = render_pdf_first_page_preview(pdf_path, preview_png)
    if ok:
        print(f"SUCCESS: Preview PNG rendered at {preview_png} (size={os.path.getsize(preview_png)} bytes)")
    else:
        print("WARNING: Preview rendering failed.")

    return True

if __name__ == "__main__":
    success = asyncio.run(test_pdf_generation())
    sys.exit(0 if success else 1)
