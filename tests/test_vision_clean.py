import sys
import os
sys.path.insert(0, r"c:\Users\danat\Desktop\stomchat")

import vision

raw_sample = """
[ПОДРОБНОЕ ОПИСАНИЕ]
<разбор>
Клинический протокол 21: от деструкции до временной реабилитации.
На прицельной визиографии 21: коронковая часть разрушена на 2/3.
[КРАТКАЯ ПОДПИСЬ]
<108 симв>
21: деструкция коронковой части, апикальный периодонтит
[МЕТАДАННЫЕ]
{"modality":"CLINICAL_PHOTO","has_clinical_doubt":false,"target_tooth":21,"doubt_subject":null,"recommended_quadrant":2}
"""

detailed, short = vision.parse_dual_vision_description(raw_sample)
print("Detailed:", repr(detailed))
print("Short:", repr(short))

assert "<разбор>" not in detailed, "<разбор> leaked in detailed"
assert "<108 симв>" not in short, "<108 симв> leaked in short"
assert "modality" not in detailed, "JSON leaked in detailed"
assert "modality" not in short, "JSON leaked in short"
print("SUCCESS: vision dual parsing clean!")
