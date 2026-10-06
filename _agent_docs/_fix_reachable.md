# Лана reachable: до чего врач может ДОЙТИ

Мои файлы: `assistant.py`, `taxonomy.py`, `test_reachable.py` (создан).
Боевые базы — только `mode=ro` (`file:...?mode=ro`, `uri=True`).

## 0. Базовый прогон ДО правок (2026-07-29)

`_run_reachable_suites.py` (мой прогонщик, `run_all_tests.py` не запускался — его
md5-охрана врёт, когда рядом пишут другие агенты):

| набор | PASSED | FAILED |
|---|---|---|
| test_commands_surface.py | 73 | 0 |
| test_wiki_pagination.py | 40 | 0 |
| test_protocols_ui.py | 32 | 0 |
| test_taxonomy_single.py | 76 | 0 |
| test_distill_pipeline.py | 145 | 0 |
| test_search_command.py | 123 | 0 |
| test_clip_single.py | 80 | 0 |
| test_validator_coverage.py | 24 | 0 |
| test_silence_all_paths.py | 21 | 0 |
| test_routing_behaviour.py | 31 | 0 |
| test_bookmarks.py | 25 | 0 |
| test_group_quiz.py | 28 | 0 |
| **итого** | **698** | **0** |

Дальше — замеры. Раздел пополняется по ходу работы.
