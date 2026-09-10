import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

state_path = "assistant_state.json"
with open(state_path, "r", encoding="utf-8") as f:
    state = json.load(f)

print(f"Total keys in {state_path}: {len(state.keys())}")
print("Keys and values:")
for k, v in state.items():
    if k == "pm_pings":
        print(f"  {k}: dict with {len(v)} doctor records: {list(v.keys())}")
    elif k == "processed_threads":
        print(f"  {k}: {v} (len={len(v)})")
    else:
        print(f"  {k}: {v!r}")

# Check specific values mentioned in report:
assert state.get("last_passive_run") == "2000-01-01T00:00:00", f"Unexpected last_passive_run: {state.get('last_passive_run')}"
assert "silenced_until" in state, "silenced_until not in state"
assert isinstance(state.get("processed_threads"), list) and len(state.get("processed_threads")) == 0, f"processed_threads not empty list: {state.get('processed_threads')}"
assert len(state.get("pm_pings", {})) == 22, f"pm_pings length expected 22, got {len(state.get('pm_pings', {}))}"
print("\nAll assistant_state.json assertions PASSED!")
