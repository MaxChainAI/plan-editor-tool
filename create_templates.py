"""One-time script to generate templates from exported plan files."""
import json
import os

REPLACEMENTS = {
    "admin@M365x48062851.onmicrosoft.com": "{{TENANT_EMAIL}}",
    "u}6kB=hB693I]#T0D7}(lc#j0oX$9MrM": "{{TENANT_PASSWORD}}",
    "'Forrester AI'": "'{{APP_NAME}}'",
    "https://msteamspmewebapi.azurewebsites.net/api/GetSMPWorkFlowDescriptionDetails/8f3eb11c-7f88-451d-b260-d05b6fc4c3d0/1152921505700594928": "{{SMP_API_URL}}",
}

PLANS = [
    {
        "src": r"C:\Users\v-guptneha\Downloads\plan-63-fa-final-mtc-1-15-.json",
        "dst": "templates/MetadataTC_1-15.json",
        "task": "Metadata TC 1-15 - {{APP_NAME}}",
    },
    {
        "src": r"C:\Users\v-guptneha\Downloads\plan-64-fa-final-mtc-16-32.json",
        "dst": "templates/MetadataTC_16-30.json",
        "task": "Metadata TC 16-30 - {{APP_NAME}}",
    },
]

os.chdir(os.path.dirname(os.path.abspath(__file__)))

for plan in PLANS:
    with open(plan["src"], "r", encoding="utf-8") as f:
        data = json.load(f)

    # Remove fields not needed for import templates
    data.pop("id", None)
    data.pop("session_id", None)
    data.pop("user_id", None)

    # Set template task name and version
    data["task"] = plan["task"]
    data["version"] = "0.0.1"

    # Serialize, replace dynamic values, write
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    for old, new in REPLACEMENTS.items():
        raw = raw.replace(old, new)

    with open(plan["dst"], "w", encoding="utf-8") as f:
        f.write(raw)

    # Quick validation
    step_count = len(data["steps"])
    print(f"Created {plan['dst']}: {step_count} steps, task=\"{plan['task']}\"")
