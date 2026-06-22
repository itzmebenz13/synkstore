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

# ─── PERSISTENT STATS (survives Railway volume across deploys) ───────────────
# Mount a Railway Volume at /data and set STATS_FILE=/data/stats_total.json
# to persist across redeployments. Falls back to local file otherwise.
STATS_FILE  = os.environ.get("STATS_FILE",  "stats_total.json")
_stats_lock = threading.Lock()


def _load_stats_unlocked():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"total_claims": 0, "last_updated": ""}


def _save_stats_unlocked(s):
    with open(STATS_FILE, "w") as f:
        json.dump(s, f, indent=2)


def _increment_stats(n=1):
    """Increment claim counter atomically."""
    with _stats_lock:
        s = _load_stats_unlocked()
        s["total_claims"] = int(s.get("total_claims", 0)) + n
        s["last_updated"] = datetime.now().isoformat()
        _save_stats_unlocked(s)


def _get_stats():
    with _stats_lock:
        return _load_stats_unlocked()


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
    }


def build_delivery_headers(cfg, token):
    claim_country  = cfg.get("claim_country", "PH")
    currency_map   = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    gm_device = cfg.get("gm_device_id", cfg.get("device_id", ""))
    gm_site   = cfg.get("gm_site", "andshph")
    av        = cfg.get("app_version", "11.2.3")
    di        = cfg.get("device_info", "")
    return {
        "host": "api-shein.shein.com", "content-type": "application/json",
        "accept": "application/json, text/plain, */*", "appname": "shein app",
        "apptype": "shein", "brand": "shein", "channel": "h5",
        "siteuid": gm_site, "localcountry": claim_country,
        "currency": claim_currency, "appcurrency": claim_currency,
        "applanguage": LANGUAGE, "language": LANGUAGE, "timezone": "GMT+8",
        "appversion": av, "deviceid": gm_device,
        "smdeviceid": cfg.get("smdevice_id", ""), "ugid": cfg.get("ugid", ""),
        "armortoken": cfg.get("armor_token", ""), "x-gw-auth": cfg.get("gw_auth", ""),
        "token": token, "x-request-by": "bridgeX", "route-bff": "TRUE",
        "user-agent": f"Mozilla/5.0 (Linux; Android 14; {di} Build/UKQ1; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.0.0 Mobile Safari/537.36 SheinApp(shein/{av}) TTID/shein Wing/1.0.1",
        "referer": f"https://api-shein.shein.com/ark/11504?app=shein&device_type=android&language=en&site_uid={gm_site}&region=PH",
        "origin": "https://api-shein.shein.com", "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
        "accept-encoding": "gzip, deflate, br", "accept-language": "en-PH,en-US;q=0.9,en;q=0.8",
    }


def build_ark_get_headers(cfg, token):
    """Headers that mimic the SHEIN app WebView opening the ark/11504 QR-code URL.
    The server reads these headers to build the sessionID cookie it returns,
    which is then required by the delivery POST that follows."""
    claim_country  = cfg.get("claim_country", "PH")
    currency_map   = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    gm_site        = cfg.get("gm_site", "andshph")
    gm_device      = cfg.get("gm_device_id", cfg.get("device_id", ""))
    av             = cfg.get("app_version", "11.2.3")
    di             = cfg.get("device_info", "")
    sm             = cfg.get("smdevice_id", "")
    newuid         = cfg.get("sortuid", "")
    return {
        "user-agent": (
            f"Mozilla/5.0 (Linux; Android 14; {di} Build/UKQ1; wv) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
            f"Chrome/118.0.0.0 Mobile Safari/537.36 "
            f"SheinApp(shein/{av}) TTID/shein Wing/1.0.1"
        ),
        "accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,image/apng,*/*;"
            "q=0.8,application/signed-exchange;v=b3;q=0.7"
        ),
        "upgrade-insecure-requests": "1",
        "sec-ch-ua": '"Android WebView";v="118", "Chromium";v="118", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        # ── App / auth headers (mirror of what the app sends) ──
        "smdeviceid":          sm,
        "siteuid":             "android",
        "paltform-app-siteuid": gm_site,
        "appcountry":          cfg.get("appcountry", claim_country),
        "language":            LANGUAGE,
        "usercountry":         claim_country,
        "localcountry":        claim_country,
        "newuid":              newuid,
        "platform":            "app-h5",
        "appname":             "shein app",
        "apptype":             "shein",
        "login-state":         "1",
        "currency":            claim_currency,
        "appcurrency":         claim_currency,
        "ugid":                cfg.get("ugid", ""),
        "deviceid":            gm_device,
        "device":              f"{di} Android14",
        "devtype":             "Android",
        "applanguage":         LANGUAGE,
        "appversion":          av,
        "armortoken":          cfg.get("armor_token", ""),
        "token":               token,
        "servertime":          str(int(time.time())),
        "timezonestring":      "Asia/Manila",
        "iscdn":               "1",
        "x-requested-with":    "com.zzkko",
        # ── Fetch metadata ──
        "sec-fetch-site":    "none",
        "sec-fetch-mode":    "navigate",
        "sec-fetch-user":    "?1",
        "sec-fetch-dest":    "document",
        "accept-encoding":   "gzip, deflate, br",
        "accept-language":   "en-PH,en-US;q=0.9,en;q=0.8",
    }


def is_login_error(top_code, top_msg):
    if top_code in ERR_LOGIN_CODES:
        return True
    return any(kw in top_msg.lower() for kw in ERR_LOGIN_KEYWORDS)


def sse(msg):
    return f"data: {msg}\n\n"


def run_collect(cfg, q):
    tokens = cfg.get("tokens", [])
    codes  = cfg.get("codes", [])
    pkg_id = cfg.get("pkg_id", "")
    mode   = cfg.get("mode", "bind")

    def emit(msg): q.put(msg)

    if mode == "brute":
        _run_brute(cfg, q)
        return

    if mode == "ark":
        emit("=" * 60)
        emit("  ARK VOUCHERS  —  mid=" + ARK_MID)
        emit("=" * 60)
        emit(f"  Accounts : {len(tokens)}")
        emit("=" * 60)
        delay = int(cfg.get("account_delay", 3))
        for i, token in enumerate(tokens):
            emit(f"\n{'#'*60}")
            emit(f"  Account {i+1}/{len(tokens)}  [...{token[-20:]}]")
            emit(f"{'#'*60}")
            _collect_ark(cfg, token, emit)
            if i < len(tokens) - 1:
                emit(f"\n  Waiting {delay}s before next account...")
                time.sleep(delay)
        emit("\n" + "=" * 60)
        emit("  All done!")
        emit("=" * 60)
        q.put(None)
        return

    emit("=" * 60)
    emit("  SHEIN COUPON COLLECTOR  —  App API Mode")
    emit("=" * 60)
    emit(f"  Accounts : {len(tokens)}")
    emit(f"  Codes    : {len(codes)}")
    emit(f"  Package  : {pkg_id}")
    emit(f"  Country  : {cfg.get('claim_country','PH')}")
    emit("=" * 60)

    delay = int(cfg.get("account_delay", 3))
    for i, token in enumerate(tokens):
        emit(f"\n{'#'*60}")
        emit(f"  Account {i+1}/{len(tokens)}  [...{token[-20:]}]")
        emit(f"{'#'*60}")
        if mode == "delivery":
            _collect_delivery(cfg, token, emit)
        else:
            _collect_bind(cfg, token, codes, pkg_id, emit)
        if i < len(tokens) - 1:
            emit(f"\n  Waiting {delay}s before next account...")
            time.sleep(delay)

    emit("\n" + "=" * 60)
    emit("  All done!")
    emit("=" * 60)
    q.put(None)


def _collect_bind(cfg, token, codes, pkg_id, emit):
    headers = build_headers(cfg, token)
    payload = {
        "couponPackages": [{"couponPackageId": str(pkg_id), "couponCodes": ",".join(codes)}],
        "scene": "home", "idempotentCode": str(uuid.uuid4()),
    }
    emit(f"\n  {'─'*54}")
    emit(f"  Package  : {pkg_id}")
    emit(f"  Country  : {cfg.get('claim_country','PH')}")
    emit(f"  Claiming {len(codes)} code(s): {', '.join(codes)}")
    emit(f"  {'─'*54}")
    try:
        r = requests.post(COUPON_URL, json=payload, headers=headers, timeout=15, verify=False)
        try: raw = r.json()
        except: raw = {}
        data = raw if isinstance(raw, dict) else {}
        top_code = str(data.get("code") or data.get("ret_msg_code") or "")
        top_msg  = str(data.get("msg")  or data.get("tips") or "")
        info     = (data.get("info") or {}) if isinstance(data.get("info"), dict) else {}
        success_list = [str(c).strip() for c in (info.get("successCodeList") or []) if c]
        fail_list    = [str(c).strip() for c in (info.get("failCodeList") or []) if c]
        result_list  = info.get("bindResult") or []
        result_list  = result_list if isinstance(result_list, list) else []

        if is_login_error(top_code, top_msg):
            emit(f"  \U0001f512 NOT LOGGED IN \u2014 Please login. (code={top_code})")
            return
        if top_code == str(ERR_ALREADY_CLAIMED):
            emit(f"  \u26a0\ufe0f  ALREADY CLAIMED [501405] \u2014 {', '.join(codes)}")
        elif success_list:
            emit(f"  \u2705 CLAIMED! codes={', '.join(success_list)}")
        elif result_list:
            claimed_c, conflict_c, other_c = [], [], []
            for item in result_list:
                if not isinstance(item, dict): continue
                cv = str(item.get("couponCode") or "?")
                ec = str(item.get("errorCode") or item.get("code") or "")
                if ec in ("0","200",""): claimed_c.append(cv)
                elif ec == str(ERR_ALREADY_CLAIMED): conflict_c.append(cv)
                else: other_c.append(f"{cv}[{ec}]")
            if claimed_c: emit(f"  \u2705 CLAIMED! {claimed_c}")
            if conflict_c: emit(f"  \u26a0\ufe0f  ALREADY CLAIMED [501405] \u2014 {', '.join(conflict_c)}")
            if other_c: emit(f"  \u274c FAILED \u2192 {other_c}")
        elif fail_list:
            emit(f"  \u274c FAILED {fail_list}")
        elif top_code not in ("0","200",""):
            emit(f"  \u274c ERR {top_code}: {top_msg[:80]}")
        elif top_code in ("0","200"):
            emit(f"  \u2753 Ambiguous \u2014 code={top_code}, no detail from SHEIN.")
        else:
            emit(f"  \u2753 Ambiguous response \u2014 code={top_code} msg={top_msg[:60]}")
    except requests.exceptions.Timeout:
        emit("  \u274c Request timed out")
    except Exception as e:
        emit(f"  \u274c Error: {str(e)[:120]}")
    emit(f"  {'─'*54}")


def _collect_delivery(cfg, token, emit):
    gm_site = cfg.get("gm_site", "andshph")
    gm_mid  = cfg.get("gm_mid", "4142402")
    claim_country  = cfg.get("claim_country", "PH")
    claim_currency = {"PH":"PHP","MY":"MYR","TH":"THB"}.get(claim_country, "PHP")
    av        = cfg.get("app_version", "11.2.3")
    gm_device = cfg.get("gm_device_id", cfg.get("device_id", ""))

    headers = build_delivery_headers(cfg, token)
    payload = {
        "client_info": {"app_version": av, "client_id": 100, "currency": claim_currency,
                        "dev_id": gm_device, "language": LANGUAGE, "site_uid": gm_site,
                        "token": token, "brand": "shein"},
        "material_request_info": {
            "mid": gm_mid,
            "param_map": {"coupon_common_req": {"coupon_type": 2, "coupon_sequence": 3}, "auto_bind": True},
            "data_type": "SwiftCouponOnePlugin", "data_scene": 0,
            "data_scene_flag": "0_SwiftCouponOnePlugin"},
        "ext_map": {},
    }
    emit(f"\n  {'─'*54}")
    emit(f"  Mode     : DELIVERY API (auto_bind)")
    emit(f"  MID      : {gm_mid}")
    emit(f"  Country  : {claim_country} ({claim_currency})")
    emit(f"  {'─'*54}")
    try:
        r = requests.post(DELIVERY_URL, json=payload, headers=headers,
                          params={"sw_site": gm_site, "sw_lang": LANGUAGE}, timeout=15, verify=False)
        try: data = r.json()
        except: data = {}
        code = str(data.get("code", ""))
        if code not in ("0","200"):
            top_msg_d = str(data.get("msg","Unknown"))
            if is_login_error(code, top_msg_d):
                emit(f"  \U0001f512 NOT LOGGED IN \u2014 Please login. (code={code})")
            else:
                emit(f"  \u274c API error: {top_msg_d}")
            return
        info         = data.get("info") or {}
        coupon_info  = info.get("coupon_info") or {}
        bind_result  = coupon_info.get("bind_result") or {}
        bind_data    = bind_result.get("bindResult") or {}
        success_list = bind_data.get("successList") or []
        fail_list    = bind_data.get("failList") or []
        hit_coupon   = coupon_info.get("hit_coupon") or []
        recv_coupon  = coupon_info.get("received_coupon") or []
        if success_list:
            emit(f"  \u2705 {len(success_list)} claimed, {len(fail_list)} failed")
            for c in success_list:
                emit(f"     \u2713 {c.get('couponCode','?')} (id:{c.get('couponId','?')})")
        elif hit_coupon:
            emit(f"  \u26a0\ufe0f  Already claimed \u2014 {len(hit_coupon)} coupon(s) already in account")
        elif recv_coupon:
            emit(f"  \u2705 {len(recv_coupon)} already received")
        else:
            emit(f"  \u2753 No bind result \u2014 bindCode={bind_result.get('bindCode','')} detail={info.get('detail_msg') or data.get('msg') or 'No result'}")
    except requests.exceptions.Timeout:
        emit("  \u274c Request timed out")
    except Exception as e:
        emit(f"  \u274c Error: {str(e)[:120]}")
    emit(f"  {'─'*54}")



def _collect_ark(cfg, token, emit):
    """Claim ark/11504 delivery campaign — auto_bind + SwiftCouponOnePlugin.

    ARK errors use ⚠ (warn) not ❌ (err) so they never cause
    "Claim Failed" in the modal when the main claim already succeeded.
    """
    gm_site        = cfg.get("gm_site") or "andshph"
    claim_country  = cfg.get("claim_country") or "PH"
    claim_currency = {"PH": "PHP", "MY": "MYR", "TH": "THB"}.get(claim_country, "PHP")
    av             = cfg.get("app_version") or "11.2.3"
    gm_device      = cfg.get("gm_device_id") or cfg.get("device_id") or ""

    emit(f"\n  {chr(0x2550)*54}")
    emit(f"  ARK     : Claiming mid={ARK_MID} (gm0 campaign)")
    emit(f"  {chr(0x2550)*54}")

    headers = build_delivery_headers(cfg, token)
    payload = {
        "client_info": {
            "app_version": av, "client_id": 100, "currency": claim_currency,
            "dev_id": gm_device, "language": LANGUAGE, "site_uid": gm_site,
            "token": token, "brand": "shein",
        },
        "material_request_info": {
            "mid": ARK_MID,
            "param_map": {
                "coupon_common_req": {"coupon_type": 2, "coupon_sequence": 3},
                "auto_bind": True,
            },
            "data_type":       "SwiftCouponOnePlugin",
            "data_scene":      0,
            "data_scene_flag": "0_SwiftCouponOnePlugin",
        },
        "ext_map": {},
    }
    try:
        r = requests.post(
            DELIVERY_URL, json=payload, headers=headers,
            params={"sw_site": gm_site, "sw_lang": LANGUAGE},
            timeout=15, verify=False,
        )
        try:    data = r.json()
        except: data = {}

        code    = str(data.get("code", ""))
        top_msg = str(data.get("msg", "Unknown"))

        if code not in ("0", "200"):
            if is_login_error(code, top_msg):
                emit(f"  \U0001f512 ARK: NOT LOGGED IN (code={code})")
            else:
                # ⚠ not ❌ — ARK API error should not cause "Claim Failed"
                emit(f"  \u26a0\ufe0f  ARK: API error [{code}] — {top_msg[:60]}")
            emit(f"  {chr(0x2500)*54}")
            return

        info         = data.get("info") or {}
        coupon_info  = info.get("coupon_info") or {}
        no_reason    = coupon_info.get("no_coupon_reason") or ""
        risk_code    = coupon_info.get("risk_result_code")
        recv_coupon  = coupon_info.get("received_coupon") or []
        bind_result  = coupon_info.get("bind_result") or {}
        bind_data    = (bind_result.get("bindResult") or {}) if isinstance(bind_result, dict) else {}
        success_list = bind_data.get("successList") or []
        fail_list    = bind_data.get("failList")    or []

        # ── Case 1: Successful fresh claim ──────────────────────────────────
        if success_list:
            codes = [c.get("couponCode", "?") for c in success_list]
            emit(f"  \u2705 CLAIMED! [{', '.join(codes)}]")
            for code_name in codes:
                emit(f"     \u2713 {code_name}")
            if fail_list:
                emit(f"  \u26a0\ufe0f  ARK fail: {[c.get('couponCode','?') for c in fail_list]}")

        # ── Case 2: Already claimed (COUPON_EXCLUSIVE) ──────────────────────
        elif no_reason == "COUPON_EXCLUSIVE" or recv_coupon:
            ark_codes = [c.get("couponCode", "?") for c in recv_coupon] if recv_coupon else []
            emit(f"  \u26a0\ufe0f  ALREADY CLAIMED [COUPON_EXCLUSIVE]"
                 + (f" — {', '.join(ark_codes)}" if ark_codes else " — ARK campaign"))

        # ── Case 3: Risk check blocked (1004) ────────────────────────────────
        elif risk_code and str(risk_code) not in ("0", "1002"):
            # ⚠ not ❌ — risk block is informational, not a "failure"
            emit(f"  \u26a0\ufe0f  ARK: Risk blocked [risk_code={risk_code}]")

        # ── Case 4: Other / no result ─────────────────────────────────────────
        elif no_reason:
            emit(f"  \u26a0\ufe0f  ARK: {no_reason}")
        else:
            emit(f"  \u26a0\ufe0f  ARK: No result — {top_msg[:60]}")

    except requests.exceptions.Timeout:
        # ⚠ not ❌ — timeout is non-fatal for the overall run
        emit("  \u26a0\ufe0f  ARK: Request timed out")
    except Exception as e:
        emit(f"  \u26a0\ufe0f  ARK: {str(e)[:120]}")
    emit(f"  {chr(0x2500)*54}")


def _run_brute(cfg, q):
    tokens = cfg.get("tokens", [])
    codes  = cfg.get("codes", [])
    start  = int(cfg.get("brute_start", 17130000))
    end    = int(cfg.get("brute_end",   17140000))
    threads     = min(int(cfg.get("brute_threads", 10)), 30)
    max_claims  = int(cfg.get("brute_max_claims", 1))
    claim_country = cfg.get("claim_country", "PH")
    claim_currency = {"PH":"PHP","MY":"MYR","TH":"THB"}.get(claim_country, "PHP")
    token = tokens[0] if tokens else ""

    print_lock  = threading.Lock()
    claims_lock = threading.Lock()
    done_lock   = threading.Lock()
    stop_event  = threading.Event()
    claims_total  = [0]
    done_count    = [0]
    already_claimed = []

    def emit(msg):
        with print_lock: q.put(msg)

    pkg_range = list(range(start, end + 1))
    total = len(pkg_range)
    emit("=" * 64)
    emit("  BRUTE-FORCE MODE  —  live output")
    emit(f"  Range   : {start} \u2192 {end}  ({total} IDs)")
    emit(f"  Threads : {threads}")
    emit(f"  Stop at : {max_claims} claim(s)  (0 = unlimited)")
    emit(f"  Country : {claim_country} ({claim_currency})")
    emit(f"  Codes   : {', '.join(codes)}")
    emit("=" * 64)

    def probe(pkg_id):
        if stop_event.is_set(): return
        headers = build_headers(cfg, token)
        payload = {
            "couponPackages": [{"couponPackageId": str(pkg_id), "couponCodes": ",".join(codes)}],
            "scene": "home", "idempotentCode": str(uuid.uuid4()),
        }
        with done_lock:
            done_count[0] += 1
            done = done_count[0]
        pct = min(done * 100 // total, 100)
        try:
            r = requests.post(COUPON_URL, json=payload, headers=headers, timeout=12, verify=False)
            try: raw = r.json()
            except: raw = {}
            data = raw if isinstance(raw, dict) else {}
            top_code = str(data.get("code") or data.get("ret_msg_code") or "")
            top_msg  = str(data.get("msg")  or data.get("tips") or "")
            info = (data.get("info") or {}) if isinstance(data.get("info"), dict) else {}
            success_list = [str(c).strip() for c in (info.get("successCodeList") or []) if c]
            pkg_code   = str(info.get("couponPackageCode") or "")
            error_code = str(info.get("errorCode") or "")

            if top_code == str(ERR_INVALID_PKG): return
            if top_code == str(ERR_ALREADY_CLAIMED) or pkg_code == str(ERR_ALREADY_CLAIMED) or error_code == str(ERR_ALREADY_CLAIMED):
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u26a0  ALREADY CLAIMED [501405]")
                already_claimed.append(pkg_id); return
            if success_list:
                with claims_lock:
                    claims_total[0] += len(success_list)
                    total_so_far = claims_total[0]
                    if max_claims > 0 and claims_total[0] >= max_claims: stop_event.set()
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2605 CLAIMED! codes=[{', '.join(success_list)}] total={total_so_far}"); return
            result_list = info.get("bindResult") or []
            result_list = result_list if isinstance(result_list, list) else []
            if result_list:
                claimed_c, conflict_c, other_c = [], [], []
                for item in result_list:
                    if not isinstance(item, dict): continue
                    cv = str(item.get("couponCode") or "?")
                    ec = str(item.get("errorCode") or item.get("code") or "")
                    if ec in ("0","200",""): claimed_c.append(cv)
                    elif ec == str(ERR_ALREADY_CLAIMED): conflict_c.append(cv)
                    else: other_c.append(f"{cv}[{ec}]")
                if claimed_c:
                    with claims_lock:
                        claims_total[0] += len(claimed_c)
                        total_so_far = claims_total[0]
                        if max_claims > 0 and claims_total[0] >= max_claims: stop_event.set()
                    emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2605 CLAIMED! {claimed_c}")
                if conflict_c: emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u26a0  [501405] \u2192 {conflict_c}")
                if other_c:    emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2717 FAIL \u2192 {other_c}")
                return
            fail_list = [str(c).strip() for c in (info.get("failCodeList") or []) if c]
            if fail_list:
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2717 FAILED {fail_list}"); return
            if top_code not in ("0","200",""):
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2717 ERR {top_code}: {top_msg[:50]}")
        except requests.exceptions.Timeout:
            emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2717 TIMEOUT")
        except Exception as e:
            emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} \u2717 EXC: {str(e)[:80]}")

    with ThreadPoolExecutor(max_workers=threads) as exe:
        futs = [exe.submit(probe, pkg) for pkg in pkg_range]
        try:
            for fut in as_completed(futs):
                if stop_event.is_set():
                    exe.shutdown(wait=False, cancel_futures=True); break
        except Exception: pass

    emit("=" * 64)
    emit(f"  Done. Scanned {done_count[0]}/{total} IDs.")
    emit(f"  Total claimed   : {claims_total[0]} coupon(s)")
    emit(f"  Already claimed : {len(already_claimed)} package(s)")
    for p in already_claimed: emit(f"    \u2192 {p}")
    emit("=" * 64)
    q.put(None)


# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return "", 404

@app.route("/mainscript.html")
def mainscript():
    return render_template("mainscript.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/stats", methods=["GET"])
def get_stats_endpoint():
    """Public stats endpoint — returns cumulative claim count and income estimate."""
    s = _get_stats()
    total = int(s.get("total_claims", 0))
    return jsonify({
        "ok": True,
        "total_claims": total,
        "min_income": total * 700,
        "max_income": total * 750,
        "last_updated": s.get("last_updated", ""),
    })


@app.route("/user/login", methods=["POST"])
def user_login():
    data = request.get_json(force=True) or {}
    key  = data.get("access_key", "").strip()
    first_name = (data.get("first_name") or "").strip()
    if not key:
        return jsonify({"error": "No access key provided"}), 400
    gemini_key = get_gemini_key()

    if key == ADMIN_KEY:
        return jsonify({"ok": True, "is_admin": True, "username": "Admin",
                        "coins": {"bronze": 999, "silver": 999, "gold": 999},
                        "gemini_key": gemini_key,
                        "spin_next_in": 0})

    user = get_user(key)
    if not user:
        return jsonify({"error": "Invalid access key"}), 401

    # ── Group key path ──
    if _is_group_key(user):
        device_id = (data.get("device_id") or "").strip()

        # If this device already holds a seat, auto-resume it (no name prompt needed).
        existing_seat, _existing_key = _find_seat_by_device(user, device_id)
        if existing_seat and not first_name:
            first_name = existing_seat.get("first_name", "")
        elif existing_seat and _norm_name(first_name) != _norm_name(existing_seat.get("first_name", "")):
            # Different name attempt from a device that's already seated.
            return jsonify({
                "ok": False,
                "code": "device_seated",
                "seated_as": existing_seat.get("first_name", ""),
                "group_label": user.get("label", "Group"),
                "error": "This device already holds the \"" + existing_seat.get("first_name", "") + "\" seat. Continue with that name.",
            }), 403

        # No first_name yet — ask for it (and ship the whitelist so the page can show options)
        if not first_name:
            return jsonify({
                "ok": False,
                "requires_name": True,
                "group_label": user.get("label", "Group"),
                "seats_used": len((user.get("seats") or {})),
                "max_seats": int(user.get("max_seats", 0)),
                "allowed_names": get_whitelist(),
            })

        # Try to seat the user
        with _users_lock:
            users = _load_users()
            u = users.get(key)
            if not u or not _is_group_key(u):
                return jsonify({"error": "Invalid access key"}), 401
            seat, err = _add_or_get_seat(u, first_name, device_id)
            if err and err.get("code") == SEAT_ERR_SEAT_FULL:
                return jsonify({
                    "ok": False,
                    "seat_full": True,
                    "group_label": u.get("label", "Group"),
                    "seats_used": len(u.get("seats", {})),
                    "max_seats": int(u.get("max_seats", 0)),
                })
            if err:
                resp = {"ok": False, "code": err.get("code"), "error": err.get("error")}
                if "seated_as" in err: resp["seated_as"] = err["seated_as"]
                if err.get("code") == SEAT_ERR_NAME_NOT_ALLOWED:
                    resp["allowed_names"] = get_whitelist()
                return jsonify(resp), 403
            _save_users(users)
        _apply_daily_coins()
        with _users_lock:
            u = _load_users().get(key) or u
            seat = _get_seat(u, first_name) or seat
        return jsonify({
            "ok": True,
            "is_admin": False,
            "is_group": True,
            "username": seat["first_name"],
            "first_name": seat["first_name"],
            "group_label": u.get("label", "Group"),
            "coins": u.get("coins", {"bronze": 0, "silver": 0, "gold": 0}),
            "gemini_key": gemini_key,
            "spin_next_in": _spin_remaining_seconds(u),
            "max_claims_per_day": int(u.get("max_claims_per_day", 0)),
            "claims_today": _seat_claims_today(seat),
            "seats_used": len(u.get("seats", {})),
            "max_seats": int(u.get("max_seats", 0)),
        })

    # ── Regular user path ──
    _apply_daily_coins()
    user = get_user(key)
    return jsonify({"ok": True, "is_admin": False,
                    "username": user.get("username", "User"),
                    "coins": user.get("coins", {"bronze": 0, "silver": 0, "gold": 0}),
                    "gemini_key": gemini_key,
                    "spin_next_in": _spin_remaining_seconds(user)})


@app.route("/user/use_coin", methods=["POST"])
def use_coin():
    data  = request.get_json(force=True) or {}
    key   = data.get("access_key", "").strip()
    first_name = (data.get("first_name") or "").strip()
    batch = data.get("batch", "").strip()
    if not key or not batch:
        return jsonify({"error": "Missing access_key or batch"}), 400

    log_id = str(uuid.uuid4())
    now_str = datetime.now().isoformat()

    if key == ADMIN_KEY:
        _append_log({
            "log_id":    log_id,
            "user":      "Admin",
            "access_key": "[admin]",
            "batch":     batch,
            "coin_type": "—",
            "result":    "pending",
            "detail":    "",
            "created_at": now_str,
            "completed_at": None,
        })
        return jsonify({"ok": True, "coins": {"bronze": 999, "silver": 999, "gold": 999},
                        "used_coin": "—", "log_id": log_id})

    cost_info = VOUCHER_COIN_COST.get(batch)
    if not cost_info:
        return jsonify({"error": f"Unknown batch: {batch}"}), 400
    coin_type = cost_info["coin"]

    with _users_lock:
        users = _load_users()
        user  = users.get(key)
        if not user:
            return jsonify({"error": "Invalid access key"}), 401

        # ── Group key: enforce per-seat daily claim limit ──
        is_group = _is_group_key(user)
        log_user = user.get("username", "User")
        if is_group:
            seat = _get_seat(user, first_name)
            if not seat:
                return jsonify({"error": "No active seat — please re-enter your first name"}), 401
            limit = int(user.get("max_claims_per_day", 0))
            claims = _seat_claims_today(seat)
            if limit > 0 and claims >= limit:
                return jsonify({
                    "error": f"Daily limit reached ({claims}/{limit}). Try again tomorrow.",
                    "claims_today": claims,
                    "max_claims_per_day": limit,
                }), 429
            log_user = seat.get("first_name") or first_name

        # GT deduction (group-shared if group)
        coins   = user.setdefault("coins", {"bronze": 0, "silver": 0, "gold": 0})
        balance = coins.get(coin_type, 0)
        if balance <= 0:
            return jsonify({"error": f"Not enough {coin_type} coins for {cost_info['label']}"}), 402
        coins[coin_type] = balance - 1
        _save_users(users)

    _append_log({
        "log_id":     log_id,
        "user":       log_user,
        "access_key": key,
        "first_name": (first_name if is_group else ""),
        "is_group":   is_group,
        "batch":      batch,
        "coin_type":  coin_type,
        "result":     "pending",
        "detail":     "",
        "created_at": now_str,
        "completed_at": None,
    })
    return jsonify({"ok": True, "coins": coins, "used_coin": coin_type, "log_id": log_id})


def _require_admin(data):
    return data.get("admin_key") == ADMIN_KEY


@app.route("/admin/users", methods=["POST"])
def admin_list_users():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    users = all_users()
    individuals = []
    groups = []
    today = date.today().isoformat()
    for k, v in users.items():
        if _is_group_key(v):
            seats_out = []
            for sk, s in (v.get("seats") or {}).items():
                claims_today = int(s.get("claims_today", 0)) if s.get("last_claim_date") == today else 0
                seats_out.append({
                    "first_name": s.get("first_name", sk),
                    "joined_at":  s.get("joined_at", ""),
                    "claims_today": claims_today,
                })
            groups.append({
                "key": k,
                "label": v.get("label", "Group"),
                "max_seats": int(v.get("max_seats", 0)),
                "max_claims_per_day": int(v.get("max_claims_per_day", 0)),
                "seats_used": len(seats_out),
                "seats": seats_out,
                "coins": v.get("coins", {"bronze": 0, "silver": 0, "gold": 0}),
            })
        else:
            individuals.append({
                "key": k,
                "username": v.get("username", ""),
                "coins": v.get("coins", {"bronze": 0, "silver": 0, "gold": 0}),
                "last_daily": v.get("last_daily", ""),
            })
    return jsonify({"ok": True, "users": individuals, "groups": groups})


@app.route("/admin/create_group_key", methods=["POST"])
def admin_create_group_key():
    """Create a new group access key with N seats and daily claim limit."""
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key   = (data.get("key") or "").strip()
    label = (data.get("label") or "Group").strip() or "Group"
    max_seats = int(data.get("max_seats", 1))
    max_claims = int(data.get("max_claims_per_day", 0))
    initial_gt = float(data.get("gt", 0))
    if not key:
        return jsonify({"error": "No key provided"}), 400
    if get_user(key):
        return jsonify({"error": "Key already exists"}), 409
    if max_seats < 1 or max_seats > 100:
        return jsonify({"error": "max_seats must be between 1 and 100"}), 400
    if max_claims < 0:
        return jsonify({"error": "max_claims_per_day must be >= 0"}), 400
    save_user(key, {
        "type": "group",
        "label": label,
        "max_seats": max_seats,
        "max_claims_per_day": max_claims,
        "coins": {"bronze": 0, "silver": 0, "gold": initial_gt, "gt": initial_gt},
        "last_daily": "",
        "created": datetime.now().isoformat(),
        "seats": {},
    })
    return jsonify({"ok": True, "key": key})


@app.route("/admin/edit_group_key", methods=["POST"])
def admin_edit_group_key():
    """Update label / max_seats / max_claims_per_day on an existing group."""
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key = (data.get("key") or "").strip()
    with _users_lock:
        users = _load_users()
        u = users.get(key)
        if not u or not _is_group_key(u):
            return jsonify({"error": "Group key not found"}), 404
        if "label" in data:
            u["label"] = (data["label"] or "").strip() or u.get("label", "Group")
        if "max_seats" in data:
            ms = int(data["max_seats"])
            if ms < 1 or ms > 100:
                return jsonify({"error": "max_seats must be 1-100"}), 400
            u["max_seats"] = ms
        if "max_claims_per_day" in data:
            mc = int(data["max_claims_per_day"])
            if mc < 0:
                return jsonify({"error": "max_claims_per_day must be >= 0"}), 400
            u["max_claims_per_day"] = mc
        _save_users(users)
    return jsonify({"ok": True})


@app.route("/admin/get_name_whitelist", methods=["POST"])
def admin_get_name_whitelist():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"ok": True, "names": get_whitelist()})


@app.route("/admin/set_name_whitelist", methods=["POST"])
def admin_set_name_whitelist():
    """Replace the whitelist with the names list provided. Empty list resets
    to the default seed list."""
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    names = data.get("names")
    if not isinstance(names, list):
        return jsonify({"error": "names must be a list"}), 400
    if not names:
        names = list(DEFAULT_WHITELIST)
    clean = set_whitelist(names)
    return jsonify({"ok": True, "names": clean})


@app.route("/admin/kick_seat", methods=["POST"])
def admin_kick_seat():
    """Remove a single seat from a group, freeing the slot."""
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key = (data.get("key") or "").strip()
    first_name = (data.get("first_name") or "").strip()
    name_key = _norm_name(first_name)
    with _users_lock:
        users = _load_users()
        u = users.get(key)
        if not u or not _is_group_key(u):
            return jsonify({"error": "Group key not found"}), 404
        if name_key in (u.get("seats") or {}):
            del u["seats"][name_key]
            _save_users(users)
            return jsonify({"ok": True})
        return jsonify({"error": "Seat not found"}), 404


@app.route("/admin/create_user", methods=["POST"])
def admin_create_user():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key      = data.get("key", "").strip()
    username = data.get("username", "User").strip()
    coins    = data.get("coins", {"bronze": 0, "silver": 0, "gold": 0})
    if not key: return jsonify({"error": "No key provided"}), 400
    if get_user(key): return jsonify({"error": f"Key already exists"}), 409
    save_user(key, {"username": username,
                    "coins": {"bronze": int(coins.get("bronze",0)),
                              "silver": int(coins.get("silver",0)),
                              "gold":   int(coins.get("gold",0))},
                    "last_daily": "", "created": datetime.now().isoformat()})
    return jsonify({"ok": True, "key": key})


@app.route("/admin/set_coins", methods=["POST"])
def admin_set_coins():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key   = data.get("key", "").strip()
    coins = data.get("coins", {})
    with _users_lock:
        users = _load_users()
        user  = users.get(key)
        if not user: return jsonify({"error": "User not found"}), 404
        user["coins"] = {"bronze": int(coins.get("bronze", user["coins"].get("bronze",0))),
                         "silver": int(coins.get("silver", user["coins"].get("silver",0))),
                         "gold":   int(coins.get("gold",   user["coins"].get("gold",0)))}
        _save_users(users)
    return jsonify({"ok": True, "coins": user["coins"]})


@app.route("/admin/add_coins", methods=["POST"])
def admin_add_coins():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key   = data.get("key", "").strip()
    coins = data.get("coins", {})
    with _users_lock:
        users = _load_users()
        user  = users.get(key)
        if not user: return jsonify({"error": "User not found"}), 404
        for ct in ("bronze","silver","gold"):
            user["coins"][ct] = user["coins"].get(ct,0) + int(coins.get(ct,0))
        _save_users(users)
    return jsonify({"ok": True, "coins": user["coins"]})


@app.route("/admin/delete_user", methods=["POST"])
def admin_delete_user():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    key = data.get("key", "").strip()
    with _users_lock:
        users = _load_users()
        if key not in users: return jsonify({"error": "User not found"}), 404
        del users[key]
        _save_users(users)
    return jsonify({"ok": True})


@app.route("/admin/get_daily_config", methods=["POST"])
def admin_get_daily_config():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"ok": True, "daily_coins": _load_daily_config()})


@app.route("/admin/set_daily_config", methods=["POST"])
def admin_set_daily_config():
    data = request.get_json(force=True) or {}
    if not _require_admin(data): return jsonify({"error": "Unauthorized"}), 401
    cfg = {"bronze": int(data.get("bronze",0)),
           "silver": int(data.get("silver",0)),
           "gold":   int(data.get("gold",1))}
    _save_daily_config(cfg)
    return jsonify({"ok": True, "daily_coins": cfg})


@app.route("/admin/set_gemini_key", methods=["POST"])
def admin_set_gemini_key():
    """Admin-only — store the shared Gemini API key in gemini_key.json.
    Pass {admin_key, gemini_key}. Empty gemini_key clears it."""
    data = request.get_json(force=True) or {}
    if not _require_admin(data):
        return jsonify({"error": "Unauthorized"}), 401
    new_key = (data.get("gemini_key") or "").strip()
    if new_key and not (new_key.startswith("AIza") or new_key.startswith("AQ.")):
        return jsonify({"error": "Invalid key format (expected AIza... or AQ....)"}), 400
    set_gemini_key(new_key)
    return jsonify({"ok": True})


# ─── SPIN TO WIN (daily lottery) ─────────────────────────────────────────────
# Prize index here MUST match the SPIN_PRIZES array in mainscript.html.
# Server is authoritative for both the random pick and the GT credit.
# Weights MUST stay in sync with SPIN_PRIZES in mainscript.html.
# Probabilities (total weight 200):
#   5 GT      → 0.5 %     0.5 GT   → 4.5 %
#   3 GT      → 1.0 %     0.2 GT   → 10  %
#   2 GT      → 1.5 %     0.1 GT   → 30  %
#   1 GT      → 2.5 %     Try Again → 50 %
SPIN_PRIZES = [
    {"index": 0, "label": "5 GT",       "value": 5.0,  "weight": 1},
    {"index": 1, "label": "Try Again",  "value": 0.0,  "weight": 100},
    {"index": 2, "label": "1 GT",       "value": 1.0,  "weight": 5},
    {"index": 3, "label": "0.1 GT",     "value": 0.1,  "weight": 60},
    {"index": 4, "label": "3 GT",       "value": 3.0,  "weight": 2},
    {"index": 5, "label": "0.5 GT",     "value": 0.5,  "weight": 9},
    {"index": 6, "label": "0.2 GT",     "value": 0.2,  "weight": 20},
    {"index": 7, "label": "2 GT",       "value": 2.0,  "weight": 3},
]


def _pick_spin_prize():
    weights = [p["weight"] for p in SPIN_PRIZES]
    return random.choices(SPIN_PRIZES, weights=weights, k=1)[0]


SPIN_COOLDOWN_SECONDS = 86400  # rolling 24h


def _spin_remaining_seconds(user):
    """Returns seconds remaining on a user's spin cooldown, or 0 if available."""
    last_at_str = user.get("last_spin_at", "") if user else ""
    if not last_at_str:
        return 0
    try:
        last_dt = datetime.fromisoformat(last_at_str)
        elapsed = (datetime.now() - last_dt).total_seconds()
        if elapsed >= SPIN_COOLDOWN_SECONDS:
            return 0
        return int(SPIN_COOLDOWN_SECONDS - elapsed)
    except Exception:
        return 0


@app.route("/user/spin", methods=["POST"])
def user_spin():
    """One free spin per rolling 24h window. Server picks the prize and credits GT.

    Admins can spin freely (no cooldown, no GT credit since they have ∞).
    Non-admins are gated by user.last_spin_at (ISO timestamp).
    """
    data = request.get_json(force=True) or {}
    key = (data.get("access_key") or "").strip()
    if not key:
        return jsonify({"error": "Missing access_key"}), 400

    now = datetime.now()

    # Admin path — always pick a prize but don't track or credit
    if key == ADMIN_KEY:
        prize = _pick_spin_prize()
        return jsonify({
            "ok": True,
            "prize_index": prize["index"],
            "prize_label": prize["label"],
            "prize_value": prize["value"],
            "gt": 999.0,
            "is_admin": True,
            "next_spin_in": 0,
        })

    # Non-admin: enforce 24h cooldown + credit GT atomically
    with _users_lock:
        users = _load_users()
        user = users.get(key)
        if not user:
            return jsonify({"error": "Invalid access key"}), 401

        remaining = _spin_remaining_seconds(user)
        if remaining > 0:
            return jsonify({
                "error": "Spin on cooldown. Please wait.",
                "next_spin_in": remaining,
            }), 429

        prize = _pick_spin_prize()

        # GT is stored under coins.gold for legacy compat; mirror to coins.gt as float
        coins = user.setdefault("coins", {"bronze": 0, "silver": 0, "gold": 0})
        current_gt = float(coins.get("gt", coins.get("gold", 0) or 0))
        new_gt = round(current_gt + float(prize["value"]), 2)
        coins["gt"] = new_gt
        coins["gold"] = new_gt        # keep legacy field in sync so /user/use_coin still works
        user["last_spin_at"] = now.isoformat()
        # Drop legacy field if present
        user.pop("last_spin", None)
        _save_users(users)

    return jsonify({
        "ok": True,
        "prize_index": prize["index"],
        "prize_label": prize["label"],
        "prize_value": prize["value"],
        "gt": new_gt,
        "coins": coins,
        "next_spin_in": SPIN_COOLDOWN_SECONDS,
    })


@app.route("/logs", methods=["POST"])
def get_logs():
    data = request.get_json(force=True) or {}
    key  = data.get("access_key", "").strip()
    if not key:
        return jsonify({"error": "Missing access_key"}), 400
    is_admin = (key == ADMIN_KEY)
    logs = _load_logs()
    # Only show SUCCESSFUL claims — never failures, already-claimed, or pending.
    logs = [l for l in logs if l.get("result") == "success"]
    if not is_admin:
        user = get_user(key)
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        # Both regular users and group users see every successful claim under their key.
        logs = [l for l in logs if l.get("access_key") == key]
    safe = []
    for l in logs:
        e = dict(l)
        e.pop("access_key", None)
        safe.append(e)
    return jsonify({"ok": True, "logs": safe})


@app.route("/logs/update", methods=["POST"])
def update_log():
    data   = request.get_json(force=True) or {}
    key    = data.get("access_key", "").strip()
    first_name = (data.get("first_name") or "").strip()
    log_id = data.get("log_id", "").strip()
    result = data.get("result", "unknown").strip()   # 'success' | 'failed' | 'already_claimed' | 'stopped'
    detail = data.get("detail", "").strip()
    if not key or not log_id:
        return jsonify({"error": "Missing access_key or log_id"}), 400
    is_admin = (key == ADMIN_KEY)
    user = None
    if not is_admin:
        user = get_user(key)
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
    _update_log(log_id, result, detail)

    # Persist the success to the long-running stats counter
    if result == "success":
        _increment_stats(1)

    # On a successful claim, bump the seat's daily count.
    if result == "success" and user and _is_group_key(user) and first_name:
        with _users_lock:
            users = _load_users()
            u = users.get(key)
            if u and _is_group_key(u):
                seat = _get_seat(u, first_name)
                if seat:
                    _record_seat_claim(seat)
                    _save_users(users)
    return jsonify({"ok": True})


@app.route("/logs/clear", methods=["POST"])
def clear_logs():
    data = request.get_json(force=True) or {}
    if not _require_admin(data):
        return jsonify({"error": "Unauthorized"}), 401
    with _log_lock:
        _save_logs([])
    return jsonify({"ok": True})


@app.route("/run", methods=["POST"])
def run_script():
    data = request.get_json(force=True) or {}
    access_key = data.get("access_key", "").strip()
    is_admin   = access_key == ADMIN_KEY
    if not is_admin:
        if not get_user(access_key):
            return jsonify({"error": "Unauthorized"}), 401
    cfg   = data.get("cfg", {})
    batch = data.get("batch", "").strip()
    if not cfg.get("tokens"):
        return jsonify({"error": "No tokens provided"}), 400

    # ── Server-side code whitelist enforcement for non-admins ──
    if not is_admin and batch:
        allowed = VALID_BATCH_CODES.get(batch)
        if allowed is None:
            return jsonify({"error": f"Unknown batch: {batch}"}), 400
        submitted = cfg.get("codes", [])
        bad = [c for c in submitted if c not in allowed]
        if bad:
            return jsonify({"error": f"Unauthorized codes for batch '{batch}': {', '.join(bad)}"}), 403

    q = queue.Queue()
    def worker():
        try: run_collect(cfg, q)
        except Exception as e:
            q.put(f"  \u274c Fatal error: {str(e)}"); q.put(None)
    threading.Thread(target=worker, daemon=True).start()

    def generate():
        while True:
            try:
                msg = q.get(timeout=120)
                if msg is None: yield sse("[DONE]"); break
                yield sse(msg)
            except queue.Empty:
                yield sse("  \u26a0\ufe0f  Timeout waiting for output")
                yield sse("[DONE]"); break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})




# ─── VOUCHER CHECKER — Check SHEIN coupon code validity ─────────────────────────
# Uses m.shein.com (mobile web) endpoint, NOT the app API.
# Requires headers/cookies captured from a live browser session.

def _parse_raw_request(raw_text):
    """Parse a raw HTTP request block into headers and cookies dicts."""
    headers = {}
    cookies = {}
    lines = raw_text.replace("\r\n", "\n").split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("------") or line.startswith("Content-Disposition"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        # Skip pseudo-headers
        if key in ("host", ":method", ":path", ":scheme", ":authority",
                   "content-length", "accept-encoding"):
            continue
        if key == "cookie":
            # Parse cookie string
            for part in val.split(";"):
                part = part.strip()
                if "=" in part:
                    ck, _, cv = part.partition("=")
                    cookies[ck.strip()] = cv.strip()
        else:
            headers[key] = val
    return headers, cookies



# ─── VOUCHER CHECKER TOKENS (from check4.py — update when expired) ──────────────
_VC_CSRF_TOKEN  = "H4m5n5g4-M69ByiNrBD4fPe5765ZY2TNB4xA"
_VC_ARMOR_TOKEN = "T1_3.11.1_i7HCNI8sWSUN4Wsff-xWfA_wymkZr8ZujyE4SsVt9utwml9IWwOcS7ira_dRreckHQcsoFJKau3k91-w442rYjAQjYfCjfFyqRSedoUDvS7Q9JCqUlbf8vNIB1glhOV2FQFl1yZkSfTFkWj0j3g9Sz8gRFRQg43YKJbBPvQfuIR2nMJof8NfUqmc905vqvrk_1771293583244"
_VC_GW_AUTH     = "a=xjqHR52UWJdjKJ0x6QrCsus66rNXR9@2.0.13&b=1771293617218&d=06942fbc37be6a98b8dee877d03ae8f6&e=mFh0dNjY5MTNhZjUyMDQ4ZjI3ODllNzI0ZDM3ZGIzNDlhOWJmYjI5NTZiZWFjMzBhMjA3MmI0YTYyYTE3YTdjNGE1NQ%3D%3D"
_VC_OETS        = "Q0RERERBOEZfRDdBOHwxNzcxMjkzNjE3MjE0fF9BOEM0XzAzMzNfMjFFRTQ4NkVDOEUz"
_VC_AD_FLAG     = "8FkZa3NuCnoO+IBsqSDbVS0ujpVi/IXf//kOjWMsgnOZ0k+h6+rK67aH0c1E5efGgWYq61YJe9rvWC5xPj7ZVVYoP3i9GVpBfVkWfa8EsrsGa2KgaixOSZmJfGKy6YI874yU0pb2l85B3RSMhSMlbQ=="
_VC_CS_RANDOM   = "13d0ed2175cf112b17e661ea448bf807dc073fff312f9a732f747fddde75b7081483643d1d2b8b191459bb55b8152817be5a149d8282469aeb2e021dd95d3806380d1b37b49f2"
_VC_SMDEVICE_ID = "WHJMrwNw1k/E+z84j8tgt0f9TxszxjZNAF2hDlFX7M/j2A2yEqRYNlBkLVY6ldWz+PAi3O8iCtcEt8WvL21GJWz3he++ufgg9dCW1tldyDzmQI99+chXEirQLphdG1x7TYp5HxsF710xU/V4b7llpcwCHPPxycwCneu8bpbMPuOTJc3aMEBGDbKyOlsVOXoQi+2yltWZPHiNiNVCcw5ywKABbHDKE2ZJX47xtY+olePHdwMGnu62zyIYmEYy08SbpU5HFArMcZ/s4=1487582755342"
_VC_COOKIES     = "AT=MDEwMDE.eyJiIjo3LCJnIjoxNzcxMjkyNjMzLCJyIjoiVjE3a29QIiwidCI6MX0.521118d5108204c6; armorUuid=20260217094353400b02ad9368610b36a11e1aeab7548a000118dbe4250f9400; sessionID_shein_m_pwa=s%3ACawpVdJzUolGkgtr2urOf8OdVvYm5Oy4.lzw6IjzIJMrSHiPSHtjcBcb7NT1s6m7GwUyukf4oQoc; _cfuvid=TeMBOHqid.Gp1YtFdYVdEk.Rt.mbWkfvUqCGp7KUVU0-1771292633255-0.0.1.1-604800000; smidV2=20260217094355ab6d3ee5a4abe43ca0b980dfee559e3500e2453714a609340; zpnvSrwrNdywdz=center; _fbp=fb.1.1771292636328.841372440921999766; _gcl_au=1.1.2083526680.1771292636; _pin_unauth=dWlkPU5UZGlabVF6WWpNdFpqYzVOaTAwTW1VNUxUbG1ZVFV0WkRVek5qZGlOamM0TWpCbQ; _cbp=fb.1.1771292637098.15324706; cf_clearance=hx8uapTLCiBc_3.dCP5APmgp3VS8_LKV3_GhBq.WaaQ-1771293582-1.2.1.1-lg0cXDZt5Qe.Yg4q.ytI_NZC5a9lPEMp5SOVApPlAP1Bja7aJheu0Rzx6.jhXxUNmiMSV8fTz1wgVSM3QkD5j.JAUQJ_.85F1exuU6LI.BxyzaZ_Azv4s7N1eM1yJN0VaVKFc9p7KJI_BKzJcnXWeuxcLcUn8bV.zBw7WXCL8V7GC_ut9z0FC_HPgM1uOLVFd5xRj1PFa1PAkuR_vLkObhC_GEOnnN0Uvam803KSHhs; _uetsid=1f429ae00ba211f1b1a9ed8eb5ef1e3d; _uetvid=1f4304700ba211f18fc439a6eadea8ec; cto_bundle=98A2_l9yJTJCSEN0eHUlMkYyRFAwRlZLb0hCcThXUmtDUjlVYkFwUCUyRlZ6WDB0eDZYNHdlQmhFS3pleVBhUmlPejJ4anR6bFdCJTJGZ2dDSHpjVHRzVlhUNzRyaEVwUXlyVnVSZkFRc0dTUVp6dVRVU3R0NU9QUkE3T2dVTCUyRkJRRUVpeWFyMGpSTWo; language=ph"

_VC_HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36",
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "smdeviceid":   _VC_SMDEVICE_ID,
    "timezone":     "GMT+8",
    "x-cs-random":  _VC_CS_RANDOM,
    "x-gw-auth":    _VC_GW_AUTH,
    "armortoken":   _VC_ARMOR_TOKEN,
    "x-csrf-token": _VC_CSRF_TOKEN,
    "x-oets":       _VC_OETS,
    "x-ad-flag":    _VC_AD_FLAG,
    "Cookie":       _VC_COOKIES,
}


@app.route("/check_coupon", methods=["POST"])
def check_coupon():
    """Check a single SHEIN voucher code for validity.
    Expects JSON: { code: str }
    Returns: { ok: bool, valid: bool, code: str, discount: str, min_spend: str }
    """
    data = request.get_json(force=True) or {}
    code = (data.get("code") or "").strip()

    if not code:
        return jsonify({"ok": False, "error": "No code provided"}), 400

    try:
        r = requests.post(
            "https://m.shein.com/ph/bff-api/user-api/lure/query_coupons",
            params={"_ver": "1.1.8", "_lang": "en"},
            json={"couponCodes": [code], "login_from": "coupon"},
            headers=_VC_HEADERS,
            timeout=8,
            verify=False,
        )
        result = r.json()
        if str(result.get("code")) == "0":
            items = result.get("info", {}).get("list", [])
            if items:
                item      = items[0]
                discount  = item.get("maxValue")
                min_spend = item.get("threshold")
                if (discount not in (None, "", "N/A")
                        and min_spend not in (None, "", "No minimum", 0, "0")):
                    return jsonify({
                        "ok":        True,
                        "valid":     True,
                        "code":      code,
                        "discount":  str(discount),
                        "min_spend": str(min_spend),
                    })
        return jsonify({"ok": True, "valid": False, "code": code})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# ─── PRODUCT CHECKER TOKENS (from cart/checkout RAW — update when expired) ────
_PC_TOKEN    = "MDEwMDE.eyJiIjo3LCJnIjoxNzgyMDk5Nzk3LCJyIjoiS3lCWTRQIiwidCI6MiwibSI6NjE3NjM0ODU1MCwibCI6MTc4MjA5OTc5N30.3f4cd89c419d9ace.2af8846031eb93acee157f4f59dd583f4d76f52964c41acfc238a8ff3844899c"
_PC_ARMOR    = "T2_4.2.3_dyiSCTrDINqeOVnZoY4i2RfFFqQ3bFnGANIalapIcI9x7CmEzu3VSRDlFAnCDuIOiPe-WlwN1rOEZG2gm6RG8wgNOC-3MgyxAuB_Qes-JJOJ4hnIiEBWUUVeHAFBVP-zstS7hM7lR_aHh9WyPCBMXbSXpseYI5tJwTzw0YOrvwtRzYFbp_Yy4sXkFKPkIpTvapQ78bIJx7NFu3kYgVcTEiE7w9VDjDNuAjcMGQPYCDg_1782099942944"
_PC_AD_FLAG  = "plXxKLfeqJOYrUN5z1gN6iEjmfeAHd5t5i/TeZsiOKz8D3aIHr0KitBCKO6R4izH5kg3+ym8heA41X6YtSfzSQuH4dkWVzxy7b8ePL9yNK5vMx27SrDIc5D8r/DhNYLQLTAQ9rtyE31/sggdSwdW1c/l2AZltwU+Fny5FvLldGu4ykDgfrBY8rEd+xph1yq4Y03EZWqQDJ7PdcLuTE/POodJahkijQBq+ZW4nivGYhk="
_PC_GW_AUTH  = "a=vhOvoNStVeIKfdWT0Gp55rGPHqjicT@8.6.4&b=1782099953499&d=1446885240&e=2cXNCNDkzNjgwYTNhYTc1NzFmYmUyM2Q2OTc4OTY1NmQ0YjU0YjZjZjg2MjQ4M2M2YzEwYmJjNGZhODhjZjc4NzM3MA%3D%3D"
_PC_CS_RND   = "23d0ed2171bd00aab1fa8f9e8dedc46281f44bf38e563d275b69a0e7d3be871e8ed1b9d0a1d2511de48680581e18d86da61342c1ae60763238e625e9ae1637b4b446fb322a052"
_PC_ANTI_IN  = "2_4.2.3_d4eaff_1jnW8-fGBLGeY1qbHh61uxwrccjQ-cfgkapYR_RGKkrWUwcoPE3Egd9WpW6-eEVM5bFwIfhB_XahsbFadxipq7K6iZS0UBprNkUh7Af8kMzexWRstxsPc1XVpkPb6ceSiD1iBDF6JEz2M2cmD5FO7oRXDbfoIk4H9c1flZ6FU0KCkakJOA9dm3zehxF0dVuLA_KqI7rRosFGiQfDMOePFTO44qkIRSPsBTD2TbQNQAy_FCQvdjADd4UQR3YomCbrT_rIgEqxcJMZCIg8Rn6pJKRvC8lSsDKacDy6gfR6HJzvDC7pCXt5-m9oP4MPX4xgVVc_pHvMwFDO8oaABLJj2h_7D8q6WILVz4J4IN5Cmujtfb90icOCZSHG_9g8aVGk8axipZmZyiV_79znyvPknJ4ixxSZ5uuzFF581NB8ClzuWTrKvOFdjB2KIcGXnKM3ycAGXj-0BmmrwtQkHRYOfOR9g0-fhsavh0MmqsamyVY89pNrP_2cXbXmuCftAoqRAp9GtoBBz7uy_yZtYi74kEVTBTw-iXO1AqJL679nzJ5x2iqKzFaHsXt8ZEKS6f8Q1W3dYrgXvXc3FbpU9FEvKw"
_PC_SMDEV    = "20260607134457b84e1eabb13a1e5df5c906e87e9b70b801b456fe4e06ba32"
_PC_UGID     = "040mf5ex2w"
_PC_SORTUID  = "6176348550"
_PC_DEV_ID   = "shein_3be5c0ff-9628-3d14-a923-7541c2494e60"
_PC_DEVICE   = "ELP-NX9 Android16"
_PC_VERSION  = "13.9.8"
_PC_UA       = "Shein 13.9.8 Android 16 ELP-NX9 US en 6176348550"
_PC_COOKIE   = "_f_c_llbs_=K2909_1782099533_V0Ii4yY0WdG1H2ZLT5ZAQQ0WQgqDxLLNejE1mNs9wTNTi8fMo8n5b_whzT6AgimrkIv0GWkZL4pEojihYfmGuFP6uc6JL-K1o-Ay4_RidXDVzdmKOHr04sRWWdSdpFpDEx1Bxx2e3r-Ci_eIeI3_pY7lSZjMk2X7wfqp4hz7DBouPRh_eC5NB81yOQzkD6eLUkEtmurxLxdODyPDrKPvP2D3KRMtHf5sfE1LsmkHiRatndTlOSws9QqNRd63C-FltrObOWMOLR-d7MSneekyhVS6I9L4shsqUt8C4riPDed7na5bGb-OcDkFtKfWAJ0jU1bVRJwPlctpvkgRcj0Ccw"
_PC_ADDR = {
    "address_id": "2130879055", "city": "DUMAGUETE-CITY",
    "postcode": "6200", "state": "NEGROS-ORIENTAL", "country_id": "170",
}


def _pc_hdr(extra=None, auth=None):
    """Build app API headers. Overrides time-sensitive tokens from fresh user RAW when provided."""
    a   = auth or {}
    tok = a.get("token")      or _PC_TOKEN
    arm = a.get("armor")      or _PC_ARMOR
    gw  = a.get("gw_auth")    or _PC_GW_AUTH
    sm  = a.get("smdevice")   or _PC_SMDEV
    ug  = a.get("ugid")       or _PC_UGID
    uid = a.get("sortuid")    or _PC_SORTUID
    did = a.get("device_id")  or _PC_DEV_ID
    ac  = a.get("appcountry") or "US"
    av  = a.get("app_version") or _PC_VERSION
    di  = a.get("device_info") or _PC_DEVICE
    ua  = f"Shein {av} Android 16 {di} {ac} en {uid}"
    h = {
        "host":             "api-service.shein.com",
        "app-from":         "shein",
        "siteuid":          "android",
        "appcountry":       ac,
        "uberctx-traffic-mark-member": "6",
        "devtype":          "Android",
        "clientid":         "100",
        "ugid":             ug,
        "accept":           "application/json",
        "device":           di,
        "armortoken":       arm,
        "applanguage":      "en",
        "usercountry":      "PH",
        "version":          av,
        "devicelanguage":   "en",
        "x-ad-flag":        _PC_AD_FLAG,
        "dev-id":           did,
        "sortuid":          uid,
        "device_language":  "en",
        "apptype":          "shein",
        "localcountry":     "PH",
        "smdeviceid":       sm,
        "deviceid":         did,
        "uberctx-personal-switch": "r-1.s-1.u-1",
        "platform":         "app-native",
        "appname":          "shein app",
        "appversion":       av,
        "newuid":           uid,
        "language":         "en",
        "currency":         "PHP",
        "network-type":     "4G",
        "token":            tok,
        "os-version":       "16",
        "devicesystemversion": "Android16",
        "appcurrency":      "PHP",
        "user-agent":       ua,
        "anti-in":          _PC_ANTI_IN,
        "x-gw-auth":        gw,
        "accept-encoding":  "br,gzip",
        "x-cs-random":      _PC_CS_RND,
        "content-type":     "application/json; charset=utf-8",
        "cookie":           _PC_COOKIE,
    }
    if extra:
        h.update(extra)
    return h


@app.route("/pc/lookup", methods=["POST"])
def pc_lookup():
    """Fetch product image by goods_id.
    Skips the unreliable static-data-v2 endpoint (returns 836000 on some accounts).
    Full product info (name, price, SKU) is returned by /pc/add_to_cart.
    """
    data     = request.get_json(force=True) or {}
    goods_id = str(data.get("goods_id", "")).strip()
    auth     = data.get("auth") or {}
    if not goods_id:
        return jsonify({"ok": False, "error": "No goods_id provided"}), 400

    try:
        ir = requests.get(
            "https://api-service.shein.com/product/get_goods_detail_image",
            params={"goods_id": goods_id},
            headers=_pc_hdr(auth=auth),
            timeout=10, verify=False,
        )
        img_data = ir.json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Lookup failed: {e}"}), 500

    img_code = str(img_data.get("code", ""))
    if img_code != "0":
        msg = img_data.get("msg") or f"SHEIN error (code {img_code})"
        if img_code in ("100002", "200401", "10000", "401", "460101"):
            msg = f"Token expired — re-paste your app RAW in Cloud Runner (code {img_code})"
        return jsonify({"ok": False, "error": msg})

    imgs     = (img_data.get("info") or {}).get("goods_images") or []
    main_img = imgs[0].get("image_url") if imgs else ""

    # Try realtime data to get first available SKU and basic product info
    sku_code  = ""
    sku_label = ""
    goods_name = f"Product #{goods_id}"
    sale_price = ""
    try:
        # Exact params from captured URL
        rd_r = requests.get(
            "https://api-service.shein.com/product/get_goods_detail_realtime_data",
            params=[
                ("priorityMallType",           "1"),
                ("sceneFromPage",              ""),
                ("isRelatedColorNeedPromotion",""),
                ("promotionId",                ""),
                ("isAppointMall",              "0"),
                ("useSupplyGoods",             ""),
                ("isUserSelectedMallCode",     "0"),
                ("sceneFlag",                  ""),
                ("mallCode",                   "1"),
                ("localSiteQueryFlag",         "0"),
                ("orderPrice",                 ""),
                ("isHideNotSatisfied",         ""),
                ("isSizeGatherTag",            ""),
                ("hasReportMember",            "0"),
                ("sourceFrom",                 "goods_detail"),
                ("promotionLogoType",          ""),
                ("promotionType",              ""),
                ("isHidePromotionTip",         ""),
                ("goods_id",                   goods_id),
                ("timeZone",                   "Asia/Manila"),
                ("isHideEstimatePriceInfo",    ""),
                ("popComponentEntry",          ""),
                ("bundledPurchaseMainGoodsId", ""),
                ("visitNumOfDay",              "2"),
                ("isShowMall",                 "0"),
                ("isPaidMember",               "0"),
                ("billno",                     ""),
                ("promotionProductMark",       ""),
            ],
            headers=_pc_hdr(auth=auth),
            timeout=10, verify=False,
        )
        rd = rd_r.json()
        if str(rd.get("code")) == "0":
            rdi = rd.get("info") or {}

            # Exhaustive SKU list extraction — try every known path
            def _find_sku_list(d):
                for key in ("sku_list", "skuList", "sku_detail", "skuDetail"):
                    v = d.get(key)
                    if v:
                        return v
                return []

            sku_list = (_find_sku_list(rdi)
                        or _find_sku_list(rdi.get("detail") or {})
                        or _find_sku_list(rdi.get("productInfo") or {})
                        or _find_sku_list(rdi.get("skuInfo") or {})
                        or _find_sku_list(rdi.get("multiSkcPrice") or {}))

            if sku_list:
                first_sku = sku_list[0]
                sku_code  = (first_sku.get("sku_code")
                             or first_sku.get("skuCode")
                             or first_sku.get("sku") or "")
                attrs = (first_sku.get("sku_sale_attr")
                         or first_sku.get("skuSaleAttr")
                         or first_sku.get("saleAttr") or [])
                sku_label = ", ".join(
                    a.get("attrValue") or a.get("attr_value") or ""
                    for a in attrs
                    if (a.get("attrValue") or a.get("attr_value"))
                )

            # Name / price
            det = rdi.get("detail") or {}
            gn  = det.get("goods_name") or rdi.get("goods_name") or ""
            if gn:
                goods_name = gn
            sp = (det.get("salePrice")
                  or (rdi.get("priceInfo") or {}).get("salePrice")
                  or {})
            if sp.get("amountWithSymbol"):
                sale_price = sp["amountWithSymbol"]
    except Exception:
        pass  # image card still shows; add_to_cart response will fill details

    return jsonify({
        "ok":         True,
        "goods_id":   goods_id,
        "image_url":  main_img,
        "goods_name": goods_name,
        "sale_price": sale_price,
        "sku_code":   sku_code,
        "sku_label":  sku_label or ("Tap Add to Cart to load full details" if not sku_code else "Default"),
    })


@app.route("/pc/add_to_cart", methods=["POST"])
def pc_add_to_cart():
    """Add a product to the SHEIN cart."""
    data     = request.get_json(force=True) or {}
    goods_id = str(data.get("goods_id", "")).strip()
    sku_code = str(data.get("sku_code", "")).strip()
    if not goods_id:
        return jsonify({"ok": False, "error": "No goods_id"}), 400

    auth = data.get("auth") or {}
    body = {"sku_code": sku_code, "quantity": 1, "mall_code": "1", "goods_id": goods_id}
    try:
        r = requests.post(
            "https://api-service.shein.com/order/add_to_cart",
            params={"goods_id": goods_id},
            json=body,
            headers=_pc_hdr(auth=auth),
            timeout=12, verify=False,
        )
        result = r.json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Add to cart failed: {e}"}), 500

    rc = str(result.get("code", ""))
    if rc != "0":
        raw_msg = result.get("msg") or ""
        err = raw_msg or f"SHEIN add-to-cart error (code {rc})"
        if rc in ("100002", "200401", "401"):
            err = f"Token expired — re-paste RAW in Cloud Runner (code {rc})"
        return jsonify({"ok": False, "error": err})

    info    = result.get("info") or {}
    cart    = info.get("cart") or {}
    product = cart.get("product") or {}

    # Extract SKU label (color + size) from sku_sale_attr
    attrs     = product.get("sku_sale_attr") or []
    sku_label = ", ".join(a.get("attrValue", "") for a in attrs if a.get("attrValue"))
    sku_code  = product.get("sku_code") or cart.get("skuCode") or ""

    return jsonify({
        "ok":         True,
        "quantity":   info.get("effectiveProductLineSumQuantity", 1),
        "unit_price": (cart.get("unitPrice") or {}).get("amountWithSymbol", ""),
        "saved":      (info.get("savedPrice") or {}).get("amountWithSymbol", ""),
        "goods_name": product.get("goods_name", ""),
        "sku_code":   sku_code,
        "sku_label":  sku_label or "Default",
    })


@app.route("/pc/checkout", methods=["POST"])
def pc_checkout():
    """Simulate SHEIN checkout — returns price breakdown + available coupons."""
    data        = request.get_json(force=True) or {}
    coupon_code = (data.get("coupon_code") or "").strip()
    auth        = data.get("auth") or {}

    # Build checkout body
    co_body = {
        "biz_mode_list":     ["0"],
        "and_page":          "v2",
        "request_card_token":"1",
        "hasCardBin":        "1",
        "goods_type":        "0",
        "userLocalSizeCountry": "",
        "is_old_version":    "0",
        "giftcard_verify":   "0",
        "isFirst":           "1",
        "popup":             {"oneClickLowestTimes": "0"},
        **_PC_ADDR,
    }
    if coupon_code:
        co_body["cart_optimal_coupon_list"] = [coupon_code]

    try:
        co_r = requests.post(
            "https://api-service.shein.com/order/order/checkout",
            json=co_body,
            headers=_pc_hdr(extra={
                "frontend-scene": "page_checkout",
                "ruleids":        "56830_1782099945483",
                "sessionid":      f"{_PC_SORTUID}1782099953496",
            }, auth=auth),
            timeout=15, verify=False,
        )
        co = co_r.json()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Checkout failed: {e}"}), 500

    if str(co.get("code")) != "0":
        return jsonify({"ok": False, "error": co.get("msg") or "Checkout error"})

    info = co.get("info") or {}

    # Price rows (only visible ones)
    price_rows = []
    for row in (info.get("sorted_price") or []):
        if row.get("show") == 1:
            price_rows.append({
                "label":    row.get("local_name", ""),
                "value":    row.get("price_with_symbol", ""),
                "type":     row.get("type", ""),
                "negative": "negative_price" in (row.get("show_type") or []),
            })

    grand_total = (
        (info.get("total_price_info") or {})
        .get("grandTotalPrice") or {}
    ).get("amountWithSymbol", "")

    saved_tip = info.get("saved_total_price_text", "")

    # Applied coupons (from checkout response)
    coupon_list = info.get("coupon_list") or []
    coupons_out = []
    for c in coupon_list:
        coupons_out.append({
            "couponCode":    c.get("couponCode", ""),
            "discount_price": (c.get("discount_price") or {}).get("amountWithSymbol", ""),
        })

    # Also fetch full available coupon list
    try:
        cl_r = requests.get(
            "https://api-service.shein.com/order/cart/coupon/list",
            params={"is_return": "1", "enableCouponCmp": "1", "is_old_version": "0"},
            headers=_pc_hdr(auth=auth), timeout=10, verify=False,
        )
        cl = cl_r.json()
        usable = (cl.get("info") or {}).get("usableCouponList") or []
        for c in usable:
            code = c.get("couponCode") or c.get("coupon_code") or ""
            if code and not any(x["couponCode"] == code for x in coupons_out):
                coupons_out.append({
                    "couponCode":    code,
                    "discount_price": (c.get("discount_price") or {}).get("amountWithSymbol", ""),
                })
    except Exception:
        pass

    coupon_info = info.get("couponInfo") or {}
    applied_tip = coupon_info.get("optimalCouponTip", "")
    import re as _re
    applied_tip = _re.sub(r"<[^>]+>", "", applied_tip)  # strip HTML tags

    return jsonify({
        "ok":          True,
        "grand_total": grand_total,
        "saved_tip":   saved_tip,
        "price_rows":  price_rows,
        "coupons":     coupons_out,
        "applied_tip": applied_tip,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
