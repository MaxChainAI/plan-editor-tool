import json
import sys

template = "templates/BotTestCases_16-30.json"
tc_file = "_tc_temp.txt"
reset = "reset" in sys.argv

with open(template, "r", encoding="utf-8") as f:
    data = json.load(f)

if reset:
    data["steps"] = data["steps"][:4]

with open(tc_file, "r", encoding="utf-8") as f:
    text = f.read().rstrip("\n")

data["steps"].append({
    "title": text,
    "details": text,
    "enabled": True,
    "open": False,
    "agent_name": ""
})

with open(template, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"Now {len(data['steps'])} steps. Added test case (reset={reset}).")
