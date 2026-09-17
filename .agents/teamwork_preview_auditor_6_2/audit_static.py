import ast
import re
import os

PROD_FILES = ["assistant.py", "gemini_client.py", "config.py"]

print("=== 1. SCANNING FOR MOCK IMPORTS / CHEAT STRINGS IN PROD FILES ===")
for pf in PROD_FILES:
    with open(pf, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Check imports
    tree = ast.parse(content, filename=pf)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "mock" in alias.name.lower():
                    print(f"[FLAG] {pf}: import {alias.name} at line {node.lineno}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and "mock" in node.module.lower():
                print(f"[FLAG] {pf}: from {node.module} import ... at line {node.lineno}")
    
    # Check for hardcoded test markers or bypasses
    suspicious_patterns = [
        r"__test__",
        r"if\s+.*_TEST_MODE",
        r"if\s+.*MOCK_MODE",
        r"if\s+.*pytest",
        r"SKIP_VALIDATION",
        r"BYPASS_FOR_TEST",
    ]
    for sp in suspicious_patterns:
        matches = re.finditer(sp, content)
        for m in matches:
            # find line number
            line_no = content[:m.start()].count("\n") + 1
            print(f"[FLAG] {pf}: suspicious pattern '{m.group(0)}' at line {line_no}")

print("\n=== 2. OCCURRENCES OF 'MagicMock' OR 'mock' IN PROD FILES ===")
for pf in PROD_FILES:
    with open(pf, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            if "mock" in line.lower():
                print(f"{pf}:{idx}: {line.strip()}")

print("\n=== AUDIT STATIC SCAN COMPLETE ===")
