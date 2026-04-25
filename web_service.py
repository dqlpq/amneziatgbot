"""
web_service.py — Публичный веб-сервис для подключения VPN по секретному ключу.
Запускается вместе с miniapp.py.
"""

import asyncio
import html
import json
import logging
import random
import re
import string
import threading
import traceback

from flask import Flask, request, jsonify, render_template_string
from config import settings
from database import Database, MAX_PROFILES_PER_USER
from amnezia_client import AmneziaClient
from shared import generate_dynamic_token, verify_dynamic_token, get_shared_ping

logger = logging.getLogger(__name__)

web_app = Flask(__name__)
web_app.config["JSON_AS_ASCII"] = False


SLUG_CHARS = string.ascii_lowercase + string.digits
SECRET_KEY_CHARS = string.ascii_letters + string.digits

_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z\u0430-\u044f\u0410-\u042f\u04510-9]{1,16}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9]{32}$")
_SLUG_RE = re.compile(r"^[a-z0-9]{5,6}$")


def _sanitize_key(raw: str) -> str | None:
    if not raw or not isinstance(raw, str): return None
    key = raw.strip()[:64]
    return key if _KEY_RE.match(key) else None

def _sanitize_name(raw: str) -> str | None:
    if not raw or not isinstance(raw, str): return None
    name = raw.strip()[:16]
    return name if _PROFILE_NAME_RE.match(name) else None

def generate_slug() -> str:
    return "".join(random.choices(SLUG_CHARS, k=5))

def generate_secret_key() -> str:
    return "".join(random.choices(SECRET_KEY_CHARS, k=32))


_db: Database | None = None
_amnezia: AmneziaClient | None = None
_loop = asyncio.new_event_loop()
_DB_TIMEOUT = 10

def _start_bg_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(target=_start_bg_loop, args=(_loop,), daemon=True)
_loop_thread.start()

def run_async(coro, timeout: float = _DB_TIMEOUT):
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    try: return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise RuntimeError(f"Database timeout after {timeout}s — возможно, база заблокирована")

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

WEB_HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>🤮 FQof</title>
<style>
  :root {
    --bg: #0d0d0d; --s1: #141414; --s2: #1c1c1c; --s3: #242424;
    --border: #2e2e2e; --border2: #3a3a3a;
    --text: #f0f0f0; --text2: #a0a0a0; --text3: #606060;
    --white: #ffffff; --green: #3ddc84; --red: #ff4d4d; --amber: #f5a623;
    --radius: 12px; --radius-s: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: 'Inter', -apple-system, system-ui, sans-serif;
    -webkit-font-smoothing: antialiased; display: flex; justify-content: center;
  }
  .wrap { width: 100%; max-width: 440px; padding: 48px 20px; display: flex; flex-direction: column; gap: 16px; align-items: center; }
  .header { text-align: center; display: flex; flex-direction: column; gap: 8px; margin-bottom: 8px; }
  .logo { font-size: 40px; }
  .header h1 { font-size: 22px; color: var(--white); }
  .header p { font-size: 13px; color: var(--text3); line-height: 1.5; }
  .ping-badge { display: inline-flex; align-items: center; gap: 6px; background: var(--s2); border: 1px solid var(--border); border-radius: 20px; padding: 4px 12px; font: 600 12px 'JetBrains Mono', monospace; }
  .ping-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text3); }
  .ping-dot.good { background: var(--green); }
  .ping-dot.warn { background: var(--amber); }
  .ping-dot.bad { background: var(--red); }
  .card { width: 100%; background: var(--s1); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; display: flex; flex-direction: column; gap: 16px; }
  .card.hidden { display: none !important; }
  .card-title { font-size: 13px; font-weight: 600; color: var(--text3); letter-spacing: 1px; text-transform: uppercase; }
  .result-title { color: var(--green); }
  .divider { border-top: 1px solid var(--border); padding-top: 16px; margin-top: 8px; display: flex; flex-direction: column; gap: 12px; }
  .field { display: flex; flex-direction: column; gap: 6px; }
  .label { font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: var(--text3); }
  .input { background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); color: var(--text); font: 14px 'JetBrains Mono', monospace; padding: 12px 14px; outline: none; transition: 0.2s; }
  .input:focus { border-color: var(--border2); }
  .hint { font-size: 11px; color: var(--text3); }
  .btn { width: 100%; border: none; border-radius: var(--radius-s); font: 600 14px inherit; padding: 12px; cursor: pointer; transition: 0.15s; display: flex; align-items: center; justify-content: center; gap: 8px; }
  .btn:active { transform: scale(0.98); }
  .btn:disabled { opacity: 0.5; pointer-events: none; }
  .btn-primary { background: var(--white); color: #000; }
  .btn-primary:hover { background: #e0e0e0; }
  .btn-outline { background: transparent; border: 1px solid var(--border2); color: var(--text2); }
  .btn-outline:hover { background: var(--s2); color: var(--text); }
  .link-box { background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); padding: 12px; font: 12px 'JetBrains Mono', monospace; color: var(--text2); word-break: break-all; position: relative; cursor: pointer; transition: 0.15s; }
  .link-box:hover { border-color: var(--border2); }
  .copy-hint { font-size: 9px; color: var(--text3); text-transform: uppercase; position: absolute; top: 6px; right: 8px; }
  .truncate { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; }
  .truncate.open { -webkit-line-clamp: unset; }
  .toggle-btn { font-size: 10px; font-weight: 600; color: var(--green); cursor: pointer; text-transform: uppercase; letter-spacing: 1px; }
  .g-section { background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); overflow: hidden; }
  .dl-link { display: flex; align-items: center; justify-content: space-between; padding: 12px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--white); font-size: 13px; font-weight: 600; transition: 0.15s; }
  .dl-link:hover { background: var(--s3); }
  .dl-link:last-child { border-bottom: none; }
  .dl-left { display: flex; align-items: center; gap: 8px; }
  .g-head { padding: 12px; display: flex; justify-content: space-between; cursor: pointer; font-size: 13px; font-weight: 600; color: var(--white); }
  .g-arrow { transition: 0.2s; color: var(--text3); }
  .g-arrow.open { transform: rotate(90deg); }
  .g-body { display: none; padding: 0 12px 12px; gap: 8px; flex-direction: column; }
  .g-body.open { display: flex; }
  .step { display: flex; gap: 10px; font-size: 12px; color: var(--text2); align-items: flex-start; }
  .step-n { width: 18px; height: 18px; border-radius: 50%; background: var(--s3); border: 1px solid var(--border2); display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; flex-shrink: 0; }
  code { background: var(--s3); padding: 2px 4px; border-radius: 4px; font-family: monospace; }
  .g-note { background: var(--s3); border-left: 3px solid var(--amber); padding: 8px 12px; font-size: 11px; color: var(--text2); border-radius: 4px; }
  .error-card { background: rgba(255,77,77,0.1); border: 1px solid rgba(255,77,77,0.3); color: var(--red); padding: 12px; border-radius: var(--radius-s); font-size: 13px; display: none; width: 100%; text-align: center; }
  .error-card.show { display: block; }
  .toast { position: fixed; bottom: 20px; background: var(--s3); border: 1px solid var(--border2); border-radius: 20px; padding: 8px 16px; font-size: 12px; font-weight: 600; opacity: 0; transform: translateY(10px); transition: 0.25s; pointer-events: none; }
  .toast.show { opacity: 1; transform: translateY(0); }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <div class="logo">🤮</div>
    <h1>FQof</h1>
    <p>Введите ключ для получения<br>конфигурации AmneziaVPN</p>
  </div>

  <div class="ping-badge">
    <div class="ping-dot" id="ping-dot"></div>
    <span id="ping-text">— ms</span>
  </div>

  <div class="error-card" id="error-card"></div>

  <div class="card" id="form-card">
    <div class="card-title">Секретный ключ</div>
    <div class="field">
      <label class="label">Ключ</label>
      <input class="input" id="key-input" type="text" placeholder="🫠" maxlength="32" autocomplete="off">
      <span class="hint">Ключ выдаётся индивидуально</span>
    </div>
    <div class="field">
      <label class="label">Имя профиля</label>
      <input class="input" id="name-input" type="text" placeholder="например: phone" maxlength="16" autocomplete="off">
      <span class="hint">Буквы (a–z, а–я) и цифры, до 16 символов</span>
    </div>
    <button class="btn btn-primary" id="connect-btn" onclick="doConnect()">Получить конфигурацию</button>
  </div>

  <div class="card hidden" id="result-block">
    <div class="card-title result-title">✓ Конфигурация готова</div>
    
    <div class="field">
      <div style="display:flex; justify-content:space-between; align-items:flex-end;">
        <label class="label">Строка vpn://</label>
        <span class="toggle-btn" id="cfg-toggle" onclick="toggleConfig()">Развернуть</span>
      </div>
      <div class="link-box" onclick="copyText(_config, '📋 Конфиг скопирован!')">
        <span class="copy-hint">нажать для копирования</span>
        <span id="config-text" class="truncate">…</span>
      </div>
      <button class="btn btn-outline" onclick="copyText(_config, '📋 Конфиг скопирован!')">📋 Скопировать vpn://</button>
    </div>

    <div class="field">
      <label class="label">Короткая ссылка (на 24 часа)</label>
      <div class="link-box" onclick="copyText(_shortLink, '🔗 Ссылка скопирована!')">
        <span class="copy-hint">нажать для копирования</span>
        <span id="short-link-text">…</span>
      </div>
      <button class="btn btn-outline" onclick="copyText(_shortLink, '🔗 Ссылка скопирована!')">📋 Скопировать ссылку</button>
    </div>


    <div class="divider">
      <div class="card-title" style="color: var(--white);">📖 Инструкция по подключению</div>
      <label class="label">Скачать AmneziaVPN</label>
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

      <div class="g-section">
        <div class="g-head" onclick="toggleG(this)">
          <div>📋 Как подключиться? (vpn://)</div><span class="g-arrow">›</span>
        </div>
        <div class="g-body">
          <div class="step"><div class="step-n">1</div><div>Откройте <strong>AmneziaVPN</strong> и нажмите <strong>«+»</strong>.</div></div>
          <div class="step"><div class="step-n">2</div><div>Нажмите <strong>«Вставить» </strong>.</div></div>
          <div class="step"><div class="step-n">3</div><div>Вставится скопированная строка <code>vpn://…</code></div></div>
          <div class="step"><div class="step-n">4</div><div>Нажмите <strong>Продолжить→Подключиться</strong>.</div></div>
          <div class="g-note"><strong>Важно:</strong> Не удаляйте приставку <code>vpn://</code>.</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  const $ = (id) => document.getElementById(id);
  const DYNAMIC_TOKEN = '__DYNAMIC_TOKEN__'; 
  let _config = '', _shortLink = '';
  let toastTimer;

  const showToast = (msg) => {
    $('toast').textContent = msg; 
    $('toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').classList.remove('show'), 2200);
  };
  
  const showError = (msg) => {
    if(!msg) return $('error-card').classList.remove('show');
    $('error-card').textContent = msg;
    $('error-card').classList.add('show');
  };

  const copyText = (text, successMsg) => {
    if (!text) return;
    navigator.clipboard?.writeText(text)
      .then(() => showToast(successMsg))
      .catch(() => {
        const t = document.createElement('textarea');
        t.value = text; document.body.appendChild(t); t.select();
        document.execCommand('copy'); t.remove(); showToast(successMsg);
      });
  };

  const toggleG = (head) => {
    const body = head.nextElementSibling;
    const arr = head.querySelector('.g-arrow');
    const isOpen = body.classList.toggle('open');
    arr.classList.toggle('open', isOpen);
  };

  const toggleConfig = () => {
    const t = $('config-text');
    const btn = $('cfg-toggle');
    const isOpen = t.classList.toggle('open');
    btn.textContent = isOpen ? 'Свернуть' : 'Развернуть';
  };

  async function doConnect() {
    showError(false);
    const key = $('key-input').value.trim();
    const name = $('name-input').value.trim();
    const btn = $('connect-btn');

    if (!key) return showError('Совсем дебил?🤨');
    if (!/^[A-Za-z0-9]{32}$/.test(key)) return showError('Ого!Иди нахуй👍🏻');
    if (!name) return showError('Введите имя профиля');
    if (!/^[a-zA-Zа-яА-ЯёЁ0-9]{1,16}$/.test(name)) return showError('Имя: только буквы/цифры, до 16 симв.');

    btn.disabled = true; btn.textContent = 'Подключаю…';
    
    try {
      const resp = await fetch('/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Dynamic-Token': DYNAMIC_TOKEN },
        body: JSON.stringify({ key, name }),
      });
      const data = await resp.json();
      
      if (!resp.ok || data.error) throw new Error(data.error || 'Ошибка сервера');
      
      _config = data.config; _shortLink = data.short_link;
      $('config-text').textContent = data.config;
      $('short-link-text').textContent = data.short_link;
      
      $('form-card').classList.add('hidden');
      $('result-block').classList.remove('hidden');
      
      window.scrollTo({ top: 0, behavior: 'smooth' });
      
    } catch(e) {
      showError(e.message === 'Failed to fetch' ? 'Сетевая ошибка. Попробуйте ещё раз.' : e.message);
    } finally {
      btn.disabled = false; btn.textContent = 'Получить конфигурацию';
    }
  }

  async function fetchPing() {
    try {
      const r = await fetch('/api/ping');
      const { ping_ms: ms } = await r.json();
      $('ping-text').textContent = ms + ' ms';
      $('ping-dot').className = 'ping-dot ' + (ms < 100 ? 'good' : ms < 250 ? 'warn' : 'bad');
    } catch (e) {}
  }
  
  document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !$('form-card').classList.contains('hidden')) doConnect();
  });
  
  fetchPing();
  setInterval(fetchPing, 180000);
</script>
</body>
</html>"""


@web_app.route("/")
def web_index():
    dyn_token = generate_dynamic_token()
    html_content = WEB_HTML.replace("__DYNAMIC_TOKEN__", dyn_token)
    return render_template_string(html_content)

@web_app.route("/api/ping")
def api_ping():
    ping_host = settings.VPN_HOST or settings.AMNEZIA_API_URL.split("//")[-1].split(":")[0] or "127.0.0.1"
    ms = get_shared_ping(ping_host, settings.AMNEZIA_API_URL)
    return jsonify({"ping_ms": ms})

@web_app.route("/connect", methods=["POST"])
def web_connect():
    client_token = request.headers.get("X-Dynamic-Token")
    if not verify_dynamic_token(client_token, max_age_seconds=300):
        return jsonify({"error": "Сессия устарела. Пожалуйста, обновите страницу."}), 403

    if not request.is_json: return jsonify({"error": "Ожидается JSON"}), 400
    data = request.get_json(silent=True) or {}

    raw_key = data.get("key", "")
    key = _sanitize_key(raw_key)
    if not key: return jsonify({"error": "Некорректный формат ключа"}), 400

    raw_name = data.get("name", "")
    name = _sanitize_name(raw_name)
    if not name: return jsonify({"error": "Некорректное имя профиля (только буквы и цифры, до 16 символов)"}), 400

    try:
        db = get_db()
        amnezia = get_amnezia()

        key_record = run_async(db.get_secret_key_by_value(key))
        if not key_record: return jsonify({"error": "Ты вводишь какую-то дичь💩"}), 403
        if key_record.get("revoked"): return jsonify({"error": "Ключ отозван"}), 403
        if key_record.get("used"): return jsonify({"error": "Ключ уже использован"}), 403

        tg_id = key_record["telegram_id"]

        key_blocked = run_async(db.get_user_key_blocked(tg_id))
        if key_blocked: return jsonify({"error": "Создание профилей заблокировано администратором"}), 403

        if not run_async(db.can_create_profile(tg_id)):
            return jsonify({"error": f"У пользователя достигнут лимит пользователей ({MAX_PROFILES_PER_USER})"}), 400

        if run_async(db.is_vpn_name_taken(name)):
            return jsonify({"error": "Имя профиля уже занято, выберите другое"}), 409

        result = run_async(amnezia.create_user(name), timeout=30)
        if result is None: return jsonify({"error": "Ошибка сервера. Попробуйте позже."}), 502

        peer_id = result.get("client", {}).get("id")
        config_str = result.get("client", {}).get("config", "")

        profile_id = run_async(db.add_profile(tg_id, name, peer_id, json.dumps(result, ensure_ascii=False)))
        run_async(db.set_key_used(key_record["id"]))

        slug = _unique_slug(db)
        run_async(db.get_or_create_short_link(profile_id, slug))
        domain = settings.SHORT_LINK_DOMAIN.rstrip("/")
        short_url = f"https://{domain}/c/{slug}"

        return jsonify({
            "ok": True, "config": config_str, "short_link": short_url,
            "vpn_name": name, "profile_id": profile_id,
        })

    except RuntimeError as e:
        logger.error("web_connect runtime error: %s", e)
        return jsonify({"error": "Сервер временно недоступен. Попробуйте позже."}), 503
    except Exception as e:
        logger.error("web_connect unexpected error: %s\n%s", e, traceback.format_exc())
        return jsonify({"error": "Внутренняя ошибка сервера"}), 500

def _unique_slug(db: Database) -> str:
    for _ in range(20):
        slug = generate_slug()
        existing = run_async(db.get_short_link_by_slug(slug))
        if not existing: return slug
    return "".join(random.choices(SLUG_CHARS, k=6))

@web_app.route("/c/<slug>")
def web_short_link(slug: str):
    clean_slug = slug.strip()[:10]
    if not _SLUG_RE.match(clean_slug): return render_template_string(_error_page("Ссылка недействительна")), 404

    try:
        db = get_db()
        link = run_async(db.get_short_link_by_slug(clean_slug))
        if not link: return render_template_string(_error_page("Ссылка не найдена (истек срок действия или удалена)")), 404

        profile = run_async(db.get_profile_by_id(link["profile_id"]))
        if not profile: return render_template_string(_error_page("Профиль удалён")), 404
        if profile.get("disabled"): return render_template_string(_error_page("Профиль отключён администратором")), 403

        config_str = None
        raw = profile.get("raw_response")
        if raw:
            try: config_str = json.loads(raw).get("client", {}).get("config")
            except: pass

        if not config_str:
            amnezia = get_amnezia()
            try: config_str = run_async(amnezia.get_client_config(profile.get("peer_id") or profile["vpn_name"]), timeout=15)
            except: pass

        if not config_str: return render_template_string(_error_page("Конфигурация недоступна")), 503

        return render_template_string(_config_page(profile["vpn_name"], config_str))

    except RuntimeError as e:
        return render_template_string(_error_page("Сервер временно недоступен, попробуйте позже")), 503
    except Exception as e:
        return render_template_string(_error_page("Внутренняя ошибка сервера")), 500

def _error_page(msg: str) -> str:
    safe = html.escape(msg)
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚠️Ошибка⚠️</title>
<style>body{{background:#0d0d0d;color:#f0f0f0;font-family:system-ui;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;}}
.box{{padding:32px;}} h1{{font-size:48px;margin-bottom:12px;}} p{{color:#606060;font-size:14px;}}</style></head>
<body><div class="box"><h1>⚠️</h1><p>{safe}</p></div></body></html>"""

def _config_page(vpn_name: str, config: str) -> str:
    safe_name = html.escape(vpn_name)
    safe_cfg = html.escape(config)
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
    <title>🤮 {safe_name} — config AmneziaVPN</title>
    <style>
        :root {{
            --bg: #0d0d0d; --s1: #141414; --s2: #1c1c1c; --s3: #242424;
            --border: #2e2e2e; --border2: #3a3a3a;
            --text: #f0f0f0; --text2: #a0a0a0; --text3: #606060;
            --white: #ffffff; --green: #3ddc84; --radius: 12px; --radius-s: 8px;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg); color: var(--text);
            font-family: 'Inter', -apple-system, system-ui, sans-serif;
            -webkit-font-smoothing: antialiased; display: flex; justify-content: center;
        }}
        .wrap {{ width: 100%; max-width: 440px; padding: 40px 20px; display: flex; flex-direction: column; gap: 16px; }}
        .card {{ width: 100%; background: var(--s1); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; display: flex; flex-direction: column; gap: 16px; }}
        .card-title {{ font-size: 13px; font-weight: 600; color: var(--green); letter-spacing: 1px; text-transform: uppercase; }}
        .label-row {{ display: flex; justify-content: space-between; align-items: flex-end; }}
        .label {{ font-size: 11px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase; color: var(--text3); }}
        .toggle-btn {{ font-size: 10px; font-weight: 700; color: var(--green); cursor: pointer; text-transform: uppercase; }}
        .link-box {{ background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); padding: 14px; font: 12px 'JetBrains Mono', monospace; color: var(--text2); word-break: break-all; position: relative; cursor: pointer; transition: 0.15s; line-height: 1.6; }}
        .link-box:hover {{ border-color: var(--border2); }}
        .truncate {{ display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
        .truncate.open {{ -webkit-line-clamp: unset; display: block; }}
        .copy-hint {{ font-size: 9px; color: var(--text3); text-transform: uppercase; position: absolute; top: 6px; right: 8px; }}
        .btn {{ width: 100%; border: none; border-radius: var(--radius-s); font: 600 14px inherit; padding: 14px; cursor: pointer; transition: 0.15s; display: flex; align-items: center; justify-content: center; gap: 8px; }}
        .btn-outline {{ background: transparent; border: 1px solid var(--border2); color: var(--text2); }}
        .btn-outline:hover {{ background: var(--s2); color: var(--text); }}
        .divider {{ border-top: 1px solid var(--border); padding-top: 16px; margin-top: 8px; display: flex; flex-direction: column; gap: 12px; }}
        .g-section {{ background: var(--s2); border: 1px solid var(--border); border-radius: var(--radius-s); overflow: hidden; }}
        .dl-link {{ display: flex; align-items: center; justify-content: space-between; padding: 12px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--white); font-size: 13px; font-weight: 600; }}
        .dl-left {{ display: flex; align-items: center; gap: 8px; }}
        .g-head {{ padding: 12px; display: flex; justify-content: space-between; cursor: pointer; font-size: 13px; font-weight: 600; color: var(--white); }}
        .g-arrow {{ transition: 0.2s; color: var(--text3); }}
        .g-arrow.open {{ transform: rotate(90deg); }}
        .g-body {{ display: none; padding: 0 12px 12px; gap: 8px; flex-direction: column; }}
        .g-body.open {{ display: flex; }}
        .step {{ display: flex; gap: 10px; font-size: 12px; color: var(--text2); align-items: flex-start; }}
        .step-n {{ width: 18px; height: 18px; border-radius: 50%; background: var(--s3); border: 1px solid var(--border2); display: flex; align-items: center; justify-content: center; font-size: 10px; font-weight: 700; flex-shrink: 0; }}
        .toast {{ position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%) translateY(10px); background: var(--s3); border: 1px solid var(--border2); border-radius: 30px; padding: 9px 18px; font-size: 13px; font-weight: 600; color: var(--text); opacity: 0; transition: 0.25s; pointer-events: none; z-index: 99; }}
        .toast.show {{ opacity: 1; transform: translateX(-50%) translateY(0); }}
    </style>
</head>
<body>
<div class="wrap">
    <div class="card">
        <div class="card-title">📋 Профиль: {safe_name}</div>
        <div style="display:flex; flex-direction:column; gap:8px;">
            <div class="label-row">
                <label class="label">Строка конфигурации</label>
                <span class="toggle-btn" id="cfg-toggle" onclick="toggleCfg()">Развернуть</span>
            </div>
            <div class="link-box" onclick="copyCfg()">
                <span class="copy-hint">копировать</span>
                <span id="cfg-text" class="truncate">{safe_cfg}</span>
            </div>
            <button class="btn btn-outline" onclick="copyCfg()">📋 Скопировать vpn://</button>
        </div>
        <div class="divider">
            <div class="card-title" style="color: var(--white);">📖 Инструкция по подключению</div>
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
            <div class="g-section">
                <div class="g-head" onclick="toggleG(this)">
                <div>📋 Как подключиться? (vpn://)</div><span class="g-arrow">›</span>
                </div>
                <div class="g-body">
          <div class="step"><div class="step-n">1</div><div>Откройте <strong>AmneziaVPN</strong> и нажмите <strong>«+»</strong>.</div></div>
          <div class="step"><div class="step-n">2</div><div>Нажмите <strong>«Вставить» </strong>.</div></div>
          <div class="step"><div class="step-n">3</div><div>Вставится скопированная строка <code>vpn://…</code></div></div>
          <div class="step"><div class="step-n">4</div><div>Нажмите <strong>Продолжить→Подключиться</strong>.</div></div>
          <div class="g-note"><strong>Важно:</strong> Не удаляйте приставку <code>vpn://</code>.</div>
                </div>
            </div>
        </div>
    </div>
</div>
<div class="toast" id="toast">📋 Скопировано!</div>
<script>
    function showToast() {{
        const t = document.getElementById('toast');
        t.classList.add('show');
        setTimeout(() => t.classList.remove('show'), 2000);
    }}
    function copyCfg() {{
        const text = document.getElementById('cfg-text').innerText;
        navigator.clipboard.writeText(text).then(showToast).catch(() => {{
            const el = document.createElement('textarea'); el.value = text;
            document.body.appendChild(el); el.select(); document.execCommand('copy');
            document.body.removeChild(el); showToast();
        }});
    }}
    function toggleCfg() {{
        const t = document.getElementById('cfg-text');
        const b = document.getElementById('cfg-toggle');
        const isOpen = t.classList.toggle('open');
        b.textContent = isOpen ? 'Свернуть' : 'Развернуть';
    }}
    function toggleG(head) {{
        const body = head.nextElementSibling;
        const arr = head.querySelector('.g-arrow');
        const open = body.classList.toggle('open');
        arr.classList.toggle('open', open);
    }}
</script>
</body>
</html>"""


if __name__ == "__main__":
    host = getattr(settings, "WEB_HOST", "0.0.0.0")
    port = getattr(settings, "WEB_PORT", 5001)
    logger.info("Web Service запущен на http://%s:%s", host, port)
    web_app.run(host=host, port=port, debug=False, threaded=True)