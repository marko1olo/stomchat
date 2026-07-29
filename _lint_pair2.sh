#!/bin/sh
# Парный прогон соседних наборов, которые читают videosi.py и visionproc.py.
cd /c/Users/Admin/Desktop/_Organized/08_Projects/stomchat || exit 1
MINE="test_fix_db.py test_llm_failover.py test_media_selection.py test_round3.py test_tg_delivery.py test_wiki_pagination.py videosi.py visionproc.py"
SETS="test_video_import.py test_dead_tools.py test_json_scan.py test_distill_scripts_safety.py"

echo "--- AFTER (мои правки на месте) ---"
for t in $SETS; do echo "$t: $(python $t 2>&1 | grep -E '^PASSED:|PASSED: ' | tail -1)"; done

for f in $MINE; do cp "$f" "$f.new"; cp "$f.bak" "$f"; done
echo "--- BEFORE (.bak на месте) ---"
for t in $SETS; do echo "$t: $(python $t 2>&1 | grep -E '^PASSED:|PASSED: ' | tail -1)"; done
for f in $MINE; do cp "$f.new" "$f"; rm -f "$f.new"; done
echo "--- восстановлено ---"
python -m ruff check --exclude stomchat --select F401,F841 $MINE
