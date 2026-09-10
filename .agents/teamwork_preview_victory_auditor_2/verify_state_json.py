import json
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("=== FORENSIC STATE HYGIENE AUDIT OF assistant_state.json ===")

with open('assistant_state.json', 'r', encoding='utf-8') as f:
    state = json.load(f)

print(f"Total keys: {len(state.keys())}")
print("Keys list:", list(state.keys()))

for k, v in state.items():
    if isinstance(v, (dict, list)):
        print(f"  Key '{k}': type={type(v).__name__}, len={len(v)}")
    else:
        print(f"  Key '{k}': {v}")

# Verify specific claims in Section 2.6:
# 1. last_passive_run: "2000-01-01T00:00:00"
print("\nVerifying specific keys:")
print("  last_passive_run =", repr(state.get("last_passive_run")))
print("  silenced_until =", repr(state.get("silenced_until")))
print("  processed_threads =", repr(state.get("processed_threads")))
print("  pm_pings count =", len(state.get("pm_pings", {})))
