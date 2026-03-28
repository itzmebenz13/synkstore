import os
import sys
import uuid
import time
import threading
import queue
import requests
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, Response, jsonify
from flask_cors import CORS

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
CORS(app)

ACCESS_KEY = os.environ.get("ACCESS_KEY", "ssu2024")

COUPON_URL   = "https://api-service.shein.com/promotion/coupon/bind_coupon"
DELIVERY_URL = "https://api-shein.shein.com/deliveryapi/delivery-material/material_list"
LANGUAGE     = "en"

ERR_INVALID_PKG     = 1000
ERR_ALREADY_CLAIMED = 501405


# ─── helpers ──────────────────────────────────────────────────────────────────

def build_headers(cfg, token):
    claim_country  = cfg.get("claim_country", "PH")
    currency_map   = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    return {
        "app-from":            "shein",
        "siteuid":             "android",
        "appcountry":          cfg.get("appcountry", "GB"),
        "devtype":             "Android",
        "clientid":            "100",
        "ugid":                cfg.get("ugid", ""),
        "accept":              "application/json",
        "device":              cfg.get("device_info", ""),
        "armortoken":          cfg.get("armor_token", ""),
        "applanguage":         LANGUAGE,
        "usercountry":         claim_country,
        "version":             cfg.get("app_version", "11.3.4"),
        "devicelanguage":      LANGUAGE,
        "dev-id":              cfg.get("device_id", ""),
        "sortuid":             cfg.get("sortuid", ""),
        "device_language":     LANGUAGE,
        "apptype":             "shein",
        "localcountry":        claim_country,
        "smdeviceid":          cfg.get("smdevice_id", ""),
        "deviceid":            cfg.get("device_id", ""),
        "platform":            "app-native",
        "appname":             "shein app",
        "appversion":          cfg.get("app_version", "11.3.4"),
        "newuid":              cfg.get("sortuid", ""),
        "language":            LANGUAGE,
        "currency":            claim_currency,
        "network-type":        "WIFI",
        "token":               token,
        "os-version":          "14",
        "devicesystemversion": "Android14",
        "appcurrency":         claim_currency,
        "user-agent":          f"Shein {cfg.get('app_version','11.3.4')} Android 14 {cfg.get('device_info','')} {cfg.get('appcountry','GB')} {LANGUAGE} {cfg.get('sortuid','')}",
        "x-gw-auth":           cfg.get("gw_auth", ""),
        "content-type":        "application/json; charset=utf-8",
    }


def build_delivery_headers(cfg, token):
    claim_country  = cfg.get("claim_country", "PH")
    currency_map   = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    gm_device      = cfg.get("gm_device_id", cfg.get("device_id", ""))
    gm_site        = cfg.get("gm_site", "andshph")
    app_version    = cfg.get("app_version", "11.3.4")
    device_info    = cfg.get("device_info", "")
    return {
        "host":              "api-shein.shein.com",
        "content-type":      "application/json",
        "accept":            "application/json, text/plain, */*",
        "appname":           "shein app",
        "apptype":           "shein",
        "brand":             "shein",
        "channel":           "h5",
        "siteuid":           gm_site,
        "localcountry":      claim_country,
        "currency":          claim_currency,
        "appcurrency":       claim_currency,
        "applanguage":       LANGUAGE,
        "language":          LANGUAGE,
        "timezone":          "GMT+8",
        "appversion":        app_version,
        "deviceid":          gm_device,
        "smdeviceid":        cfg.get("smdevice_id", ""),
        "ugid":              cfg.get("ugid", ""),
        "armortoken":        cfg.get("armor_token", ""),
        "x-gw-auth":         cfg.get("gw_auth", ""),
        "token":             token,
        "x-request-by":      "bridgeX",
        "route-bff":         "TRUE",
        "user-agent":        f"Mozilla/5.0 (Linux; Android 14; {device_info} Build/UKQ1; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.0.0 Mobile Safari/537.36 SheinApp(shein/{app_version}) TTID/shein Wing/1.0.1",
        "referer":           f"https://api-shein.shein.com/ark/11504?app=shein&device_type=android&language=en&site_uid={gm_site}&region=PH",
        "origin":            "https://api-shein.shein.com",
        "sec-fetch-site":    "same-origin",
        "sec-fetch-mode":    "cors",
        "sec-fetch-dest":    "empty",
        "accept-encoding":   "gzip, deflate, br",
        "accept-language":   "en-PH,en-US;q=0.9,en;q=0.8",
    }


# ─── SSE helper ───────────────────────────────────────────────────────────────

def sse(msg):
    return f"data: {msg}\n\n"


# ─── claim logic (streaming to queue) ────────────────────────────────────────

def run_collect(cfg, q):
    tokens  = cfg.get("tokens", [])
    codes   = cfg.get("codes", [])
    pkg_id  = cfg.get("pkg_id", "")
    mode    = cfg.get("mode", "bind")   # bind | delivery | brute

    def emit(msg):
        q.put(msg)

    if mode == "brute":
        _run_brute(cfg, q)
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
    q.put(None)  # sentinel


def _collect_bind(cfg, token, codes, pkg_id, emit):
    headers = build_headers(cfg, token)
    payload = {
        "couponPackages": [{
            "couponPackageId": str(pkg_id),
            "couponCodes":     ",".join(codes),
        }],
        "scene":          "home",
        "idempotentCode": str(uuid.uuid4()),
    }

    emit(f"\n  {'─'*54}")
    emit(f"  Package  : {pkg_id}")
    emit(f"  Country  : {cfg.get('claim_country','PH')}")
    emit(f"  Claiming {len(codes)} code(s): {', '.join(codes)}")
    emit(f"  {'─'*54}")

    try:
        r = requests.post(COUPON_URL, json=payload, headers=headers, timeout=15, verify=False)
        try:
            raw = r.json()
        except Exception:
            raw = {}

        data        = raw if isinstance(raw, dict) else {}
        top_code    = str(data.get("code") or data.get("ret_msg_code") or "")
        top_msg     = str(data.get("msg")  or data.get("tips") or "")
        _info       = data.get("info") or {}
        info        = _info if isinstance(_info, dict) else {}
        _sl         = info.get("successCodeList") or []
        _fl         = info.get("failCodeList")    or []
        success_list = [str(c).strip() for c in (_sl if isinstance(_sl, list) else []) if c]
        fail_list    = [str(c).strip() for c in (_fl if isinstance(_fl, list) else []) if c]
        result_list  = info.get("bindResult") or []
        result_list  = result_list if isinstance(result_list, list) else []

        if top_code == str(ERR_ALREADY_CLAIMED):
            emit("  ⚠️  ALREADY CLAIMED [501405]")
        elif success_list:
            emit(f"  ✅ CLAIMED! codes={', '.join(success_list)}")
        elif result_list:
            claimed_c, conflict_c, other_c = [], [], []
            for item in result_list:
                if not isinstance(item, dict): continue
                cv  = str(item.get("couponCode") or "?")
                ec  = str(item.get("errorCode") or item.get("code") or "")
                if ec in ("0", "200", ""):  claimed_c.append(cv)
                elif ec == str(ERR_ALREADY_CLAIMED): conflict_c.append(cv)
                else: other_c.append(f"{cv}[{ec}]")
            if claimed_c:   emit(f"  ✅ CLAIMED! {claimed_c}")
            if conflict_c:  emit(f"  ⚠️  ALREADY CLAIMED → {conflict_c}")
            if other_c:     emit(f"  ❌ FAILED → {other_c}")
        elif fail_list:
            emit(f"  ❌ FAILED {fail_list}")
        elif top_code not in ("0", "200", ""):
            emit(f"  ❌ ERR {top_code}: {top_msg[:80]}")
        else:
            emit(f"  ❓ Ambiguous response — code={top_code} msg={top_msg[:60]}")

    except requests.exceptions.Timeout:
        emit("  ❌ Request timed out")
    except Exception as e:
        emit(f"  ❌ Error: {str(e)[:120]}")

    emit(f"  {'─'*54}")


def _collect_delivery(cfg, token, emit):
    gm_site    = cfg.get("gm_site", "andshph")
    gm_mid     = cfg.get("gm_mid", "4142402")
    claim_country  = cfg.get("claim_country", "PH")
    currency_map   = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    app_version    = cfg.get("app_version", "11.3.4")
    gm_device      = cfg.get("gm_device_id", cfg.get("device_id", ""))

    headers = build_delivery_headers(cfg, token)
    payload = {
        "client_info": {
            "app_version": app_version, "client_id": 100,
            "currency": claim_currency, "dev_id": gm_device,
            "language": LANGUAGE, "site_uid": gm_site,
            "token": token, "brand": "shein",
        },
        "material_request_info": {
            "mid": gm_mid,
            "param_map": {
                "coupon_common_req": {"coupon_type": 2, "coupon_sequence": 3},
                "auto_bind": True,
            },
            "data_type": "SwiftCouponOnePlugin",
            "data_scene": 0,
            "data_scene_flag": "0_SwiftCouponOnePlugin",
        },
        "ext_map": {},
    }
    params = {"sw_site": gm_site, "sw_lang": LANGUAGE}

    emit(f"\n  {'─'*54}")
    emit(f"  Mode     : DELIVERY API (auto_bind)")
    emit(f"  MID      : {gm_mid}")
    emit(f"  Country  : {claim_country} ({claim_currency})")
    emit(f"  {'─'*54}")

    try:
        r = requests.post(DELIVERY_URL, json=payload, headers=headers, params=params, timeout=15, verify=False)
        try:
            data = r.json()
        except Exception:
            data = {}

        code = str(data.get("code", ""))
        if code not in ("0", "200"):
            emit(f"  ❌ API error: {data.get('msg', 'Unknown')}")
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
            emit(f"  ✅ {len(success_list)} claimed, {len(fail_list)} failed")
            for c in success_list:
                emit(f"     ✓ {c.get('couponCode','?')} (id:{c.get('couponId','?')})")
        elif hit_coupon:
            emit(f"  ⚠️  Already claimed — {len(hit_coupon)} coupon(s) already in account")
        elif recv_coupon:
            emit(f"  ✅ {len(recv_coupon)} already received")
        else:
            bind_code = bind_result.get("bindCode", "")
            detail    = info.get("detail_msg") or data.get("msg") or "No result"
            emit(f"  ❓ No bind result — bindCode={bind_code} detail={detail}")

    except requests.exceptions.Timeout:
        emit("  ❌ Request timed out")
    except Exception as e:
        emit(f"  ❌ Error: {str(e)[:120]}")

    emit(f"  {'─'*54}")


# ─── brute force ──────────────────────────────────────────────────────────────

def _run_brute(cfg, q):
    tokens       = cfg.get("tokens", [])
    codes        = cfg.get("codes", [])
    start        = int(cfg.get("brute_start", 17130000))
    end          = int(cfg.get("brute_end",   17140000))
    threads      = min(int(cfg.get("brute_threads", 10)), 30)
    max_claims   = int(cfg.get("brute_max_claims", 1))
    claim_country = cfg.get("claim_country", "PH")
    currency_map  = {"PH": "PHP", "MY": "MYR", "TH": "THB"}
    claim_currency = currency_map.get(claim_country, "PHP")
    token        = tokens[0] if tokens else ""

    print_lock    = threading.Lock()
    claims_lock   = threading.Lock()
    done_lock     = threading.Lock()
    stop_event    = threading.Event()
    claims_total  = [0]
    done_count    = [0]
    already_claimed = []

    def emit(msg):
        with print_lock:
            q.put(msg)

    pkg_range = list(range(start, end + 1))
    total     = len(pkg_range)

    emit("=" * 64)
    emit("  BRUTE-FORCE MODE  —  live output")
    emit(f"  Range   : {start} → {end}  ({total} IDs)")
    emit(f"  Threads : {threads}")
    emit(f"  Stop at : {max_claims} claim(s)  (0 = unlimited)")
    emit(f"  Country : {claim_country} ({claim_currency})")
    emit(f"  Codes   : {', '.join(codes)}")
    emit("=" * 64)

    def probe(pkg_id):
        if stop_event.is_set():
            return
        headers = build_headers(cfg, token)
        payload = {
            "couponPackages": [{"couponPackageId": str(pkg_id), "couponCodes": ",".join(codes)}],
            "scene": "home",
            "idempotentCode": str(uuid.uuid4()),
        }
        with done_lock:
            done_count[0] += 1
            done = done_count[0]
        pct = min(done * 100 // total, 100)

        try:
            r = requests.post(COUPON_URL, json=payload, headers=headers, timeout=12, verify=False)
            try:
                raw = r.json()
            except Exception:
                raw = {}
            data      = raw if isinstance(raw, dict) else {}
            top_code  = str(data.get("code") or data.get("ret_msg_code") or "")
            top_msg   = str(data.get("msg")  or data.get("tips") or "")
            _info     = data.get("info") or {}
            info      = _info if isinstance(_info, dict) else {}
            _sl       = info.get("successCodeList") or []
            success_list = [str(c).strip() for c in (_sl if isinstance(_sl, list) else []) if c]
            pkg_code  = str(info.get("couponPackageCode") or "")
            error_code = str(info.get("errorCode") or "")

            if top_code == str(ERR_INVALID_PKG):
                return
            if top_code == str(ERR_ALREADY_CLAIMED) or pkg_code == str(ERR_ALREADY_CLAIMED) or error_code == str(ERR_ALREADY_CLAIMED):
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ⚠  ALREADY CLAIMED [501405]")
                already_claimed.append(pkg_id)
                return
            if success_list:
                with claims_lock:
                    claims_total[0] += len(success_list)
                    total_so_far = claims_total[0]
                    if max_claims > 0 and claims_total[0] >= max_claims:
                        stop_event.set()
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ★ CLAIMED! codes=[{', '.join(success_list)}] total={total_so_far}")
                return

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
                        if max_claims > 0 and claims_total[0] >= max_claims:
                            stop_event.set()
                    emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ★ CLAIMED! {claimed_c}")
                if conflict_c:
                    emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ⚠  [501405] → {conflict_c}")
                if other_c:
                    emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ✗ FAIL → {other_c}")
                return

            _fl = info.get("failCodeList") or []
            fail_list = [str(c).strip() for c in (_fl if isinstance(_fl, list) else []) if c]
            if fail_list:
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ✗ FAILED {fail_list}")
                return
            if top_code not in ("0","200",""):
                emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ✗ ERR {top_code}: {top_msg[:50]}")

        except requests.exceptions.Timeout:
            emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ✗ TIMEOUT")
        except Exception as e:
            emit(f"  [{pct:3d}%] [{done}/{total}] pkg={pkg_id} ✗ EXC: {str(e)[:80]}")

    with ThreadPoolExecutor(max_workers=threads) as exe:
        futs = [exe.submit(probe, pkg) for pkg in pkg_range]
        try:
            for fut in as_completed(futs):
                if stop_event.is_set():
                    exe.shutdown(wait=False, cancel_futures=True)
                    break
        except Exception:
            pass

    emit("=" * 64)
    emit(f"  Done. Scanned {done_count[0]}/{total} IDs.")
    emit(f"  Total claimed   : {claims_total[0]} coupon(s)")
    emit(f"  Already claimed : {len(already_claimed)} package(s)")
    if already_claimed:
        for p in already_claimed:
            emit(f"    → {p}")
    emit("=" * 64)
    q.put(None)


# ─── routes ───────────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/run", methods=["POST"])
def run_script():
    data = request.get_json(force=True) or {}

    # Auth check
    if data.get("access_key") != ACCESS_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    cfg = data.get("cfg", {})
    if not cfg.get("tokens"):
        return jsonify({"error": "No tokens provided"}), 400

    q = queue.Queue()

    def worker():
        try:
            run_collect(cfg, q)
        except Exception as e:
            q.put(f"  ❌ Fatal error: {str(e)}")
            q.put(None)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    def generate():
        while True:
            try:
                msg = q.get(timeout=120)
                if msg is None:
                    yield sse("[DONE]")
                    break
                yield sse(msg)
            except queue.Empty:
                yield sse("  ⚠️  Timeout waiting for output")
                yield sse("[DONE]")
                break

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
