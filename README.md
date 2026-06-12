# Plan Editor Tool for Magentic UI

Standalone tool to generate ready-to-import Magentic UI plan JSON files from templates.  
Uses `{{PLACEHOLDER}}` syntax — no dependencies on the Magentic UI codebase.

## Folder Structure

```
plan-editor-tool/
├── plan_editor.py              # Main script (Python 3.8+, no pip deps)
├── templates/
│   └── app_validation_template.json   # Example template
├── output/                     # Generated plans download here
└── README.md
```

## Quick Start

```powershell
cd "C:\Magentic UI- CUA\plan-editor-tool"

# 1. List available templates
python plan_editor.py --list-templates

# 2. See what placeholders a template needs
python plan_editor.py --list-vars templates/app_validation_template.json

# 3a. Interactive mode (prompts for each value)
python plan_editor.py --interactive templates/app_validation_template.json

# 3b. CLI mode (pass values directly)
python plan_editor.py --template templates/app_validation_template.json ^
    --var VALIDATION_ID=2482446 ^
    --var APP_NAME="Contoso App" ^
    --output output/plan_2482446.json
```

## Placeholders

Templates use `{{PLACEHOLDER_NAME}}` syntax. Built-in defaults:

| Placeholder | Default | Description |
|---|---|---|
| `ADO_ORG_URL` | `https://domoreexp.visualstudio.com` | Azure DevOps org URL |
| `ADO_PROJECT` | `MSTeams` | ADO project name |
| `VALIDATION_ID` | *(required)* | App Validation work item ID |
| `APP_NAME` | *(required)* | Application name |

You can add any custom `{{CUSTOM_VAR}}` to your templates.

## Creating Custom Templates

1. Copy `templates/app_validation_template.json`
2. Edit steps as needed, using `{{PLACEHOLDER}}` for dynamic values
3. Supported step fields:
   - `title` (string) — step heading
   - `details` (string) — full instructions for the agent
   - `agent_name` — one of: `web_surfer`, `coder_agent`, `file_surfer`, `user_proxy`
   - `enabled` (boolean) — include in execution
4. Save as `templates/your_template.json`

## Importing into Magentic UI

1. Run the tool → JSON saved in `output/`
2. Open Magentic UI → **Saved Plans** panel
3. Click **Import** (upload icon) → select the JSON file
4. Plan appears in the list, ready to use
