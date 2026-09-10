import ast
import re

print("=== INDEPENDENT AST VALIDATION OF REPORT DIFFS ===")

with open('REPORT_CHAT_BALANCE_AND_LOGS.md', 'r', encoding='utf-8') as f:
    report = f.read()

# Extract code blocks
python_blocks = re.findall(r'```python(.*?)```', report, re.DOTALL)
print(f"Total python code blocks found in report: {len(python_blocks)}")

valid_blocks = 0
for idx, code in enumerate(python_blocks, 1):
    cleaned = code.strip()
    # If it's a snippet or diff, try parsing
    try:
        ast.parse(cleaned)
        valid_blocks += 1
    except SyntaxError as e:
        # Check if it contains diff markers (+ / -) or ellipsis (...)
        # Let's remove diff markers if present
        cleaned_diff = re.sub(r'^[+-]\s?', '', cleaned, flags=re.MULTILINE)
        cleaned_diff = cleaned_diff.replace('...', 'pass')
        try:
            ast.parse(cleaned_diff)
            valid_blocks += 1
        except SyntaxError as e2:
            print(f"Block {idx} syntax error: {e2}")
            print(f"Snippet: {cleaned[:100]}...\n")

print(f"Valid Python blocks: {valid_blocks}/{len(python_blocks)}")
