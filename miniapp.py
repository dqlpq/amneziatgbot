import asyncio
import hashlib
import hmac
import json
import logging
import re
import threading
import urllib.parse
from functools import wraps

import aiohttp
from flask import Flask, request, jsonify, render_template_string, g
from config import settings
from database import Database, MAX_PROFILES_PER_USER
from amnezia_client import AmneziaClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# ──────────────── Глобальные экземпляры (lazy init) ─────────────────

_db: Database | None = None
_amnezia: AmneziaClient | None = None

VPN_NAME_RE = re.compile(r"^[a-zA-Z\u0430-\u044f\u0410-\u042f\u0451\u04010-9]{1,16}$")

# Создаем один глобальный цикл событий
_loop = asyncio.new_event_loop()

# Функция для постоянной работы цикла в отдельном потоке
def _start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

# Запускаем фоновый поток при старте приложения
_loop_thread = threading.Thread(target=_start_background_loop, args=(_loop,), daemon=True)
_loop_thread.start()


def run_async(coro):
    """Отправляет корутину в фоновый event loop и синхронно ждет результат."""
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


# ──────────────── Telegram initData валидация ────────────────────────

def validate_telegram_init_data(init_data: str) -> dict | None:
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(
            b"WebAppData",
            settings.BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()
        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()

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


# ──────────────── Вспомогательные функции ───────────────────────────

def fmt_bytes(b: float) -> str:
    if not b:
        return "0 Б"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ТБ"


def find_peer(clients_data: dict | None, vpn_name: str) -> dict | None:
    if not clients_data:
        return None
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


# ──────────────── API эндпоинты ──────────────────────────────────────

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

    if not name:
        return jsonify({"error": "Имя не может быть пустым"}), 400
    if not VPN_NAME_RE.match(name):
        return jsonify({"error": "Только буквы (латиница/кириллица) и цифры, до 16 символов"}), 400
    if not run_async(db.can_create_profile(uid)):
        return jsonify({"error": f"Достигнут лимит профилей ({MAX_PROFILES_PER_USER})"}), 400
    if run_async(db.is_vpn_name_taken(name)):
        return jsonify({"error": "Имя уже занято, выберите другое"}), 409

    result = run_async(amnezia.create_user(name))
    if result is None:
        return jsonify({"error": "Ошибка API Amnezia. Попробуйте позже."}), 502

    peer_id = result.get("client", {}).get("id")
    profile_id = run_async(
        db.add_profile(uid, name, peer_id, json.dumps(result, ensure_ascii=False))
    )

    return jsonify({
        "ok": True,
        "profile_id": profile_id,
        "vpn_name": name,
    })


@app.route("/api/profile/<int:profile_id>", methods=["DELETE"])
@require_auth
def api_delete_profile(profile_id: int):
    uid = g.tg_user["id"]
    db = get_db()
    amnezia = get_amnezia()

    profile = run_async(db.get_profile_by_id(profile_id))
    if not profile or profile["telegram_id"] != uid:
        return jsonify({"error": "Профиль не найден"}), 404

    # Удаляем из Amnezia
    peer_id = profile.get("peer_id")
    if peer_id:
        run_async(amnezia.delete_user(peer_id))

    # Удаляем из БД
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
        try:
            config_str = json.loads(raw).get("client", {}).get("config")
        except Exception:
            pass

    if not config_str:
        config_str = run_async(
            amnezia.get_client_config(profile.get("peer_id") or profile["vpn_name"])
        )

    if not config_str:
        return jsonify({"error": "Конфиг недоступен. Обратитесь к администратору."}), 404

    return jsonify({
        "config": config_str,
        "vpn_name": profile["vpn_name"],
        "filename": f"{profile['vpn_name']}.vpn",
    })


@app.route("/api/server", methods=["GET"])
@require_auth
def api_server():
    amnezia = get_amnezia()
    info = run_async(amnezia.get_server_info())
    load = run_async(amnezia.get_server_load())
    online = run_async(amnezia.health_check())

    result = {"online": online}

    if info:
        result["region"] = info.get("region") or info.get("serverRegion") or "—"
        pr = info.get("protocols") or info.get("protocolsEnabled") or []
        if isinstance(pr, str):
            pr = [pr]
        result["protocols"] = pr
        result["peers_count"] = info.get("peersCount") or info.get("clientsCount") or 0
        result["max_peers"] = info.get("maxPeers") or "—"

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
    if user is None:
        return jsonify({"valid": False}), 401
    return jsonify({"valid": True, "user": user})


# ──────────────── Mini App HTML ──────────────────────────────────────

MINIAPP_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<title>FQof_VPN</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
  // Блокировка доступа вне Telegram (с учетом dev_mode для тестирования)
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
    --bg:       #0d0d0d;
    --s1:       #141414;
    --s2:       #1c1c1c;
    --s3:       #242424;
    --border:   #2e2e2e;
    --border2:  #3a3a3a;
    --text:     #f0f0f0;
    --text2:    #a0a0a0;
    --text3:    #606060;
    --white:    #ffffff;
    --green:    #3ddc84;
    --green-bg: rgba(61,220,132,0.08);
    --green-bd: rgba(61,220,132,0.2);
    --red:      #ff4d4d;
    --red-bg:   rgba(255,77,77,0.08);
    --red-bd:   rgba(255,77,77,0.2);
    --amber:    #f5a623;
    --blue:     #4a9eff;
    --blue-bg:  rgba(74,158,255,0.08);
    --blue-bd:  rgba(74,158,255,0.2);
    --radius:   12px;
    --radius-s: 8px;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', -apple-system, sans-serif;
    -webkit-font-smoothing: antialiased;
    overflow: hidden;
    touch-action: manipulation;
  }

  #app {
    display: flex;
    flex-direction: column;
    height: 100vh;
    max-width: 480px;
    margin: 0 auto;
    overflow: hidden;
  }

  /* ── Content scroll area ── */
  #content {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
  }

  /* ── Header ── */
  .header {
    flex-shrink: 0;
    background: rgba(13,13,13,0.94);
    backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--border);
    padding: 14px 16px 12px;
  }
  .header-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .logo { display: flex; align-items: center; gap: 9px; }
  .logo-icon { font-size: 22px; line-height: 1; }
  .logo-text { font-size: 16px; font-weight: 700; color: var(--white); letter-spacing: -0.3px; }

  .user-chip {
    display: flex; align-items: center; gap: 6px;
    background: var(--s2); border: 1px solid var(--border);
    border-radius: 20px; padding: 5px 10px 5px 8px;
    font-size: 12px; font-weight: 500; color: var(--text2);
  }
  .chip-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text3); flex-shrink: 0; }
  .chip-dot.on  { background: var(--green); }
  .chip-dot.off { background: var(--red); }

  /* ── Server bar ── */
  .srv-bar {
    display: flex; align-items: center; justify-content: space-between;
    background: var(--s2); border: 1px solid var(--border);
    border-radius: var(--radius-s); padding: 8px 12px; font-size: 12px;
  }
  .srv-left { display: flex; align-items: center; gap: 8px; color: var(--text2); font-weight: 500; }
  .srv-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text3); flex-shrink: 0; }
  .srv-dot.on  { background: var(--green); animation: ping 2s ease infinite; }
  .srv-dot.off { background: var(--red); }
  @keyframes ping {
    0%   { box-shadow: 0 0 0 0 rgba(61,220,132,0.4); }
    70%  { box-shadow: 0 0 0 6px rgba(61,220,132,0); }
    100% { box-shadow: 0 0 0 0 rgba(61,220,132,0); }
  }
  .srv-right { display: flex; gap: 10px; color: var(--text3); font-family: 'JetBrains Mono',monospace; font-size: 11px; }
  .srv-right span { display: flex; align-items: center; gap: 3px; }

  /* ── Pages ── */
  .page { animation: fadeIn 0.2s ease both; }
  @keyframes fadeIn {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  /* ── Section ── */
  .section { padding: 16px 16px 0; }
  .section-label {
    font-size: 11px; font-weight: 600; letter-spacing: 1.5px;
    text-transform: uppercase; color: var(--text3); margin-bottom: 10px;
  }

  /* ── Card ── */
  .card {
    background: var(--s1); border: 1px solid var(--border);
    border-radius: var(--radius); margin-bottom: 8px; overflow: hidden;
    transition: border-color 0.15s;
  }
  .card:hover { border-color: var(--border2); }
  .card-body { padding: 14px 14px 0; }
  .card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .card-title-row { display: flex; align-items: center; gap: 8px; }
  .card-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--text3); }
  .card-dot.on  { background: var(--green); }
  .card-dot.off { background: var(--red); }
  .card-dot.dis { background: var(--text3); }
  .card-name { font-size: 15px; font-weight: 600; color: var(--white); letter-spacing: -0.2px; }

  .badge {
    font-size: 11px; font-weight: 500; border-radius: 5px;
    padding: 2px 7px; font-family: 'JetBrains Mono',monospace;
  }
  .badge-g  { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-bd); }
  .badge-r  { background: var(--red-bg);   color: var(--red);   border: 1px solid var(--red-bd); }
  .badge-gr { background: var(--s3);       color: var(--text3); border: 1px solid var(--border); }

  .card-meta {
    display: flex; gap: 12px; font-size: 12px;
    color: var(--text3); font-family: 'JetBrains Mono',monospace;
    margin-bottom: 12px; flex-wrap: wrap;
  }
  .card-meta span { display: flex; align-items: center; gap: 4px; }

  .card-foot {
    display: flex; border-top: 1px solid var(--border); margin: 0 -1px;
  }
  .foot-btn {
    flex: 1; padding: 11px 8px; font-size: 12px; font-weight: 600;
    color: var(--text2); background: transparent; border: none; cursor: pointer;
    font-family: 'Inter',sans-serif; transition: background 0.15s, color 0.15s;
    display: flex; align-items: center; justify-content: center; gap: 5px;
  }
  .foot-btn:hover { background: var(--s2); color: var(--text); }
  .foot-btn.prim  { color: var(--white); }
  .foot-btn.prim:hover { background: var(--s3); }
  .foot-btn.del:hover  { background: var(--red-bg); color: var(--red); }
  .foot-btn + .foot-btn { border-left: 1px solid var(--border); }
  .foot-btn:disabled { opacity: 0.3; cursor: not-allowed; }
  .foot-btn:disabled:hover { background: transparent; color: var(--text2); }

  /* ── Add card ── */
  .add-card {
    border: 1px dashed var(--border2); border-radius: var(--radius);
    padding: 16px; display: flex; align-items: center; justify-content: center;
    gap: 8px; cursor: pointer; color: var(--text3); font-size: 13px; font-weight: 600;
    background: transparent; width: 100%; font-family: 'Inter',sans-serif;
    transition: all 0.15s; margin-top: 4px;
  }
  .add-card:hover { border-color: var(--text3); color: var(--text2); background: var(--s1); }
  .add-card:active { transform: scale(0.99); }

  /* ── Overlay / Sheet ── */
  .overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.78); backdrop-filter: blur(6px);
    z-index: 100; display: flex; align-items: flex-end;
    opacity: 0; pointer-events: none; transition: opacity 0.25s;
  }
  .overlay.open { opacity: 1; pointer-events: all; }
  .sheet {
    background: var(--s1); border: 1px solid var(--border); border-bottom: none;
    border-radius: 20px 20px 0 0; padding: 20px 16px 32px;
    width: 100%; max-height: 92vh; overflow-y: auto;
    transform: translateY(100%);
    transition: transform 0.3s cubic-bezier(0.32,0.72,0,1);
  }
  .overlay.open .sheet { transform: translateY(0); }
  .sheet-handle { width: 32px; height: 3px; background: var(--border2); border-radius: 2px; margin: 0 auto 18px; }
  .sheet-title { font-size: 18px; font-weight: 700; color: var(--white); margin-bottom: 16px; letter-spacing: -0.3px; }

  /* ── Buttons ── */
  .btn {
    display: inline-flex; align-items: center; justify-content: center;
    gap: 6px; border: none; border-radius: var(--radius-s);
    font-family: 'Inter',sans-serif; font-weight: 600; font-size: 14px;
    cursor: pointer; transition: all 0.15s; white-space: nowrap;
  }
  .btn:active { transform: scale(0.97); }
  .btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
  .btn-white   { background: var(--white); color: #000; padding: 13px 20px; }
  .btn-white:hover { background: #e8e8e8; }
  .btn-outline { background: transparent; color: var(--text2); border: 1px solid var(--border2); padding: 13px 18px; }
  .btn-outline:hover { border-color: var(--text3); color: var(--text); }
  .btn-ghost   { background: transparent; color: var(--text3); padding: 11px 14px; font-size: 13px; }
  .btn-ghost:hover { color: var(--text2); }
  .btn-red-o   { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-bd); padding: 13px 18px; }
  .btn-red-o:hover { background: rgba(255,77,77,0.14); }
  .btn-full    { width: 100%; padding: 14px; font-size: 15px; }

  /* ── Field ── */
  .field { margin-bottom: 14px; }
  .field-label {
    display: block; font-size: 11px; font-weight: 600;
    letter-spacing: 1px; text-transform: uppercase;
    color: var(--text3); margin-bottom: 7px;
  }
  .input {
    width: 100%; background: var(--s2); border: 1px solid var(--border);
    border-radius: var(--radius-s); color: var(--text);
    font-family: 'JetBrains Mono',monospace; font-size: 15px; font-weight: 500;
    padding: 13px 14px; outline: none; transition: border-color 0.15s;
    -webkit-appearance: none;
  }
  .input:focus { border-color: var(--border2); }
  .input.err   { border-color: var(--red); }
  .field-hint  { font-size: 12px; color: var(--text3); margin-top: 6px; font-family: 'JetBrains Mono',monospace; }
  .field-hint.err { color: var(--red); }

  /* ── Config ── */
  .cfg-wrap {
    position: relative; background: var(--s2); border: 1px solid var(--border);
    border-radius: var(--radius-s); margin-bottom: 12px;
    cursor: pointer; transition: border-color 0.15s; overflow: hidden;
  }
  .cfg-wrap:hover { border-color: var(--border2); }
  .cfg-text {
    padding: 14px; font-family: 'JetBrains Mono',monospace;
    font-size: 11px; color: var(--text2); word-break: break-all;
    line-height: 1.7; max-height: 180px; overflow-y: auto;
  }
  .cfg-tap {
    position: absolute; top: 8px; right: 10px;
    font-size: 10px; font-weight: 500; color: var(--text3);
    letter-spacing: 0.5px; text-transform: uppercase;
    font-family: 'Inter',sans-serif; pointer-events: none;
  }
  .import-tip {
    background: var(--s2); border: 1px solid var(--border);
    border-radius: var(--radius-s); padding: 12px 14px;
    font-size: 12px; color: var(--text3); line-height: 1.6; margin-bottom: 14px;
  }
  .import-tip strong { color: var(--text2); font-weight: 600; }

  /* ── Guide ── */
  .g-section {
    background: var(--s1); border: 1px solid var(--border);
    border-radius: var(--radius); margin-bottom: 8px; overflow: hidden;
  }
  .g-head {
    padding: 14px; display: flex; align-items: center; justify-content: space-between;
    cursor: pointer; user-select: none;
    border-bottom: 1px solid transparent; transition: border-color 0.15s;
  }
  .g-head.open { border-bottom-color: var(--border); }
  .g-title { font-size: 14px; font-weight: 600; color: var(--white); display: flex; align-items: center; gap: 8px; }
  .g-arrow { font-size: 12px; color: var(--text3); transition: transform 0.2s; display: inline-block; }
  .g-arrow.open { transform: rotate(90deg); }
  .g-body { display: none; padding: 14px; }
  .g-body.open { display: block; }

  .g-step { display: flex; gap: 12px; margin-bottom: 12px; }
  .g-step:last-child { margin-bottom: 0; }
  .step-n {
    width: 22px; height: 22px; border-radius: 50%;
    background: var(--s3); border: 1px solid var(--border2);
    color: var(--text3); font-size: 11px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0; margin-top: 1px;
    font-family: 'JetBrains Mono',monospace;
  }
  .step-t { font-size: 13px; color: var(--text2); line-height: 1.6; }
  .step-t strong { color: var(--text); font-weight: 600; }
  .step-t code {
    background: var(--s3); border: 1px solid var(--border); border-radius: 4px;
    padding: 1px 6px; font-family: 'JetBrains Mono',monospace; font-size: 11px; color: var(--text);
  }

  .g-note {
    background: var(--s3); border: 1px solid var(--border);
    border-left: 3px solid var(--amber); border-radius: var(--radius-s);
    padding: 10px 12px; font-size: 12px; color: var(--text2); line-height: 1.6; margin-top: 12px;
  }
  .g-note strong { color: var(--amber); }

  .dl-list { display: flex; flex-direction: column; gap: 8px; padding: 10px; }
  .dl-link {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 14px; background: var(--s2); border: 1px solid var(--border);
    border-radius: var(--radius-s); text-decoration: none; transition: border-color 0.15s;
  }
  .dl-link:hover { border-color: var(--border2); }
  .dl-left { display: flex; align-items: center; gap: 10px; }
  .dl-icon { font-size: 18px; }
  .dl-name { font-size: 13px; font-weight: 600; color: var(--white); }
  .dl-arr  { font-size: 14px; color: var(--text3); }

  /* ── Empty / loader ── */
  .empty { text-align: center; padding: 48px 20px; color: var(--text3); }
  .empty-icon  { font-size: 40px; margin-bottom: 12px; opacity: 0.4; }
  .empty-title { font-size: 14px; font-weight: 600; color: var(--text2); margin-bottom: 4px; }
  .empty-sub   { font-size: 12px; }

  .loader { display: flex; justify-content: center; align-items: center; padding: 56px; }
  .spinner {
    width: 28px; height: 28px; border: 2px solid var(--border2);
    border-top-color: var(--text2); border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  .shimmer {
    background: linear-gradient(90deg, var(--s1) 25%, var(--s2) 50%, var(--s1) 75%);
    background-size: 200% 100%; animation: shim 1.2s infinite;
    border-radius: var(--radius); height: 96px; margin-bottom: 8px;
  }
  @keyframes shim {
    from { background-position: 200% 0; }
    to   { background-position: -200% 0; }
  }

  /* ── Nav ── */
  .nav {
    flex-shrink: 0;
    background: rgba(13,13,13,0.95);
    backdrop-filter: blur(16px);
    border-top: 1px solid var(--border);
    display: flex;
    padding-bottom: env(safe-area-inset-bottom, 0px);
  }
  .nav-btn {
    flex: 1; display: flex; flex-direction: column; align-items: center;
    padding: 10px 8px 11px; gap: 4px; cursor: pointer;
    background: none; border: none; color: var(--text3);
    font-family: 'Inter',sans-serif; font-size: 10px; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px; transition: color 0.15s;
  }
  .nav-btn.active { color: var(--white); }
  .nav-icon { font-size: 19px; line-height: 1; }

  /* ── Toast ── */
  .toast {
    position: fixed; bottom: 80px; left: 50%;
    transform: translateX(-50%) translateY(10px);
    background: var(--s3); border: 1px solid var(--border2); border-radius: 30px;
    padding: 9px 18px; font-size: 13px; font-weight: 600; color: var(--text);
    z-index: 999; opacity: 0; transition: all 0.25s; white-space: nowrap;
    box-shadow: 0 4px 24px rgba(0,0,0,0.5); pointer-events: none;
  }
  .toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }

  .confirm-text { font-size: 14px; color: var(--text2); line-height: 1.6; margin-bottom: 20px; }
  .confirm-text strong { color: var(--white); }
  .btn-row { display: flex; gap: 8px; }
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
      <div class="user-chip">
        <div class="chip-dot" id="chip-dot"></div>
        <span id="user-name">…</span>
      </div>
    </div>
    <div class="srv-bar">
      <div class="srv-left">
        <div class="srv-dot" id="srv-dot"></div>
        <span id="srv-text">Проверяю…</span>
      </div>
      <div class="srv-right" id="srv-load"></div>
    </div>
  </header>

  <div id="content">
    <div id="page-profiles" class="page">
      <div class="section" style="padding-top:14px">
        <div class="section-label">Профили</div>
        <div id="profiles-list">
          <div class="shimmer"></div>
          <div class="shimmer" style="height:72px;opacity:0.5"></div>
        </div>
        <button id="add-btn" class="add-card" style="display:none" onclick="openCreate()">
          <span style="font-size:18px;font-weight:300;color:var(--text3)">+</span>
          Добавить профиль
        </button>
      </div>
    </div>

    <div id="page-guide" class="page" style="display:none">
      <div class="section" style="padding-top:14px">

        <div class="section-label">Скачать AmneziaVPN</div>
        <div class="g-section">
          <div class="dl-list">
            <a class="dl-link" href="https://apps.apple.com/app/amneziavpn/id1600529900" target="_blank">
              <div class="dl-left"><span class="dl-icon">🍎</span><span class="dl-name">iOS — App Store</span></div>
              <span class="dl-arr">↗</span>
            </a>
            <a class="dl-link" href="https://play.google.com/store/apps/details?id=org.amnezia.vpn" target="_blank">
              <div class="dl-left"><span class="dl-icon">🤖</span><span class="dl-name">Android — Google Play</span></div>
              <span class="dl-arr">↗</span>
            </a>
            <a class="dl-link" href="https://github.com/amnezia-vpn/amnezia-client/releases/download/4.8.14.5/AmneziaVPN_4.8.14.5_x64.exe" target="_blank">
              <div class="dl-left"><span class="dl-icon">🖥</span><span class="dl-name">Windows — GitHub</span></div>
              <span class="dl-arr">↗</span>
            </a>
          </div>
        </div>

        <div class="section-label" style="margin-top:16px">Способы подключения</div>

        <div class="g-section">
          <div class="g-head" onclick="toggleG(this)">
            <div class="g-title"><span>📋</span> Способ 1 — Текстовый ключ (vpn://)</div>
            <span class="g-arrow">›</span>
          </div>
          <div class="g-body">
            <div class="g-step"><div class="step-n">1</div><div class="step-t">Открой приложение <strong>AmneziaVPN</strong> на телефоне или компьютере.</div></div>
            <div class="g-step"><div class="step-n">2</div><div class="step-t">Нажми <strong>«+»</strong> или <strong>«Get Started»</strong>, если подключений нет.</div></div>
            <div class="g-step"><div class="step-n">3</div><div class="step-t">Выбери <strong>«Ввод ключа»</strong> или <strong>«Paste key»</strong>.</div></div>
            <div class="g-step"><div class="step-n">4</div><div class="step-t">Вставь ключ целиком — он начинается с <code>vpn://…</code></div></div>
            <div class="g-step"><div class="step-n">5</div><div class="step-t">Нажми <strong>«Добавить»</strong> или <strong>«Connect»</strong>.</div></div>
            <div class="g-step"><div class="step-n">6</div><div class="step-t">Разреши необходимые разрешения (VPN, уведомления).</div></div>
            <div class="g-step" style="margin-bottom:0"><div class="step-n">7</div><div class="step-t">Подключись.</div></div>
            <div class="g-note"><strong>Важно:</strong> Вставляй ключ целиком. Не удаляй приставку <code>vpn://</code>.</div>
          </div>
        </div>

        <div class="g-section" style="margin-bottom:16px">
          <div class="g-head" onclick="toggleG(this)">
            <div class="g-title"><span>📁</span> Способ 2 — Файл конфигурации (.vpn)</div>
            <span class="g-arrow">›</span>
          </div>
          <div class="g-body">
            <div class="g-step"><div class="step-n">1</div><div class="step-t">Скачай конфиг через кнопку <strong>«Скачать .vpn»</strong>.</div></div>
            <div class="g-step"><div class="step-n">2</div><div class="step-t">В приложении AmneziaVPN нажми <strong>«+»</strong>.</div></div>
            <div class="g-step"><div class="step-n">3</div><div class="step-t">Выбери <strong>«Файл с настройками»</strong> или <strong>«Import from file»</strong>.</div></div>
            <div class="g-step"><div class="step-n">4</div><div class="step-t">Найди и выбери сохранённый файл на устройстве.</div></div>
            <div class="g-step" style="margin-bottom:0"><div class="step-n">5</div><div class="step-t">Нажми <strong>«Импорт»</strong> / <strong>«Добавить»</strong>.</div></div>
          </div>
        </div>

      </div>
    </div>
  </div><nav class="nav">
    <button class="nav-btn active" onclick="switchTab('profiles',this)">
      <span class="nav-icon">🔑</span>Профили
    </button>
    <button class="nav-btn" onclick="switchTab('guide',this)">
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
      <input class="input" id="name-input" type="text" placeholder="название" maxlength="16"
             autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
      <div class="field-hint" id="name-hint">Буквы (a–z, а–я) и цифры, до 16 символов</div>
    </div>
    <button class="btn btn-white btn-full" id="create-btn" onclick="doCreate()">Создать</button>
    <button class="btn btn-ghost btn-full" style="margin-top:6px" onclick="closeO('modal-create')">Отмена</button>
  </div>
</div>

<div id="modal-config" class="overlay">
  <div class="sheet">
    <div class="sheet-handle"></div>
    <div class="sheet-title" id="cfg-title">Конфиг</div>
    <div class="cfg-wrap" onclick="copyConfig()">
      <span class="cfg-tap">нажмите чтобы скопировать</span>
      <div class="cfg-text" id="cfg-content">Загружаю…</div>
    </div>
    <div class="import-tip">
      AmneziaVPN → <strong>«+»</strong> → Вставить из буфера обмена / Импорт из файла
    </div>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <button class="btn btn-white" style="flex:1;padding:13px" onclick="copyConfig()">📋 Скопировать</button>
      <button class="btn btn-outline" style="flex:1;padding:13px" onclick="dlConfig()">📥 .vpn</button>
    </div>
    <button class="btn btn-ghost btn-full" onclick="closeO('modal-config')">Закрыть</button>
  </div>
</div>

<div id="modal-del" class="overlay">
  <div class="sheet">
    <div class="sheet-handle"></div>
    <div class="sheet-title">Удалить профиль?</div>
    <div class="confirm-text" id="del-text"></div>
    <div class="btn-row">
      <button class="btn btn-red-o" style="flex:1;padding:13px" id="del-btn" onclick="doDelete()">Удалить</button>
      <button class="btn btn-outline" style="flex:1;padding:13px" onclick="closeO('modal-del')">Отмена</button>
    </div>
  </div>
</div>

<div id="toast" class="toast"></div>

<script>
const tg = window.Telegram?.WebApp;
if (tg && tg.initData) { tg.ready(); tg.expand(); }

let currentConfig = null, currentCfgName = null;
let pendingDelId = null, pendingDelName = null;

function authH() {
  const h = { 'Content-Type': 'application/json' };
  if (tg?.initData) h['X-Telegram-Init-Data'] = tg.initData;
  return h;
}

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...authH(), ...(opts.headers||{}) } });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    throw new Error(e.error || `HTTP ${r.status}`);
  }
  return r.json();
}

function toast(msg, dur=2200) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), dur);
}

function switchTab(name, btn) {
  document.querySelectorAll('#content .page').forEach(p => p.style.display = 'none');
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).style.display = '';
  btn.classList.add('active');
  document.getElementById('content').scrollTop = 0;
}

function openO(id)  { document.getElementById(id).classList.add('open'); }
function closeO(id) { document.getElementById(id).classList.remove('open'); }

document.querySelectorAll('.overlay').forEach(o => {
  o.addEventListener('click', e => { if (e.target === o) o.classList.remove('open'); });
});

// ── Server status & Ping ───────────────────────────────────────────
const PING_HOST = '77.88.8.1';
let _lastPingMs = null;

async function measurePing() {
  const start = performance.now();
  try {
    await fetch(`https://${PING_HOST}`, { method: 'HEAD', mode: 'no-cors', cache: 'no-store' });
  } catch(_) {}
  return Math.round(performance.now() - start);
}

async function updatePingDisplay() {
  const ms = await measurePing();
  _lastPingMs = ms;
  const load = document.getElementById('srv-load');
  if (load) {
    let color = 'var(--green)';
    if (ms > 150) color = 'var(--amber)';
    if (ms > 300) color = 'var(--red)';
    load.innerHTML = `<span style="color:${color}">${ms} ms</span>`;
  }
}

async function loadServer() {
  try {
    const d = await api('/api/server');
    const dot  = document.getElementById('srv-dot');
    const cdot = document.getElementById('chip-dot');
    const txt  = document.getElementById('srv-text');

    if (d.online) {
      dot.className = 'srv-dot on';
      cdot.className = 'chip-dot on';
      txt.textContent = d.region || 'Сервер';
    } else {
      dot.className = 'srv-dot off';
      cdot.className = 'chip-dot off';
      txt.textContent = 'Сервер недоступен';
    }
  } catch { document.getElementById('srv-text').textContent = 'Нет данных'; }

  // Сразу замеряем пинг и запускаем обновление каждые 3 минуты
  await updatePingDisplay();
  setInterval(updatePingDisplay, 3 * 60 * 1000);
}

// ── Profiles ───────────────────────────────────────────────────────
async function loadProfiles() {
  try {
    const data = await api('/api/me');
    document.getElementById('user-name').textContent = data.user?.name || 'VPN';
    renderProfiles(data);
  } catch(e) {
    document.getElementById('profiles-list').innerHTML =
      `<div class="empty"><div class="empty-icon">⚠️</div><div class="empty-title">${esc(e.message)}</div></div>`;
  }
}

function renderProfiles(data) {
  const list = document.getElementById('profiles-list');
  const btn  = document.getElementById('add-btn');
  list.innerHTML = data.profiles.length
    ? data.profiles.map(profileCard).join('')
    : `<div class="empty">
         <div class="empty-icon">🔐</div>
         <div class="empty-title">Профилей нет</div>
         <div class="empty-sub">Создайте первый VPN-профиль</div>
       </div>`;
  btn.style.display = data.can_create ? '' : 'none';
}

function profileCard(p) {
  const peer = p.peer;
  let dotCls = 'dis', lbl = 'Неизвестно', bdg = 'badge-gr';
  let meta = '';

  if (p.disabled) { lbl = 'Отключён'; }
  else if (peer) {
    if (peer.online) { dotCls='on'; lbl='Онлайн'; bdg='badge-g'; }
    else             { dotCls='off'; lbl='Офлайн'; bdg='badge-r'; }
    const parts = [];
    if (peer.rx && peer.rx !== '0 Б') parts.push(`⬇ ${peer.rx}`);
    if (peer.tx && peer.tx !== '0 Б') parts.push(`⬆ ${peer.tx}`);
    if (peer.protocol) parts.push(peer.protocol);
    if (parts.length) meta = `<div class="card-meta">${parts.map(s=>`<span>${esc(s)}</span>`).join('')}</div>`;
  }

  const created = p.created_at ? p.created_at.slice(0,10) : '';
  const dis = p.disabled ? 'disabled' : '';

  return `
  <div class="card">
    <div class="card-body">
      <div class="card-head">
        <div class="card-title-row">
          <div class="card-dot ${dotCls}"></div>
          <div class="card-name">${esc(p.vpn_name)}</div>
          <span class="badge ${bdg}">${lbl}</span>
        </div>
      </div>
      ${meta}
      ${created ? `<div style="font-size:11px;color:var(--text3);margin-bottom:12px;font-family:'JetBrains Mono',monospace">${created}</div>` : ''}
    </div>
    <div class="card-foot">
      <button class="foot-btn prim" onclick="getConfig(${p.id},'${esc(p.vpn_name)}')" ${dis}>📥 Конфиг</button>
      <button class="foot-btn del"  onclick="confirmDel(${p.id},'${esc(p.vpn_name)}')">🗑</button>
    </div>
  </div>`;
}

// ── Create ─────────────────────────────────────────────────────────
function openCreate() {
  document.getElementById('name-input').value = '';
  const h = document.getElementById('name-hint');
  h.textContent = 'Буквы (a–z, а–я) и цифры, до 16 символов';
  h.className = 'field-hint';
  document.getElementById('name-input').className = 'input';
  openO('modal-create');
  setTimeout(() => document.getElementById('name-input').focus(), 320);
}

document.getElementById('name-input').addEventListener('keydown', e => { if (e.key === 'Enter') doCreate(); });

async function doCreate() {
  const inp  = document.getElementById('name-input');
  const hint = document.getElementById('name-hint');
  const btn  = document.getElementById('create-btn');
  const name = inp.value.trim();

  const err = msg => { hint.textContent=msg; hint.className='field-hint err'; inp.className='input err'; };

  if (!name) { err('Введите имя'); return; }
  if (!/^[a-zA-Zа-яА-Я0-9ёЁ]{1,16}$/.test(name)) { err('Только буквы и цифры, до 16 символов'); return; }

  btn.disabled = true; btn.textContent = 'Создаю…';
  try {
    await api('/api/create', { method:'POST', body:JSON.stringify({name}) });
    closeO('modal-create');
    toast('✓ Профиль создан');
    tg?.HapticFeedback?.notificationOccurred('success');
    await loadProfiles();
  } catch(e) {
    err(e.message);
    tg?.HapticFeedback?.notificationOccurred('error');
  } finally { btn.disabled=false; btn.textContent='Создать'; }
}

// ── Config ─────────────────────────────────────────────────────────
async function getConfig(id, name) {
  openO('modal-config');
  document.getElementById('cfg-title').textContent = name;
  document.getElementById('cfg-content').textContent = 'Загружаю…';
  currentConfig = null; currentCfgName = name;
  try {
    const d = await api(`/api/config/${id}`);
    currentConfig = d.config;
    document.getElementById('cfg-content').textContent = d.config;
    tg?.HapticFeedback?.impactOccurred('light');
  } catch(e) { document.getElementById('cfg-content').textContent = '❌ ' + e.message; }
}

function copyConfig() {
  if (!currentConfig) return;
  if (navigator.clipboard) {
    navigator.clipboard.writeText(currentConfig)
      .then(() => { toast('📋 Скопировано'); tg?.HapticFeedback?.impactOccurred('medium'); })
      .catch(fbCopy);
  } else fbCopy();
}
function fbCopy() {
  const t = document.createElement('textarea');
  t.value = currentConfig; document.body.appendChild(t); t.select();
  document.execCommand('copy'); document.body.removeChild(t);
  toast('📋 Скопировано');
}
function dlConfig() {
  if (!currentConfig) return;
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([currentConfig],{type:'text/plain'}));
  a.download = (currentCfgName||'config') + '.vpn'; a.click();
  toast('📥 Скачивание…');
}

// ── Delete ─────────────────────────────────────────────────────────
function confirmDel(id, name) {
  pendingDelId = id; pendingDelName = name;
  document.getElementById('del-text').innerHTML =
    `Профиль <strong>${esc(name)}</strong> будет удалён из системы. Это действие необратимо.`;
  openO('modal-del');
}

async function doDelete() {
  if (!pendingDelId) return;
  const btn = document.getElementById('del-btn');
  btn.disabled=true; btn.textContent='Удаляю…';
  try {
    await api(`/api/profile/${pendingDelId}`, {method:'DELETE'});
    closeO('modal-del');
    toast('🗑 Удалено');
    tg?.HapticFeedback?.notificationOccurred('warning');
    await loadProfiles();
  } catch(e) { toast('❌ ' + e.message); }
  finally { btn.disabled=false; btn.textContent='Удалить'; pendingDelId=null; }
}

// ── Guide accordion ────────────────────────────────────────────────
function toggleG(head) {
  const body = head.nextElementSibling;
  const arr  = head.querySelector('.g-arrow');
  const open = body.classList.contains('open');
  body.classList.toggle('open', !open);
  arr.classList.toggle('open', !open);
  head.classList.toggle('open', !open);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

loadProfiles();
loadServer();
</script>
</body>
</html>"""


@app.route("/")
def index():
    # Пробрасываем флаг dev_mode в шаблон, чтобы скрипт защиты не мешал локальной разработке
    return render_template_string(MINIAPP_HTML, dev_mode=settings.MINIAPP_DEV_MODE)


if __name__ == "__main__":
    host  = getattr(settings, "MINIAPP_HOST", "0.0.0.0")
    port  = getattr(settings, "MINIAPP_PORT", 5000)
    debug = getattr(settings, "MINIAPP_DEV_MODE", False)
    logger.info("Mini App запущен на http://%s:%s", host, port)
    app.run(host=host, port=port, debug=debug, threaded=True)