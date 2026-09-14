import base64
import csv
import html
import io
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
import qrcode
from qrcode.image.svg import SvgPathImage
from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)
APP_VERSION = "CSV-ROSTER-V3-LIVE-TOKEN-QR-GENERATOR"

# Atom endpoints retained from the original three scripts.
BASE_URL = "https://eagleapi.atom.com.mm/eagleapi/v1"
COMMON_HEADERS = {
    "User-Agent": "Eagle/9.31.1(1776) Huawei/36 Nothing(A142)",
    "Build-Type": "live",
    "Accept-Language": "en",
}
DEFAULT_MPIN = "5000"
DEFAULT_SEC_PWD = "@Atom979"
# Delay between POS loop iterations, not after the final POS.
TASK_WAIT_SECONDS = {"checkin": 5, "datapack": 8, "drcv": 20, "saleorder": 5, "qr_generate": 0}
DRCV_PRODUCTS = {
    "1K DRCV": {"code": "482000001", "price": 1000.0},
    "2K DRCV": {"code": "482000002", "price": 2000.0},
    "3K DRCV": {"code": "482000003", "price": 3000.0},
    "5K DRCV": {"code": "482000004", "price": 5000.0},
}
DATA_DIR = Path(os.environ.get("POS_DATA_DIR", Path.home() / ".pos_operations"))
ROSTER_FILE = DATA_DIR / "pos_accounts.json"
TOKEN_FILE = DATA_DIR / "token_cache.json"
DATA_LOCK = threading.RLock()
STOP_EVENTS: dict[str, threading.Event] = {}
QR_SESSIONS: dict[str, dict[str, Any]] = {}
QR_SESSION_LOCK = threading.RLock()


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass


def read_json(path: Path, fallback: Any) -> Any:
    ensure_data_dir()
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return fallback


def write_json(path: Path, value: Any) -> None:
    ensure_data_dir()
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def clean_phone(value: Any) -> str:
    """Keep the account phone number in the digits-only form expected by the API."""
    return re.sub(r"\D", "", str(value or "").strip())


def clean_record(raw: dict[str, Any], existing_id: str = "") -> dict[str, str]:
    return {
        "id": str(raw.get("id") or existing_id or uuid.uuid4().hex),
        "phone": clean_phone(raw.get("phone")),
        "mpin": str(raw.get("mpin") or DEFAULT_MPIN).strip(),
        "name": " ".join(str(raw.get("name") or "").strip().split()),
        "secondary_password": str(raw.get("secondary_password") or DEFAULT_SEC_PWD).strip(),
    }


def validate_record(record: dict[str, str]) -> Optional[str]:
    if not record["phone"]:
        return "POS phone number is required."
    if len(record["phone"]) < 8 or len(record["phone"]) > 15:
        return "POS phone number must contain 8–15 digits."
    if not record["mpin"]:
        return "MPIN is required."
    if not record["name"]:
        return "POS name is required."
    if not record["secondary_password"]:
        return "Secondary password is required."
    return None


# --------------------------- Shared POS roster ---------------------------

def load_roster() -> list[dict[str, str]]:
    with DATA_LOCK:
        stored = read_json(ROSTER_FILE, [])
        if not isinstance(stored, list):
            return []
        clean: list[dict[str, str]] = []
        for item in stored:
            if isinstance(item, dict):
                record = clean_record(item)
                if record["phone"]:
                    clean.append(record)
        return clean


def save_roster(records: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    final: list[dict[str, str]] = []
    seen_phones: set[str] = set()
    for raw in records:
        record = clean_record(raw)
        error = validate_record(record)
        if error:
            raise ValueError(error)
        if record["phone"] in seen_phones:
            raise ValueError(f"Duplicate POS phone: {record['phone']}")
        seen_phones.add(record["phone"])
        final.append(record)
    with DATA_LOCK:
        write_json(ROSTER_FILE, final)
    return final


def get_roster_records(ids: list[str]) -> list[dict[str, str]]:
    selected = set(ids)
    if not selected:
        return []
    return [record for record in load_roster() if record["id"] in selected]


def parse_pos_file(file_bytes: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Accept phone/mpin/name lines as requested, and normal comma-separated CSV files."""
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The file must be UTF-8 encoded.") from exc

    imported: list[dict[str, str]] = []
    problems: list[str] = []
    for line_number, original_line in enumerate(text.splitlines(), start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        lower = line.lower().replace(" ", "")
        if lower in {
            "phone/mpin/name",
            "phone/mpin/name/sec_pwd",
            "phone,mpin,name",
            "phone,mpin,name,secondarypassword",
            "phone,mpin,name,sec_pwd",
        }:
            continue

        if "/" in line:
            parts = [part.strip() for part in line.split("/")]
        else:
            try:
                parts = next(csv.reader([line], skipinitialspace=True))
                parts = [part.strip() for part in parts]
            except csv.Error:
                problems.append(f"Line {line_number}: cannot read this row.")
                continue
        if len(parts) < 3:
            problems.append(f"Line {line_number}: use phone/mpin/name.")
            continue

        record = clean_record({
            "phone": parts[0],
            "mpin": parts[1] or DEFAULT_MPIN,
            "name": parts[2],
            "secondary_password": parts[3] if len(parts) > 3 and parts[3] else DEFAULT_SEC_PWD,
        })
        error = validate_record(record)
        if error:
            problems.append(f"Line {line_number}: {error}")
        else:
            imported.append(record)
    if not imported and problems:
        raise ValueError("No valid POS rows were found. " + " ".join(problems[:3]))
    return imported, problems


# --------------------------- Token cache ---------------------------

def load_tokens() -> dict[str, dict[str, str]]:
    with DATA_LOCK:
        stored = read_json(TOKEN_FILE, {})
        return stored if isinstance(stored, dict) else {}


def save_tokens(tokens: dict[str, dict[str, str]]) -> None:
    with DATA_LOCK:
        write_json(TOKEN_FILE, tokens)


def invalidate_token(phone: str) -> None:
    with DATA_LOCK:
        tokens = load_tokens()
        tokens.pop(clean_phone(phone), None)
        save_tokens(tokens)


def login_and_cache(phone: str, mpin: str, secondary_password: str, role: str = "partner") -> Optional[str]:
    phone = clean_phone(phone)
    headers = {"Content-Type": "application/json", **COMMON_HEADERS}
    try:
        first = requests.post(
            f"{BASE_URL}/login",
            json={"loginType": role, "mpin": mpin, "msisdn": phone},
            headers=headers,
            timeout=20,
        )
        first.raise_for_status()
        second = requests.post(
            f"{BASE_URL}/users/secondary-password/check",
            json={"msisdn": phone, "network_type": "wifi", "password": secondary_password},
            headers=headers,
            timeout=20,
        )
        if second.status_code != 200:
            return None
        token = second.json().get("data", {}).get("login_data", {}).get("access_token")
        if not token:
            return None
        with DATA_LOCK:
            tokens = load_tokens()
            tokens[phone] = {"token": token, "updated_at": str(int(time.time()))}
            save_tokens(tokens)
        return token
    except (requests.RequestException, ValueError, OSError):
        return None


def get_token(phone: str, mpin: str, secondary_password: str, refresh: bool = False) -> Optional[str]:
    return get_token_with_source(phone, mpin, secondary_password, refresh)[0]


def get_token_with_source(phone: str, mpin: str, secondary_password: str, refresh: bool = False) -> tuple[Optional[str], str]:
    """Return (token, source) without exposing the token itself in the UI.

    CACHE means the saved token was reused. LOGIN means a new login was needed.
    REFRESH means a cached token had failed authentication and a new login was attempted.
    """
    phone = clean_phone(phone)
    if not refresh:
        token_record = load_tokens().get(phone, {})
        token = token_record.get("token") if isinstance(token_record, dict) else None
        if token:
            return token, "CACHE"
    token = login_and_cache(phone, mpin, secondary_password)
    return token, ("REFRESH" if refresh else "LOGIN")


def cached_token_age(phone: str) -> str:
    record = load_tokens().get(clean_phone(phone), {})
    try:
        updated = int(record.get("updated_at", "0"))
        if updated:
            seconds = max(0, int(time.time()) - updated)
            if seconds < 60:
                return f"{seconds}s ago"
            if seconds < 3600:
                return f"{seconds // 60}m ago"
            return f"{seconds // 3600}h ago"
    except (TypeError, ValueError):
        pass
    return "age unknown"


# --------------------------- API operations ---------------------------

def api_post(path: str, token: str, payload: dict[str, Any]):
    try:
        return requests.post(
            f"{BASE_URL}{path}",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", **COMMON_HEADERS},
            timeout=30,
        )
    except requests.RequestException:
        return None


def generate_qr(token: str, tranx_id: str):
    # The 45-second lifetime is sent to Atom API; the UI also starts its own countdown.
    return api_post("/generate-qr", token, {"latitude": "0.0", "longitude": "0.0", "timeout": "45", "tranx_id": tranx_id})


def _qr_candidate_score(key: str, value: str) -> int:
    key = key.lower().replace("-", "_")
    score = 0
    for word, points in (("qr", 100), ("code", 40), ("data", 35), ("payload", 30), ("string", 20), ("content", 20), ("value", 10), ("token", -80), ("access", -100)):
        if word in key:
            score += points
    if value.startswith(("http://", "https://", "otpauth://", "upi://", "data:")):
        score += 15
    if len(value) >= 8:
        score += 5
    return score


def extract_qr_payload(response: Any) -> tuple[Optional[str], str, str]:
    """Extract a scan payload from the generate-qr response.

    Returns (payload, kind, source_key). The exact Atom response schema is not
    guaranteed, so this deliberately supports common qr/data/payload fields
    recursively. It never treats access_token as QR data.
    """
    if response is None:
        return None, "none", ""
    try:
        body = response.json()
    except (ValueError, TypeError):
        body = response.text or ""
    candidates: list[tuple[int, str, str]] = []

    def visit(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                if isinstance(child, str):
                    text = child.strip()
                    if text and "access_token" not in child_path.lower() and "token" not in child_path.lower():
                        candidates.append((_qr_candidate_score(str(key), text), text, child_path))
                else:
                    visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")
        elif isinstance(value, str) and value.strip():
            candidates.append((_qr_candidate_score(path, value.strip()), value.strip(), path))

    visit(body)
    if not candidates:
        return None, "none", ""
    _, payload, source = max(candidates, key=lambda item: item[0])
    low = payload.lower()
    if low.startswith("data:image/"):
        kind = "image-data"
    elif low.startswith(("http://", "https://")) and any(x in low for x in ("qr", "barcode", "image")):
        kind = "image-url"
    else:
        kind = "text"
    return payload, kind, source


def qr_png_data_uri(payload: str) -> str:
    """Create a PNG QR image data URI. Requires the `qrcode` PyPI package."""
    image = qrcode.make(payload)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def qr_svg_data_uri(payload: str) -> str:
    """Create a dependency-light SVG QR image data URI."""
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    svg = buffer.getvalue().decode("utf-8")
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def check_in(delivery_token: str, tranx_id: str):
    return api_post("/check-in", delivery_token, {"latitude": "0.0", "longitude": "0.0", "tranx_id": tranx_id})


def send_feedback(delivery_token: str, tranx_id: str):
    return api_post("/check-in/feedback", delivery_token, {"feedback": "Visit Market", "tranx_id": tranx_id})


def sell_data_pack(token: str, pos: dict[str, str], customer_phone: str, amount: str):
    return api_post("/sale/eload", token, {"amount": amount, "mpin": pos["mpin"], "posMsisdn": pos["phone"], "msisdn": customer_phone})


def sell_drcv(token: str, pos: dict[str, str], customer_phone: str, market_price: str, product_code: str, product_name: str, quantity: str):
    return api_post("/drcv-tertiary-orders", token, {
        "customer_msisdn": customer_phone, "market_price": market_price, "mpin": pos["mpin"],
        "product_code": product_code, "product_name": product_name, "quantity": quantity, "type": "sms",
    })


def api_get(path: str, token: str):
    try:
        return requests.get(
            f"{BASE_URL}{path}",
            headers={"Authorization": f"Bearer {token}", **COMMON_HEADERS},
            timeout=30,
        )
    except requests.RequestException:
        return None


def delivery_pos_list(token: str):
    return api_get("/users/CSE/pos-list", token)


def parse_delivery_pos_records(response: Any) -> dict[str, dict[str, Any]]:
    """Map POS msisdn to the complete POS identity returned by Delivery POS list."""
    if response is None or response.status_code != 200:
        return {}
    try:
        body = response.json()
        rows = body.get("data", []) if isinstance(body, dict) else []
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            phone = clean_phone(row.get("msisdn"))
            try:
                user_id = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            code = str(row.get("code") or "").strip()
            if phone and user_id and code:
                result[phone] = {
                    "user_id": user_id,
                    "user_code": code,
                    "phone": phone,
                    "name": str(row.get("name") or "").strip(),
                    "role": str(row.get("role_code") or "POS").strip() or "POS",
                }
        return result
    except (ValueError, TypeError, AttributeError):
        return {}


def parse_delivery_pos_ids(response: Any) -> dict[str, int]:
    """Backward-compatible ID-only mapping used by Sale Order."""
    return {phone: int(row["user_id"]) for phone, row in parse_delivery_pos_records(response).items()}


def build_qr_payload(pos_identity: dict[str, Any], tranx_id: str) -> str:
    """Build the same JSON payload encoded by the original POS app QR."""
    return json.dumps({
        "expired_at": "45",
        "tranx_id": tranx_id,
        "user_code": str(pos_identity["user_code"]),
        "user_id": int(pos_identity["user_id"]),
        "user_msisdn": clean_phone(pos_identity["phone"]),
        "user_name": str(pos_identity.get("name") or ""),
        "user_role": str(pos_identity.get("role") or "POS"),
    }, separators=(",", ":"), ensure_ascii=False)


def validate_sale_order(token: str, pos_user_id: int, serial_no: str, quantity: int, order_type: str = "rcv"):
    return api_post("/sales-order/rcv-suk-validate", token, {
        "pos_user_id": pos_user_id, "quantity": quantity,
        "sale_type": "sales_order", "serial_no": serial_no, "type": order_type,
    })


def submit_sale_order(token: str, pos_user_id: int, serial_no: str, quantity: int, price: float, item_code: str, order_type: str = "rcv"):
    return api_post("/sales-order", token, {
        "is_qr_scanned": False, "pos_user_id": pos_user_id, "sale_type": "sales_order",
        "sales_order": [{
            "after_discounted_price": price, "discount_percentage": 0.0,
            "itemCode": item_code, "item_type": order_type, "mrp": price,
            "phone_numbers": [], "price": price, "quantity": quantity,
            "serial_no": serial_no, "sukType": "range",
        }],
    })


def auth_failed(response: Any) -> bool:
    return response is not None and response.status_code in (401, 403)


def response_status(response: Any) -> str:
    return "Network error" if response is None else ("OK" if response.status_code == 200 else f"HTTP {response.status_code}")


def esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


# --------------------------- User interface ---------------------------

HTML_PAGE = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>POS Operations</title><style>
*{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#f4f6f8;color:#17202a}.app{max-width:900px;margin:auto;padding:12px}.top{background:#fff;border:1px solid #e2e6ea;border-radius:16px;padding:14px;margin-bottom:10px}.title{font-size:20px;font-weight:800}.sub{font-size:12px;color:#718096;margin-top:3px}.tabs{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:12px}.tab{border:0;background:#eef1f4;border-radius:10px;padding:10px 4px;font-weight:700;font-size:12px}.tab.active{background:#17202a;color:#fff}.card{background:#fff;border:1px solid #e2e6ea;border-radius:16px;padding:12px;margin:10px 0}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.field{display:flex;flex-direction:column;gap:4px}.field.full{grid-column:1/-1}label{font-size:11px;color:#687585;font-weight:700}input,select{width:100%;border:1px solid #d8dde3;border-radius:10px;padding:10px;background:#fff;font-size:14px;outline:none}input:focus,select:focus{border-color:#65717f}.actions{display:flex;gap:7px;flex-wrap:wrap}.btn{border:0;border-radius:10px;padding:10px 12px;font-weight:750;font-size:13px;background:#edf0f3}.btn.primary{background:#17202a;color:#fff}.btn.danger{background:#fff0f0;color:#b42318}.small{font-size:11px;color:#718096}.tablewrap{overflow:auto;border:1px solid #e1e5e9;border-radius:12px}.posrow{display:grid;grid-template-columns:30px 1.3fr .65fr 1fr 1fr 40px;min-width:760px;align-items:center;border-bottom:1px solid #edf0f2;padding:7px}.poscards{display:block;max-height:72vh;overflow-y:auto;border:1px solid #e1e5e9;border-radius:13px}.poscard{display:grid;grid-template-columns:1.2fr 1fr 1fr 28px;gap:14px;align-items:center;border-bottom:1px solid #e5e8ec;padding:13px 14px;background:#fff}.poscard:last-child{border-bottom:0}.poscard .posname{font-weight:800;font-size:16px}.poscard .posphone{font-size:13px;color:#687585;margin-top:2px}.poscard label{display:flex;flex-direction:column;gap:4px;font-size:11px;color:#687585;font-weight:700}.poscard input{font-size:13px;padding:8px}.poscard .cardline{display:flex;gap:7px;align-items:center;margin:0}.poscard .cardline input[type=checkbox]{width:auto}.poscards .icon{background:transparent;border:0;font-size:18px}@media(max-width:650px){.poscard{grid-template-columns:1fr 1fr;gap:8px}.poscard .cardline{grid-column:1/-1}.poscard .posphone{grid-column:1/-1}}@media(max-width:560px){.poscards{grid-template-columns:1fr}}.posrow:last-child{border-bottom:0}.posrow.selected{background:#f0f3f5}.posrow input{padding:7px;border-radius:7px;font-size:12px}.head{font-size:10px;font-weight:800;color:#697586;background:#fafbfc}.icon{border:0;background:transparent;font-size:17px;padding:6px}.log{white-space:pre-wrap;background:#11161c;color:#d7e0e8;border-radius:12px;padding:10px;font:12px ui-monospace,SFMono-Regular,monospace;max-height:300px;overflow:auto}.log .ok{color:#79e2a6}.log .bad{color:#ff8f98}.log .info{color:#d7e0e8}.hidden{display:none!important}.modal{position:fixed;inset:0;background:#17202a88;display:flex;align-items:center;justify-content:center;padding:12px;z-index:10}.modal.hidden{display:none}.modalbox{background:#fff;border-radius:24px;width:min(860px,100%);height:65vh;max-height:65vh;overflow:auto;padding:16px;box-shadow:0 18px 60px #17202a44}.modalhead{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}.modalhead b{font-size:16px}.close{border:0;background:#eef1f4;border-radius:9px;padding:7px 11px;font-size:18px}.summary{border:1px solid #e1e5e9;border-radius:12px;overflow:hidden}.summaryrow{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px;border-bottom:1px solid #edf0f2;font-size:12px}.summaryrow:last-child{border-bottom:0}.summaryrow b{font-size:13px}.summaryrow span{color:#687585}@media(max-width:560px){.grid{grid-template-columns:1fr}.tabs{grid-template-columns:repeat(2,1fr)}.app{padding:8px}}
</style><style>
:root{--ink:#111827;--muted:#7b8494;--line:#e8ebf0;--soft:#f5f7fb;--blue:#315ee8;--blue2:#edf2ff}
body{background:#f7f8fc;color:var(--ink);font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.app{max-width:520px;padding:0 14px 28px}.top{margin:0 -14px 14px;padding:22px 16px 16px;border:0;border-radius:0 0 24px 24px;background:linear-gradient(145deg,#111827,#253b78);color:white;box-shadow:0 10px 28px #253b7826}.title{font-size:25px;letter-spacing:-.5px}.sub{color:#c7d2fe;margin-top:5px}.task-menu-toggle{width:100%;display:flex;align-items:center;gap:10px;margin-top:18px;padding:13px 14px;border:1px solid #ffffff33;border-radius:15px;background:#ffffff16;color:#fff;font-size:14px}.task-menu-toggle i{margin-left:auto;font-style:normal}.task-grid{display:grid;gap:9px;margin-top:9px}.task-grid:not(.open) .task-choice:not(.active){display:none}.task-choice{width:100%;display:flex;align-items:center;gap:11px;text-align:left;padding:12px;border:1px solid #ffffff22;border-radius:15px;background:#ffffff12;color:white;cursor:pointer;transition:.18s}.task-choice.active,.task-choice:hover{background:#fff;color:var(--ink);border-color:#fff;transform:translateY(-1px)}.task-choice span:nth-child(2){display:flex;flex-direction:column;gap:2px;flex:1}.task-choice small{font-size:11px;color:#aab5d4}.task-choice.active small,.task-choice:hover small{color:var(--muted)}.task-choice i{font-size:22px;font-style:normal;color:#aab5d4}.task-icon{width:33px;height:33px;display:grid;place-items:center;border-radius:11px;background:#ffffff1c;font-size:18px}.task-icon svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.task-choice.active .task-icon,.task-choice:hover .task-icon{background:var(--blue2);color:var(--blue)}.card{border:1px solid var(--line);border-radius:18px;box-shadow:0 5px 20px #16213d08;padding:14px}.btn{border-radius:12px}.primary-soft{background:var(--blue2);color:var(--blue)}.file-name{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px;align-self:center}.qrbox{margin:12px 0;padding:14px;text-align:center;background:white;border:1px solid var(--line);border-radius:18px;color:var(--ink)}.qrbox img{display:block;margin:0 auto 9px;border-radius:8px}.qrbox .small{color:var(--muted)}.log{border-radius:16px}.modalbox{border-radius:24px;height:65vh;max-height:65vh}.modalhead{position:sticky;top:0;background:inherit;z-index:2;padding-bottom:10px}.tabs{display:none!important}@media(max-width:560px){.grid{grid-template-columns:1fr}.actions{align-items:center}}
.theme-toggle{position:absolute;right:18px;top:20px;border:1px solid #ffffff55;background:#ffffff18;color:#fff;border-radius:12px;width:40px;height:36px;font-size:18px}.top{position:relative}body.dark{background:#0d1424;color:#edf2ff}body.dark .card,body.dark .modalbox,body.dark .qrbox,body.dark input,body.dark select,body.dark .poscards,body.dark .poscard,body.dark .summary,body.dark .summaryrow,body.dark .tablewrap{background:#151f33;color:#edf2ff;border-color:#2b3a56}body.dark .poscard{border-bottom-color:#2b3a56}body.dark .poscard.selected,body.dark .posrow.selected{background:#1d2b46}body.dark .head{background:#10192b;color:#aab8d1}body.dark .small,body.dark label,body.dark .file-name,body.dark .posphone,body.dark .summaryrow span{color:#aab8d1}body.dark .btn{background:#22324f;color:#eaf0ff}body.dark .btn.primary{background:#315ee8;color:#fff}body.dark .close{background:#22324f;color:#edf2ff}body.dark .log{background:#070b13}body.dark .modal{background:#030712cc}</style></head><body><main class="app"><section class="top"><div class="title">POS Operations</div><div class="sub">Fast POS operations</div><button class="theme-toggle" id="themeToggle" aria-label="Toggle theme">☾</button><button class="task-menu-toggle" id="taskMenuToggle"><span>☰</span><b>Tasks</b><i>⌄</i></button><div class="task-grid" id="taskGrid" aria-label="Tasks">
<button class="task-choice active" data-tab="qr_generate"><span class="task-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v14H4zM8 9h8M8 13h5"/></svg></span><span><b>QR Generate</b><small>Generate a QR for a POS</small></span><i>›</i></button>
<button class="task-choice" data-tab="checkin"><span class="task-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg></span><span><b>QR Check-in</b><small>Check in with Delivery</small></span><i>›</i></button>
<button class="task-choice" data-tab="datapack"><span class="task-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 5h14v14H5zM8 9h8M8 13h5"/></svg></span><span><b>Data Sale</b><small>Sell a data pack</small></span><i>›</i></button>
<button class="task-choice" data-tab="drcv"><span class="task-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 9-8 9-8-9 8-9Z"/></svg></span><span><b>DRCV Sale</b><small>Create a DRCV order</small></span><i>›</i></button>
<button class="task-choice" data-tab="saleorder"><span class="task-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M5 12h14M5 17h14"/></svg></span><span><b>Sale Order</b><small>Create a serial order</small></span><i>›</i></button>
</div></section>
<section class="card"><div class="actions"><input id="file" type="file" accept=".csv,.txt" hidden><button class="btn primary-soft" id="importBtn">⇧ Import POS file</button><span id="fileName" class="file-name">No file selected</span><button class="btn" id="addBtn">＋ POS</button></div></section>
<section class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px"><b>POS List</b><span class="small" id="count">0 total · 0 selected</span></div><div id="summary" class="summary"></div><div class="actions" style="margin-top:9px"><button class="btn" id="viewAllBtn">⌄ View all POS</button><button class="btn" id="selectAllBtn">✓ All</button><button class="btn" id="unselectAllBtn">□ None</button></div></section><div id="posModal" class="modal hidden"><div class="modalbox"><div class="modalhead"><b>All POS · <span id="pageLabel">All</span></b><button class="close" id="closeModal">×</button></div><div class="small" style="margin-bottom:9px">Manage all POS accounts, MPINs and secondary passwords.</div><div id="rows" class="poscards"></div></div></div><div id="addPosModal" class="modal hidden"><div class="modalbox addbox"><div class="modalhead"><b>Add POS</b><button class="close" id="closeAddPos">×</button></div><div class="grid"><div class="field full"><label for="newPosName">Name</label><input id="newPosName" autocomplete="off"></div><div class="field full"><label for="newPosPhone">Phone Number</label><input id="newPosPhone" inputmode="numeric" autocomplete="tel" placeholder="09xxxxxxxxx"></div><div class="field"><label for="newPosMpin">MPIN</label><input id="newPosMpin" type="password" inputmode="numeric" value="5000"></div><div class="field"><label for="newPosSecondary">Secondary Password</label><input id="newPosSecondary" type="password" value="@Atom979"></div></div><div class="actions" style="margin-top:14px"><button class="btn" id="cancelAddPos">Cancel</button><button class="btn primary" id="confirmAddPos">Add POS</button></div></div></div>
<section id="forms"></section><section class="card"><div class="actions"><button class="btn primary" id="runBtn" style="flex:1">▶ Run</button><button class="btn danger" id="stopBtn" disabled>■ Stop</button></div></section><section class="card"><div style="display:flex;justify-content:space-between;align-items:center"><b>Output</b><button class="btn" id="viewOutputBtn">⌁ View live output</button></div></section><div id="outputModal" class="modal hidden"><div class="modalbox"><div class="modalhead"><b>Live Output</b><div class="actions"><button class="btn primary-soft hidden" id="refreshQrBtn">↻ Refresh QR</button><button class="btn danger" id="modalStopBtn" disabled>■ Stop</button><button class="close" id="closeOutput">×</button></div></div><div id="log" class="log">Ready.</div></div></div></main>
<script>
const $=id=>document.getElementById(id);let records=[],activeTask='qr_generate';
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function render(){const box=$('rows'),summary=$('summary');summary.innerHTML='';if(!records.length){summary.innerHTML='<div class="small" style="padding:18px;text-align:center">Import a POS file to get started.</div>'}records.slice(0,2).forEach(r=>{const d=document.createElement('div');d.className='summaryrow';d.innerHTML=`<div><b>${esc(r.name||'Unnamed POS')}</b><br><span>${esc(r.phone)}</span></div><div><span>MPIN ${esc(r.mpin)}</span><br><span>${r.selected?'Selected':'Not selected'}</span></div>`;summary.appendChild(d)});renderPosList();$('count').textContent=records.length+' total · '+records.filter(x=>x.selected).length+' selected';}function renderPosList(){const box=$('rows');box.innerHTML='';records.forEach(r=>{const d=document.createElement('div');d.className='poscard '+(r.selected?'selected':'');d.dataset.id=r.id;d.innerHTML=`<div class="cardline"><input type="checkbox" class="sel" ${r.selected?'checked':''}><input class="name posname" value="${esc(r.name||'Unnamed POS')}" aria-label="POS name"></div><div class="posphone">${esc(r.phone)}</div><label>MPIN<input class="mpin" value="${esc(r.mpin)}"></label><label>Secondary password<input class="secondary" value="${esc(r.secondary_password||'@Atom979')}"></label><button class="icon del" title="Delete">×</button>`;box.appendChild(d)});$('pageLabel').textContent='All';}
$('rows').addEventListener('click',e=>{const row=e.target.closest('.poscard');if(!row)return;const r=records.find(x=>x.id===row.dataset.id);if(e.target.closest('.del')){records=records.filter(x=>x.id!==r.id);render();return}if(e.target.closest('.sel')){r.selected=e.target.checked;renderPosList();$('count').textContent=records.length+' total · '+records.filter(x=>x.selected).length+' selected'}});
$('rows').addEventListener('input',e=>{const row=e.target.closest('.poscard');if(!row)return;const r=records.find(x=>x.id===row.dataset.id);if(e.target.classList.contains('phone'))r.phone=e.target.value.replace(/\D/g,'');if(e.target.classList.contains('mpin'))r.mpin=e.target.value;if(e.target.classList.contains('name'))r.name=e.target.value;if(e.target.classList.contains('secondary'))r.secondary_password=e.target.value});
$('viewAllBtn').onclick=()=>{$('posModal').classList.remove('hidden');renderPosList()};$('closeModal').onclick=()=>$('posModal').classList.add('hidden');$('posModal').onclick=e=>{if(e.target.id==='posModal')$('posModal').classList.add('hidden')};$('selectAllBtn').onclick=()=>{records.forEach(x=>x.selected=true);render()};$('unselectAllBtn').onclick=()=>{records.forEach(x=>x.selected=false);render()};$('viewOutputBtn').onclick=()=>$('outputModal').classList.remove('hidden');$('closeOutput').onclick=()=>$('outputModal').classList.add('hidden');$('outputModal').onclick=e=>{if(e.target.id==='outputModal')$('outputModal').classList.add('hidden')};const nextPosName=()=>{let n=1;while(records.some(x=>x.name===`POS${n}`))n++;return `POS${n}`};const openAddPos=()=>{$('newPosName').value=nextPosName();$('newPosPhone').value='';$('newPosMpin').value='5000';$('newPosSecondary').value='@Atom979';$('addPosModal').classList.remove('hidden');setTimeout(()=>$('newPosName').focus(),0)};$('addBtn').onclick=openAddPos;$('closeAddPos').onclick=()=>$('addPosModal').classList.add('hidden');$('cancelAddPos').onclick=()=>$('addPosModal').classList.add('hidden');$('addPosModal').onclick=e=>{if(e.target.id==='addPosModal')$('addPosModal').classList.add('hidden')};$('confirmAddPos').onclick=()=>{const name=$('newPosName').value.trim()||nextPosName(),phone=$('newPosPhone').value.replace(/\D/g,''),mpin=$('newPosMpin').value.trim(),secondary_password=$('newPosSecondary').value.trim();if(!phone)return alert('Phone Number is required');if(phone.length<8||phone.length>15)return alert('Phone Number must contain 8–15 digits');if(!mpin)return alert('MPIN is required');if(!secondary_password)return alert('Secondary Password is required');if(records.some(x=>x.phone===phone))return alert('This Phone Number already exists');records.push({id:crypto.randomUUID(),phone,mpin,name,secondary_password,selected:false});$('addPosModal').classList.add('hidden');render();$('posModal').classList.remove('hidden');renderPosList()};
async function saveRoster(){for(const r of records){if(!r.phone)throw Error('Phone is required')}const x=await fetch('/api/pos',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({records:records.map(({selected,...r})=>r)})});const j=await x.json();if(!x.ok)throw Error(j.error||'Save failed');records=(j.records||[]).map((x,i)=>({...x,selected:records[i]?.selected??false}));render()}

$('importBtn').onclick=()=>$('file').click();$('file').onchange=async()=>{const f=$('file').files[0];if(!f)return;$('fileName').textContent=f.name;try{const fd=new FormData();fd.append('file',f);const x=await fetch('/api/pos/import',{method:'POST',body:fd});const j=await x.json();if(!x.ok)throw Error(j.error||'Import failed');records=(j.records||[]).map(x=>({...x,selected:true}));render();$('log').textContent='Imported '+records.length+' POS accounts — all selected.'}catch(e){alert('Import failed: '+e.message)}finally{$('file').value=''}};

function formHtml(){let s='';if(activeTask==='qr_generate')s=`<div class="grid"><div class="field"><label>POS phone</label><input id="qr_pos_phone" inputmode="numeric" placeholder="Uses the selected POS automatically"></div><div class="field"><label>POS MPIN</label><input id="qr_pos_mpin" placeholder="Uses the selected POS automatically"></div><div class="field full"><label>POS secondary password</label><input id="qr_pos_pwd" type="password" placeholder="Uses the selected POS automatically"></div><div class="field"><label>Delivery phone</label><input id="qr_delivery_phone" inputmode="numeric"></div><div class="field"><label>Delivery MPIN</label><input id="qr_delivery_mpin" value="1200"></div><div class="field full"><label>Delivery secondary password</label><input id="qr_delivery_pwd" type="password" value="@Atom979"></div></div>`;if(activeTask==='checkin')s=`<div class="grid"><div class="field"><label>Delivery phone</label><input id="delivery_phone"></div><div class="field"><label>Delivery MPIN</label><input id="delivery_mpin" value="1200"></div><div class="field full"><label>Delivery secondary password</label><input id="delivery_pwd" value="@Atom979" type="password"></div></div>`;if(activeTask==='datapack')s=`<div class="grid"><div class="field"><label>Customer phone</label><input id="customer_phone"></div><div class="field"><label>Amount</label><input id="amount" type="number"></div></div>`;if(activeTask==='drcv')s=`<div class="grid"><div class="field"><label>Customer number</label><input id="customer_phone"></div><div class="field"><label>Product</label><select id="product_name"><option value="">Select a product</option><option>1K DRCV</option><option>2K DRCV</option><option>3K DRCV</option><option>5K DRCV</option></select></div><div class="field"><label>Quantity</label><input id="quantity" type="number" value="1" min="1"></div><div class="field"><label>Code</label><input id="product_code" readonly placeholder="Auto"></div><div class="field"><label>Price</label><input id="market_price" readonly placeholder="Auto"></div></div>`;if(activeTask==='saleorder')s=`<div class="grid"><div class="field"><label>Starting serial</label><input id="sale_serial" inputmode="numeric"></div><div class="field"><label>Quantity / POS</label><input id="sale_quantity" type="number" value="1" min="1"></div><div class="field"><label>Delivery phone</label><input id="delivery_phone"></div><div class="field"><label>Delivery MPIN</label><input id="delivery_mpin" value="1200"></div><div class="field full"><label>Delivery secondary password</label><input id="delivery_pwd" value="@Atom979" type="password"></div></div>`;$('forms').innerHTML=`<section class="card"><b>${activeTask==='saleorder'?'SALE ORDER':activeTask==='qr_generate'?'POS QR GENERATE':activeTask.toUpperCase()}</b>${s}</section>`;if(activeTask==='qr_generate'){const selected=records.find(x=>x.selected);if(selected){$('qr_pos_phone').value=selected.phone;$('qr_pos_mpin').value=selected.mpin;$('qr_pos_pwd').value=selected.secondary_password||''}}if(activeTask==='drcv')$('product_name').onchange=e=>{const p={'1K DRCV':['482000001','1000'],'2K DRCV':['482000002','2000'],'3K DRCV':['482000003','3000'],'5K DRCV':['482000004','5000']}[e.target.value]||['',''];$('product_code').value=p[0];$('market_price').value=p[1]};}
$('themeToggle').onclick=()=>{document.body.classList.toggle('dark');$('themeToggle').textContent=document.body.classList.contains('dark')?'☀':'☾';localStorage.setItem('pos-theme',document.body.classList.contains('dark')?'dark':'light')};if(localStorage.getItem('pos-theme')==='dark'){document.body.classList.add('dark');$('themeToggle').textContent='☀'}$('taskMenuToggle').onclick=()=>{$('taskGrid').classList.toggle('open');$('taskMenuToggle').querySelector('i').textContent=$('taskGrid').classList.contains('open')?'⌃':'⌄'};document.querySelectorAll('.task-choice').forEach(b=>b.onclick=()=>{document.querySelectorAll('.task-choice').forEach(x=>x.classList.remove('active'));b.classList.add('active');activeTask=b.dataset.tab;formHtml();$('taskGrid').classList.remove('open');$('taskMenuToggle').querySelector('i').textContent='⌄'});
let currentRunId='',qrSessionId='';
$('runBtn').onclick=async()=>{try{if(activeTask!=='qr_generate' && !records.some(x=>x.selected))throw Error('Select at least one POS');await saveRoster();const fd=new FormData();const vals={task:activeTask,pos_ids:JSON.stringify(records.filter(x=>x.selected).map(x=>x.id)),qr_pos_phone:$('qr_pos_phone')?.value||'',qr_pos_mpin:$('qr_pos_mpin')?.value||'',qr_pos_password:$('qr_pos_pwd')?.value||'',qr_delivery_phone:$('qr_delivery_phone')?.value||'',qr_delivery_mpin:$('qr_delivery_mpin')?.value||'',qr_delivery_password:$('qr_delivery_pwd')?.value||'',delivery_phone:$('delivery_phone')?.value||'',delivery_mpin:$('delivery_mpin')?.value||'',delivery_password:$('delivery_pwd')?.value||'',customer_phone:$('customer_phone')?.value||'',amount:$('amount')?.value||'',quantity:$('quantity')?.value||'',product_name:$('product_name')?.value||'',product_code:$('product_code')?.value||'',market_price:$('market_price')?.value||'',sale_serial:$('sale_serial')?.value||'',sale_quantity:$('sale_quantity')?.value||''};Object.entries(vals).forEach(([k,v])=>fd.append(k,v));$('log').textContent='Starting...';$('outputModal').classList.remove('hidden');$('runBtn').disabled=true;$('stopBtn').disabled=false;$('modalStopBtn').disabled=false;const x=await fetch('/process',{method:'POST',body:fd});currentRunId=x.headers.get('X-Run-ID')||'';const reader=x.body.getReader(),decoder=new TextDecoder();let streamBuffer='';while(true){const {value,done}=await reader.read();if(done)break;const raw=decoder.decode(value,{stream:true});streamBuffer+=raw;const sessionMatch=streamBuffer.match(/data-qr-session=[\"']([^\"']+)[\"']/i);if(sessionMatch){qrSessionId=sessionMatch[1];$('refreshQrBtn').classList.remove('hidden')}const qrMatch=streamBuffer.match(/<div class='qrbox'[^>]*>([\s\S]*?)<\/div>/i);if(qrMatch){const qr=document.createElement('div');qr.className='qrbox';qr.innerHTML=qrMatch[1];$('log').appendChild(qr);streamBuffer=streamBuffer.replace(qrMatch[0],'');let left=45;const timer=qr.querySelector('#qr-countdown');if(timer){const tick=setInterval(()=>{left--;timer.textContent=left>0?(left+' seconds remaining — scan now'):'QR expired';if(left<=0)clearInterval(tick)},1000)}}const matches=[...streamBuffer.matchAll(/<div class='(info|ok|bad)'>([\s\S]*?)<\/div>/gi)];if(matches.length){for(const match of matches){const line=document.createElement('div');line.className=match[1];const holder=document.createElement('div');holder.innerHTML=match[2];line.textContent=holder.textContent;$('log').appendChild(line);streamBuffer=streamBuffer.replace(match[0],'')}}else if(!streamBuffer.includes('<div')){const text=streamBuffer.replace(/<style[\s\S]*?<\/style>/gi,'').replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim();if(text){$('log').appendChild(document.createTextNode(text+'\n'));streamBuffer=''}}$('log').scrollTop=$('log').scrollHeight}$('log').appendChild(document.createTextNode('\nFinished.'));$('log').scrollTop=$('log').scrollHeight}catch(e){$('log').textContent+=`\nError: ${e.message}`}finally{$('runBtn').disabled=false;$('stopBtn').disabled=true;$('modalStopBtn').disabled=true;currentRunId=''}};
$('refreshQrBtn').onclick=async()=>{if(!qrSessionId)return;const b=$('refreshQrBtn');b.disabled=true;b.textContent='Refreshing…';try{const r=await fetch('/api/qr/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:qrSessionId})});const j=await r.json();if(!r.ok)throw Error(j.error||'Refresh failed');const old=$('log').querySelector('.qrbox');if(old)old.remove();const qr=document.createElement('div');qr.className='qrbox';qr.innerHTML=`<img src="${j.image_uri}" alt="POS QR" style="width:280px;height:280px"><p class="small">New QR. Scan within 45 seconds.</p>`;$('log').appendChild(qr);let left=45;const t=setInterval(()=>{left--;const p=qr.querySelector('p');if(p)p.textContent=left>0?(left+' seconds remaining — scan now'):'QR expired';if(left<=0)clearInterval(t)},1000)}catch(e){$('log').appendChild(document.createTextNode('\nRefresh error: '+e.message))}finally{b.disabled=false;b.textContent='↻ Refresh QR'}};$('stopBtn').onclick=async()=>{if(!currentRunId){$('log').textContent+='\nStop requested; waiting for run ID...';return}await fetch('/stop/'+currentRunId,{method:'POST'});$('log').textContent+='\nStop signal sent.'};$('modalStopBtn').onclick=()=>$('stopBtn').click();formHtml();render();
</script></body></html>'''


# --------------------------- Routes ---------------------------

@app.get("/")
def index():
    return HTML_PAGE


@app.get("/api/pos")
def list_pos():
    # Visible roster is intentionally session/import driven. Previously saved
    # POS records are not sent to the browser on startup; import a CSV to show
    # a new working set. The backend still resolves selected IDs during the
    # same page session, and token_cache.json remains private.
    return jsonify(records=[])


@app.put("/api/pos")
def replace_pos():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("records"), list):
        return jsonify(error="Records must be a list."), 400
    try:
        return jsonify(records=save_roster(payload["records"]))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/api/pos/import")
def import_pos():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify(error="Please choose a CSV or TXT file."), 400
    try:
        imported, problems = parse_pos_file(uploaded.read())
        # A new CSV is the new visible working set. Do not merge it with
        # the previous visible roster. The separate token cache is untouched,
        # so previously used phone numbers and tokens remain available only
        # in the backend cache and never appear in this table.
        records = save_roster(imported)
        return jsonify(records=records, problems=problems, message=f"{len(imported)} POS row(s) imported and displayed. Previous visible rows were replaced.")
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.post("/process")
def process():
    run_id = uuid.uuid4().hex
    STOP_EVENTS[run_id] = threading.Event()
    task = request.form.get("task", "")
    try:
        pos_ids = json.loads(request.form.get("pos_ids", "[]"))
    except json.JSONDecodeError:
        pos_ids = []
    if task not in {"checkin", "datapack", "drcv", "saleorder", "qr_generate"} or not isinstance(pos_ids, list):
        return Response("Invalid request.", status=400)
    selected = get_roster_records([str(item) for item in pos_ids])
    if not selected and task != "qr_generate":
        return Response("No POS records selected.", status=400)
    if task == "qr_generate":
        # Allow one explicitly entered POS, while still preferring the selected roster record.
        entered_phone = clean_phone(request.form.get("qr_pos_phone", ""))
        if entered_phone:
            entered = next((record for record in load_roster() if record["phone"] == entered_phone), None)
            if entered:
                selected = [entered]
            else:
                selected = [{"id": uuid.uuid4().hex, "phone": entered_phone, "mpin": request.form.get("qr_pos_mpin", "").strip(), "name": entered_phone, "secondary_password": request.form.get("qr_pos_password", "").strip()}]
        if not selected[0]["phone"] or not selected[0]["mpin"] or not selected[0]["secondary_password"]:
            return Response("QR Generate requires POS phone, MPIN and secondary password.", status=400)

    customer_phone = clean_phone(request.form.get("customer_phone", ""))
    drcv_product_code = ""
    drcv_market_price = ""
    if task in {"datapack", "drcv"} and not customer_phone:
        return Response("Customer phone is required for sales.", status=400)
    if task == "datapack" and not request.form.get("amount", "").strip():
        return Response("Data pack amount is required.", status=400)
    if task == "drcv" and any(not request.form.get(field, "").strip() for field in ("quantity", "product_name")):
        return Response("DRCV product and quantity are required.", status=400)
    if task == "drcv":
        product_name = request.form.get("product_name", "").strip()
        product = DRCV_PRODUCTS.get(product_name)
        if product is None:
            return Response("Please choose a valid DRCV product.", status=400)
        # Code and price are taken from the catalog, not manually trusted from the browser.
        drcv_product_code = product["code"]
        drcv_market_price = str(product["price"])
    if task == "saleorder":
        try:
            serial_start = int(request.form.get("sale_serial", "").strip())
            sale_quantity = int(request.form.get("sale_quantity", "").strip())
        except ValueError:
            return Response("Sale Order serial number and quantity must be whole numbers.", status=400)
        if serial_start < 0 or sale_quantity < 1:
            return Response("Sale Order serial number must be non-negative and quantity must be at least 1.", status=400)

    delivery = {
        "phone": clean_phone(request.form.get("delivery_phone", "") or request.form.get("qr_delivery_phone", "")),
        "mpin": (request.form.get("delivery_mpin", "") or request.form.get("qr_delivery_mpin", "")).strip(),
        "secondary_password": (request.form.get("delivery_password", "") or request.form.get("qr_delivery_password", "")).strip(),
    }
    if task in {"checkin", "saleorder", "qr_generate"} and not all(delivery.values() if task != "qr_generate" else (clean_phone(request.form.get("qr_delivery_phone", "")), request.form.get("qr_delivery_mpin", "").strip(), request.form.get("qr_delivery_password", "").strip())):
        return Response("Delivery account details are required for this task.", status=400)

    def log(kind: str, text: str) -> str:
        return f"<div class='{kind}'>{text}</div>"

    def generator():
        labels = {"checkin": "QR Check-in", "datapack": "Data Pack Sale", "drcv": "DRCV Sale", "saleorder": "Sale Order", "qr_generate": "POS QR Generate"}
        yield """<!doctype html><html lang='my'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Processing</title><style>body{margin:0;padding:25px;background:#f4f7fc;color:#172033;font-family:system-ui,'Noto Sans Myanmar',sans-serif}.box{max-width:780px;margin:auto;background:white;border-radius:18px;padding:23px;box-shadow:0 15px 45px #17203315}.log{background:#172033;color:#e9f0ff;border-radius:12px;padding:16px;line-height:1.75}.info{color:#ffe595}.ok{color:#7ce7a8}.bad{color:#ff9fa6}a{display:inline-block;margin-top:16px;color:#315ed3;font-weight:700;text-decoration:none}</style><style>
:root{--ink:#111827;--muted:#7b8494;--line:#e8ebf0;--soft:#f5f7fb;--blue:#315ee8;--blue2:#edf2ff}
body{background:#f7f8fc;color:var(--ink);font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.app{max-width:520px;padding:0 14px 28px}.top{margin:0 -14px 14px;padding:22px 16px 16px;border:0;border-radius:0 0 24px 24px;background:linear-gradient(145deg,#111827,#253b78);color:white;box-shadow:0 10px 28px #253b7826}.title{font-size:25px;letter-spacing:-.5px}.sub{color:#c7d2fe;margin-top:5px}.task-menu-toggle{width:100%;display:flex;align-items:center;gap:10px;margin-top:18px;padding:13px 14px;border:1px solid #ffffff33;border-radius:15px;background:#ffffff16;color:#fff;font-size:14px}.task-menu-toggle i{margin-left:auto;font-style:normal}.task-grid{display:grid;gap:9px;margin-top:9px}.task-grid:not(.open) .task-choice:not(.active){display:none}.task-choice{width:100%;display:flex;align-items:center;gap:11px;text-align:left;padding:12px;border:1px solid #ffffff22;border-radius:15px;background:#ffffff12;color:white;cursor:pointer;transition:.18s}.task-choice.active,.task-choice:hover{background:#fff;color:var(--ink);border-color:#fff;transform:translateY(-1px)}.task-choice span:nth-child(2){display:flex;flex-direction:column;gap:2px;flex:1}.task-choice small{font-size:11px;color:#aab5d4}.task-choice.active small,.task-choice:hover small{color:var(--muted)}.task-choice i{font-size:22px;font-style:normal;color:#aab5d4}.task-icon{width:33px;height:33px;display:grid;place-items:center;border-radius:11px;background:#ffffff1c;font-size:18px}.task-icon svg{width:19px;height:19px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.task-choice.active .task-icon,.task-choice:hover .task-icon{background:var(--blue2);color:var(--blue)}.card{border:1px solid var(--line);border-radius:18px;box-shadow:0 5px 20px #16213d08;padding:14px}.btn{border-radius:12px}.primary-soft{background:var(--blue2);color:var(--blue)}.file-name{font-size:11px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:180px;align-self:center}.qrbox{margin:12px 0;padding:14px;text-align:center;background:white;border:1px solid var(--line);border-radius:18px;color:var(--ink)}.qrbox img{display:block;margin:0 auto 9px;border-radius:8px}.qrbox .small{color:var(--muted)}.log{border-radius:16px}.modalbox{border-radius:24px;height:65vh;max-height:65vh}.modalhead{position:sticky;top:0;background:inherit;z-index:2;padding-bottom:10px}.tabs{display:none!important}@media(max-width:560px){.grid{grid-template-columns:1fr}.actions{align-items:center}}
.theme-toggle{position:absolute;right:18px;top:20px;border:1px solid #ffffff55;background:#ffffff18;color:#fff;border-radius:12px;width:40px;height:36px;font-size:18px}.top{position:relative}body.dark{background:#0d1424;color:#edf2ff}body.dark .card,body.dark .modalbox,body.dark .qrbox,body.dark input,body.dark select{background:#151f33;color:#edf2ff;border-color:#2b3a56}body.dark .small,body.dark label,body.dark .file-name{color:#aab8d1}body.dark .btn{background:#22324f;color:#eaf0ff}body.dark .log{background:#070b13}</style></head><body><div class='box'><h2>Processing...</h2><div class='log'>"""
        yield log("info", f"Task: {esc(labels[task])} — {len(selected)} POS selected")

        delivery_token: Optional[str] = None
        delivery_pos_ids: dict[str, int] = {}
        if task == "qr_generate":
            pos = selected[0]
            yield log("info", f"Delivery account ({esc(delivery['phone'])}) Logging in...")
            delivery_token, delivery_source = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"])
            if not delivery_token:
                yield log("bad", "Delivery login failed.")
                yield "</div></body></html>"
                return
            yield log("info", "Reading POS user ID and user code from the Delivery POS list...")
            pos_list_response = delivery_pos_list(delivery_token)
            if auth_failed(pos_list_response):
                invalidate_token(delivery["phone"])
                delivery_token, _ = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"], refresh=True)
                pos_list_response = delivery_pos_list(delivery_token) if delivery_token else None
            pos_records = parse_delivery_pos_records(pos_list_response)
            identity = pos_records.get(pos["phone"])
            if not identity:
                yield log("bad", f"The Delivery POS list does not contain {esc(pos['phone'])} a matching user ID/code.")
                yield "</div></body></html>"
                return
            yield log("info", f"POS ID {identity['user_id']} / Code {esc(identity['user_code'])} found.")
            token, token_source = get_token_with_source(pos["phone"], pos["mpin"], pos["secondary_password"])
            if not token:
                yield log("bad", "POS login failed.")
                yield "</div></body></html>"
                return
            tranx_id = f"QR{int(time.time() * 1000)}1"
            yield log("info", f"Calling /generate-qr API. timeout=45s")
            result = generate_qr(token, tranx_id)
            if auth_failed(result):
                invalidate_token(pos["phone"])
                token, _ = get_token_with_source(pos["phone"], pos["mpin"], pos["secondary_password"], refresh=True)
                result = generate_qr(token, tranx_id) if token else None
            if result is None or result.status_code != 200:
                yield log("bad", f"QR generation failed ({esc(response_status(result))})")
                yield "</div></body></html>"
                return
            if not result.json().get("status", False) if result is not None else True:
                yield log("bad", "QR Generate API did not return success.")
                yield "</div></body></html>"
                return
            payload = build_qr_payload(identity, tranx_id)
            qr_session_id = uuid.uuid4().hex
            with QR_SESSION_LOCK:
                QR_SESSIONS[qr_session_id] = {"phone": pos["phone"], "mpin": pos["mpin"], "secondary_password": pos["secondary_password"], "token": token, "identity": identity}
            try:
                image_uri = qr_svg_data_uri(payload)
                yield f"<div class='ok'>QR generated successfully.</div><div class='qrbox' data-qr-session='{qr_session_id}'><h3 style='margin:2px 0 4px'>{esc(identity.get('name') or pos['name'])}</h3><p class='small' style='margin:0 0 10px'>{esc(identity['phone'])}</p><img src='{esc(image_uri)}' alt='POS QR' style='width:280px;height:280px;image-rendering:auto'><p class='small'>Scan within 45 seconds.</p><p class='small' id='qr-countdown'>00:45</p></div>"
            except Exception as exc:
                yield log("bad", f"Could not create the QR image: {esc(exc)}")
            yield "</div></body></html>"
            return
        if task in {"checkin", "saleorder"}:
            yield log("info", f"Checking the Delivery account token ({esc(delivery['phone'])})...")
            delivery_token, delivery_source = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"])
            yield log("info", f"[TOKEN {delivery_source}] Delivery token {'using cache (' + cached_token_age(delivery['phone']) + ')' if delivery_source == 'CACHE' else 'requesting a new token'}")
            if not delivery_token:
                yield log("bad", f"[TOKEN {delivery_source}] Delivery login failed.")
                yield "</div><a href='/'>← Back to home</a></div></body></html>"
                return
            if task == "saleorder":
                yield log("info", "Reading the POS list and POS IDs from the Delivery account...")
                pos_list_response = delivery_pos_list(delivery_token)
                if auth_failed(pos_list_response):
                    invalidate_token(delivery["phone"])
                    delivery_token, delivery_source = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"], refresh=True)
                    yield log("info", "[TOKEN REFRESH] requesting a new Delivery token for the POS list...")
                    pos_list_response = delivery_pos_list(delivery_token) if delivery_token else None
                delivery_pos_ids = parse_delivery_pos_ids(pos_list_response)
                yield log("info", f"Received {len(delivery_pos_ids)} POS records from the Delivery POS list.")
                if not delivery_pos_ids:
                    yield log("bad", "Delivery POS list unavailable. Check the Delivery account and POS-list permission.")
                    yield "</div><a href='/'>← Back to home</a></div></body></html>"
                    return

        wait_seconds = TASK_WAIT_SECONDS[task]
        for index, pos in enumerate(selected, start=1):
            if STOP_EVENTS[run_id].is_set():
                yield log("bad", "Stopped by user.")
                break
            if index > 1:
                yield log("info", f"Waiting {wait_seconds} seconds before the next POS...")
                time.sleep(wait_seconds)
                if STOP_EVENTS[run_id].is_set():
                    yield log("bad", "Stopped by user.")
                    break
            label = esc(f"{pos['name']} ({pos['phone']})")
            if task == "saleorder":
                pos_user_id = delivery_pos_ids.get(pos["phone"])
                if pos_user_id is None:
                    yield log("bad", f"[{index}] {label}: POS ID not found in the Delivery POS list.")
                    continue
                serial_no = str(serial_start + (index - 1) * sale_quantity)
                yield log("info", f"[{index}/{len(selected)}] {label}: POS ID {pos_user_id}, serial {serial_no}, quantity {sale_quantity} is being validated...")
                validation = validate_sale_order(delivery_token, pos_user_id, serial_no, sale_quantity)
                if auth_failed(validation):
                    invalidate_token(delivery["phone"])
                    delivery_token, delivery_source = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"], refresh=True)
                    yield log("info", f"[TOKEN REFRESH] {label}: requesting a new Delivery token for validation...")
                    validation = validate_sale_order(delivery_token, pos_user_id, serial_no, sale_quantity) if delivery_token else None
                if validation is None or validation.status_code != 200:
                    yield log("bad", f"[{index}] {label}: Sale Order validation failed ({esc(response_status(validation))})")
                    continue
                try:
                    validation_data = validation.json().get("data", {})
                    price = float(validation_data.get("price", validation_data.get("mrp", 0)))
                    item_code = str(validation_data.get("item_code", "")).strip()
                except (ValueError, TypeError, AttributeError):
                    price, item_code = 0.0, ""
                if price <= 0 or not item_code:
                    yield log("bad", f"[{index}] {label}: validation response is missing price/item_code.")
                    continue
                result = submit_sale_order(delivery_token, pos_user_id, serial_no, sale_quantity, price, item_code)
                if auth_failed(result):
                    invalidate_token(delivery["phone"])
                    delivery_token, delivery_source = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"], refresh=True)
                    yield log("info", f"[TOKEN REFRESH] {label}: requesting a new Delivery token for submission...")
                    result = submit_sale_order(delivery_token, pos_user_id, serial_no, sale_quantity, price, item_code) if delivery_token else None
                if result is not None and result.status_code == 200:
                    yield log("ok", f"[{index}] {label}: Sale order succeeded. serial={serial_no}, quantity={sale_quantity}, item={esc(item_code)}, price={price:g}")
                else:
                    yield log("bad", f"[{index}] {label}: Sale order failed ({esc(response_status(result))})")
                continue
            yield log("info", f"[{index}/{len(selected)}] {label}: Checking POS token...")
            token, token_source = get_token_with_source(pos["phone"], pos["mpin"], pos["secondary_password"])
            yield log("info", f"[{index}] [TOKEN {token_source}] {label}: {'using cached token (' + cached_token_age(pos['phone']) + ')' if token_source == 'CACHE' else 'requesting a new token'}")
            if not token:
                yield log("bad", f"[{index}] [TOKEN {token_source}] {label}: POS login failed.")
                continue

            if task == "checkin":
                tranx_id = f"QR{int(time.time() * 1000)}{index}"
                result = generate_qr(token, tranx_id)
                if auth_failed(result):
                    invalidate_token(pos["phone"])
                    token, token_source = get_token_with_source(pos["phone"], pos["mpin"], pos["secondary_password"], refresh=True)
                    yield log("info", f"[TOKEN REFRESH] {label}: cached token failed; requesting a new token...")
                    result = generate_qr(token, tranx_id) if token else None
                if result is None or result.status_code != 200:
                    yield log("bad", f"[{index}] {label}: QR generation failed ({esc(response_status(result))})")
                    continue
                result = check_in(delivery_token, tranx_id)
                if auth_failed(result):
                    invalidate_token(delivery["phone"])
                    delivery_token, delivery_source = get_token_with_source(delivery["phone"], delivery["mpin"], delivery["secondary_password"], refresh=True)
                    yield log("info", f"[TOKEN REFRESH] Delivery cached token failed; requesting a new token...")
                    result = check_in(delivery_token, tranx_id) if delivery_token else None
                feedback = send_feedback(delivery_token, tranx_id) if result is not None and result.status_code == 200 else None
                if result is not None and result.status_code == 200 and feedback is not None and feedback.status_code == 200:
                    yield log("ok", f"[{index}] {label}: QR check-in succeeded.")
                else:
                    yield log("bad", f"[{index}] {label}: Check-in failed ({esc(response_status(result))})")
            elif task == "datapack":
                result = sell_data_pack(token, pos, customer_phone, request.form["amount"].strip())
                if auth_failed(result):
                    invalidate_token(pos["phone"])
                    token, token_source = get_token_with_source(pos["phone"], pos["mpin"], pos["secondary_password"], refresh=True)
                    yield log("info", f"[TOKEN REFRESH] {label}: cached token failed; requesting a new token...")
                    result = sell_data_pack(token, pos, customer_phone, request.form["amount"].strip()) if token else None
                kind = "ok" if result is not None and result.status_code == 200 else "bad"
                text = "Data pack sale succeeded." if kind == "ok" else f"Data pack sale failed ({esc(response_status(result))})"
                yield log(kind, f"[{index}] {label}: {text}")
            else:
                result = sell_drcv(token, pos, customer_phone, drcv_market_price, drcv_product_code, product_name, request.form["quantity"].strip())
                if auth_failed(result):
                    invalidate_token(pos["phone"])
                    token, token_source = get_token_with_source(pos["phone"], pos["mpin"], pos["secondary_password"], refresh=True)
                    yield log("info", f"[TOKEN REFRESH] {label}: cached token failed; requesting a new token...")
                    result = sell_drcv(token, pos, customer_phone, drcv_market_price, drcv_product_code, product_name, request.form["quantity"].strip()) if token else None
                kind = "ok" if result is not None and result.status_code == 200 else "bad"
                text = "DRCV sale succeeded." if kind == "ok" else f"DRCV sale failed ({esc(response_status(result))})"
                yield log(kind, f"[{index}] {label}: {text}")
        yield "</div><a href='/'>← Back to home</a></div></body></html>"

    response = Response(stream_with_context(generator()), mimetype="text/html")
    response.headers["X-Run-ID"] = run_id
    return response


@app.post("/api/qr/refresh")
def refresh_qr():
    payload = request.get_json(silent=True) or {}
    session_id = str(payload.get("session_id", ""))
    with QR_SESSION_LOCK:
        session = QR_SESSIONS.get(session_id)
    if not session:
        return jsonify(error="QR session not found. Generate a new QR."), 404
    token = session.get("token")
    tranx_id = f"QR{int(time.time() * 1000)}R"
    result = generate_qr(token, tranx_id)
    if auth_failed(result):
        invalidate_token(session["phone"])
        token = login_and_cache(session["phone"], session["mpin"], session["secondary_password"])
        if not token:
            return jsonify(error="POS token refresh failed."), 401
        with QR_SESSION_LOCK:
            session["token"] = token
        result = generate_qr(token, tranx_id)
    if result is None or result.status_code != 200:
        return jsonify(error=f"QR generation failed ({response_status(result)})"), 502
    try:
        body = result.json()
    except (ValueError, TypeError):
        body = {}
    if not body.get("status", False):
        return jsonify(error="QR Generate API did not return success."), 502
    qr_payload = build_qr_payload(session["identity"], tranx_id)
    with QR_SESSION_LOCK:
        session["token"] = token
    return jsonify(ok=True, image_uri=qr_svg_data_uri(qr_payload), expires_in=45, tranx_id=tranx_id)


@app.post("/stop/<run_id>")
def stop_process(run_id: str):
    event = STOP_EVENTS.get(run_id)
    if event is None:
        return jsonify(ok=False, message="Run not found."), 404
    event.set()
    return jsonify(ok=True, message="Stop signal sent.")


if __name__ == "__main__":
    # PORT=5001 python combined_pos_app.py can be used if 5000 is already occupied.
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)
