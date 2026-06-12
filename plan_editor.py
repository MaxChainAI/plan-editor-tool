"""
Plan Editor Tool for Magentic UI

Generates ready-to-import plan JSON files from templates by replacing
placeholders (e.g. {{VALIDATION_ID}}, {{APP_NAME}}) with actual values.

Supports secure PAT retrieval from Azure Key Vault (recommended) or
manual PAT via --pat / ADO_PAT env var.

Usage:
  # Auto-fill from ADO work item (Key Vault PAT — recommended)
  python plan_editor.py --interactive templates/MetadataTC_1-15.json \
      --ado-url "https://domoreexp.visualstudio.com/MSTeams/_workitems/edit/4941424"

  # CLI mode with explicit variables
  python plan_editor.py --template templates/MetadataTC_1-15.json \
      --ado-url "https://domoreexp.visualstudio.com/MSTeams/_workitems/edit/4941424" \
      --var TEAMS_ID=admin@M365x48062851.onmicrosoft.com \
      --var TEAMS_PASSWORD="mypassword" \
      --output output/plan_4941424.json

  python plan_editor.py --list-vars templates/MetadataTC_1-15.json
"""

import argparse
import base64
import io
import json
import os
import re
import struct
import sys
import urllib.request
import urllib.error
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Force UTF-8 output on Windows so Unicode characters print correctly
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_][A-Z0-9_]*)\}\}")

# Regex to parse ADO work-item URLs like:
#   https://domoreexp.visualstudio.com/MSTeams/_workitems/edit/4941424/?view=edit
ADO_URL_RE = re.compile(
    r"https?://([^/]+)/([^/]+)/_workitems/edit/(\d+)"
)

# Default values for common placeholders (can be overridden via --var)
DEFAULTS: Dict[str, str] = {
    "TEAMS_URL": os.environ.get("TEAMS_URL", "https://teams.microsoft.com/v2/"),
    "SMP_API_URL": os.environ.get("SMP_API_URL", "https://msteamspmewebapi.azurewebsites.net"),
}

# ── Azure Key Vault PAT configuration ───────────────────────────────────
# When --pat is NOT provided, the PAT is fetched from Azure Key Vault.
# Requires: pip install azure-identity azure-keyvault-secrets
# Auth:     az login  (or Managed Identity / VS Code auth)
KEY_VAULT_URL   = os.environ.get("KEY_VAULT_URL", "https://kv-msteamsappcert-prod.vault.azure.net/")
PAT_SECRET_NAME = os.environ.get("PAT_SECRET_NAME", "VSO-PAT")

# ADO org defaults (used when only a work-item ID or URL is given)
# Can be overridden via ADO_ORG_URL and ADO_PROJECT env vars from .env
ADO_ORG_URL = os.environ.get("ADO_ORG_URL", "https://domoreexp.visualstudio.com")
ADO_PROJECT = os.environ.get("ADO_PROJECT", "MSTeams")


# ── ADO integration ──────────────────────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags and return plain text, preserving line breaks."""
    def __init__(self):
        super().__init__()
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs):
        if tag in ("br", "p", "div"):
            self._parts.append("\n")

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    return parser.get_text()


# ── Environment file loading ────────────────────────────────────────────

def load_env_file(env_path: str = ".env") -> None:
    """Load environment variables from a .env file (only if it exists).
    
    Uses only stdlib - no external dependencies.
    Skips lines that are empty or start with '#'.
    """
    if not os.path.exists(env_path):
        return
    
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if not line or line.startswith("#"):
                    continue
                # Parse KEY=VALUE format
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    # Remove quotes if present
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    # Only set if not already in environment (env vars take precedence)
                    if key and key not in os.environ:
                        os.environ[key] = value
    except Exception as e:
        print(f"Warning: Could not load {env_path}: {e}", file=sys.stderr)


# ── Key Vault PAT retrieval ──────────────────────────────────────────────

_cached_kv_pat: Optional[str] = None


def resolve_pat(explicit_pat: Optional[str] = None) -> str:
    """Return the ADO PAT using the following priority:
    1. Explicit --pat argument
    2. ADO_PAT environment variable
    3. Azure Key Vault secret (KEY_VAULT_URL / PAT_SECRET_NAME)
    """
    global _cached_kv_pat  # noqa: PLW0603

    # 1. Explicit PAT
    if explicit_pat:
        return explicit_pat

    # 2. Environment variable
    env_pat = os.environ.get("ADO_PAT", "")
    if env_pat:
        return env_pat

    # 3. Azure Key Vault
    if _cached_kv_pat:
        return _cached_kv_pat

    print(f"  Retrieving PAT from Azure Key Vault ({KEY_VAULT_URL}) ...")
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=KEY_VAULT_URL, credential=credential)
        secret = client.get_secret(PAT_SECRET_NAME)

        token = secret.value or ""
        if not token:
            raise ValueError(f"Secret '{PAT_SECRET_NAME}' is empty")

        _cached_kv_pat = token
        print("  PAT retrieved successfully from Azure Key Vault.")
        return _cached_kv_pat
    except ImportError as exc:
        print(
            f"ERROR: Azure SDK not installed: {exc}\n"
            "  Install with: pip install azure-identity azure-keyvault-secrets",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR: Failed to retrieve PAT from Azure Key Vault: {exc}", file=sys.stderr)
        print("  Tip: run 'az login', or provide PAT via --pat / ADO_PAT env var.", file=sys.stderr)
        sys.exit(1)


# ── ADO REST API ─────────────────────────────────────────────────────────

def parse_ado_url(url: str) -> Tuple[str, str, str]:
    """Extract (org_host, project, work_item_id) from an ADO URL."""
    m = ADO_URL_RE.search(url)
    if not m:
        raise ValueError(f"Could not parse ADO URL: {url}")
    return m.group(1), m.group(2), m.group(3)


def fetch_ado_work_item(org_host: str, project: str, work_item_id: str,
                        pat: str) -> dict:
    """Fetch work item JSON from ADO REST API (stdlib only)."""
    api_url = (
        f"https://{org_host}/{project}/_apis/wit/workitems/{work_item_id}"
        f"?api-version=7.1"
    )
    token = base64.b64encode(f":{pat}".encode()).decode()
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def extract_fields_from_work_item(wi: dict) -> Dict[str, str]:
    """Parse work item fields into placeholder values.

    Extracts:
      VALIDATION_ID   — work item id
      APP_NAME        — from Description "AppName: ..." line
      PRODUCT_ID      — from AppSpecificData "Product Id: ..." (fallback: Custom.AppID)
      SUBMISSION_ID   — from AppSpecificData "SubmissionId: ..."
    """
    fields = wi.get("fields", {})
    result: Dict[str, str] = {}

    # VALIDATION_ID = work item id
    result["VALIDATION_ID"] = str(wi.get("id", ""))

    # ── APP_NAME from Description HTML ──
    desc_html = fields.get("System.Description", "")
    desc_text = ""
    if desc_html:
        desc_text = _html_to_text(desc_html)
        m = re.search(r"AppName\s*:\s*(.+)", desc_text)
        if m:
            app_name = m.group(1).strip().split("\n")[0].strip()
            result["APP_NAME"] = app_name

    # Fallback: derive APP_NAME from Title (e.g. "Forrester AI-Dormant" → "Forrester AI")
    if "APP_NAME" not in result:
        title = fields.get("System.Title", "")
        if title:
            result["APP_NAME"] = title.split("-")[0].strip()

    # ── Parse Custom.AppSpecificData for Product Id and SubmissionId ──
    app_specific_html = fields.get("Custom.AppSpecificData", "")
    app_specific_text = _html_to_text(app_specific_html) if app_specific_html else ""

    # PRODUCT_ID from AppSpecificData "Product Id: <guid>"
    # [\s\u00a0] matches regular and non-breaking spaces from HTML
    m = re.search(r"Product[\s\u00a0]*Id[\s\u00a0]*:[\s\u00a0]*([0-9a-f\-]{36})", app_specific_text, re.IGNORECASE)
    if not m:
        m = re.search(r"Product\s*Id\s*(?:<[^>]+>)?\s*:[\s\u00a0]*([0-9a-f\-]{36})", app_specific_html, re.IGNORECASE)
    if m:
        result["PRODUCT_ID"] = m.group(1).strip()
    else:
        # Fallback: Custom.AppID field
        app_id = fields.get("Custom.AppID", "")
        if app_id and re.match(r"[0-9a-f]{8}-[0-9a-f]{4}", str(app_id), re.IGNORECASE):
            result["PRODUCT_ID"] = str(app_id).strip()

    # SUBMISSION_ID from AppSpecificData "SubmissionId: <number>"
    # Try on plain text first, then fall back to raw HTML (handles <b>SubmissionId</b>: format)
    m = re.search(r"SubmissionId[\s\u00a0]*:[\s\u00a0]*(\d+)", app_specific_text, re.IGNORECASE)
    if not m:
        m = re.search(r"SubmissionId\s*(?:<[^>]+>)?\s*:[\s\u00a0]*(\d+)", app_specific_html, re.IGNORECASE)
    if m:
        result["SUBMISSION_ID"] = m.group(1).strip()
    else:
        # Fallback: try known direct ADO field names
        for field_name in ("Custom.SubmissionID", "Custom.SubmissionId",
                           "Custom.Submissionid", "Custom.submission_id"):
            val = fields.get(field_name, "")
            if val and str(val).strip().isdigit():
                result["SUBMISSION_ID"] = str(val).strip()
                break

    return result


def fetch_and_extract(ado_url: str, pat: str) -> Dict[str, str]:
    """One-shot: parse ADO URL → fetch work item → extract all fields."""
    org_host, project, wi_id = parse_ado_url(ado_url)
    print(f"  Parsed ADO URL → VALIDATION_ID = {wi_id}")
    print(f"  Fetching work item {wi_id} from ADO API ...")
    wi = fetch_ado_work_item(org_host, project, wi_id, pat)
    extracted = extract_fields_from_work_item(wi)

    # Print what we found
    for k, v in sorted(extracted.items()):
        print(f"  Extracted → {k} = {v}")

    # Warn about fields we couldn't extract
    expected_from_ado = {"VALIDATION_ID", "APP_NAME", "PRODUCT_ID", "SUBMISSION_ID"}
    missing = expected_from_ado - set(extracted.keys())
    if missing:
        print(f"  Warning: Could not auto-extract: {', '.join(sorted(missing))}")

    return extracted


# ── SMP API + PNG dimension extraction ──────────────────────────────────

def read_png_dimensions_from_bytes(data: bytes) -> Tuple[int, int]:
    """Return (width, height) from a PNG file's IHDR chunk (stdlib only).

    PNG layout:
      bytes  0-7   : 8-byte PNG signature
      bytes  8-11  : IHDR chunk length (always 13)
      bytes 12-15  : chunk type "IHDR"
      bytes 16-19  : image width  (big-endian uint32)
      bytes 20-23  : image height (big-endian uint32)
    """
    PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
    if len(data) < 24:
        raise ValueError("File too short to be a valid PNG")
    if data[:8] != PNG_SIGNATURE:
        raise ValueError("Not a valid PNG file (bad signature)")
    if data[12:16] != b'IHDR':
        raise ValueError("First chunk is not IHDR")
    width = struct.unpack('>I', data[16:20])[0]
    height = struct.unpack('>I', data[20:24])[0]
    return width, height


def fetch_smp_sasuri(api_base_url: str, product_id: str, submission_id: str,
                     pat: Optional[str] = None) -> str:
    """Call the SMP WorkFlow Manifest API and return the SAS URI for the zip package.

    Endpoint: GET {api_base_url}/api/GetSMPWorkFlowManifestDetails/{productId}/{submissionId}

    The response JSON contains a top-level "sasUri" field with the Azure Blob
    Storage SAS URL that can be used to download the app package zip.
    """
    url = (
        f"{api_base_url.rstrip('/')}"
        f"/api/GetSMPWorkFlowManifestDetails/{product_id}/{submission_id}"
    )
    headers: Dict[str, str] = {"Accept": "application/json"}
    if pat:
        token = base64.b64encode(f":{pat}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"SMP API returned HTTP {exc.code} for {url}"
        ) from exc

    # Unwrap single-element list responses
    if isinstance(payload, list):
        if not payload:
            raise ValueError("SMP API returned an empty list")
        payload = payload[0]

    sas_uri = payload.get("sasUri", "")
    if not sas_uri:
        raise ValueError(
            f"Could not find 'sasUri' in SMP API response. "
            f"Available keys: {list(payload.keys())}"
        )
    return str(sas_uri)


def analyze_png_background(data: bytes) -> Dict[str, str]:
    """Analyze a PNG file's background compliance using Pillow.

    Checks:
      - Is the image square (width == height)?
      - Are all pixels either fully opaque (alpha=255) or fully transparent (alpha=0)?
        Semi-transparent pixels (0 < alpha < 255) are non-compliant.
      - What is the background type: SOLID, TRANSPARENT, or NON_COMPLIANT?

    Returns a dict with keys (STEM is filled in by the caller):
      IS_SQUARE          → "PASS" / "FAIL"
      BACKGROUND_TYPE    → "SOLID" / "TRANSPARENT" / "NON_COMPLIANT"
      BG_COMPLIANT       → "PASS" / "FAIL"
      SEMI_TRANSPARENT_COUNT → number of semi-transparent pixels as string

    Requires Pillow. Returns an empty dict if Pillow is unavailable.
    """
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        print("  Note: Pillow not installed — skipping background analysis. "
              "Install with: pip install Pillow")
        return {}

    img = Image.open(io.BytesIO(data)).convert("RGBA")
    w, h = img.size
    pixels = list(img.getdata())
    total = len(pixels)

    is_square = w == h

    # ── Strict alpha counts (used for BG_TYPE / BG_COMPLIANT) ────────────
    semi_transparent = sum(1 for _, _, _, a in pixels if 0 < a < 255)
    fully_transparent = sum(1 for _, _, _, a in pixels if a == 0)

    if semi_transparent > 0:
        bg_type = "NON_COMPLIANT"
    elif fully_transparent > 0:
        bg_type = "TRANSPARENT"
    else:
        bg_type = "SOLID"

    bg_compliant = bg_type in ("SOLID", "TRANSPARENT")

    result: Dict[str, str] = {
        "IS_SQUARE": "PASS" if is_square else "FAIL",
        "BACKGROUND_TYPE": bg_type,
        "BG_COMPLIANT": "PASS" if bg_compliant else "FAIL",
        "SEMI_TRANSPARENT_COUNT": str(semi_transparent),
    }

    # ── Color / outline-type classification (threshold-based) ────────────
    # Uses lenient alpha thresholds so anti-aliased edge pixels (common in
    # PNG exports) don't block classification.
    #
    #   effectively transparent : alpha <= 10
    #   effectively opaque      : alpha >= 245
    #   anti-aliasing edge      : everything in between (tolerated)
    #
    # WHITE_ON_TRANSPARENT  — all effectively-opaque pixels are white
    #                         AND the image has effectively-transparent pixels
    # TRANSPARENT_ON_WHITE  — all effectively-opaque pixels are white
    #                         AND the image has NO effectively-transparent pixels
    #                         (transparent pixels are the "symbol cutout")
    # NON_COMPLIANT         — any effectively-opaque pixel is non-white (colored)
    WHITE_THRESHOLD  = 200   # R, G, B all >= this → "white"
    ALPHA_OPAQUE_TH  = 245   # alpha >= this → "effectively opaque"
    ALPHA_TRANSP_TH  = 10    # alpha <= this → "effectively transparent"

    effectively_opaque      = [(r, g, b, a) for r, g, b, a in pixels if a >= ALPHA_OPAQUE_TH]
    effectively_transparent = [(r, g, b, a) for r, g, b, a in pixels if a <= ALPHA_TRANSP_TH]

    non_white_opaque = [
        (r, g, b, a) for r, g, b, a in effectively_opaque
        if not (r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD)
    ]

    has_eff_transparent = len(effectively_transparent) > 0
    has_eff_opaque      = len(effectively_opaque) > 0
    all_opaque_white    = len(non_white_opaque) == 0

    if not has_eff_opaque:
        # Entirely transparent image — nothing to classify
        color_type = "NON_COMPLIANT"
        color_type_note = "image contains no opaque pixels"
    elif not all_opaque_white:
        color_type = "NON_COMPLIANT"
        color_type_note = (
            f"{len(non_white_opaque)} non-white opaque pixel(s) found "
            f"(colored content detected)"
        )
    elif has_eff_transparent:
        color_type = "WHITE_ON_TRANSPARENT"
        color_type_note = (
            f"{len(effectively_opaque)} white px, "
            f"{len(effectively_transparent)} transparent px"
            + (f", {semi_transparent} anti-aliased edge px" if semi_transparent else "")
        )
    else:
        # All opaque + white, no transparent pixels → likely transparent-on-white
        color_type = "TRANSPARENT_ON_WHITE"
        color_type_note = (
            f"{len(effectively_opaque)} white opaque px"
            + (f", {semi_transparent} anti-aliased edge px" if semi_transparent else "")
        )

    result["COLOR_TYPE"]      = color_type
    result["COLOR_TYPE_NOTE"] = color_type_note

    # ── Padding check (threshold-based bounding box) ─────────────────────
    # "Symbol" pixels = anything with alpha > ALPHA_TRANSP_TH (catches both
    # opaque content and anti-aliased edge pixels).
    # Extra padding = more than PADDING_THRESHOLD px of empty space on any side.
    PADDING_THRESHOLD = 2
    try:
        from PIL import Image as _PILImage  # already imported above

        if color_type in ("WHITE_ON_TRANSPARENT", "TRANSPARENT_ON_WHITE"):
            # Build a binary mask: 255 where pixel is "symbol content", 0 where background
            if color_type == "WHITE_ON_TRANSPARENT":
                # Symbol = opaque/semi-transparent pixels (alpha > ALPHA_TRANSP_TH)
                mask = _PILImage.new("L", (w, h), 0)
                mask_data = [255 if a > ALPHA_TRANSP_TH else 0 for _, _, _, a in pixels]
            else:
                # TRANSPARENT_ON_WHITE: symbol = transparent pixels (the cutout)
                mask = _PILImage.new("L", (w, h), 0)
                mask_data = [255 if a <= ALPHA_TRANSP_TH else 0 for _, _, _, a in pixels]

            mask.putdata(mask_data)
            bbox = mask.getbbox()

            if bbox:
                bleft, btop, bright, bbottom = bbox
                pad_left   = bleft
                pad_top    = btop
                pad_right  = w - bright
                pad_bottom = h - bbottom
                has_extra  = any(
                    p > PADDING_THRESHOLD
                    for p in (pad_left, pad_top, pad_right, pad_bottom)
                )
                result["PADDING_STATUS"] = "FAIL" if has_extra else "PASS"
                result["PADDING_INFO"] = (
                    f"symbol {bright - bleft}x{bbottom - btop}px; "
                    f"padding L:{pad_left} T:{pad_top} R:{pad_right} B:{pad_bottom}"
                )
            else:
                result["PADDING_STATUS"] = "UNKNOWN"
                result["PADDING_INFO"] = "Mask is empty — could not determine bounding box"
        else:
            # NON_COMPLIANT color type — still try a best-effort bounding box
            # using any non-transparent pixel as "content"
            mask_data = [255 if a > ALPHA_TRANSP_TH else 0 for _, _, _, a in pixels]
            from PIL import Image as _PIL2
            mask = _PIL2.new("L", (w, h), 0)
            mask.putdata(mask_data)
            bbox = mask.getbbox()
            if bbox:
                bleft, btop, bright, bbottom = bbox
                pad_left   = bleft
                pad_top    = btop
                pad_right  = w - bright
                pad_bottom = h - bbottom
                has_extra  = any(
                    p > PADDING_THRESHOLD
                    for p in (pad_left, pad_top, pad_right, pad_bottom)
                )
                result["PADDING_STATUS"] = "FAIL" if has_extra else "PASS"
                result["PADDING_INFO"] = (
                    f"symbol {bright - bleft}x{bbottom - btop}px (best-effort); "
                    f"padding L:{pad_left} T:{pad_top} R:{pad_right} B:{pad_bottom}"
                )
            else:
                result["PADDING_STATUS"] = "UNKNOWN"
                result["PADDING_INFO"] = "No content pixels found"

    except Exception as exc:
        result["PADDING_STATUS"] = "UNKNOWN"
        result["PADDING_INFO"] = f"Padding analysis failed: {exc}"

    return result


def download_zip_and_get_png_dimensions(sasuri: str) -> Dict[str, str]:
    """Download a zip from the SAS URI, locate all PNG files inside, and
    return a variable dict with dimensions and background compliance info.

    For example, if the zip contains:
      color.png   (192 × 192, solid background)
      outline.png (32 × 32, transparent background)

    The returned dict will include:
      COLOR_WIDTH, COLOR_HEIGHT, COLOR_IS_SQUARE, COLOR_BACKGROUND_TYPE,
      COLOR_BG_COMPLIANT, COLOR_SEMI_TRANSPARENT_COUNT
      OUTLINE_WIDTH, OUTLINE_HEIGHT, OUTLINE_IS_SQUARE, ...

    Use these keys as placeholders in your template, e.g. {{COLOR_WIDTH}},
    {{COLOR_BACKGROUND_TYPE}}, {{COLOR_BG_COMPLIANT}}.
    """
    print(f"  Downloading zip package from SAS URI ...")
    try:
        with urllib.request.urlopen(sasuri, timeout=120) as resp:
            zip_data = resp.read()
    except Exception as exc:
        raise RuntimeError(f"Failed to download zip from SAS URI: {exc}") from exc

    print(f"  Downloaded {len(zip_data):,} bytes. Scanning for PNG files ...")

    variables: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:

        # ── Parse manifest.json for icons section ─────────────────────────
        manifest_entries = [
            name for name in zf.namelist()
            if Path(name).name.lower() == "manifest.json"
        ]
        if manifest_entries:
            try:
                with zf.open(manifest_entries[0]) as mf:
                    manifest = json.loads(mf.read().decode("utf-8"))
                icons = manifest.get("icons", {})
                color_val   = icons.get("color", "")
                outline_val = icons.get("outline", "")
                variables["MANIFEST_HAS_COLOR_ICON"]   = "YES" if color_val   else "NO"
                variables["MANIFEST_HAS_OUTLINE_ICON"] = "YES" if outline_val else "NO"
                variables["MANIFEST_COLOR_ICON_VALUE"]   = color_val
                variables["MANIFEST_OUTLINE_ICON_VALUE"] = outline_val
                print(f"  manifest.json icons.color   = {color_val!r}")
                print(f"  manifest.json icons.outline = {outline_val!r}")
            except Exception as exc:
                print(f"  Warning: Could not parse manifest.json: {exc}")
                variables["MANIFEST_HAS_COLOR_ICON"]   = "UNKNOWN"
                variables["MANIFEST_HAS_OUTLINE_ICON"] = "UNKNOWN"
        else:
            print("  Warning: manifest.json not found in zip package.")
            variables["MANIFEST_HAS_COLOR_ICON"]   = "MISSING"
            variables["MANIFEST_HAS_OUTLINE_ICON"] = "MISSING"

        png_entries = [name for name in zf.namelist() if name.lower().endswith(".png")]

        if not png_entries:
            print("  Warning: No PNG files found inside the zip package.")
            return variables

        print(f"  Found {len(png_entries)} PNG file(s): {png_entries}")

        raw_bytes: Dict[str, bytes] = {}  # stem → raw bytes (for shape comparison)

        for entry in png_entries:
            # Derive a safe placeholder stem: "icons/color.png" → "COLOR"
            stem = (
                Path(entry).stem
                .upper()
                .replace("-", "_")
                .replace(" ", "_")
                .replace(".", "_")
            )
            # Normalize common icon filename variants to expected stem names
            # e.g. "icon-color.png" → ICON_COLOR → COLOR
            #      "icon-outline.png" → ICON_OUTLINE → OUTLINE
            STEM_MAP = {
                "ICON_COLOR": "COLOR",
                "COLOR_ICON": "COLOR",
                "ICON_OUTLINE": "OUTLINE",
                "OUTLINE_ICON": "OUTLINE",
            }
            stem = STEM_MAP.get(stem, stem)
            try:
                with zf.open(entry) as f:
                    data = f.read()
                raw_bytes[stem] = data
                width, height = read_png_dimensions_from_bytes(data)
                variables[f"{stem}_WIDTH"] = str(width)
                variables[f"{stem}_HEIGHT"] = str(height)
                print(f"  {entry}: {width} × {height}")

                # Background compliance analysis (requires Pillow)
                bg_info = analyze_png_background(data)
                for key, val in bg_info.items():
                    variables[f"{stem}_{key}"] = val
                    print(f"    {stem}_{key} = {val}")

            except Exception as exc:
                print(f"  Warning: Could not read dimensions from {entry}: {exc}")

    return variables


def fetch_png_dimensions_from_smp(api_base_url: str, product_id: str, submission_id: str,
                                   pat: Optional[str] = None) -> Dict[str, str]:
    """End-to-end: call SMP API → download zip → return PNG dimension variables.

    Returns an empty dict (with a warning) rather than raising if anything fails,
    so the rest of plan generation can continue uninterrupted.
    """
    print(f"\n── Fetching PNG dimensions from SMP API ──")
    print(f"  PRODUCT_ID   = {product_id}")
    print(f"  SUBMISSION_ID = {submission_id}")
    try:
        sasuri = fetch_smp_sasuri(api_base_url, product_id, submission_id, pat)
        print(f"  SAS URI obtained (length={len(sasuri)})")
        dims = download_zip_and_get_png_dimensions(sasuri)
        for k, v in sorted(dims.items()):
            print(f"  Extracted → {k} = {v}")
        return dims
    except Exception as exc:
        print(f"  Warning: PNG dimension extraction failed: {exc}", file=sys.stderr)
        return {}


# ── Test case framework ──────────────────────────────────────────────────

from typing import Callable  # noqa: E402 (already imported via typing above)


def _tc(tc_id: str, title: str, check: Callable[[Dict[str, str]], Tuple[bool, str]],
        recommendation: str = "") -> dict:
    return {"id": tc_id, "title": title, "check": check, "recommendation": recommendation}


# ---------------------------------------------------------------------------
# Icon test cases — evaluated against the variables dict (populated from the
# zip package via the SMP API).
# ---------------------------------------------------------------------------

def _check_color_icon(v: Dict[str, str]) -> Tuple[bool, str]:
    """TC-1140.4.1.2.1 — Combined check:
      1. Color icon must be exactly 192x192 px (square).
      2. Must sit on a solid or fully transparent background (no semi-transparent pixels).
    """
    w = v.get("COLOR_WIDTH", "")
    h = v.get("COLOR_HEIGHT", "")
    is_square = v.get("COLOR_IS_SQUARE", "")
    bg_type = v.get("COLOR_BACKGROUND_TYPE", "")
    semi = v.get("COLOR_SEMI_TRANSPARENT_COUNT", "0")

    findings: List[str] = []
    passed = True

    # --- Dimension check ---
    if not w or not h:
        passed = False
        findings.append("Dimensions unavailable (SMP data missing)")
    elif w == "192" and h == "192":
        findings.append(f"Dimensions: {w}x{h} px (PASS)")
    else:
        passed = False
        findings.append(f"Dimensions: {w}x{h} px -- expected 192x192 px (FAIL)")

    # --- Square check ---
    if is_square and is_square != "PASS":
        passed = False
        findings.append("Icon is not square (FAIL)")

    # --- Background check ---
    if not bg_type:
        passed = False
        findings.append("Background type unavailable (Pillow not installed or SMP data missing)")
    elif bg_type == "SOLID":
        findings.append("Background: solid / fully opaque (PASS)")
    elif bg_type == "TRANSPARENT":
        findings.append("Background: fully transparent (PASS)")
    else:
        passed = False
        findings.append(
            f"Background: non-compliant -- {semi} semi-transparent pixel(s) found "
            f"(alpha must be 0 or 255 only) (FAIL)"
        )

    return passed, "; ".join(findings)


def _check_outline_icon(v: Dict[str, str]) -> Tuple[bool, str]:
    """TC-1140.4.1.2.2 — Combined check for outline icon:
      1. Must be exactly 32x32 pixels.
      2. Must be white with a transparent background OR transparent with a white background.
      3. Must not have any extra padding around the symbol.
    """
    w            = v.get("OUTLINE_WIDTH", "")
    h            = v.get("OUTLINE_HEIGHT", "")
    color_type   = v.get("OUTLINE_COLOR_TYPE", "")
    color_note   = v.get("OUTLINE_COLOR_TYPE_NOTE", "")
    padding_st   = v.get("OUTLINE_PADDING_STATUS", "")
    padding_info = v.get("OUTLINE_PADDING_INFO", "")
    semi         = v.get("OUTLINE_SEMI_TRANSPARENT_COUNT", "0")

    findings: List[str] = []
    passed = True

    # --- Dimension check ---
    if not w or not h:
        passed = False
        findings.append("Dimensions unavailable (SMP data missing)")
    elif w == "32" and h == "32":
        findings.append(f"Dimensions: {w}x{h} px (PASS)")
    else:
        passed = False
        findings.append(f"Dimensions: {w}x{h} px -- expected 32x32 px (FAIL)")

    # --- Color / background check ---
    if not color_type:
        passed = False
        findings.append(
            "Color type unavailable (Pillow not installed or SMP data missing)"
        )
    elif color_type == "WHITE_ON_TRANSPARENT":
        note = f" ({color_note})" if color_note else ""
        findings.append(f"Color: white symbol on transparent background{note} (PASS)")
    elif color_type == "TRANSPARENT_ON_WHITE":
        note = f" ({color_note})" if color_note else ""
        findings.append(f"Color: transparent symbol on white background{note} (PASS)")
    else:
        passed = False
        findings.append(
            f"Color: non-compliant -- {semi} semi-transparent pixel(s) detected; "
            f"outline icon must be white-on-transparent or transparent-on-white (FAIL)"
        )

    # --- Padding check ---
    if padding_st == "PASS":
        findings.append(f"Padding: none detected ({padding_info}) (PASS)")
    elif padding_st == "FAIL":
        passed = False
        findings.append(f"Padding: extra padding detected -- {padding_info} (FAIL)")
    elif padding_st == "UNKNOWN" and padding_info:
        findings.append(f"Padding: {padding_info} (could not verify)")

    return passed, "; ".join(findings)


def _check_icons_manifest(v: Dict[str, str]) -> Tuple[bool, str]:
    """TC-1140.4.1.2.4 — App package must contain both color and outline icon
    entries in the manifest.json 'icons' section.
    """
    has_color   = v.get("MANIFEST_HAS_COLOR_ICON", "")
    has_outline = v.get("MANIFEST_HAS_OUTLINE_ICON", "")
    color_val   = v.get("MANIFEST_COLOR_ICON_VALUE", "")
    outline_val = v.get("MANIFEST_OUTLINE_ICON_VALUE", "")

    findings: List[str] = []
    passed = True

    if has_color == "MISSING" or has_outline == "MISSING":
        return False, "manifest.json not found in the app package zip (FAIL)"

    if has_color == "UNKNOWN" or has_outline == "UNKNOWN":
        return False, "manifest.json could not be parsed (FAIL)"

    if has_color == "YES":
        findings.append(f'icons.color = "{color_val}" (PASS)')
    else:
        passed = False
        findings.append('icons.color entry missing from manifest.json (FAIL)')

    if has_outline == "YES":
        findings.append(f'icons.outline = "{outline_val}" (PASS)')
    else:
        passed = False
        findings.append('icons.outline entry missing from manifest.json (FAIL)')

    return passed, "; ".join(findings)


# Master list of icon-related test cases (extend freely)
ICON_TEST_CASES: List[dict] = [
    _tc(
        "1140.4.1.2.1",
        "Incorrect dimensions of color icon",
        _check_color_icon,
        recommendation=(
            "The color version of your icon must be 192x192 pixels. Your icon symbol can be "
            "any color or colors, but it must sit on a solid or fully transparent square "
            "background. Every pixel's alpha must be either 0 (fully transparent) or 255 "
            "(fully opaque) -- no semi-transparent pixels allowed. "
            "Re-export as a 192x192 square PNG and resubmit the package."
        ),
    ),
    _tc(
        "1140.4.1.2.2",
        "Outline icon not transparent",
        _check_outline_icon,
        recommendation=(
            "The outline icon must be exactly 32x32 pixels. It can be white with a transparent "
            "background or transparent with a white background -- no other colors or "
            "semi-transparent pixels are allowed. The icon symbol must not have any extra "
            "padding around it; the content should fill the canvas without excessive surrounding "
            "empty space. Re-export as a 32x32 square PNG and resubmit the package."
        ),
    ),
    _tc(
        "1140.4.1.2.4",
        "App package does not contain both icons",
        _check_icons_manifest,
        recommendation=(
            "The app package (.zip) manifest.json must declare both icons under the 'icons' "
            "section: \"color\" (pointing to the color PNG) and \"outline\" (pointing to the "
            "outline PNG). Add the missing entry to manifest.json and resubmit the package."
        ),
    ),
]


def run_icon_test_cases(variables: Dict[str, str]) -> List[dict]:
    """Run all ICON_TEST_CASES against the resolved variables dict.

    Returns a list of result dicts:
      {id, title, result: "PASS"|"FAIL"|"SKIP", reason, recommendation}
    """
    results: List[dict] = []
    for tc in ICON_TEST_CASES:
        try:
            passed, reason = tc["check"](variables)
            results.append({
                "id": tc["id"],
                "title": tc["title"],
                "result": "PASS" if passed else "FAIL",
                "reason": reason,
                # recommendation only included on FAIL
                "recommendation": tc["recommendation"] if not passed else "",
            })
        except Exception as exc:
            results.append({
                "id": tc["id"],
                "title": tc["title"],
                "result": "SKIP",
                "reason": f"Error during evaluation: {exc}",
                "recommendation": tc["recommendation"],
            })
    return results


def print_test_report(results: List[dict], app_name: str = "", validation_id: str = "") -> None:
    """Print a formatted test report to stdout."""
    header = "Icon Test Case Report"
    if app_name:
        header += f" -- {app_name}"
    if validation_id:
        header += f" (ID: {validation_id})"

    print(f"\n{'='*60}")
    print(header)
    print(f"{'='*60}")

    pass_count = sum(1 for r in results if r["result"] == "PASS")
    fail_count = sum(1 for r in results if r["result"] == "FAIL")
    skip_count = sum(1 for r in results if r["result"] == "SKIP")

    for r in results:
        symbol = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}.get(r["result"], "[?]")
        print(f"\n  {symbol} TC-{r['id']}: {r['title']}")
        print(f"      Result         : {r['result']}")
        print(f"      Detail         : {r['reason']}")
        if r.get("recommendation"):
            print(f"      Recommendation : {r['recommendation']}")

    print(f"\n{'─'*60}")
    print(f"  Total: {len(results)}  |  PASS: {pass_count}  |  FAIL: {fail_count}  |  SKIP: {skip_count}")
    print(f"{'='*60}\n")


def save_test_report(results: List[dict], validation_id: str,
                     app_name: str = "", output_dir: str = "output") -> str:
    """Save the test report as a JSON file and return the path.

    Each test case entry contains:
      id, title, result (PASS/FAIL/SKIP), reason, recommendation (only on FAIL/SKIP)
    
    If app_name is provided, report is saved to output/{app_name}/IconReport_*.json.
    """
    # Build clean per-TC entries: omit empty recommendation on PASS
    test_cases_out = []
    for r in results:
        entry: Dict[str, str] = {
            "tc_id": r["id"],
            "title": r["title"],
            "result": r["result"],
        }
        if r["result"] != "PASS" and r.get("recommendation"):
            entry["recommendation"] = r["recommendation"]
        test_cases_out.append(entry)

    report = {
        "validation_id": validation_id,
        "app_name": app_name,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r["result"] == "PASS"),
            "fail": sum(1 for r in results if r["result"] == "FAIL"),
            "skip": sum(1 for r in results if r["result"] == "SKIP"),
        },
        "test_cases": test_cases_out,
    }
    
    # If app_name provided, organize into app-specific folder (sanitized for Windows)
    if app_name:
        safe_app_name = sanitize_app_name(app_name)
        report_dir = os.path.join(output_dir, safe_app_name)
    else:
        report_dir = output_dir
    
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"IconReport_{validation_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"Test report saved to: {os.path.abspath(path)}")
    return path


def save_html_report(results: List[dict], validation_id: str,
                     app_name: str = "", output_dir: str = "output") -> str:
    """Generate and save a self-contained HTML test report.
    
    If app_name is provided, report is saved to output/{app_name}/IconReport_*.html.
    """
    import html as _html
    from datetime import datetime

    pass_count = sum(1 for r in results if r["result"] == "PASS")
    fail_count = sum(1 for r in results if r["result"] == "FAIL")
    skip_count = sum(1 for r in results if r["result"] == "SKIP")
    total = len(results)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build table rows
    rows_html = ""
    for r in results:
        result = r["result"]
        badge_class = {"PASS": "badge-pass", "FAIL": "badge-fail", "SKIP": "badge-skip"}.get(result, "")
        rec = _html.escape(r.get("recommendation", ""))
        rec_cell = f'<span class="rec">{rec}</span>' if rec else '<span class="na">N/A</span>'
        rows_html += f"""
        <tr class="row-{result.lower()}">
          <td><code>TC-{_html.escape(r['id'])}</code></td>
          <td>{_html.escape(r['title'])}</td>
          <td>{_html.escape(r['reason'])}</td>
          <td>{rec_cell}</td>
          <td><span class="badge {badge_class}">{result}</span></td>
        </tr>"""

    # Summary bar percentages
    pass_pct = int(pass_count / total * 100) if total else 0
    fail_pct = int(fail_count / total * 100) if total else 0

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Icon Test Report -- {_html.escape(app_name or validation_id)}</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f4f6f9; color: #222; }}
    header {{
      background: linear-gradient(135deg, #0078d4 0%, #005a9e 100%);
      color: #fff; padding: 28px 40px;
    }}
    header h1 {{ font-size: 1.6rem; font-weight: 600; }}
    header p  {{ font-size: 0.9rem; opacity: 0.85; margin-top: 4px; }}
    .container {{ max-width: 1100px; margin: 30px auto; padding: 0 24px 60px; }}
    .summary-cards {{
      display: flex; gap: 16px; margin-bottom: 28px; flex-wrap: wrap;
    }}
    .card {{
      flex: 1; min-width: 130px; background: #fff;
      border-radius: 10px; padding: 20px 24px;
      box-shadow: 0 2px 8px rgba(0,0,0,.08);
      text-align: center;
    }}
    .card .num {{ font-size: 2.2rem; font-weight: 700; }}
    .card .lbl {{ font-size: 0.8rem; color: #666; margin-top: 4px; text-transform: uppercase; letter-spacing: .05em; }}
    .card.pass .num {{ color: #107c10; }}
    .card.fail .num {{ color: #c50f1f; }}
    .card.skip .num {{ color: #7a7a7a; }}
    .card.total .num {{ color: #0078d4; }}
    .progress-bar {{ background: #e0e0e0; border-radius: 6px; height: 10px; margin-bottom: 28px; overflow: hidden; }}
    .progress-bar .seg-pass {{ background: #107c10; height: 100%; width: {pass_pct}%; float: left; }}
    .progress-bar .seg-fail {{ background: #c50f1f; height: 100%; width: {fail_pct}%; float: left; }}
    table {{
      width: 100%; border-collapse: collapse; background: #fff;
      border-radius: 10px; overflow: hidden;
      box-shadow: 0 2px 8px rgba(0,0,0,.08);
    }}
    thead th {{
      background: #0078d4; color: #fff; font-size: 0.82rem;
      text-transform: uppercase; letter-spacing: .05em;
      padding: 12px 16px; text-align: left;
    }}
    tbody td {{ padding: 14px 16px; font-size: 0.9rem; border-bottom: 1px solid #eee; vertical-align: top; }}
    tbody tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover {{ background: #f0f6ff; }}
    code {{ background: #eef2f7; border-radius: 4px; padding: 2px 6px; font-size: 0.82rem; }}
    .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px;
              font-size: 0.78rem; font-weight: 700; letter-spacing: .04em; }}
    .badge-pass {{ background: #dff6dd; color: #107c10; }}
    .badge-fail {{ background: #fde7e9; color: #c50f1f; }}
    .badge-skip {{ background: #f0f0f0; color: #555; }}
    .rec {{ color: #a80000; font-size: 0.85rem; }}
    .na  {{ color: #bbb; font-size: 0.85rem; }}
    footer {{ text-align: center; color: #aaa; font-size: 0.78rem; margin-top: 20px; }}
  </style>
</head>
<body>
  <header>
    <h1>Icon Test Case Report</h1>
    <p>{_html.escape(app_name)} &nbsp;|&nbsp; Validation ID: {_html.escape(validation_id)} &nbsp;|&nbsp; Generated: {generated}</p>
  </header>
  <div class="container">
    <div class="summary-cards">
      <div class="card total"><div class="num">{total}</div><div class="lbl">Total</div></div>
      <div class="card pass" ><div class="num">{pass_count}</div><div class="lbl">Pass</div></div>
      <div class="card fail" ><div class="num">{fail_count}</div><div class="lbl">Fail</div></div>
      <div class="card skip" ><div class="num">{skip_count}</div><div class="lbl">Skip</div></div>
    </div>
    <div class="progress-bar">
      <div class="seg-pass"></div>
      <div class="seg-fail"></div>
    </div>
    <table>
      <thead>
        <tr>
          <th>TC ID</th>
          <th>Title</th>
          <th>Detail</th>
          <th>Recommendation</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody>{rows_html}
      </tbody>
    </table>
  </div>
  <footer>Generated by Plan Editor Tool</footer>
</body>
</html>
"""

    # If app_name provided, organize into app-specific folder (sanitized for Windows)
    if app_name:
        safe_app_name = sanitize_app_name(app_name)
        report_dir = os.path.join(output_dir, safe_app_name)
    else:
        report_dir = output_dir

    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"IconReport_{validation_id}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"HTML report saved to:  {os.path.abspath(path)}")
    return path


# ── Magentic UI live-server import ──────────────────────────────────────

def upload_plan_to_server(
    plan_json: dict,
    user_id: str,
    server_url: str = "http://localhost:8081",
    dry_run: bool = False,
) -> Optional[dict]:
    """POST a generated plan JSON to a running Magentic UI server.

    The server endpoint is POST /api/plans/.
    The payload must include at minimum: task, steps, user_id.

    Args:
        plan_json: The fully-resolved plan dict (output of process_template).
        user_id:   The user e-mail / ID registered in Magentic UI.
        server_url: Base URL of the running server (default http://localhost:8081).
        dry_run:   If True, print the request but do NOT send it.

    Returns:
        The parsed JSON response from the server, or None on failure.
    """
    task = plan_json.get("task", "")
    steps = plan_json.get("steps", [])

    # Ensure each step has at least the required fields
    normalized_steps = []
    for step in steps:
        if isinstance(step, dict):
            normalized_steps.append({
                "title":      step.get("title", ""),
                "details":    step.get("details", ""),
                "enabled":    step.get("enabled", True),
                "open":       step.get("open", False),
                "agent_name": step.get("agent_name", ""),
            })

    payload = {
        "task":       task,
        "steps":      normalized_steps,
        "user_id":    user_id,
        "session_id": None,
    }

    endpoint = f"{server_url.rstrip('/')}/api/plans/"

    if dry_run:
        print(f"[DRY RUN] Would POST to {endpoint}")
        print(f"  task     : {task}")
        print(f"  steps    : {len(normalized_steps)}")
        print(f"  user_id  : {user_id}")
        return None

    print(f"  Uploading plan to Magentic UI server → {endpoint}")
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept":       "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
        if result.get("status"):
            plan_id = (result.get("data") or {}).get("id", "?")
            print(f"  Plan imported successfully (server plan_id={plan_id})")
        else:
            print(f"  Server returned status=false: {result.get('message', result)}")
        return result
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        print(f"  ERROR uploading plan (HTTP {exc.code}): {body_text}", file=sys.stderr)
    except Exception as exc:
        print(f"  ERROR uploading plan: {exc}", file=sys.stderr)
    return None


def upload_all_outputs(
    output_dir: str,
    user_id: str,
    server_url: str = "http://localhost:8081",
    dry_run: bool = False,
) -> None:
    """Upload every *.json plan file found in output_dir to the server.

    Skips IconReport_*.json and non-plan JSON files (those without a
    'task' key at the top level).
    """
    from pathlib import Path as _Path
    output_path = _Path(output_dir)
    if not output_path.exists():
        print(f"Output directory not found: {output_dir}")
        return

    plan_files = sorted(
        f for f in output_path.glob("*.json")
        if not f.name.startswith("IconReport_")
    )
    if not plan_files:
        print(f"No plan JSON files found in {output_dir}")
        return

    print(f"\n── Uploading {len(plan_files)} plan file(s) to {server_url} ──")
    uploaded = 0
    for plan_file in plan_files:
        try:
            with open(plan_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            print(f"  Skipping {plan_file.name} (could not parse: {exc})")
            continue

        if "task" not in data or "steps" not in data:
            print(f"  Skipping {plan_file.name} (no 'task'/'steps' keys — not a plan file)")
            continue

        print(f"\n  → {plan_file.name}")
        result = upload_plan_to_server(data, user_id, server_url, dry_run)
        if result is not None or dry_run:
            uploaded += 1

    print(f"\nUpload complete: {uploaded}/{len(plan_files)} file(s) processed.")


def upload_selected_files(
    files: List[str],
    user_id: str,
    server_url: str = "http://localhost:8081",
    dry_run: bool = False,
) -> None:
    """Upload a specific list of plan JSON file(s) to the server.

    Skips files that don't exist, can't be parsed, or lack 'task'/'steps' keys.
    """
    from pathlib import Path as _Path

    print(f"\n── Uploading {len(files)} selected plan file(s) to {server_url} ──")
    uploaded = 0
    for fp in files:
        p = _Path(fp)
        if not p.exists():
            print(f"  Skipping {fp} (not found)")
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:
            print(f"  Skipping {p.name} (could not parse: {exc})")
            continue

        if "task" not in data or "steps" not in data:
            print(f"  Skipping {p.name} (no 'task'/'steps' keys — not a plan file)")
            continue

        print(f"\n  → {p.name}")
        result = upload_plan_to_server(data, user_id, server_url, dry_run)
        if result is not None or dry_run:
            uploaded += 1

    print(f"\nUpload complete: {uploaded}/{len(files)} file(s) processed.")


def find_placeholders(text: str) -> List[str]:
    """Return sorted unique placeholder names found in text."""
    return sorted(set(PLACEHOLDER_RE.findall(text)))


def sanitize_app_name(app_name: str) -> str:
    """Sanitize app name for use as a Windows directory name.
    
    Replaces invalid characters (< > : \" / \\ | ? *) with underscores.
    """
    invalid_chars = r'<>:"/\|?*'
    for char in invalid_chars:
        app_name = app_name.replace(char, '_')
    return app_name


def replace_placeholders(text: str, variables: Dict[str, str]) -> str:
    """Replace all {{KEY}} placeholders with their values."""
    def _sub(match: re.Match) -> str:
        key = match.group(1)
        if key in variables:
            return variables[key]
        return match.group(0)  # leave unreplaced if not provided
    return PLACEHOLDER_RE.sub(_sub, text)


def load_template(path: str) -> dict:
    """Load a JSON template file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def process_template(template: dict, variables: Dict[str, str]) -> dict:
    """Deep-replace all string values in the template dict."""
    raw = json.dumps(template, ensure_ascii=False)
    filled = replace_placeholders(raw, variables)
    return json.loads(filled)


def save_output(data: dict, path: str, app_name: str = "") -> None:
    """Write the final JSON to a local file.
    
    If app_name is provided, the file is saved to output/{sanitized_app_name}/{filename}.
    Otherwise, uses the provided path as-is.
    """
    if app_name:
        # Sanitize app name for Windows compatibility and extract just the filename
        safe_app_name = sanitize_app_name(app_name)
        filename = os.path.basename(path)
        path = os.path.join("output", safe_app_name, filename)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"Output saved to: {os.path.abspath(path)}")


def cmd_list_vars(template_path: str) -> None:
    """List all placeholders found in a template."""
    template = load_template(template_path)
    raw = json.dumps(template, ensure_ascii=False)
    placeholders = find_placeholders(raw)
    if not placeholders:
        print("No placeholders found in template.")
        return
    print(f"Placeholders in {template_path}:")
    for p in placeholders:
        default = DEFAULTS.get(p)
        suffix = f"  (default: {default})" if default else ""
        print(f"  {{{{  {p}  }}}}{suffix}")


def cmd_interactive(template_path: str, output_path: Optional[str],
                    ado_url: Optional[str] = None, pat: Optional[str] = None) -> None:
    """Interactively prompt for each placeholder value.
    If --ado-url is provided, auto-fills from ADO work item via Key Vault PAT."""
    template = load_template(template_path)
    raw = json.dumps(template, ensure_ascii=False)
    placeholders = find_placeholders(raw)

    if not placeholders:
        print("No placeholders found. Nothing to fill.")
        return

    # Pre-fill from ADO work item if URL provided
    ado_vars: Dict[str, str] = {}
    if ado_url:
        try:
            resolved_pat = resolve_pat(pat)
            ado_vars = fetch_and_extract(ado_url, resolved_pat)
        except Exception as e:
            print(f"  Warning: Could not process ADO URL: {e}")

    # Merge defaults + ADO vars to build pre-fill pool
    prefill: Dict[str, str] = {**DEFAULTS, **ado_vars}

    # Auto-fetch PNG dimensions from SMP if we have the required IDs
    smp_url = prefill.get("SMP_API_URL", "")
    product_id = prefill.get("PRODUCT_ID", "")
    submission_id = prefill.get("SUBMISSION_ID", "")
    if smp_url and product_id and submission_id:
        png_dims = fetch_png_dimensions_from_smp(
            smp_url, product_id, submission_id, pat
        )
        prefill.update(png_dims)
    else:
        missing_smp = [k for k, v in [
            ("SMP_API_URL", smp_url), ("PRODUCT_ID", product_id),
            ("SUBMISSION_ID", submission_id)
        ] if not v]
        if missing_smp:
            print(f"  Skipping SMP PNG fetch — missing: {missing_smp}")

    print(f"\nTemplate: {template_path}")
    print(f"Found {len(placeholders)} placeholder(s). Enter values:\n")

    variables: Dict[str, str] = {}
    for p in placeholders:
        # Priority: ADO-fetched/SMP-fetched > DEFAULTS
        default = prefill.get(p, "")
        prompt = f"  {p}"
        if default:
            prompt += f" [{default}]"
        prompt += ": "
        value = input(prompt).strip()
        variables[p] = value if value else default

    result = process_template(template, variables)

    # Auto-generate output path if not provided
    if not output_path:
        vid = variables.get("VALIDATION_ID", "plan")
        output_path = f"output/plan_{vid}.json"

    _app = variables.get("APP_NAME", "")
    save_output(result, output_path, app_name=_app)

    # Run icon test cases and print report
    tc_results = run_icon_test_cases(variables)
    print_test_report(tc_results,
                      app_name=_app,
                      validation_id=variables.get("VALIDATION_ID", ""))
    _vid = variables.get("VALIDATION_ID", "plan")
    save_test_report(tc_results, validation_id=_vid, app_name=_app)
    save_html_report(tc_results, validation_id=_vid, app_name=_app)

    print("\nPreview:")
    print(json.dumps(result, indent=2, ensure_ascii=False)[:1000])


def cmd_generate(template_path: str, var_pairs: List[str], output_path: str,
                 ado_url: Optional[str] = None, pat: Optional[str] = None) -> None:
    """Generate a plan from template + CLI variables."""
    template = load_template(template_path)

    # Start with defaults
    variables: Dict[str, str] = dict(DEFAULTS)

    # Overlay ADO-derived values if URL provided
    if ado_url:
        try:
            resolved_pat = resolve_pat(pat)
            ado_vars = fetch_and_extract(ado_url, resolved_pat)
            variables.update(ado_vars)
        except Exception as e:
            print(f"Warning: Could not process ADO URL: {e}", file=sys.stderr)

    # Auto-fetch PNG dimensions from SMP using resolved PRODUCT_ID / SUBMISSION_ID
    smp_url = variables.get("SMP_API_URL", "")
    product_id = variables.get("PRODUCT_ID", "")
    submission_id = variables.get("SUBMISSION_ID", "")
    if smp_url and product_id and submission_id:
        png_dims = fetch_png_dimensions_from_smp(
            smp_url, product_id, submission_id, pat
        )
        variables.update(png_dims)  # --var pairs below will still override if needed

    # Parse --var KEY=VALUE pairs (highest priority — override everything)
    for pair in var_pairs:
        if "=" not in pair:
            print(f"ERROR: Invalid --var format: '{pair}'. Use KEY=VALUE.", file=sys.stderr)
            sys.exit(1)
        key, value = pair.split("=", 1)
        variables[key.strip()] = value.strip()

    # Check for unfilled placeholders
    raw = json.dumps(template, ensure_ascii=False)
    all_keys = find_placeholders(raw)
    missing = [k for k in all_keys if k not in variables]
    if missing:
        print(f"WARNING: These placeholders have no value and will remain as-is: {missing}",
              file=sys.stderr)

    result = process_template(template, variables)
    _app = variables.get("APP_NAME", "")
    save_output(result, output_path, app_name=_app)

    # Run icon test cases and print/save report
    tc_results = run_icon_test_cases(variables)
    print_test_report(tc_results,
                      app_name=_app,
                      validation_id=variables.get("VALIDATION_ID", ""))
    _vid = variables.get("VALIDATION_ID", "plan")
    save_test_report(tc_results, validation_id=_vid, app_name=_app)
    save_html_report(tc_results, validation_id=_vid, app_name=_app)


def cmd_quick_generate(validation_id: str, pat: Optional[str] = None,
                       templates_dir: str = "templates") -> str:
    """Quick mode: just a validation ID → generate JSON for ALL templates.

    Constructs the ADO URL from defaults, fetches the work item, auto-fills
    VALIDATION_ID, APP_NAME, PRODUCT_ID, SUBMISSION_ID from ADO.
    Prompts for TEAMS_ID, TEAMS_PASSWORD (and confirms TEAMS_URL, SMP_API_URL defaults).
    Saves each output to output/<app_name>/<template_name>_<VALIDATION_ID>.json.
    
    Returns the APP_NAME for use in upload operations.
    """
    tdir = Path(templates_dir)
    templates = sorted(tdir.glob("*.json"))
    if not templates:
        print(f"No templates found in {tdir}/")
        sys.exit(1)

    # Construct ADO URL from defaults
    ado_url = f"{ADO_ORG_URL}/{ADO_PROJECT}/_workitems/edit/{validation_id}"
    print(f"\n{'='*60}")
    print(f"Quick Generate — Validation ID: {validation_id}")
    print(f"ADO URL: {ado_url}")
    print(f"{'='*60}\n")

    # Resolve PAT and fetch work item
    resolved_pat = resolve_pat(pat)
    ado_vars = fetch_and_extract(ado_url, resolved_pat)

    # Build variable map: defaults + ADO-derived
    variables: Dict[str, str] = dict(DEFAULTS)
    variables.update(ado_vars)

    # Prompt for values not available from ADO
    print("\n── Provide remaining values ──")
    for key, prompt_label in [
        ("TEAMS_URL", "Teams URL"),
        ("SMP_API_URL", "SMP API URL"),
        ("TEAMS_ID", "Teams login ID (email)"),
        ("TEAMS_PASSWORD", "Teams login password"),
    ]:
        current = variables.get(key, "")
        if current:
            val = input(f"  {prompt_label} [{current}]: ").strip()
            if val:
                variables[key] = val
        else:
            val = input(f"  {prompt_label}: ").strip()
            if val:
                variables[key] = val

    # Auto-fetch PNG dimensions from SMP API
    smp_url = variables.get("SMP_API_URL", "")
    product_id = variables.get("PRODUCT_ID", "")
    submission_id = variables.get("SUBMISSION_ID", "")
    if smp_url and product_id and submission_id:
        png_dims = fetch_png_dimensions_from_smp(
            smp_url, product_id, submission_id, resolved_pat
        )
        variables.update(png_dims)
    else:
        missing_smp = [k for k, v in [
            ("SMP_API_URL", smp_url), ("PRODUCT_ID", product_id),
            ("SUBMISSION_ID", submission_id)
        ] if not v]
        print(f"  Skipping SMP PNG fetch — missing: {missing_smp}")

    # Final summary
    print(f"\nAll placeholders:")
    for k, v in sorted(variables.items()):
        print(f"  {k} = {v}")
    unfilled_keys = set()
    for tpl_path in templates:
        raw = json.dumps(load_template(str(tpl_path)), ensure_ascii=False)
        for ph in find_placeholders(raw):
            if ph not in variables:
                unfilled_keys.add(ph)
    if unfilled_keys:
        print(f"  WARNING — still unfilled: {', '.join(sorted(unfilled_keys))}")

    # Run icon test cases once (shared across all templates)
    tc_results = run_icon_test_cases(variables)
    print_test_report(tc_results,
                      app_name=variables.get("APP_NAME", ""),
                      validation_id=validation_id)
    save_test_report(tc_results, validation_id=validation_id, app_name=variables.get("APP_NAME", ""))
    save_html_report(tc_results, validation_id=validation_id, app_name=variables.get("APP_NAME", ""))

    # Process every template
    app_name = variables.get("APP_NAME", "")
    safe_app_name = sanitize_app_name(app_name) if app_name else ""
    print(f"\nProcessing {len(templates)} template(s)...\n")
    for tpl_path in templates:
        template = load_template(str(tpl_path))
        result = process_template(template, variables)
        stem = tpl_path.stem  # e.g. "MetadataTC_1-15"
        out_path = f"output/{stem}_{validation_id}.json"
        save_output(result, out_path, app_name=app_name)

    print(f"\nDone! Generated {len(templates)} plan(s) in output/{safe_app_name}/ folder.")
    return safe_app_name


def cmd_report(ado_url_or_id: str, pat: Optional[str] = None) -> None:
    """Fetch from ADO + SMP and generate an icon test case report only.

    Accepts either a validation ID (e.g. "4941424") or a full ADO URL.
    If a validation ID is provided, constructs the ADO URL from defaults.
    No templates, no Teams credentials needed.
    Saves output/IconReport_<VALIDATION_ID>.json
    """
    # Detect if input is a URL or a validation ID
    if "http" in ado_url_or_id.lower():
        ado_url = ado_url_or_id
    else:
        # It's a validation ID — construct the URL
        validation_id = ado_url_or_id
        ado_url = f"{ADO_ORG_URL}/{ADO_PROJECT}/_workitems/edit/{validation_id}"
    
    resolved_pat = resolve_pat(pat)

    # Fetch ADO work item
    print(f"\nFetching ADO work item ...")
    ado_vars = fetch_and_extract(ado_url, resolved_pat)

    variables: Dict[str, str] = dict(DEFAULTS)
    variables.update(ado_vars)

    product_id = variables.get("PRODUCT_ID", "")
    submission_id = variables.get("SUBMISSION_ID", "")
    smp_url = variables.get("SMP_API_URL", "")

    if not product_id:
        print("ERROR: Could not extract PRODUCT_ID from ADO work item.", file=sys.stderr)
        sys.exit(1)

    # Fetch PNG data from SMP (requires both PRODUCT_ID and SUBMISSION_ID)
    if submission_id:
        png_vars = fetch_png_dimensions_from_smp(smp_url, product_id, submission_id, resolved_pat)
        variables.update(png_vars)
    else:
        print("  Warning: SUBMISSION_ID not found in ADO work item — skipping icon PNG analysis.")

    # Run and output report
    results = run_icon_test_cases(variables)
    app_name = variables.get("APP_NAME", "")
    print_test_report(results,
                      app_name=app_name,
                      validation_id=variables.get("VALIDATION_ID", ""))
    vid = variables.get("VALIDATION_ID", "report")
    save_test_report(results, validation_id=vid, app_name=app_name)
    save_html_report(results, validation_id=vid, app_name=app_name)


def list_templates(templates_dir: str = "templates") -> None:
    """List available templates."""
    tdir = Path(templates_dir)
    if not tdir.exists():
        print(f"Templates directory not found: {tdir}")
        return
    templates = list(tdir.glob("*.json"))
    if not templates:
        print("No templates found.")
        return
    print("Available templates:")
    for t in templates:
        try:
            data = json.loads(t.read_text(encoding="utf-8"))
            task = data.get("task", "(no task)")
            steps = len(data.get("steps", []))
            print(f"  {t.name}  — {task}  ({steps} steps)")
        except Exception:
            print(f"  {t.name}  (invalid JSON)")


def main() -> None:
    # Load environment variables from .env file if it exists
    load_env_file()
    
    parser = argparse.ArgumentParser(
        description="Plan Editor Tool — Generate Magentic UI plan JSON from templates",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Icon test case report only — using validation ID (no templates needed)
  python plan_editor.py --report 4941424

  # Icon test case report using full ADO URL
  python plan_editor.py --report "https://domoreexp.visualstudio.com/MSTeams/_workitems/edit/4941424"

  # Quick mode — just the validation ID (generates ALL templates + report)
  python plan_editor.py 4941424

  # Quick mode + auto-import into running Magentic UI server
  python plan_editor.py 4941424 --upload --upload-user me@contoso.com

  # Upload all JSON files from the output/ folder to the server (no generation)
  python plan_editor.py --upload --upload-user me@contoso.com --server-url http://localhost:8081

  # Upload only specific plan file(s) from the output/ folder
  python plan_editor.py --upload --upload-user me@contoso.com \\
      --upload-file output/MetadataTC_1-15_5366273.json \\
      --upload-file output/BotTestCases_1-15_5366273.json

  # Dry-run: see what would be posted without actually calling the server
  python plan_editor.py 4941424 --upload --upload-user me@contoso.com --dry-run

  # List available templates
  python plan_editor.py --list-templates

  # Show placeholders in a template
  python plan_editor.py --list-vars templates/MetadataTC_1-15.json

  # Interactive mode with ADO URL (auto-fills from work item via Key Vault PAT)
  python plan_editor.py --interactive templates/MetadataTC_1-15.json \\
      --ado-url "https://domoreexp.visualstudio.com/MSTeams/_workitems/edit/4941424"

  # CLI mode — ADO URL auto-fills APP_NAME, VALIDATION_ID, PRODUCT_ID, SUBMISSION_ID
  python plan_editor.py --template templates/MetadataTC_1-15.json \\
      --ado-url "https://domoreexp.visualstudio.com/MSTeams/_workitems/edit/4941424" \\
      --var TEAMS_ID=admin@M365x48062851.onmicrosoft.com \\
      --var TEAMS_PASSWORD="mypassword" \\
      --output output/plan_4941424.json \\
      --upload --upload-user me@contoso.com

PAT Resolution Order:
  1. --pat argument
  2. ADO_PAT environment variable (from .env or shell)
  3. Azure Key Vault (vault: kv-msteamsappcert-prod, secret: VSO-PAT)
     Requires: pip install azure-identity azure-keyvault-secrets
     Auth: az login (or Managed Identity in Azure)

Environment Variables (.env or shell):
  ADO_PAT               - Azure DevOps Personal Access Token
  ADO_ORG_URL           - ADO organization URL (default: https://domoreexp.visualstudio.com)
  ADO_PROJECT           - ADO project name (default: MSTeams)
  TEAMS_URL             - Teams URL for template placeholders
  SMP_API_URL           - SMP API endpoint for app package details
  MAGENTIC_SERVER_URL   - Magentic UI server URL (default: http://localhost:8081)
  MAGENTIC_USER_ID      - Default user email for uploads (skips --upload-user prompt)
  KEY_VAULT_URL         - Azure Key Vault endpoint (default: kv-msteamsappcert-prod)
  PAT_SECRET_NAME       - Key Vault secret name for PAT (default: VSO-PAT)

See .env.template for all configuration options.
        """,
    )

    parser.add_argument("validation_id", nargs="?", default=None,
                        help="ADO work item ID — quick mode: generates all templates automatically")
    parser.add_argument("--list-templates", action="store_true",
                        help="List available templates in the templates/ folder")
    parser.add_argument("--list-vars", metavar="TEMPLATE",
                        help="List all placeholders in a template file")
    parser.add_argument("--interactive", metavar="TEMPLATE",
                        help="Interactively fill a template")
    parser.add_argument("--template", "-t", metavar="FILE",
                        help="Template JSON file to process")
    parser.add_argument("--var", "-v", action="append", default=[],
                        metavar="KEY=VALUE",
                        help="Set a placeholder value (repeatable)")
    parser.add_argument("--output", "-o", metavar="FILE",
                        help="Output JSON file path (default: output/plan_<ID>.json)")
    parser.add_argument("--ado-url", metavar="URL",
                        help="ADO work item URL — auto-extracts VALIDATION_ID and fetches APP_NAME")
    parser.add_argument("--report", metavar="ID_OR_URL",
                        help="Generate icon test case report from validation ID or ADO URL (no templates needed)")
    parser.add_argument("--pat", metavar="TOKEN",
                        help="ADO PAT (optional — defaults to Key Vault retrieval)")

    # ── Live server import ───────────────────────────────────────────────
    parser.add_argument("--upload", action="store_true",
                        help="After generating plans, POST each output JSON to a running "
                             "Magentic UI server so it appears in the Plans panel instantly.")
    parser.add_argument("--upload-dir", metavar="DIR", default="output",
                        help="Directory of plan JSON files to upload (used with --upload "
                             "when no template is specified). Default: output")
    parser.add_argument("--upload-file", metavar="FILE", action="append", default=[],
                        help="Upload specific plan JSON file(s) instead of a whole "
                             "directory (repeatable). Use with --upload.")
    parser.add_argument("--server-url", metavar="URL", 
                        default=os.environ.get("MAGENTIC_SERVER_URL", "http://localhost:8081"),
                        help="Base URL of the running Magentic UI server. "
                             "Can also be set via MAGENTIC_SERVER_URL env var. "
                             "Default: http://localhost:8081")
    parser.add_argument("--upload-user", metavar="EMAIL",
                        help="User e-mail (user_id) registered in Magentic UI. "
                             "Required when --upload is used.")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --upload: print what would be sent without actually "
                             "calling the server.")

    args = parser.parse_args()

    # Change to script directory so relative paths work
    os.chdir(Path(__file__).parent)

    # ── Helper: validate upload args early ──────────────────────────────
    def _require_upload_user() -> str:
        uid = args.upload_user or os.environ.get("MAGENTIC_USER_ID", "")
        if not uid:
            print(
                "ERROR: --upload requires a user e-mail. "
                "Provide --upload-user <email> or set MAGENTIC_USER_ID env var.",
                file=sys.stderr,
            )
            sys.exit(1)
        return uid

    if args.list_templates:
        list_templates()
        return

    if args.list_vars:
        cmd_list_vars(args.list_vars)
        return

    if args.report:
        cmd_report(args.report, args.pat)
        return

    # ── Standalone upload mode: upload specific file(s) ─────────────────
    # e.g.  python plan_editor.py --upload --upload-user me@corp.com \
    #           --upload-file output/MetadataTC_1-15_5366273.json
    if args.upload and args.upload_file:
        uid = _require_upload_user()
        upload_selected_files(
            files=args.upload_file,
            user_id=uid,
            server_url=args.server_url,
            dry_run=args.dry_run,
        )
        return

    # ── Standalone upload mode: upload an existing output directory ──────
    # e.g.  python plan_editor.py --upload --upload-user me@corp.com
    if args.upload and not args.validation_id and not args.template and not args.interactive:
        uid = _require_upload_user()
        upload_all_outputs(
            output_dir=args.upload_dir,
            user_id=uid,
            server_url=args.server_url,
            dry_run=args.dry_run,
        )
        return

    # Quick mode: just a validation ID
    if args.validation_id and not args.interactive and not args.template:
        app_name = cmd_quick_generate(args.validation_id, args.pat)
        if args.upload:
            uid = _require_upload_user()
            # Upload from app-specific folder
            upload_dir = os.path.join("output", app_name) if app_name else "output"
            upload_all_outputs(
                output_dir=upload_dir,
                user_id=uid,
                server_url=args.server_url,
                dry_run=args.dry_run,
            )
        return

    if args.interactive:
        cmd_interactive(args.interactive, args.output, args.ado_url, args.pat)
        if args.upload and args.output:
            uid = _require_upload_user()
            try:
                with open(args.output, "r", encoding="utf-8") as fh:
                    plan_data = json.load(fh)
                upload_plan_to_server(plan_data, uid, args.server_url, args.dry_run)
            except Exception as exc:
                print(f"  WARNING: Could not upload {args.output}: {exc}", file=sys.stderr)
        return

    if args.template:
        if not args.output:
            # Auto-generate output name from VALIDATION_ID (from --var or --ado-url)
            vid = None
            for pair in args.var:
                if pair.startswith("VALIDATION_ID="):
                    vid = pair.split("=", 1)[1].strip()
                    break
            if not vid and args.ado_url:
                try:
                    _, _, vid = parse_ado_url(args.ado_url)
                except ValueError:
                    pass
            args.output = f"output/plan_{vid or 'output'}.json"
        cmd_generate(args.template, args.var, args.output, args.ado_url, args.pat)
        if args.upload:
            uid = _require_upload_user()
            try:
                with open(args.output, "r", encoding="utf-8") as fh:
                    plan_data = json.load(fh)
                upload_plan_to_server(plan_data, uid, args.server_url, args.dry_run)
            except Exception as exc:
                print(f"  WARNING: Could not upload {args.output}: {exc}", file=sys.stderr)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
