import os
import random
import uuid
import time
import threading
import queue
import requests
import urllib3
import json
from datetime import datetime, date
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, Response, jsonify, render_template
from flask_cors import CORS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
CORS(app)

# ─── ADMIN KEY ────────────────────────────────────────────────────────────────
ADMIN_KEY = "Kepler2026"

# ─── VOUCHER ACCESS CODES ─────────────────────────────────────────────────────
# Maps voucher_batch_id -> list of valid access codes for that voucher
VOUCHER_ACCESS_CODES = {
    "ph0313":   ["ph0313n4"],
    "ph0313 4vc": ["ph0313n3", "ph0313n4", "ph0313n5", "ph0313n6"],
    "ph031381": ["ph031381n1", "ph031381n2", "ph031381n3"],
    "gm0pha":   ["gm0pha_a1", "gm0pha_a2", "gm0pha_a3"],
}

# ─── COIN COST PER VOUCHER ────────────────────────────────────────────────────
# All batches cost 1 Gold coin per use.
VOUCHER_COIN_COST = {
    "ph0313":     {"coin": "gold", "label": "ph0313 (79%)"},
    "ph0313 4vc": {"coin": "gold", "label": "ph0313 4vc"},
    "ph031381":   {"coin": "gold", "label": "ph0313 (81%)"},
    "gm0pha":     {"coin": "gold", "label": "gm0pha"},
}

# ─── ALLOWED CODES PER BATCH (server-side enforcement) ────────────────────────
# Non-admin users may ONLY submit codes from this list for their active batch.
# ph0313 (default) accepts all 8 codes — merges ph0313 (4) + ph0313 4vc (4).
# pkgId is enforced server-side to prevent tampering.
VALID_BATCH_CODES = {
    #           ── ph0313 default: 8 codes ──
    "ph0313":     ["ph0313n1", "ph0313n2", "ph0313n3", "ph0313n4",
                   "ph0313n5", "ph0313n6", "ph0313n9", "ph0313n10"],
    #           ── ph0313 4vc: its own 4 codes ──
    "ph0313 4vc": ["ph0313n3",  "ph0313n4",  "ph0313n5",  "ph0313n6"],
    #           ── ph031381 (81%): 4 codes ──
    "ph031381":   ["ph0313n5", "ph0313n10", "ph0313n15", "ph0313n19"],
    #           ── gm0pha: one "11" code per group (all same PHP60 value) ──
    "gm0pha":     ["gm0pha11", "gm0phi11", "gm365phi11", "gm365pha11"],
}

# ─── PACKAGE IDs PER BATCH ────────────────────────────────────────────────────
# SHEIN rotates these — update when claims stop working. The Termux/offline
# script's COUPON_PKG_ID is the source of truth for the current valid ID.
BATCH_PKG_IDS = {
    "ph0313":     "17145850",
    "ph0313 4vc": "17145850",
    "ph031381":   "17145850",
    "gm0pha":     "17131185",
}

# ─── USER DATABASE (file-backed JSON) ─────────────────────────────────────────
USERS_FILE       = os.environ.get("USERS_FILE",      "users.json")
DAILY_COIN_FILE  = os.environ.get("DAILY_COIN_FILE", "daily_coins.json")
CLAIM_LOG_FILE   = os.environ.get("CLAIM_LOG_FILE",  "claim_log.json")
_users_lock      = threading.Lock()
_daily_lock      = threading.Lock()
_log_lock        = threading.Lock()


def _load_users():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def get_user(access_key):
    with _users_lock:
        return _load_users().get(access_key)


def save_user(access_key, user_data):
    with _users_lock:
        users = _load_users()
        users[access_key] = user_data
        _save_users(users)


def all_users():
    with _users_lock:
        return _load_users()


def _load_daily_config():
    if os.path.exists(DAILY_COIN_FILE):
        try:
            with open(DAILY_COIN_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"bronze": 0, "silver": 0, "gold": 1}


def _save_daily_config(cfg):
    with open(DAILY_COIN_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _apply_daily_coins():
    today_str = date.today().isoformat()
    cfg = _load_daily_config()
    with _users_lock:
        users = _load_users()
        changed = False
        for key, user in users.items():
            if user.get("last_daily", "") != today_str:
                user.setdefault("coins", {"bronze": 0, "silver": 0, "gold": 0})
                for ct in ("bronze", "silver", "gold"):
                    amt = cfg.get(ct, 0)
                    if amt > 0:
                        user["coins"][ct] = user["coins"].get(ct, 0) + amt
                user["last_daily"] = today_str
                changed = True
        if changed:
            _save_users(users)


def _daily_coin_scheduler():
    while True:
        try:
            _apply_daily_coins()
        except Exception as e:
            print(f"[daily-coins] Error: {e}")
        time.sleep(3600)


threading.Thread(target=_daily_coin_scheduler, daemon=True).start()


# ─── CLAIM LOG ───────────────────────────────────────────────────────────────
def _load_logs():
    if os.path.exists(CLAIM_LOG_FILE):
        try:
            with open(CLAIM_LOG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_logs(logs):
    with open(CLAIM_LOG_FILE, "w") as f:
        json.dump(logs, f, indent=2)


def _append_log(entry):
    """Thread-safe log append. Keeps newest 200 entries."""
    with _log_lock:
        logs = _load_logs()
        logs.insert(0, entry)
        if len(logs) > 200:
            logs = logs[:200]
        _save_logs(logs)


def _update_log(log_id, result, detail=""):
    """Update an existing log entry by log_id."""
    with _log_lock:
        logs = _load_logs()
        for entry in logs:
            if entry.get("log_id") == log_id:
                entry["result"] = result
                entry["detail"] = detail
                entry["completed_at"] = datetime.now().isoformat()
                break
        _save_logs(logs)


# ─── GEMINI API KEY (file-backed; admin sets via /admin/set_gemini_key) ──────
GEMINI_KEY_FILE = os.environ.get("GEMINI_KEY_FILE", "gemini_key.json")
_gemini_lock = threading.Lock()


def _load_gemini_key_unlocked():
    if os.path.exists(GEMINI_KEY_FILE):
        try:
            with open(GEMINI_KEY_FILE, "r") as f:
                return (json.load(f) or {}).get("key", "")
        except Exception:
            pass
    return ""


def _save_gemini_key_unlocked(key):
    with open(GEMINI_KEY_FILE, "w") as f:
        json.dump({"key": key or ""}, f)


def get_gemini_key():
    with _gemini_lock:
        return _load_gemini_key_unlocked()


def set_gemini_key(key):
    with _gemini_lock:
        _save_gemini_key_unlocked(key)


# ─── NAME WHITELIST (controls which first names can claim group seats) ──────
NAME_WHITELIST_FILE = os.environ.get("NAME_WHITELIST_FILE", "name_whitelist.json")
_whitelist_lock = threading.Lock()
DEFAULT_WHITELIST = ["Caspe", "Cabardo", "Mondragon", "Dela Cerna", "Coyme"]


def _norm_name(name):
    """Lowercase + collapse whitespace so 'Dela Cerna', '  dela cerna ', and
    'DELA  CERNA' all compare equal."""
    return " ".join((name or "").strip().lower().split())


def _load_whitelist_unlocked():
    if os.path.exists(NAME_WHITELIST_FILE):
        try:
            with open(NAME_WHITELIST_FILE, "r") as f:
                data = json.load(f) or {}
                names = data.get("names", [])
                if isinstance(names, list):
                    return [str(n) for n in names if isinstance(n, (str,)) and n.strip()]
        except Exception:
            pass
    return list(DEFAULT_WHITELIST)


def _save_whitelist_unlocked(names):
    clean = []
    seen = set()
    for n in names or []:
        s = (n or "").strip()
        k = _norm_name(s)
        if not k or k in seen:
            continue
        seen.add(k)
        clean.append(s)
    with open(NAME_WHITELIST_FILE, "w") as f:
        json.dump({"names": clean}, f, indent=2)
    return clean


def get_whitelist():
    with _whitelist_lock:
        return _load_whitelist_unlocked()


def set_whitelist(names):
    with _whitelist_lock:
        return _save_whitelist_unlocked(names)


def _is_name_allowed(name):
    nk = _norm_name(name)
    if not nk:
        return False
    for w in get_whitelist():
        if _norm_name(w) == nk:
            return True
    return False


def _canonical_name(name):
    """Return the canonical capitalization of a whitelisted name."""
    nk = _norm_name(name)
    for w in get_whitelist():
        if _norm_name(w) == nk:
            return w
    return (name or "").strip()


# ─── GROUP KEY HELPERS ───────────────────────────────────────────────────────
# A group key user record looks like:
# {
#   "type": "group",
#   "label": "Team Alpha",
#   "max_seats": 5,
#   "max_claims_per_day": 10,
#   "coins": {"bronze":0,"silver":0,"gold":0,"gt":0},
#   "last_daily": "",
#   "last_spin_at": "",
#   "seats": { "john": { "first_name":"John", "joined_at":"...",
#                         "last_claim_date":"YYYY-MM-DD", "claims_today":0 } },
#   "created": "...",
# }

def _is_group_key(user):
    return isinstance(user, dict) and user.get("type") == "group"


def _get_seat(group_user, first_name):
    if not group_user:
        return None
    return (group_user.get("seats") or {}).get(_norm_name(first_name))


def _find_seat_by_device(group_user, device_id):
    """Return (seat, name_key) for the first seat in this group whose
    device_id matches, or (None, None)."""
    if not group_user or not device_id:
        return None, None
    for nk, s in (group_user.get("seats") or {}).items():
        if (s or {}).get("device_id") == device_id:
            return s, nk
    return None, None


# Error codes returned by _add_or_get_seat (clients react to these).
SEAT_ERR_NAME_REQUIRED   = "name_required"
SEAT_ERR_NAME_TOO_LONG   = "name_too_long"
SEAT_ERR_NAME_INVALID    = "name_invalid"
SEAT_ERR_NAME_NOT_ALLOWED= "name_not_allowed"
SEAT_ERR_DEVICE_REQUIRED = "device_required"
SEAT_ERR_DEVICE_SEATED   = "device_seated"
SEAT_ERR_NAME_TAKEN      = "name_taken"
SEAT_ERR_SEAT_FULL       = "seat_full"


def _add_or_get_seat(group_user, first_name, device_id):
    """Returns (seat, error_dict) where error_dict (if any) has `code` and `error`.

    Enforces:
      * whitelist (name must be on _is_name_allowed list)
      * 1 device = 1 seat (this device cannot claim a different name if it's
        already in this group; a different device cannot claim a name that
        someone else's device already owns)
      * seat capacity
    """
    name_key = _norm_name(first_name)
    name_disp = _canonical_name(first_name) if name_key else ""
    if not name_key:
        return None, {"code": SEAT_ERR_NAME_REQUIRED, "error": "First name required"}
    if len(name_disp) > 30:
        return None, {"code": SEAT_ERR_NAME_TOO_LONG, "error": "First name too long (max 30 chars)"}
    if not all(c.isalpha() or c in " '-." for c in name_disp):
        return None, {"code": SEAT_ERR_NAME_INVALID, "error": "Letters and spaces only"}
    if not _is_name_allowed(first_name):
        return None, {"code": SEAT_ERR_NAME_NOT_ALLOWED,
                      "error": "This name is not on the allowed list."}
    if not (device_id or "").strip():
        return None, {"code": SEAT_ERR_DEVICE_REQUIRED,
                      "error": "Device ID required"}

    seats = group_user.setdefault("seats", {})

    # 1) Device already on a seat?
    dev_seat, dev_seat_key = _find_seat_by_device(group_user, device_id)
    if dev_seat:
        if dev_seat_key == name_key:
            return dev_seat, None  # re-login: same device, same name → resume
        return None, {
            "code": SEAT_ERR_DEVICE_SEATED,
            "error": "This device already holds a seat in this group.",
            "seated_as": dev_seat.get("first_name", ""),
        }

    # 2) Name already taken (by a different device)?
    if name_key in seats:
        existing = seats[name_key]
        existing_dev = (existing or {}).get("device_id", "")
        if existing_dev and existing_dev != device_id:
            return None, {
                "code": SEAT_ERR_NAME_TAKEN,
                "error": "Someone else already took this name in the group.",
            }
        # Legacy seat without device_id — claim it now
        existing["device_id"] = device_id
        return existing, None

    # 3) Seat capacity
    if len(seats) >= int(group_user.get("max_seats", 0)):
        return None, {"code": SEAT_ERR_SEAT_FULL, "error": "Seat is full"}

    # 4) Brand-new seat
    new_seat = {
        "first_name":      name_disp,
        "joined_at":       datetime.now().isoformat(),
        "device_id":       device_id,
        "last_claim_date": "",
        "claims_today":    0,
    }
    seats[name_key] = new_seat
    return new_seat, None


def _seat_claims_today(seat):
    if not seat:
        return 0
    if seat.get("last_claim_date") != date.today().isoformat():
        return 0
    return int(seat.get("claims_today", 0))


def _record_seat_claim(seat):
    if not seat:
        return
    today = date.today().isoformat()
    if seat.get("last_claim_date") != today:
        seat["claims_today"] = 0
    seat["claims_today"] = int(seat.get("claims_today", 0)) + 1
    seat["last_claim_date"] = today


# ─── SHEIN API CONSTANTS ──────────────────────────────────────────────────────
COUPON_URL   = "https://api-service.shein.com/promotion/coupon/bind_coupon"
DELIVERY_URL = "https://api-shein.shein.com/deliveryapi/delivery-material/material_list"
ARK_URL      = "https://api-shein.shein.com/ark/11504"
ARK_MID      = "4142402"   # material ID behind the ark/11504 QR-code campaign
ARK_PKG_ID   = "17131185"  # coupon package ID for the ark/11504 campaign
ARK_CODES    = [            # one "11" code per group (highest-value tier)
    "gm0pha11",
    "gm0phi11",
    "gm365phi11",
    "gm365pha11",
]
LANGUAGE     = "en"

ERR_INVALID_PKG     = 1000
ERR_ALREADY_CLAIMED = 501405
ERR_LOGIN_CODES     = {"401", "100002", "200401", "10000", "460101"}
ERR_LOGIN_KEYWORDS  = ("please login", "login", "not logged", "unauthorized",
                       "user not exist", "\u672a\u767b\u5f55")


def build_headers(cfg, token):
    claim_country  = cfg.get("claim_country", "PH")
    currency_map   = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    return {
        "app-from": "shein", "siteuid": "android",
        "appcountry": cfg.get("appcountry", "GB"), "devtype": "Android",
        "clientid": "100", "ugid": cfg.get("ugid", ""),
        "accept": "application/json", "device": cfg.get("device_info", ""),
        "armortoken": cfg.get("armor_token", ""), "applanguage": LANGUAGE,
        "usercountry": claim_country, "version": cfg.get("app_version", "11.2.3"),
        "devicelanguage": LANGUAGE, "dev-id": cfg.get("device_id", ""),
        "sortuid": cfg.get("sortuid", ""), "device_language": LANGUAGE,
        "apptype": "shein", "localcountry": claim_country,
        "smdeviceid": cfg.get("smdevice_id", ""), "deviceid": cfg.get("device_id", ""),
        "platform": "app-native", "appname": "shein app",
        "appversion": cfg.get("app_version", "11.2.3"), "newuid": cfg.get("sortuid", ""),
        "language": LANGUAGE, "currency": claim_currency, "network-type": "WIFI",
        "token": token, "os-version": "14", "devicesystemversion": "Android14",
        "appcurrency": claim_currency,
        "user-agent": f"Shein {cfg.get('app_version','11.2.3')} Android 14 {cfg.get('device_info','')} {cfg.get('appcountry','GB')} {LANGUAGE} {cfg.get('sortuid','')}",
        "x-gw-auth": cfg.get("gw_auth", ""), "content-type": "application/json; charset=utf-8",
   
