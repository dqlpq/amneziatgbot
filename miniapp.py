import asyncio
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.parse
from functools import wraps

import aiohttp
from flask import Flask, request, jsonify, render_template_string, g
from config import settings
from database import Database, MAX_PROFILES_PER_USER
from amnezia_client import AmneziaClient
from shared import get_shared_ping

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

_db: Database | None = None
_amnezia: AmneziaClient | None = None

VPN_NAME_RE = re.compile(r"^[a-zA-Z\u0430-\u044f\u0410-\u042f\u0451\u04010-9]{1,16}$")

_loop = asyncio.new_event_loop()

def _start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(target=_start_background_loop, args=(_loop,), daemon=True)
_loop_thread.start()


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result()

def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(settings.DB_PATH, settings.DB_ENCRYPTION_KEY)
        run_async(_db.init())
    return _db

def get_amnezia() -> AmneziaClient:
    global _amnezia
    if _amnezia is None:
        _amnezia = AmneziaClient(
            settings.AMNEZIA_API_URL,
            settings.AMNEZIA_API_KEY,
            settings.AMNEZIA_PROTOCOL,
        )
    return _amnezia


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    try:
        parsed = urllib.parse.parse_qsl(init_data)
        parsed_dict = dict(parsed)

        if "hash" not in parsed_dict:
            return None

        auth_date = int(parsed_dict.get("auth_date", 0))
        if int(time.time()) - auth_date > 3600:
            logger.warning("Отклонено: Устаревший ключ авторизации Telegram")
            return None

        hash_val = parsed_dict.pop("hash")
        sorted_items = sorted(parsed_dict.items(), key=lambda x: x[0])
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_items)

        secret_key = hmac.new("WebAppData".encode(), bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calc_hash != hash_val:
            return None

        if "user" in parsed_dict:
            return json.loads(parsed_dict["user"])
        return parsed_dict
    except Exception as e:
        logger.error(f"validate_init_data error: {e}")
        return None

import random
import string as _string
_SLUG_CHARS = _string.ascii_lowercase + _string.digits

def _gen_slug() -> str:
    return "".join(random.choices(_SLUG_CHARS, k=5))

def _get_or_create_slug(db: Database, profile_id: int) -> str:
    existing = run_async(db.get_short_link_by_profile(profile_id))
    if existing:
        return existing
    for _ in range(20):
        slug = _gen_slug()
        if not run_async(db.get_short_link_by_slug(slug)):
            run_async(db.get_or_create_short_link(profile_id, slug))
            return slug
    slug = "".join(random.choices(_SLUG_CHARS, k=6))
    run_async(db.get_or_create_short_link(profile_id, slug))
    return slug

def validate_telegram_init_data(init_data: str) -> dict | None:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(received_hash, expected_hash):
            return None

        user_str = parsed.get("user", "{}")
        return json.loads(user_str)
    except Exception as e:
        logger.error("initData validation error: %s", e)
        return None

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if settings.MINIAPP_DEV_MODE:
            g.tg_user = {
                "id": settings.ADMIN_IDS[0] if settings.ADMIN_IDS else 0,
                "first_name": "DevUser",
            }
            return f(*args, **kwargs)

        init_data = request.headers.get("X-Telegram-Init-Data", "")
        if not init_data:
            init_data = request.json.get("initData", "") if request.is_json else ""

        user = validate_telegram_init_data(init_data)
        if user is None:
            return jsonify({"error": "Unauthorized"}), 401

        db = get_db()
        banned = run_async(db.get_user_banned(user["id"]))
        if banned:
            return jsonify({"error": "Banned"}), 403

        g.tg_user = user
        return f(*args, **kwargs)
    return wrapper


def fmt_bytes(b: float) -> str:
    if not b: return "0 Б"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024: return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ТБ"

def find_peer(clients_data: dict | None, vpn_name: str) -> dict | None:
    if not clients_data: return None
    for item in clients_data.get("items", []):
        if item.get("username") == vpn_name:
            peers = item.get("peers", [])
            return peers[0] if peers else None
    return None

def profile_to_json(profile: dict, peer: dict | None = None) -> dict:
    result = {
        "id": profile["id"],
        "vpn_name": profile["vpn_name"],
        "created_at": profile.get("created_at", ""),
        "disabled": profile.get("disabled", False),
    }
    if peer:
        tr = peer.get("traffic", {})
        result["peer"] = {
            "online": peer.get("online", False),
            "status": peer.get("status", ""),
            "rx": fmt_bytes(float(tr.get("received", 0) or 0)),
            "tx": fmt_bytes(float(tr.get("sent", 0) or 0)),
            "protocol": peer.get("protocol", ""),
        }
    return result

@app.route("/api/me", methods=["GET"])
@require_auth
def api_me():
    uid = g.tg_user["id"]
    db = get_db()
    amnezia = get_amnezia()

    profiles = run_async(db.get_profiles(uid))
    can_create = run_async(db.can_create_profile(uid))
    clients = run_async(amnezia.get_all_clients())

    result = []
    for p in profiles:
        peer = find_peer(clients, p["vpn_name"])
        result.append(profile_to_json(p, peer))

    return jsonify({
        "profiles": result,
        "can_create": can_create,
        "max_profiles": MAX_PROFILES_PER_USER,
        "user": {
            "id": uid,
            "name": g.tg_user.get("first_name", ""),
        },
    })

@app.route("/api/create", methods=["POST"])
@require_auth
def api_create():
    uid = g.tg_user["id"]
    db = get_db()
    amnezia = get_amnezia()

    data = request.json or {}
    name = (data.get("name") or "").strip()[:16]

    if not name: return jsonify({"error": "Имя не может быть пустым"}), 400
    if not VPN_NAME_RE.match(name): return jsonify({"error": "Только буквы (латиница/кириллица) и цифры, до 16 символов"}), 400
    if not run_async(db.can_create_profile(uid)): return jsonify({"error": f"Достигнут лимит профилей ({MAX_PROFILES_PER_USER})"}), 400
    if run_async(db.is_vpn_name_taken(name)): return jsonify({"error": "Имя уже занято, выберите другое"}), 409

    result = run_async(amnezia.create_user(name))
    if result is None: return jsonify({"error": "Ошибка API Amnezia. Попробуйте позже."}), 502

    peer_id = result.get("client", {}).get("id")
    profile_id = run_async(db.add_profile(uid, name, peer_id, json.dumps(result, ensure_ascii=False)))

    return jsonify({"ok": True, "profile_id": profile_id, "vpn_name": name})

@app.route("/api/profile/<int:profile_id>", methods=["DELETE"])
@require_auth
def api_delete_profile(profile_id: int):
    uid = g.tg_user["id"]
    db = get_db()
    amnezia = get_amnezia()

    profile = run_async(db.get_profile_by_id(profile_id))
    if not profile or profile["telegram_id"] != uid:
        return jsonify({"error": "Профиль не найден"}), 404

    peer_id = profile.get("peer_id")
    if peer_id: run_async(amnezia.delete_user(peer_id))

    run_async(db.delete_profile(profile_id))
    return jsonify({"ok": True})

@app.route("/api/config/<int:profile_id>", methods=["GET"])
@require_auth
def api_config(profile_id: int):
    uid = g.tg_user["id"]
    db = get_db()
    amnezia = get_amnezia()

    profile = run_async(db.get_profile_by_id(profile_id))
    if not profile or profile["telegram_id"] != uid:
        return jsonify({"error": "Профиль не найден"}), 404
    if profile.get("disabled"):
        return jsonify({"error": "Профиль отключён администратором"}), 403

    config_str = None
    raw = profile.get("raw_response")
    if raw:
        try: config_str = json.loads(raw).get("client", {}).get("config")
        except Exception: pass

    if not config_str:
        config_str = run_async(amnezia.get_client_config(profile.get("peer_id") or profile["vpn_name"]))

    if not config_str:
        return jsonify({"error": "Конфиг недоступен. Обратитесь к администратору."}), 404

    db2 = get_db()
    slug = _get_or_create_slug(db2, profile_id)
    domain = getattr(settings, "SHORT_LINK_DOMAIN", "dqpq.ru").rstrip("/")
    short_url = f"https://{domain}/c/{slug}"

    return jsonify({
        "config": config_str,
        "vpn_name": profile["vpn_name"],
        "filename": f"{profile['vpn_name']}.vpn",
        "short_link": short_url,
    })

@app.route("/api/ping", methods=["GET"])
def api_ping():
    ping_host = settings.VPN_HOST or settings.AMNEZIA_API_URL.split("//")[-1].split(":")[0] or "127.0.0.1"
    ms = get_shared_ping(ping_host, settings.AMNEZIA_API_URL)
    return jsonify({"ping_ms": ms})

@app.route("/api/server", methods=["GET"])
@require_auth
def api_server():
    amnezia = get_amnezia()
    info = run_async(amnezia.get_server_info())
    load = run_async(amnezia.get_server_load())
    online = run_async(amnezia.health_check())
    clients_data = run_async(amnezia.get_all_clients())

    result = {"online": online}

    if info:
        result["region"] = info.get("region") or info.get("serverRegion") or "—"
        pr = info.get("protocols") or info.get("protocolsEnabled") or []
        if isinstance(pr, str): pr = [pr]
        result["protocols"] = pr
        result["max_peers"] = info.get("maxPeers") or "—"

    peers_count = 0
    if clients_data:
        for item in clients_data.get("items", []):
            peers_count += len(item.get("peers", []))
    if peers_count == 0 and info:
        peers_count = info.get("peersCount") or info.get("clientsCount") or 0
    result["peers_count"] = peers_count

    if load:
        result["load"] = {
            "cpu": load.get("cpu", 0),
            "ram": load.get("ram", 0),
            "disk": load.get("disk", 0),
        }

    return jsonify(result)

@app.route("/api/validate_hash", methods=["POST"])
def api_validate_hash():
    data = request.json or {}
    init_data = data.get("initData", "")
    user = validate_telegram_init_data(init_data)
    if user is None: return jsonify({"valid": False}), 401
    return jsonify({"valid": True, "user": user})


from web_service import generate_secret_key as _gen_secret_key

@app.route("/api/mykey", methods=["GET"])
@require_auth
def api_mykey():
    uid = g.tg_user["id"]
    db = get_db()

    blocked = run_async(db.get_user_key_blocked(uid))
    if blocked: return jsonify({"error": "Создание ключей заблокировано администратором"}), 403

    existing = run_async(db.get_secret_key_by_user(uid))
    if existing and not existing.get("revoked"):
        domain = getattr(settings, "SHORT_LINK_DOMAIN", "dqpq.ru").rstrip("/")
        return jsonify({
            "key": existing["key_value"],
            "used": bool(existing.get("used")),
            "revoked": bool(existing.get("revoked")),
            "created_at": existing.get("created_at", ""),
            "site_url": f"https://{domain}",
        })

    key_val = _gen_secret_key()
    run_async(db.create_secret_key(uid, key_val))
    domain = getattr(settings, "SHORT_LINK_DOMAIN", "dqpq.ru").rstrip("/")
    return jsonify({
        "key": key_val, "used": False, "revoked": False,
        "created_at": "", "site_url": f"https://{domain}",
    })

@app.route("/api/newkey", methods=["POST"])
@require_auth
def api_newkey():
    uid = g.tg_user["id"]
    db = get_db()

    blocked = run_async(db.get_user_key_blocked(uid))
    if blocked: return jsonify({"error": "Создание ключей заблокировано администратором"}), 403

    key_val = _gen_secret_key()
    run_async(db.create_secret_key(uid, key_val))
    domain = getattr(settings, "SHORT_LINK_DOMAIN", "dqpq.ru").rstrip("/")
    return jsonify({
        "key": key_val, "used": False, "revoked": False,
        "created_at": "", "site_url": f"https://{domain}",
    })

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>FQof_VPN</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
  (function() {
    var devMode = {{ 'true' if dev_mode else 'false' }};
    var tgApp = window.Telegram && window.Telegram.WebApp;
    if (!devMode && (!tgApp || !tgApp.initData)) {
      window.location.replace("https://google.com/");
    }
  })();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d0d0d; --s1: #141414; --s2: #1c1c1c; --s3: #242424;
    --border: #2e2e2e; --border2: #3a3a3a;
    --text: #f0f0f0; --text2: #a0a0a0; --text3: #606060;
    --white: #ffffff; --green: #3ddc84; --red: #ff4d4d; --amber: #f5a623; --blue: #4a9eff;
    --green-bg: rgba(61,220,132,0.1); --red-bg: rgba(255,77,77,0.1); --blue-bg: rgba(74,158,255,0.1);
    --radius: 12px; --radius-s: 8px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  html, body {
    height: 100%; background: var(--bg); color: var(--text);
    font-family: 'Inter', -apple-system, sans-serif;
    -webkit-font-smoothing: antialiased; overflow: hidden; touch-action: manipulation;
  }

  /* Структура приложения */
  #app { display: flex; flex-direction: column; height: 100vh; max-width: 480px; margin: 0 auto; overflow: hidden; }
  #content { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding-bottom: 24px; }
  .page { animation: fadeIn 0.2s ease both; display: flex; flex-direction: column; gap: 16px; padding: 16px 16px 0; }
  .page.hidden { display: none; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }

  /* Хедер */
  .header { flex-shrink: 0; background: rgba(13,13,13,0.94); backdrop-filter: blur(16px); border-bottom: 1px solid var(--border); padding: 14px 16px 12px; display: flex; flex-direction: column; gap: 10px; }
  .header-top { display: flex; align-items: center; justify-content: space-between; }
  .logo { display: flex; align-items: center; gap: 8px; }
  .logo-icon { font-size: 22px; line-height: 1; }
  .logo-text { font-size: 16px; font-weight: 700; color: var(--white); }

  /* Статус и пинг */
  .status-chip { display: flex; align-items: center; gap: 6px; background: var(--s2); border: 1px solid var(--border); border-radius: 20px; padding: 4px 10px; font-size: 12px; font-weight: 500; color: var(--text2); }
  .status-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text3); flex-shrink: 0; }
  .status-dot.on { background: var(--green); }
  .status-dot.off { background: var(--red); }

  .srv-bar { display: flex; align-items: center; justify-content: space-between; background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); padding: 8px 12px; font-size: 12px; }
  .srv-left { display: flex; align-items: center; gap: 8px; color: var(--text2); font-weight: 500; }
  .srv-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text3); }
  .srv-dot.on { background: var(--green); animation: ping 2s infinite; }
  .srv-right { color: var(--text3); font: 11px 'JetBrains Mono', monospace; }
  @keyframes ping { 0% { box-shadow: 0 0 0 0 rgba(61,220,132,0.4); } 70% { box-shadow: 0 0 0 6px rgba(61,220,132,0); } 100% { box-shadow: 0 0 0 0 transparent; } }

  /* Карточки */
  .section-label { font-size: 11px; font-weight: 600; letter-spacing: 1.5px; text-transform: uppercase; color: var(--text3); margin-bottom: -4px; }
  .card { background: var(--s1); border: 1px solid var(--border); border-radius: var(--radius); display: flex; flex-direction: column; transition: 0.15s; overflow: hidden; }
  .card-body { padding: 14px; display: flex; flex-direction: column; gap: 10px; }
  .card-title-row { display: flex; align-items: center; gap: 8px; }
  .card-name { font-size: 15px; font-weight: 600; color: var(--white); flex: 1; }
  .card-meta { display: flex; gap: 12px; font-size: 12px; color: var(--text3); font-family: 'JetBrains Mono', monospace; flex-wrap: wrap; }
  .card-date { font-size: 11px; color: var(--text3); font-family: 'JetBrains Mono', monospace; }

  .card-foot { display: flex; border-top: 1px solid var(--border); background: var(--bg); }
  .foot-btn { flex: 1; padding: 12px 8px; font-size: 12px; font-weight: 600; color: var(--text2); background: transparent; border: none; cursor: pointer; transition: 0.15s; border-right: 1px solid var(--border); }
  .foot-btn:last-child { border-right: none; }
  .foot-btn:hover { background: var(--s2); color: var(--text); }
  .foot-btn.prim { color: var(--white); }
  .foot-btn.del:hover { background: var(--red-bg); color: var(--red); }

  /* Бейджи */
  .badge { font-size: 10px; font-weight: 600; border-radius: 4px; padding: 3px 6px; font-family: 'JetBrains Mono', monospace; text-transform: uppercase; }
  .badge-g { background: var(--green-bg); color: var(--green); }
  .badge-r { background: var(--red-bg); color: var(--red); }
  .badge-gr { background: var(--s3); color: var(--text3); }

  /* Кнопки и инпуты */
  .btn-group { display: flex; gap: 8px; width: 100%; }
  .btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; width: 100%; border: none; border-radius: var(--radius-s); font-weight: 600; font-size: 14px; padding: 14px; cursor: pointer; transition: 0.15s; }
  .btn:active { transform: scale(0.98); }
  .btn:disabled { opacity: 0.5; pointer-events: none; }

  .btn-primary { background: var(--white); color: #000; }
  .btn-primary:hover { background: #e0e0e0; }
  .btn-outline { background: transparent; border: 1px solid var(--border2); color: var(--text2); }
  .btn-outline:hover { background: var(--s2); color: var(--text); }
  .btn-ghost { background: transparent; color: var(--text3); padding: 10px; font-size: 13px; }
  .btn-danger-outline { background: var(--red-bg); color: var(--red); border: 1px solid rgba(255,77,77,0.3); }
  .btn-danger-outline:hover { background: rgba(255,77,77,0.15); }

  .add-card { border: 1px dashed var(--border2); border-radius: var(--radius); padding: 16px; display: flex; align-items: center; justify-content: center; gap: 8px; cursor: pointer; color: var(--text3); font-size: 13px; font-weight: 600; background: transparent; transition: 0.15s; }
  .add-card:hover { border-color: var(--text3); color: var(--text2); background: var(--s1); }

  .field { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
  .field-label { font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: var(--text3); }
  .input { background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); color: var(--text); font: 15px 'JetBrains Mono', monospace; padding: 14px; outline: none; transition: 0.15s; }
  .input:focus { border-color: var(--border2); }
  .input.err { border-color: var(--red); }
  .field-hint { font-size: 11px; color: var(--text3); }
  .field-hint.err { color: var(--red); }

  /* Ссылки / Конфиги */
  .link-box { background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); padding: 16px 14px; font: 12px 'JetBrains Mono', monospace; color: var(--text2); word-break: break-all; position: relative; cursor: pointer; transition: 0.15s; line-height: 1.6; max-height: 180px; overflow-y: auto; }
  .link-box:hover { border-color: var(--border2); }
  .link-box.highlight { color: var(--white); font-weight: 600; font-size: 14px; }
  .copy-hint { font-size: 9px; color: var(--text3); text-transform: uppercase; position: absolute; top: 6px; right: 10px; }

  /* Инструкция (Аккордеоны) */
  .g-section { background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); overflow: hidden; display: flex; flex-direction: column; }
  .dl-link { display: flex; align-items: center; justify-content: space-between; padding: 14px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--white); font-size: 13px; font-weight: 600; transition: 0.15s; }
  .dl-link:hover { background: var(--s3); }
  .dl-link:last-child { border-bottom: none; }
  .dl-left { display: flex; align-items: center; gap: 8px; }

  .g-head { padding: 14px; display: flex; justify-content: space-between; cursor: pointer; font-size: 13px; font-weight: 600; color: var(--white); }
  .g-arrow { transition: 0.2s; color: var(--text3); }
  .g-arrow.open { transform: rotate(90deg); }
  .g-body { display: none; padding: 0 14px 14px; gap: 12px; flex-direction: column; }
  .g-body.open { display: flex; }

  .step { display: flex; gap: 10px; font-size: 12px; color: var(--text2); align-items: flex-start; line-height: 1.5; }
  .step-n { width: 20px; height: 20px; border-radius: 50%; background: var(--s3); border: 1px solid var(--border2); display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; flex-shrink: 0; font-family: monospace; }
  code { background: var(--s3); padding: 2px 4px; border-radius: 4px; font-family: monospace; }
  .g-note { background: var(--s3); border-left: 3px solid var(--amber); padding: 10px 12px; font-size: 11px; color: var(--text2); border-radius: 4px; }

  /* Модалки / Overlays */
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.8); backdrop-filter: blur(4px); z-index: 100; display: flex; align-items: flex-end; opacity: 0; pointer-events: none; transition: 0.25s; }
  .overlay.open { opacity: 1; pointer-events: all; }
  .sheet { background: var(--s1); border: 1px solid var(--border); border-bottom: none; border-radius: 20px 20px 0 0; padding: 16px 20px 32px; width: 100%; max-height: 92vh; overflow-y: auto; transform: translateY(100%); transition: 0.3s cubic-bezier(0.32,0.72,0,1); display: flex; flex-direction: column; gap: 16px; }
  .overlay.open .sheet { transform: translateY(0); }
  .sheet-handle { width: 36px; height: 4px; background: var(--border2); border-radius: 2px; margin: 0 auto; }
  .sheet-title { font-size: 18px; font-weight: 700; color: var(--white); text-align: center; }
  .confirm-text { font-size: 13px; color: var(--text2); line-height: 1.5; text-align: center; }

  /* Навигация */
  .nav { flex-shrink: 0; background: rgba(13,13,13,0.95); backdrop-filter: blur(16px); border-top: 1px solid var(--border); display: flex; padding-bottom: env(safe-area-inset-bottom, 0px); }
  .nav-btn { flex: 1; display: flex; flex-direction: column; align-items: center; padding: 12px 8px; gap: 4px; cursor: pointer; background: none; border: none; color: var(--text3); font-size: 10px; font-weight: 600; text-transform: uppercase; transition: 0.15s; }
  .nav-btn.active { color: var(--white); }
  .nav-icon { font-size: 20px; margin-bottom: 2px; }

  /* Утилиты */
  .empty { text-align: center; padding: 48px 20px; color: var(--text3); display: flex; flex-direction: column; gap: 8px; align-items: center; }
  .empty-icon { font-size: 40px; opacity: 0.5; }
  .empty-title { font-size: 14px; font-weight: 600; color: var(--text2); }
  .shimmer { background: linear-gradient(90deg, var(--s1) 25%, var(--s2) 50%, var(--s1) 75%); background-size: 200% 100%; animation: shim 1.2s infinite; border-radius: var(--radius); height: 96px; }
  @keyframes shim { from { background-position: 200% 0; } to { background-position: -200% 0; } }

  .toast { position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%) translateY(10px); background: var(--s3); border: 1px solid var(--border2); border-radius: 30px; padding: 10px 20px; font-size: 12px; font-weight: 600; color: var(--text); z-index: 999; opacity: 0; transition: 0.25s; pointer-events: none; box-shadow: 0 4px 20px rgba(0,0,0,0.5); white-space: nowrap; }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
</style>
</head>
<body>
<div id="app">

  <header class="header">
    <div class="header-top">
      <div class="logo">
        <span class="logo-icon">🤮</span>
        <span class="logo-text">FQof_VPN</span>
      </div>
      <div class="status-chip">
        <div class="status-dot" id="chip-dot"></div>
        <span id="user-name">...</span>
      </div>
    </div>
    <div class="srv-bar">
      <div class="srv-left">
        <div class="srv-dot" id="srv-dot"></div>
        <span id="srv-text">Проверяю...</span>
      </div>
      <div class="srv-right" id="srv-load"></div>
    </div>
  </header>

  <main id="content">

    <div id="page-profiles" class="page">
      <div class="section-label">Ваши профили</div>
      <div id="profiles-list" style="display:flex; flex-direction:column; gap:8px;">
        <div class="shimmer"></div>
        <div class="shimmer" style="opacity:0.5"></div>
      </div>
      <button id="add-btn" class="add-card" style="display:none" onclick="openCreate()">
        <span style="font-size:18px;font-weight:300;">+</span> Добавить профиль
      </button>
    </div>

    <div id="page-key" class="page hidden">
      <div class="section-label">Секретный ключ</div>

      <div class="card">
        <div class="card-body">
          <p style="font-size:12px; color:var(--text2); line-height:1.5;">
            Используйте этот ключ на сайте для того, чтобы поделиться VPN с другом, у которого нет доступа к Telegram.<br>
            Один ключ — один профиль. Ключ одноразовый.
          </p>

          <div id="key-status-badge" class="badge badge-gr" style="display:none; text-align:center; padding:6px;"></div>

          <div class="link-box highlight" onclick="copyKey()">
            <span class="copy-hint">нажать для копирования</span>
            <span id="key-value">Загружаю...</span>
          </div>

          <div class="btn-group">
            <button class="btn btn-primary" onclick="copyKey()">📋 Скопировать</button>
            <button class="btn btn-outline" onclick="openSite()">🌐 Сайт</button>
          </div>
        </div>
        <div class="card-foot" style="padding: 8px;">
          <button class="btn btn-danger-outline" onclick="confirmNewKey()">🔄 Создать новый ключ</button>
        </div>
      </div>
    </div>

    <div id="page-guide" class="page hidden">
      <div class="section-label">Скачать AmneziaVPN</div>
      <div class="g-section">
        <a class="dl-link" href="https://apps.apple.com/app/amneziavpn/id1600529900" target="_blank">
          <div class="dl-left"><span>🍎</span> iOS — App Store</div><span>↗</span>
        </a>
        <a class="dl-link" href="https://play.google.com/store/apps/details?id=org.amnezia.vpn" target="_blank">
          <div class="dl-left"><span>🤖</span> Android — Google Play</div><span>↗</span>
        </a>
        <a class="dl-link" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.14.5/AmneziaVPN_4.8.14.5_x64.exe" target="_blank">
          <div class="dl-left"><span>🖥</span> Windows — GitHub</div><span>↗</span>
        </a>
      </div>

      <div class="section-label" style="margin-top: 8px;">Способы подключения</div>

      <div class="g-section">
        <div class="g-head" onclick="toggleG(this)">
          <div style="display:flex; gap:8px;"><span>📋</span> Способ 1 — Текстовый ключ (vpn://)</div><span class="g-arrow">›</span>
        </div>
        <div class="g-body">
          <div class="step"><div class="step-n">1</div><div>Открой приложение <strong>AmneziaVPN</strong>.</div></div>
          <div class="step"><div class="step-n">2</div><div>Нажми <strong>«+»</strong> или <strong>«Get Started»</strong>.</div></div>
          <div class="step"><div class="step-n">3</div><div>Выбери <strong>«Ввод ключа»</strong>.</div></div>
          <div class="step"><div class="step-n">4</div><div>Вставь ключ целиком (начинается с <code>vpn://…</code>).</div></div>
          <div class="step"><div class="step-n">5</div><div>Нажми <strong>«Добавить»</strong> и разреши VPN.</div></div>
          <div class="g-note"><strong>Важно:</strong> Не удаляй приставку <code>vpn://</code>.</div>
        </div>
      </div>

      <div class="g-section">
        <div class="g-head" onclick="toggleG(this)">
          <div style="display:flex; gap:8px;"><span>📁</span> Способ 2 — Файл конфигурации (.vpn)</div><span class="g-arrow">›</span>
        </div>
        <div class="g-body">
          <div class="step"><div class="step-n">1</div><div>Скачай конфиг кнопкой <strong>«📥 .vpn»</strong>.</div></div>
          <div class="step"><div class="step-n">2</div><div>В AmneziaVPN нажми <strong>«+»</strong>.</div></div>
          <div class="step"><div class="step-n">3</div><div>Выбери <strong>«Файл с настройками»</strong>.</div></div>
          <div class="step"><div class="step-n">4</div><div>Найди скачанный файл и нажми <strong>«Импорт»</strong>.</div></div>
        </div>
      </div>
    </div>
  </main>

  <nav class="nav">
    <button class="nav-btn active" onclick="switchTab('profiles', this)">
      <span class="nav-icon">🔑</span>Профили
    </button>
    <button class="nav-btn" onclick="openKeyTab(this)">
      <span class="nav-icon">🗝</span>Ключ
    </button>
    <button class="nav-btn" onclick="switchTab('guide', this)">
      <span class="nav-icon">📖</span>Инструкция
    </button>
  </nav>
</div>

<div id="modal-create" class="overlay">
  <div class="sheet">
    <div class="sheet-handle"></div>
    <div class="sheet-title">Новый профиль</div>
    <div class="field">
      <label class="field-label">Имя профиля</label>
      <input class="input" id="name-input" type="text" placeholder="например: phone" maxlength="16" autocomplete="off">
      <div class="field-hint" id="name-hint">Буквы (a–z, а–я) и цифры, до 16 символов</div>
    </div>
    <div style="display:flex; flex-direction:column; gap:8px;">
      <button class="btn btn-primary" id="create-btn" onclick="doCreate()">Создать</button>
      <button class="btn btn-ghost" onclick="closeO('modal-create')">Отмена</button>
    </div>
  </div>
</div>

<div id="modal-config" class="overlay">
  <div class="sheet">
    <div class="sheet-handle"></div>
    <div class="sheet-title" id="cfg-title">Конфиг</div>

    <div class="link-box" onclick="copyConfig()">
      <span class="copy-hint">нажать для копирования</span>
      <span id="cfg-content">Загружаю...</span>
    </div>

    <div id="short-link-wrap" style="display:none;">
      <div class="field-label" style="margin-bottom:6px">Короткая ссылка (на 24 часа)</div>
      <div class="link-box" onclick="copyShortLink()" style="color:var(--blue); font-weight:600;">
        <span class="copy-hint">нажать для копирования</span>
        <span id="short-link-content"></span>
      </div>
    </div>

    <div class="g-note" style="margin-top:0">AmneziaVPN → <strong>«+»</strong> → Вставить из буфера</div>

    <div class="btn-group">
      <button class="btn btn-primary" onclick="copyConfig()">📋 Скопировать</button>
      <button class="btn btn-outline" onclick="dlConfig()">📥 .vpn</button>
    </div>
    <button class="btn btn-ghost" onclick="closeO('modal-config')">Закрыть</button>
  </div>
</div>

<div id="modal-del" class="overlay">
  <div class="sheet">
    <div class="sheet-handle"></div>
    <div class="sheet-title">Удалить профиль?</div>
    <div class="confirm-text" id="del-text"></div>
    <div class="btn-group">
      <button class="btn btn-danger-outline" id="del-btn" onclick="doDelete()">Удалить</button>
      <button class="btn btn-outline" onclick="closeO('modal-del')">Отмена</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const $ = (id) => document.getElementById(id);
const tg = window.Telegram?.WebApp;
if (tg && tg.initData) { tg.ready(); tg.expand(); }

let currentConfig = null, currentCfgName = null, currentShortLink = null;
let pendingDelId = null, pendingDelName = null;
let _currentKey = null, _siteUrl = null, _keyLoaded = false;
let toastTimer;

const authH = () => {
  const h = { 'Content-Type': 'application/json' };
  if (tg?.initData) h['X-Telegram-Init-Data'] = tg.initData;
  return h;
};

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...authH(), ...(opts.headers||{}) } });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    throw new Error(e.error || `HTTP ${r.status}`);
  }
  return r.json();
}

const showToast = (msg) => {
  $('toast').textContent = msg; 
  $('toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.remove('show'), 2200);
};

const copyText = (text, successMsg) => {
  if (!text) return;
  navigator.clipboard?.writeText(text)
    .then(() => { showToast(successMsg); tg?.HapticFeedback?.impactOccurred('medium'); })
    .catch(() => {
      const t = document.createElement('textarea');
      t.value = text; document.body.appendChild(t); t.select();
      document.execCommand('copy'); t.remove(); showToast(successMsg);
    });
};

// ── Навигация ────────────────────────────────────────────────────────

function switchTab(name, btn) {
  document.querySelectorAll('#content .page').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  $('page-' + name)?.classList.remove('hidden');
  btn.classList.add('active');
  $('content').scrollTop = 0;
}

function openKeyTab(btn) {
  switchTab('key', btn);
  if (!_keyLoaded) loadKey();
}

// ── Оверлеи ──────────────────────────────────────────────────────────

const openO = (id) => $(id).classList.add('open');
const closeO = (id) => $(id).classList.remove('open');
document.querySelectorAll('.overlay').forEach(o => {
  o.addEventListener('click', e => { if (e.target === o) o.classList.remove('open'); });
});

// ── Сервер / Пинг ────────────────────────────────────────────────────

async function loadServer() {
  try {
    const d = await api('/api/server');
    if (d.online) {
      $('srv-dot').className = 'srv-dot on';
      $('chip-dot').className = 'status-dot on';
      $('srv-text').textContent = d.region || 'Сервер';
    } else {
      $('srv-dot').className = 'srv-dot off';
      $('chip-dot').className = 'status-dot off';
      $('srv-text').textContent = 'Недоступен';
    }
  } catch { $('srv-text').textContent = 'Нет данных'; }
  await updatePing();
  setInterval(updatePing, 180000);
}

async function updatePing() {
  try {
    const { ping_ms: ms } = await api('/api/ping');
    const color = ms > 300 ? 'var(--red)' : ms > 150 ? 'var(--amber)' : 'var(--green)';
    if ($('srv-load')) $('srv-load').innerHTML = `<span style="color:${color}">${ms} ms</span>`;
  } catch(_) {}
}

// ── Профили ──────────────────────────────────────────────────────────

const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function loadProfiles() {
  try {
    const data = await api('/api/me');
    $('user-name').textContent = data.user?.name || 'VPN';

    $('add-btn').style.display = data.can_create ? '' : 'none';

    if (!data.profiles.length) {
      $('profiles-list').innerHTML = `<div class="empty"><div class="empty-icon">🔐</div><div class="empty-title">Профилей нет</div></div>`;
      return;
    }

    $('profiles-list').innerHTML = data.profiles.map(p => {
      const peer = p.peer;
      let dot = 'dis', lbl = 'Неизвестно', bdg = 'badge-gr', meta = [];

      if (p.disabled) lbl = 'Отключён';
      else if (peer) {
        if (peer.online) { dot = 'on'; lbl = 'Онлайн'; bdg = 'badge-g'; }
        else { dot = 'off'; lbl = 'Офлайн'; bdg = 'badge-r'; }
        if (peer.rx && peer.rx !== '0 Б') meta.push(`⬇ ${peer.rx}`);
        if (peer.tx && peer.tx !== '0 Б') meta.push(`⬆ ${peer.tx}`);
        if (peer.protocol) meta.push(peer.protocol);
      }

      const metaHtml = meta.length ? `<div class="card-meta">${meta.map(s=>`<span>${esc(s)}</span>`).join('')}</div>` : '';
      const dateHtml = p.created_at ? `<div class="card-date">${p.created_at.slice(0,10)}</div>` : '';

      return `
        <div class="card">
          <div class="card-body">
            <div class="card-title-row">
              <div class="status-dot ${dot}"></div>
              <div class="card-name">${esc(p.vpn_name)}</div>
              <span class="badge ${bdg}">${lbl}</span>
            </div>
            ${metaHtml} ${dateHtml}
          </div>
          <div class="card-foot">
            <button class="foot-btn prim" onclick="getConfig(${p.id},'${esc(p.vpn_name)}')" ${p.disabled?'disabled':''}>📥 Конфиг</button>
            <button class="foot-btn del" onclick="confirmDel(${p.id},'${esc(p.vpn_name)}')">🗑 Удалить</button>
          </div>
        </div>`;
    }).join('');

  } catch(e) {
    $('profiles-list').innerHTML = `<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-title">${esc(e.message)}</div></div>`;
  }
}

function openCreate() {
  $('name-input').value = '';
  $('name-hint').textContent = 'Буквы (a–z, а–я) и цифры, до 16 символов';
  $('name-hint').className = 'field-hint';
  $('name-input').className = 'input';
  openO('modal-create');
  setTimeout(() => $('name-input').focus(), 300);
}

$('name-input').addEventListener('keydown', e => e.key === 'Enter' && doCreate());

async function doCreate() {
  const name = $('name-input').value.trim();
  const err = msg => { $('name-hint').textContent=msg; $('name-hint').className='field-hint err'; $('name-input').className='input err'; };

  if (!name) return err('Введите имя');
  if (!/^[a-zA-Zа-яА-Я0-9ёЁ]{1,16}$/.test(name)) return err('Только буквы и цифры, до 16 символов');

  $('create-btn').disabled = true; $('create-btn').textContent = 'Создаю...';
  try {
    await api('/api/create', { method:'POST', body:JSON.stringify({name}) });
    closeO('modal-create');
    showToast('✓ Профиль создан');
    tg?.HapticFeedback?.notificationOccurred('success');
    await loadProfiles();
  } catch(e) { err(e.message); tg?.HapticFeedback?.notificationOccurred('error'); } 
  finally { $('create-btn').disabled=false; $('create-btn').textContent='Создать'; }
}

// ── Конфиг ───────────────────────────────────────────────────────────

async function getConfig(id, name) {
  openO('modal-config');
  $('cfg-title').textContent = name;
  $('cfg-content').textContent = 'Загружаю...';
  $('short-link-wrap').style.display = 'none';
  currentConfig = currentShortLink = null; currentCfgName = name;

  try {
    const d = await api(`/api/config/${id}`);
    currentConfig = d.config;
    currentShortLink = d.short_link;
    $('cfg-content').textContent = d.config;
    if (d.short_link) {
      $('short-link-content').textContent = d.short_link;
      $('short-link-wrap').style.display = 'block';
    }
    tg?.HapticFeedback?.impactOccurred('light');
  } catch(e) { $('cfg-content').textContent = '❌ ' + e.message; }
}

const copyConfig = () => copyText(currentConfig, '📋 Конфиг скопирован');
const copyShortLink = () => copyText(currentShortLink, '🔗 Ссылка скопирована');

function dlConfig() {
  if (!currentConfig) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([currentConfig], {type:'text/plain'}));
  a.download = (currentCfgName||'config') + '.vpn';
  a.click();
  showToast('📥 Скачивание...');
}

// ── Удаление ─────────────────────────────────────────────────────────

function confirmDel(id, name) {
  pendingDelId = id; pendingDelName = name;
  $('del-text').innerHTML = `Профиль <strong>${esc(name)}</strong> будет удалён. Это действие необратимо.`;
  openO('modal-del');
}

async function doDelete() {
  if (!pendingDelId) return;
  $('del-btn').disabled=true; $('del-btn').textContent='Удаляю...';
  try {
    await api(`/api/profile/${pendingDelId}`, {method:'DELETE'});
    closeO('modal-del'); showToast('🗑 Удалено');
    tg?.HapticFeedback?.notificationOccurred('warning');
    await loadProfiles();
  } catch(e) { showToast('❌ ' + e.message); }
  finally { $('del-btn').disabled=false; $('del-btn').textContent='Удалить'; pendingDelId=null; }
}

// ── Инструкция ───────────────────────────────────────────────────────

const toggleG = (head) => {
  const body = head.nextElementSibling;
  const isOpen = body.classList.toggle('open');
  head.querySelector('.g-arrow').classList.toggle('open', isOpen);
};

// ── Секретный ключ ───────────────────────────────────────────────────

async function loadKey() {
  $('key-value').textContent = 'Загружаю...';
  $('key-status-badge').style.display = 'none';
  try {
    const d = await api('/api/mykey');
    _currentKey = d.key; _siteUrl = d.site_url; _keyLoaded = true;
    $('key-value').textContent = d.key;

    const badge = $('key-status-badge');
    badge.style.display = 'block';
    if (d.used) {
      badge.textContent = '✅ Ключ уже использован';
      badge.className = 'badge badge-r';
    } else {
      badge.textContent = '⏳ Ключ активен (ещё не использован)';
      badge.className = 'badge badge-g';
    }
  } catch(e) { $('key-value').textContent = '❌ ' + e.message; _currentKey = null; }
}

const copyKey = () => copyText(_currentKey, '📋 Ключ скопирован');
const openSite = () => _siteUrl && window.open(_siteUrl, '_blank');

function confirmNewKey() {
  const msg = 'Создать новый ключ? Старый будет аннулирован.';
  tg?.showConfirm ? tg.showConfirm(msg, res => res && doNewKey()) : confirm(msg) && doNewKey();
}

async function doNewKey() {
  $('key-value').textContent = 'Генерирую...';
  try {
    const d = await api('/api/newkey', { method: 'POST' });
    _currentKey = d.key; _siteUrl = d.site_url;
    $('key-value').textContent = d.key;

    $('key-status-badge').textContent = '⏳ Новый ключ активен';
    $('key-status-badge').className = 'badge badge-g';
    $('key-status-badge').style.display = 'block';

    showToast('🔄 Новый ключ создан');
    tg?.HapticFeedback?.notificationOccurred('success');
  } catch(e) { showToast('❌ ' + e.message); $('key-value').textContent = _currentKey || '—'; }
}

// ── Старт ────────────────────────────────────────────────────────────
loadProfiles();
loadServer();
</script>
</body>
</html>"""

@app.route("/")
def index():
    return render_template_string(MINIAPP_HTML, dev_mode=settings.MINIAPP_DEV_MODE)

if __name__ == "__main__":
    host  = getattr(settings, "MINIAPP_HOST", "0.0.0.0")
    port  = getattr(settings, "MINIAPP_PORT", 5000)
    debug = getattr(settings, "MINIAPP_DEV_MODE", False)
    logger.info("Mini App запущен на http://%s:%s", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)
