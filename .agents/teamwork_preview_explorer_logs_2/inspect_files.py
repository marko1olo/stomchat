import os
import glob
import json
from datetime import datetime

base_dir = r"c:\Users\danat\Desktop\stomchat"
targets = ["bot.log*", "bot_supervisor.log*", "assistant_state.json*", "*.db*"]

found_files = []
for pattern in targets:
    for f in glob.glob(os.path.join(base_dir, pattern)):
        stat = os.stat(f)
        found_files.append({
            "name": os.path.basename(f),
            "path": f,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat()
        })

print(json.dumps(found_files, indent=2))
