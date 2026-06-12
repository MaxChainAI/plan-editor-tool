# Environment Setup Guide

## Security Strategy Overview

This repo supports **two security models**:

1. **Local Development**: `.env` file (simple, convenient)
2. **Production/CI-CD**: Azure Key Vault (secure, no secrets in code)

Both work automatically — the script intelligently chooses based on what's available.

## Using the .env File (Local Development)

The Plan Editor Tool supports loading secrets and configuration from a `.env` file using **only Python standard library** (no dependencies required).

### Setup Steps

1. **Copy the template:**
   ```bash
   cp .env.template .env
   ```

2. **Edit `.env` and add your secrets:**
   ```bash
   # On Windows
   notepad .env
   
   # On macOS/Linux
   nano .env
   ```

3. **Update the values:**
   ```env
   # Your Azure DevOps Personal Access Token
   ADO_PAT=your_actual_pat_token_here
   
   # Optional: Your email for automatic uploads
   DEFAULT_UPLOAD_USER=your.email@company.com
   
   # Optional: Magentic UI server URL if not on localhost:8081
   MAGENTIC_SERVER_URL=http://localhost:8081
   ```

4. **The .env file is automatically loaded on startup** — no configuration needed in the script!

### Example Usage

With `.env` file configured:

```bash
# Quick mode - generates plans, reports, and uploads to server
python plan_editor.py 5391178 --upload

# Report only
python plan_editor.py --report 5391178

# The PAT is automatically read from ADO_PAT in .env
# Upload user is automatically read from DEFAULT_UPLOAD_USER if not specified
```

### Security Notes

- **`.env` is already in `.gitignore`** — secrets will never accidentally be committed
- **Environment variables take precedence** — if you set `ADO_PAT` in your shell, it overrides the `.env` file
- **Command-line arguments take highest precedence** — `--pat` argument overrides `.env`

### Priority Order (Highest to Lowest)

1. `--pat` command-line argument
2. `ADO_PAT` environment variable (from shell or `.env`)
3. Azure Key Vault (requires `pip install azure-identity azure-keyvault-secrets` + `az login`)

## Using Azure Key Vault (Production/CI-CD)

### Setup for Production Environments

**Skip the `.env` file entirely** and use Azure Key Vault instead:

1. **Authenticate with Azure:**
   ```bash
   az login
   ```
   Or if running in Azure (App Service, Function, etc.), use Managed Identity (no login needed).

2. **Install Azure SDK** (one-time):
   ```bash
   pip install azure-identity azure-keyvault-secrets
   ```

3. **Verify Key Vault Access:**
   ```bash
   az keyvault secret show --vault-name kv-msteamsappcert-prod --name VSO-PAT
   ```

4. **Run the script** (no `--pat` needed):
   ```bash
   python plan_editor.py 5391178 --upload --upload-user user@contoso.com
   ```
   The script automatically retrieves the PAT from Key Vault.

### Key Vault Configuration

**Your Organization Settings:**
- **Vault URL**: `https://kv-msteamsappcert-prod.vault.azure.net/`
- **Secret Name**: `VSO-PAT`
- **ADO Organization**: `https://domoreexp.visualstudio.com`
- **ADO Project**: `MSTeams`

These are hardcoded in the script and match your org setup.

## Hybrid Approach (Recommended)

**Local Machine** → Use `.env` file
```bash
cp .env.template .env
# Add your PAT
python plan_editor.py 5391178 --upload --upload-user user@contoso.com
```

**Azure DevOps Pipeline** → Use Key Vault
```yaml
- script: |
    pip install azure-identity azure-keyvault-secrets
    az login --service-principal -u $(servicePrincipalId) -p $(servicePrincipalKey) --tenant $(tenantId)
    python plan_editor.py 5391178 --upload --upload-user user@contoso.com
```

**GitHub Actions** → Use Key Vault
```yaml
- name: Authenticate with Azure
  uses: azure/login@v1
  with:
    creds: ${{ secrets.AZURE_CREDENTIALS }}
    
- name: Run Plan Editor
  run: |
    pip install azure-identity azure-keyvault-secrets
    python plan_editor.py 5391178 --upload --upload-user user@contoso.com
```

### How .env Loading Works

- Automatically called at script startup
- Only loads if `.env` file exists (no error if missing)
- Skips empty lines and comments (lines starting with `#`)
- Supports `KEY=VALUE` format
- Handles quoted values: `"value"` or `'value'`
- Environment variables already set take precedence (not overwritten)

### Troubleshooting

**Q: The script doesn't seem to be using my .env values**
- Check the file exists: `.env` in the same directory as `plan_editor.py`
- Check the format: `KEY=VALUE` (no spaces around `=`)
- If you set the value via command-line arg, it takes precedence

**Q: I want to use Key Vault instead of .env**
- You can skip `.env` entirely and use Azure Key Vault (recommended for production)
- Install: `pip install azure-identity azure-keyvault-secrets`
- Authenticate: `az login` (or use Managed Identity in Azure)
- Script will automatically retrieve from Key Vault as fallback
- Your vault: `https://kv-msteamsappcert-prod.vault.azure.net/` (secret: `VSO-PAT`)

**Q: I set ADO_PAT in my shell but it's not being used**
- Check that no `.env` file exists or that `ADO_PAT` is commented out there
- Shell environment variables are checked second in priority

**Q: Which approach should I use for my team?**
- **Local dev**: Use `.env` (each dev copies template, adds own PAT)
- **Shared environments**: Use Key Vault (no secrets on shared machines)
- **CI/CD pipelines**: Use Key Vault (automatic, no manual secret management)
- **Production**: Use Key Vault (most secure, audited access)

**Q: How do I know which method the script is using?**
- If using `.env`: Script is silent (loads from file)
- If using Key Vault: Script prints `Retrieving PAT from Azure Key Vault...`
- If using `--pat` argument: Script uses that directly
- If error about PAT: Script will tell you which methods it tried

**Q: Can I use both .env and Key Vault?**
- Yes! The script checks in priority order:
  1. `--pat` argument (explicit override)
  2. `.env` file or `ADO_PAT` env var (if exists)
  3. Key Vault (if .env/env var not found)
- This allows developers to use `.env` locally, while production uses Key Vault without changing code

