#!/bin/sh
# Парный прогон test_import_safety: одна и та же папка, различаются только МОИ
# восемь файлов (текущие против .bak). Так виден мой дельта, а не приросты
# соседних лан, которые в эту волну добавляют .py прямо сейчас.
cd /c/Users/Admin/Desktop/_Organized/08_Projects/stomchat || exit 1
MINE="test_fix_db.py test_llm_failover.py test_media_selection.py test_round3.py test_tg_delivery.py test_wiki_pagination.py videosi.py visionproc.py"

python test_import_safety.py > /tmp/is_after.txt 2>&1
echo "AFTER  : $(grep -c '^  \[OK  \]' /tmp/is_after.txt) ok, $(grep -c '^  \[FAIL\]' /tmp/is_after.txt) fail, $(grep 'разобрано файлов' /tmp/is_after.txt)"

for f in $MINE; do cp "$f" "$f.new"; cp "$f.bak" "$f"; done
python test_import_safety.py > /tmp/is_before.txt 2>&1
echo "BEFORE : $(grep -c '^  \[OK  \]' /tmp/is_before.txt) ok, $(grep -c '^  \[FAIL\]' /tmp/is_before.txt) fail, $(grep 'разобрано файлов' /tmp/is_before.txt)"
for f in $MINE; do cp "$f.new" "$f"; rm -f "$f.new"; done

echo "--- различия только по МОИМ файлам ---"
for f in $MINE; do
  a=$(grep -c -- "$f" /tmp/is_before.txt)
  b=$(grep -c -- "$f" /tmp/is_after.txt)
  echo "$f: before=$a after=$b"
done
echo "--- diff строк проверок по моим файлам ---"
grep -E "test_fix_db|test_llm_failover|test_media_selection|test_round3|test_tg_delivery|test_wiki_pagination|videosi|visionproc" /tmp/is_before.txt | sort > /tmp/is_b.txt
grep -E "test_fix_db|test_llm_failover|test_media_selection|test_round3|test_tg_delivery|test_wiki_pagination|videosi|visionproc" /tmp/is_after.txt | sort > /tmp/is_a.txt
diff /tmp/is_b.txt /tmp/is_a.txt && echo "ИДЕНТИЧНО"
