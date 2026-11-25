#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, os, sys, time, uuid, threading, urllib.request, urllib.parse, logging, configparser, py_compile
import hmac, hashlib, subprocess, re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

VERSION = "1.0.2"

# ==================== 1. ШПАРГАЛКА ПО КОМАНДАМ ====================
# Папка проекта на принтере:             /root/printer_tg_bot/
# Скрипт:                                /root/printer_tg_bot/printer_tg_bot.py
# База данных (катушки, ТО, статистика): /root/printer_tg_bot/printer_state.json
# Лог ошибок:                            /root/printer_tg_bot/printer_tg_bot.log
#
# Перезапустить (Creality K1/K2):   /etc/init.d/printer_tg_bot restart
# Перезапустить (Mainsail/Fluidd):  sudo systemctl restart printer_tg_bot
# Проверить процесс в памяти:       ps w | grep printer_tg_bot
# Тестовый запуск в консоли:        python3 /root/printer_tg_bot/printer_tg_bot.py
# ===================================================================

# ==================== 2. НАСТРОЙКИ СКРИПТА ========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.ini")
STATE_FILE = os.path.join(BASE_DIR, "printer_state.json")

# Дефолтные значения (как в оригинале, служат fallback'ом)
TOKEN = ""
CHAT_ID = "210754903"
MOONRAKER = "http://localhost:7125"
POLL_INTERVAL = 5
TIMEZONE_OFFSET = 7
DIGEST_HOUR = 20
MAINT_INTERVAL_HOURS = 150
NOZZLE_LIMIT_METERS = 1000
FILTER_LIMIT_HOURS = 500
PTFE_LIMIT_METERS = 2500
CUTTER_LIMIT_CUTS = 5000
CFS_HUMIDITY_LIMIT = 25
FILAMENT_PRICE_KG = 1200
FILAMENT_PRICE_BY_TYPE = {
    "PLA": 1200, "PETG": 1000, "ABS": 1100, "ASA": 1300, "TPU": 1650,
    "PC": 2200, "NYLON": 2500, "PA": 2500, "PA-CF": 3200, "HIPS": 1150,
}
ELEC_PRICE_KWH = 5.43
PRINTER_POWER_KW = 0.25
DEPRECIATION_PER_HOUR = 10
G_PER_M_DEFAULT = 2.98
M_PER_SPOOL = 335.0
DENSITY_G_PER_M = {
    "PLA": 2.98, "PETG": 3.06, "ABS": 2.53, "ASA": 2.60, "TPU": 2.90,
    "PC": 2.98, "NYLON": 2.95, "PA": 2.95, "PA-CF": 2.70, "HIPS": 2.55,
}
CUSTOM_CAM_URL = ""
CUSTOM_GCODES_DIR = ""
CUSTOM_TIMELAPSE_DIR = ""
USE_CLOUDFLARED = True
WEBAPP_PORT = 8088
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main"

# Попытка загрузить конфиги (если они созданы установщиком, они перезапишут дефолты)
if os.path.exists(SECRETS_FILE) or os.path.exists(CONFIG_FILE):
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    config.read(SECRETS_FILE)
    
    if config.has_option("telegram", "token"): TOKEN = config.get("telegram", "token")
    if config.has_option("telegram", "chat_id"): CHAT_ID = config.get("telegram", "chat_id")
    if config.has_option("printer", "moonraker_url"): MOONRAKER = config.get("printer", "moonraker_url")
    if config.has_option("printer", "poll_interval"): POLL_INTERVAL = config.getint("printer", "poll_interval")
    if config.has_option("printer", "timezone_offset"): TIMEZONE_OFFSET = config.getint("printer", "timezone_offset")
    if config.has_option("printer", "digest_hour"): DIGEST_HOUR = config.getint("printer", "digest_hour")
    if config.has_option("maintenance", "maint_interval_hours"): MAINT_INTERVAL_HOURS = config.getint("maintenance", "maint_interval_hours")
    if config.has_option("maintenance", "nozzle_limit_meters"): NOZZLE_LIMIT_METERS = config.getint("maintenance", "nozzle_limit_meters")
    if config.has_option("maintenance", "filter_limit_hours"): FILTER_LIMIT_HOURS = config.getint("maintenance", "filter_limit_hours")
    if config.has_option("maintenance", "ptfe_limit_meters"): PTFE_LIMIT_METERS = config.getint("maintenance", "ptfe_limit_meters")
    if config.has_option("maintenance", "cutter_limit_cuts"): CUTTER_LIMIT_CUTS = config.getint("maintenance", "cutter_limit_cuts")
    if config.has_option("maintenance", "cfs_humidity_limit"): CFS_HUMIDITY_LIMIT = config.getint("maintenance", "cfs_humidity_limit")
    if config.has_option("costs", "filament_price_default"): FILAMENT_PRICE_KG = config.getfloat("costs", "filament_price_default")
    if config.has_option("costs", "elec_price_kwh"): ELEC_PRICE_KWH = config.getfloat("costs", "elec_price_kwh")
    if config.has_option("costs", "printer_power_kw"): PRINTER_POWER_KW = config.getfloat("costs", "printer_power_kw")
    if config.has_option("costs", "depreciation_per_hour"): DEPRECIATION_PER_HOUR = config.getfloat("costs", "depreciation_per_hour")
    if config.has_option("advanced", "use_cloudflared"): USE_CLOUDFLARED = config.getboolean("advanced", "use_cloudflared")
    if config.has_option("advanced", "webapp_port"): WEBAPP_PORT = config.getint("advanced", "webapp_port")
    if config.has_option("advanced", "github_raw_base"): GITHUB_RAW_BASE = config.get("advanced", "github_raw_base").rstrip("/")

GCODES_DIR = ""
TIMELAPSE_DIR = ""
CAM_SNAPSHOT_URL = ""
HAS_CFS = False

# Логирование с ротацией (защита флеш-памяти)
log = logging.getLogger("printer_tg_bot")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(os.path.join(BASE_DIR, "printer_tg_bot.log"), maxBytes=5*1024*1024, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)

if not TOKEN or not CHAT_ID:
    log.critical("ERROR: token or chat_id missing! Check secrets.ini")
    print("ERROR: token or chat_id missing! Check secrets.ini")
    sys.exit(1)

# ==================== 3. МЕНЮ И КНОПКИ TELEGRAM ====================
MAIN_KB = {
    "inline_keyboard": [
        [{"text": "📊 Статус", "callback_data": "btn_status"}, {"text": "📈 Статистика", "callback_data": "btn_stats"}],
        [{"text": "🧵 Катушки", "callback_data": "btn_spools"}, {"text": "🔩 Расходники", "callback_data": "btn_cons"}],
        [{"text": "🛠 Обслуживание", "callback_data": "btn_maint"}]
    ]
}
MAINT_KB = {
    "inline_keyboard": [
        [{"text": "↔️ Прогнать ось X", "callback_data": "move_x"}, {"text": "↕️ Прогнать ось Y", "callback_data": "move_y"}],
        [{"text": "⬆️ Прогнать ось Z (винты)", "callback_data": "move_z"}],
        [{"text": "🔄 Сброс счетчиков", "callback_data": "btn_resets"}],
        [{"text": "⬅️ Назад в меню", "callback_data": "btn_back"}]
    ]
}
RESET_KB = {
    "inline_keyboard": [
        [{"text": "Сбросить сопло", "callback_data": "rst_nozzle"}, {"text": "Сбросить фильтр", "callback_data": "rst_filter"}],
        [{"text": "Сбросить PTFE", "callback_data": "rst_ptfe"}, {"text": "Сбросить нож", "callback_data": "rst_cutter"}],
        [{"text": "⬅️ Назад в обслуживание", "callback_data": "btn_maint"}]
    ]
}

last_state = None
last_fname = ""
last_update_id = 0
awaiting_spool = False
edit_spool_id = None
print_start_files = set()
last_cam_fail_ts = 0.0

# ==================== 4. TELEGRAM: ТЕКСТ, ФОТО И ВИДЕО =============
def tg_api(method, payload, timeout=15):
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    if "reply_markup" in payload and payload["reply_markup"]:
        payload["reply_markup"] = json.dumps(payload["reply_markup"])
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.warning("Telegram API error (%s): %s", method, e)
        return {}

def tg_send(text, kb=None):
    p = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = kb
    tg_api("sendMessage", p)

def tg_edit(msg_id, text, kb=None):
    p = {"chat_id": CHAT_ID, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if kb: p["reply_markup"] = kb
    return tg_api("editMessageText", p)

def out_edit_or_send(msg_id, text, kb=None):
    r = tg_edit(msg_id, text, kb)
    if not isinstance(r, dict) or not r.get("ok"):
        tg_send(text, kb)

def tg_answer_cb(cb_id, text=""):
    if not cb_id: return
    p = {"callback_query_id": cb_id}
    if text: p["text"] = text
    tg_api("answerCallbackQuery", p, timeout=10)

def tg_send_media(is_video, file_bytes, filename, mime, caption, kb=None, timeout=30):
    method = "sendVideo" if is_video else "sendPhoto"
    field = "video" if is_video else "photo"
    url = "https://api.telegram.org/bot%s/%s" % (TOKEN, method)
    bnd = uuid.uuid4().hex
    parts = [
        ("--" + bnd).encode(), b'Content-Disposition: form-data; name="chat_id"', b"", str(CHAT_ID).encode(),
        ("--" + bnd).encode(), b'Content-Disposition: form-data; name="caption"', b"", caption.encode("utf-8"),
        ("--" + bnd).encode(), b'Content-Disposition: form-data; name="parse_mode"', b"", b"HTML"
    ]
    if is_video:
        parts.extend([("--" + bnd).encode(), b'Content-Disposition: form-data; name="supports_streaming"', b"", b"true"])
    if kb:
        parts.extend([("--" + bnd).encode(), b'Content-Disposition: form-data; name="reply_markup"', b"", json.dumps(kb).encode("utf-8")])
    parts.extend([
        ("--" + bnd).encode(), ('Content-Disposition: form-data; name="%s"; filename="%s"' % (field, filename)).encode(),
        ("Content-Type: %s" % mime).encode(), b"", file_bytes, ("--" + bnd + "--").encode(), b""
    ])
    req = urllib.request.Request(url, data=b"\r\n".join(parts))
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % bnd)
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception as e:
        log.warning("Telegram media upload error (%s): %s", method, e)
        return False

def esc(text):
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def send_usb_photo(caption, kb=None):
    global CAM_SNAPSHOT_URL, last_cam_fail_ts
    if not CAM_SNAPSHOT_URL and time.time() - last_cam_fail_ts > 300:
        detect_environment()
    if CAM_SNAPSHOT_URL:
        try:
            with urllib.request.urlopen(CAM_SNAPSHOT_URL, timeout=5) as r:
                img = r.read()
                if len(img) >= 200 and tg_send_media(False, img, "cam.jpg", "image/jpeg", caption, kb, 20):
                    return
        except Exception:
            CAM_SNAPSHOT_URL = ""
            last_cam_fail_ts = time.time()
    tg_send(caption, kb)

def send_thumbnail(meta, caption, kb=None):
    thumbs = meta.get("thumbnails") or []
    if thumbs:
        best = max(thumbs, key=lambda x: (x.get("width", 0) * x.get("height", 0)))
        rel = best.get("relative_path", "")
        if rel:
            try:
                with urllib.request.urlopen(MOONRAKER + "/server/files/gcodes/" + urllib.parse.quote(rel), timeout=10) as r:
                    img = r.read()
                    if tg_send_media(False, img, "thumb.png", "image/png", caption, kb, 20):
                        return
            except Exception:
                pass
    tg_send(caption, kb)

def _timelapse_worker(caption, kb, exclude_files):
    try:
        if TIMELAPSE_DIR and os.path.isdir(TIMELAPSE_DIR):
            start_wait, video_path, last_size = time.time(), None, -1
            while time.time() - start_wait < 120:
                all_mp4 = [f for f in os.listdir(TIMELAPSE_DIR) if f.endswith(".mp4")]
                if exclude_files:
                    cur_files = [f for f in all_mp4 if f not in exclude_files]
                else:
                    cur_files = [f for f in all_mp4 if time.time() - os.path.getmtime(os.path.join(TIMELAPSE_DIR, f)) < 900]
                if cur_files:
                    cur_files.sort(key=lambda x: os.path.getmtime(os.path.join(TIMELAPSE_DIR, x)), reverse=True)
                    cand = os.path.join(TIMELAPSE_DIR, cur_files.pop(0))
                    cur_size = os.path.getsize(cand)
                    if cur_size > 10000 and cur_size == last_size:
                        video_path = cand
                        break
                    last_size = cur_size
                time.sleep(3)
            if video_path:
                size_mb = os.path.getsize(video_path) / (1024.0 * 1024.0)
                if size_mb > 49.0:
                    tg_send(caption + "\n🎥 <i>Таймлапс записан (%.1f МБ), но превышает лимит Telegram 50 МБ.</i>" % size_mb, kb)
                    return
                with open(video_path, "rb") as vf:
                    if tg_send_media(True, vf.read(), "timelapse.mp4", "video/mp4", caption, kb, 120):
                        return
    except Exception as e:
        log.warning("Timelapse err: %s", e)
    tg_send(caption, kb)

def send_timelapse_async(caption, kb=None):
    global print_start_files
    exclude_files = set(print_start_files)
    print_start_files.clear()
    threading.Thread(target=_timelapse_worker, args=(caption, kb, exclude_files), daemon=True).start()

# ==================== 5. MOONRAKER API, АВТОПОИСК ПУТЕЙ И ЦЕН ======
def moonraker_get(path, timeout=8):
    try:
        with urllib.request.urlopen(MOONRAKER + path, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("result", {})
    except Exception as e:
        log.warning("Moonraker error (%s): %s", path, e)
        return {}

def detect_environment():
    global GCODES_DIR, TIMELAPSE_DIR, CAM_SNAPSHOT_URL, HAS_CFS
    home = os.path.expanduser("~")
    if CUSTOM_GCODES_DIR and os.path.isdir(CUSTOM_GCODES_DIR):
        GCODES_DIR = CUSTOM_GCODES_DIR
    else:
        roots_res = moonraker_get("/server/files/roots", 4)
        for rt in (roots_res if isinstance(roots_res, list) else []):
            if rt.get("name") == "gcodes" and os.path.isdir(rt.get("path", "")):
                GCODES_DIR = rt["path"]
                break
    if not GCODES_DIR:
        for d in ("/mnt/UDISK/printer_data/gcodes", os.path.join(home, "printer_data/gcodes"), "/usr/data/printer_data/gcodes", "/mnt/UDISK/creality/userdata/box/gcode"):
            if os.path.isdir(d):
                GCODES_DIR = d
                break
    if CUSTOM_TIMELAPSE_DIR and os.path.isdir(CUSTOM_TIMELAPSE_DIR):
        TIMELAPSE_DIR = CUSTOM_TIMELAPSE_DIR
    else:
        for d in ("/mnt/UDISK/creality/userdata/delay_image/video", os.path.join(home, "timelapse"), os.path.join(home, "printer_data/timelapse"), "/usr/data/timelapse"):
            if os.path.isdir(d):
                TIMELAPSE_DIR = d
                break
    if CUSTOM_CAM_URL:
        CAM_SNAPSHOT_URL = CUSTOM_CAM_URL
    else:
        candidates = []
        cams = moonraker_get("/server/webcams/list", 4).get("webcams") or []
        for c in cams:
            if c.get("enabled", True) and c.get("snapshot_url"):
                u = c["snapshot_url"].strip()
                candidates.append(("http://localhost" + u) if u.startswith("/") else u)
        candidates.extend([
            "http://localhost:8081/?action=snapshot",
            "http://localhost:8080/?action=snapshot",
            "http://localhost/webcam/?action=snapshot"
        ])
        for u in candidates:
            try:
                with urllib.request.urlopen(u, timeout=2) as r:
                    if r.read(16).startswith(b"\xff\xd8"):
                        CAM_SNAPSHOT_URL = u
                        break
            except Exception:
                pass
    objs = moonraker_get("/printer/objects/list", 4).get("objects") or []
    HAS_CFS = ("box" in objs)

def get_print_stats():
    return moonraker_get("/printer/objects/query?print_stats&display_status&toolhead&extruder&heater_bed", 10).get("status", {})

def get_cfs_status():
    return moonraker_get("/printer/objects/query?box&filament_rack", 8).get("status", {}) if HAS_CFS else {}

def get_file_meta(filename):
    return moonraker_get("/server/files/metadata?filename=" + urllib.parse.quote(filename), 5) if filename else {}

def get_gcode_slot_weights(filename, meta=None):
    total_w = float((meta or {}).get("filament_weight_total") or 0)
    if meta:
        raw_w = str(meta.get("filament_weight") or meta.get("filament_weights") or "")
        if "," in raw_w or ";" in raw_w:
            sep = "," if "," in raw_w else ";"
            res = []
            for p in raw_w.split(sep):
                try: res.append(float(p.strip().rstrip("gг ")))
                except Exception: res.append(0.0)
            if sum(res) > 0: return res[:4]
    if not filename or not GCODES_DIR: return []
    p = os.path.join(GCODES_DIR, filename)
    if os.path.isfile(p):
        try:
            with open(p, "rb") as f:
                head = f.read(32768).decode("utf-8", errors="ignore")
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 24576))
                tail = f.read().decode("utf-8", errors="ignore")
            for line in reversed(tail.splitlines()):
                if "filament used [g]" in line and "=" in line:
                    vals_str = line.split("=", 1).pop()
                    sep = "," if "," in vals_str else ";"
                    res = []
                    for item in vals_str.split(sep):
                        try: res.append(float(item.strip().rstrip("gг ")))
                        except Exception: res.append(0.0)
                    if len(res) > 1 and sum(res) > 0:
                        return res[:4]
                    if len(res) == 1 and sum(res) > 0:
                        total_w = sum(res)
                        found_tools = set()
                        for line in head.splitlines():
                            cmd = line.split(";", 1).pop(0).strip()
                            if cmd in ("T0", "T1", "T2", "T3"):
                                found_tools.add(int(cmd[1:]))
                        if len(found_tools) == 1:
                            idx = found_tools.pop()
                            res = [0.0, 0.0, 0.0, 0.0]
                            res[idx] = total_w if total_w > 0 else 1.0
                            return res
        except Exception:
            pass
    return []

def get_history_totals():
    return moonraker_get("/server/history/totals", 5).get("job_totals", {})

def get_history_list(limit=500):
    return moonraker_get("/server/history/list?limit=%d" % limit, 8).get("jobs", [])

def density_for(meta, fname=""):
    try:
        types = detect_material_types(meta, fname)
    except Exception:
        types = ["PLA"]
    return sum(DENSITY_G_PER_M.get(t, G_PER_M_DEFAULT) for t in types) / max(1, len(types))

def get_flush_weight(meta):
    fg = meta.get("flushed_filament_g") or meta.get("purge_filament_g") or 0
    if fg <= 0:
        fmm = meta.get("flushed_filament_mm") or meta.get("purge_filament_mm") or 0
        if fmm > 0: fg = (fmm / 1000.0) * density_for(meta)
    return fg

def price_for(meta, fname=""):
    try:
        types = detect_material_types(meta, fname)
    except Exception:
        types = ["PLA"]
    return sum(FILAMENT_PRICE_BY_TYPE.get(t, FILAMENT_PRICE_KG) for t in types) / max(1, len(types))

def calc_costs(meta, duration_sec=0, used_mm=0):
    dens = density_for(meta)
    price_kg = price_for(meta)
    if used_mm > 0:
        w = (used_mm / 1000.0) * dens
    else:
        w = meta.get("filament_weight_total", 0) or 0
    if w <= 0:
        lmm = meta.get("filament_total", 0) or 0
        if lmm > 0: w = (lmm / 1000.0) * dens
    h = duration_sec / 3600.0
    elec = h * PRINTER_POWER_KW * ELEC_PRICE_KWH
    direct = (w / 1000.0) * price_kg + elec
    depr = h * DEPRECIATION_PER_HOUR
    return w, direct, direct + depr, elec, depr

# ==================== 6. УЧЕТ КАТУШЕК (CFS И ОДИНОЧНАЯ КАТУШКА) ===
state_lock = threading.Lock()

def load_state():
    with state_lock:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f: return json.load(f)
            except Exception: pass
        return {}

def save_state(data):
    with state_lock:
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f: json.dump(data, f)
            os.replace(tmp, STATE_FILE)
        except Exception: pass

def cfg(key):
    st = load_state()
    s = st.get("settings") or {}
    if key in s:
        return s[key]
    defaults = {
        "spool_tracking": True,
        "timezone_offset": TIMEZONE_OFFSET,
        "digest_hour": DIGEST_HOUR,
        "maint_hours": MAINT_INTERVAL_HOURS,
        "nozzle_meters": NOZZLE_LIMIT_METERS,
        "filter_hours": FILTER_LIMIT_HOURS,
        "ptfe_meters": PTFE_LIMIT_METERS,
        "cutter_cuts": CUTTER_LIMIT_CUTS,
        "cfs_humidity": CFS_HUMIDITY_LIMIT,
        "elec_price": ELEC_PRICE_KWH,
        "power_kw": PRINTER_POWER_KW,
        "depr_hour": DEPRECIATION_PER_HOUR,
        "price_default": FILAMENT_PRICE_KG,
        "prices": dict(FILAMENT_PRICE_BY_TYPE),
    }
    return defaults.get(key)

def cfg_price(mat):
    prices = cfg("prices") or FILAMENT_PRICE_BY_TYPE
    return float(prices.get(str(mat).upper(), cfg("price_default")))

COLOR_PALETTE = (
    ((228, 238, 155), "Светло-желтый", "Желтый"),
    ((252, 130, 115), "Коралловый", "Розовый"),
    ((15, 105, 250),  "Синий", "Синий"),
    ((85, 235, 95),   "Салатовый", "Салатовый"),
    ((240, 125, 40),  "Оранжевый", "Оранжевый"),
    ((255, 20, 15),   "Красный", "Красный"),
    ((245, 245, 245), "Белый", "Белый"),
    ((195, 210, 225), "Серый", "Серый"),
    ((155, 65, 240),  "Фиолетовый", "Фиолетовый"),
    ((10, 195, 255),  "Голубой", "Голубой"),
    ((15, 235, 210),  "Бирюзовый", "Бирюзовый"),
    ((245, 190, 235), "Светло-розовый", "Розовый"),
    ((255, 165, 10),  "Оранжевый", "Оранжевый"),
    ((15, 30, 235),   "Темно-синий", "Синий"),
    ((22, 24, 30),    "Черный", "Черный"),
    ((110, 175, 240), "Голубой", "Голубой"),
    ((25, 145, 250),  "Синий", "Синий"),
    ((65, 185, 45),   "Зеленый", "Зеленый"),
    ((10, 235, 115),  "Зеленый", "Зеленый"),
    ((240, 55, 235),  "Розовый", "Розовый"),
    ((235, 245, 20),  "Желтый", "Желтый"),
    ((205, 195, 245), "Сиреневый", "Фиолетовый"),
    ((65, 35, 245),   "Фиолетовый", "Фиолетовый"),
    ((250, 205, 15),  "Желтый", "Желтый"),
)

def _nearest_palette(raw_hex):
    h = str(raw_hex or "").strip().lower().lstrip("#")
    if len(h) == 7 and h.startswith("0"): h = h[1:]
    if len(h) != 6: return None
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except Exception:
        return None
    best, best_d = None, 10**9
    for (pr, pg, pb), exact_name, grp_name in COLOR_PALETTE:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_d, best = d, (exact_name, grp_name)
    return best

def hex_to_ru_name(raw_hex):
    res = _nearest_palette(raw_hex)
    return list(res).pop(0) if res else "Неизвестный"

def normalize_color_group(text_or_hex):
    s = str(text_or_hex or "").strip().lower()
    if s.startswith("#") or (len(s) in (6, 7) and all(c in "0123456789abcdef" for c in s)):
        res = _nearest_palette(s)
        return list(res).pop() if res else "Неизвестный"
    for base, keys in (
        ("Черный", ("черн", "чёрн", "black")),
        ("Белый", ("бел", "white")),
        ("Серебряный", ("серебр", "silver")),
        ("Серый", ("сер", "граф", "grey", "gray")),
        ("Желтый", ("желт", "жёлт", "золот", "yellow")),
        ("Оранжевый", ("оранж", "orange")),
        ("Красный", ("красн", "алый", "борд", "red")),
        ("Розовый", ("розов", "коралл", "фукс", "pink")),
        ("Салатовый", ("салат", "светящ", "лайм", "lime", "glow")),
        ("Зеленый", ("зелен", "хаки", "изумруд", "green")),
        ("Бирюзовый", ("бирюз", "тиффани", "cyan", "teal")),
        ("Голубой", ("голуб", "небесн", "sky")),
        ("Синий", ("син", "blue")),
        ("Фиолетовый", ("фиолет", "сирен", "пурпур", "лаванд", "purple")),
        ("Коричневый", ("коричн", "беж", "бронз", "медь", "brown")),
        ("Прозрачный", ("прозрач", "натур", "clear", "transp"))
    ):
        if any(k in s for k in keys):
            return base
    return text_or_hex.strip().capitalize()

def sp_title(sp):
    nm = str(sp.get("name") or "").strip()
    if nm: nm = nm[:1].upper() + nm[1:]
    return "#%d %s %s" % (sp.get("id", 0), sp.get("mat", ""), nm)

def get_cfs_slots():
    if not HAS_CFS:
        return {}, "SINGLE", "?", "?"
    try:
        cfs = get_cfs_status()
        box = cfs.get("box") or {}
        t1 = box.get("T1") or cfs.get("T1") or {}
        colors = list(t1.get("color_value") or [])
        mat_map = {}
        for grp in (box.get("same_material") or []):
            if isinstance(grp, list) and len(grp) >= 2:
                g_copy = list(grp)
                mat_name = str(g_copy.pop() or "PLA").upper()
                for item in g_copy:
                    if isinstance(item, list):
                        for sc in item: mat_map[str(sc)] = mat_name
                    elif isinstance(item, str) and item.startswith("T1"):
                        mat_map[item] = mat_name
        slots = {}
        for letter in ("A", "B", "C", "D"):
            raw_c = str(colors.pop(0) if colors else "-1").strip().lower()
            if raw_c in ("-1", "", "none"): continue
            hx = raw_c[1:] if (len(raw_c) == 7 and raw_c.startswith("0")) else raw_c.lstrip("#")
            slots[letter] = {"hex": hx, "mat": mat_map.get("T1" + letter, "PLA")}
        active_letter = str(t1.get("filament") or "A").strip().upper()
        if active_letter not in slots: active_letter = "A"
        return slots, active_letter, t1.get("temperature", "?"), t1.get("dry_and_humidity", "?")
    except Exception as e:
        log.warning("CFS parse err: %s", e)
        return {}, "A", "?", "?"

def get_used_slots(filename, meta, slots, active_let, total_w=0):
    if not HAS_CFS:
        return [("SINGLE", total_w)]
    weights = get_gcode_slot_weights(filename, meta)
    used = []
    letters = ("A", "B", "C", "D")
    if len(weights) > 1 and sum(weights) > 0:
        for idx, wg in enumerate(weights):
            if wg > 0.05 and idx < 4 and letters[idx] in slots:
                used.append((letters[idx], wg))
    if not used and active_let in slots:
        used.append((active_let, total_w))
    return used

def check_cfs_humidity():
    if not HAS_CFS: return
    try:
        _, _, temp, hum = get_cfs_slots()
        hum_val = int(float(str(hum)))
        if hum_val > CFS_HUMIDITY_LIMIT:
            st = load_state()
            if time.time() - st.get("last_hum_warn_ts", 0) >= 86400:
                st["last_hum_warn_ts"] = time.time()
                save_state(st)
                tg_send("💧 <b>Повышенная влажность в CFS: %d%%</b> (темп. %s°C)\nПора заменить силикагель или включить сушку филамента." % (hum_val, temp), MAIN_KB)
    except Exception:
        pass

def resolve_slot_spool(st, letter, mat="PLA", hx="000000", auto_create=True):
    spools = st.get("spools") or []
    if letter == "SINGLE":
        cur_id = st.get("single_spool_id")
        sp = next((s for s in spools if s.get("id") == cur_id and s.get("rem_g", 0) > 0), None)
        if sp: return sp
        avail = [s for s in spools if s.get("rem_g", 0) > 0]
        if avail:
            avail.sort(key=lambda x: x.get("id", 0))
            st["single_spool_id"] = avail.pop(0)["id"]
            save_state(st)
            return next((s for s in spools if s.get("id") == st["single_spool_id"]), None)
        return None
    ov_map = st.get("slot_overrides") or {}
    ov = ov_map.get(letter)
    if ov:
        if ov.get("hex") == hx and ov.get("mat") == mat:
            sp = next((s for s in spools if s.get("id") == ov.get("id") and s.get("rem_g", 0) > 0), None)
            if sp: return sp
        del ov_map[letter]
        st["slot_overrides"] = ov_map
        save_state(st)
    exact = hex_to_ru_name(hx)
    grp = normalize_color_group(hx)
    matches = [s for s in spools if s.get("mat") == mat and normalize_color_group(s.get("group") or s.get("name") or s.get("hex")) == grp and s.get("rem_g", 0) > 0]
    if matches:
        matches.sort(key=lambda x: (0 if exact.lower() in str(x.get("name") or "").lower() else 1, x.get("id", 0)))
        return matches.pop(0)
    if not auto_create: return None
    next_id = max([s.get("id", 0) for s in spools], default=0) + 1
    new_sp = {"id": next_id, "mat": mat, "name": exact, "group": grp, "hex": hx, "rem_g": 1000.0}
    spools.append(new_sp)
    st["spools"] = spools
    save_state(st)
    return new_sp

def deduct_spool_weight(filename, meta, weight_g):
    if weight_g <= 0 or not cfg("spool_tracking"): return ""
    slots, active_let, _, _ = get_cfs_slots()
    st = load_state()
    used = get_used_slots(filename, meta, slots, active_let, weight_g)
    raw_sum = sum(g for _, g in used)
    scale = (weight_g / raw_sum) if raw_sum > 0 else 1.0
    notes = []
    for let, part_g in used:
        slot = slots.get(let) or {"mat": "PLA", "hex": "000000"}
        left_to_sub = part_g * scale
        while left_to_sub > 0.1:
            sp = resolve_slot_spool(st, let, slot["mat"], slot["hex"], auto_create=HAS_CFS)
            if not sp: break
            take = min(sp["rem_g"], left_to_sub)
            sp["rem_g"] = round(max(0.0, sp["rem_g"] - take), 1)
            left_to_sub -= take
            notes.append("%s: осталось <b>%.0f г</b>" % (sp_title(sp), sp["rem_g"]))
    ov_map = st.get("slot_overrides") or {}
    for let, _ in used:
        ov_map.pop(let, None)
    st["slot_overrides"] = ov_map
    save_state(st)
    return "\n".join("• Катушка " + n for n in notes)

def build_spools_view():
    st = load_state()
    if not cfg("spool_tracking"):
        return "🧵 <b>Учет склада катушек отключен.</b>\nВключить его можно в <b>📱 Панель → Катушки / Настройки</b>.", {"inline_keyboard": [[{"text": "⬅️ Назад в меню", "callback_data": "btn_back"}]]}
    if HAS_CFS:
        slots, active_let, temp, hum = get_cfs_slots()
        lines = ["🧵 <b>Склад катушек и CFS (%s°C | влажн. %s%%):</b>" % (temp, hum), "", "📦 <b>Сейчас в CFS:</b>"]
        if not slots:
            lines.append("• Слоты CFS пусты")
        else:
            for let in ("A", "B", "C", "D"):
                if let in slots:
                    s_info = slots[let]
                    sp = resolve_slot_spool(st, let, s_info["mat"], s_info["hex"], auto_create=False)
                    mark = " ▶️" if let == active_let else ""
                    if sp:
                        lines.append("• Слот %s%s: <b>%s</b> — %.0f г" % (let, mark, sp_title(sp), sp["rem_g"]))
                    else:
                        lines.append("• Слот %s%s: <i>не назначена</i>" % (let, mark))
        ov_btn = {"text": "🔄 Уточнить слот", "callback_data": "sp_ov_menu"}
    else:
        lines = ["🧵 <b>Склад катушек принтера:</b>", "", "📦 <b>Установлена сейчас:</b>"]
        sp = resolve_slot_spool(st, "SINGLE")
        if sp: lines.append("• Активная: <b>%s</b> — %.0f г" % (sp_title(sp), sp["rem_g"]))
        else: lines.append("• Катушка не выбрана (добавьте её ниже)")
        ov_btn = {"text": "🔄 Выбрать текущую", "callback_data": "sp_ovslot_SINGLE"}
    spools = [s for s in (st.get("spools") or []) if s.get("rem_g", 0) > 0]
    if spools:
        lines.extend(["", "🗄 <b>Все открытые катушки (%d шт.):</b>" % len(spools)])
        for sp in sorted(spools, key=lambda x: x.get("rem_g", 0)):
            lines.append("• %s: <b>%.0f г</b>" % (sp_title(sp), sp["rem_g"]))
    kb = {
        "inline_keyboard": [
            [{"text": "➕ Добавить", "callback_data": "sp_add"}, {"text": "✏️ Редактировать", "callback_data": "sp_edit_menu"}],
            [ov_btn, {"text": "🗑 Удалить", "callback_data": "sp_del_menu"}],
            [{"text": "⬅️ Назад в меню", "callback_data": "btn_back"}]
        ]
    }
    return "\n".join(lines), kb

def build_spool_picker(prefix, back_cb, page=0, pref_mat=None):
    spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
    if pref_mat:
        spools.sort(key=lambda x: (0 if x.get("mat") == pref_mat else 1, x.get("id", 0)))
    else:
        spools.sort(key=lambda x: x.get("id", 0))
    per_page = 8
    total_pages = max(1, (len(spools) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    chunk = spools[page * per_page : (page + 1) * per_page]
    rows, row = [], []
    for s in chunk:
        nm = str(s.get("name") or "").strip()
        if nm: nm = nm[:1].upper() + nm[1:]
        btn_txt = "#%d %s %s • %.0fг" % (s.get("id", 0), s.get("mat", ""), nm, s.get("rem_g", 0))
        row.append({"text": btn_txt, "callback_data": "%s%d" % (prefix, s["id"])})
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append({"text": "⬅️", "callback_data": "sp_pg_%s|%d|%s" % (prefix, page - 1, back_cb)})
        nav.append({"text": "Стр. %d / %d" % (page + 1, total_pages), "callback_data": "noop"})
        if page < total_pages - 1:
            nav.append({"text": "➡️", "callback_data": "sp_pg_%s|%d|%s" % (prefix, page + 1, back_cb)})
        rows.append(nav)
    rows.append([{"text": "⬅️ Назад", "callback_data": back_cb}])
    return {"inline_keyboard": rows}

# ==================== 7. СЧЕТЧИКИ РАСХОДНИКОВ, МАТЕРИАЛОВ И БРАКА ==
def detect_material_types(meta, fname=""):
    raw = str(meta.get("filament_type", "") or "").strip().upper()
    if raw:
        types = [t.strip() for t in raw.split(";") if t.strip()]
        if types: return types
    up = str(fname or "").upper()
    for known in ("PETG", "TPU", "ABS", "ASA", "PA-CF", "PLA"):
        if known in up: return [known]
    return ["PLA"]

def record_material_usage(meta, used_mm, fname=""):
    if used_mm <= 0: used_mm = meta.get("filament_total", 0) or 0
    if used_mm <= 0: return
    types = detect_material_types(meta, fname)
    st = load_state()
    mats, w_mats = st.get("materials_meters", {}), st.get("week_materials", {})
    share = (used_mm / 1000.0) / len(types)
    for t in types:
        mats[t] = mats.get(t, 0.0) + share
        w_mats[t] = w_mats.get(t, 0.0) + share
    st["materials_meters"], st["week_materials"] = mats, w_mats
    save_state(st)

def record_waste(weight_g, direct_rub):
    if weight_g <= 0.5 and direct_rub <= 0.5: return
    st = load_state()
    st["waste_g"] = round(st.get("waste_g", 0.0) + weight_g, 1)
    st["waste_rub"] = round(st.get("waste_rub", 0.0) + direct_rub, 1)
    st["week_waste_g"] = round(st.get("week_waste_g", 0.0) + weight_g, 1)
    st["week_waste_rub"] = round(st.get("week_waste_rub", 0.0) + direct_rub, 1)
    save_state(st)

def update_and_check_counters(total_hours, total_meters, add_cuts=0):
    st = load_state()
    changed = False
    last_m = st.get("last_maint_hours", 0)
    if last_m == 0 and total_hours >= MAINT_INTERVAL_HOURS:
        st["last_maint_hours"] = (int(total_hours) // MAINT_INTERVAL_HOURS) * MAINT_INTERVAL_HOURS
        changed = True
    elif total_hours - last_m >= MAINT_INTERVAL_HOURS:
        st["last_maint_hours"] = (int(total_hours) // MAINT_INTERVAL_HOURS) * MAINT_INTERVAL_HOURS
        changed = True
        tg_send("🛠 <b>Пора провести ТО принтера!</b>\nНаработка: <b>%.1f ч</b>\nОчистите и смажьте направляющие/винты, проверьте ремни." % total_hours, MAIN_KB)
    checks = [
        ("nozzle_start_meters", "nozzle_warned", total_meters, NOZZLE_LIMIT_METERS, "⚠️ <b>Ресурс сопла выработан!</b>\nПробег: <b>%.1f / %d м</b>.\nПосле замены сбросьте счетчик в меню Обслуживание."),
        ("filter_start_hours", "filter_warned", total_hours, FILTER_LIMIT_HOURS, "🌬 <b>Пора заменить фильтр воздуха!</b>\nНаработка фильтра: <b>%.1f / %d ч</b>.\nПосле замены сбросьте счетчик в меню Обслуживание."),
        ("ptfe_start_meters", "ptfe_warned", total_meters, PTFE_LIMIT_METERS, "➰ <b>Проверьте PTFE-трубку!</b>\nПробег филамента через трубку: <b>%.1f / %d м</b>.\nПосле замены сбросьте счетчик в меню Обслуживание.")
    ]
    for start_k, warn_k, cur_val, limit_val, msg_tpl in checks:
        if start_k not in st:
            st[start_k], st[warn_k], changed = cur_val, False, True
        else:
            used = max(0.0, cur_val - st[start_k])
            if used >= limit_val and not st.get(warn_k, False):
                st[warn_k], changed = True, True
                tg_send(msg_tpl % (used, limit_val), MAIN_KB)
    if add_cuts > 0:
        st["cutter_cuts"], changed = st.get("cutter_cuts", 0) + add_cuts, True
        cuts = st.get("cutter_cuts", 0)
        if cuts >= CUTTER_LIMIT_CUTS and not st.get("cutter_warned", False):
            st["cutter_warned"], changed = True, True
            tg_send("✂️ <b>Пора проверить лезвие резака филамента!</b>\nВыполнено резов: <b>%d / %d</b>.\nПосле замены сбросьте счетчик в меню Обслуживание." % (cuts, CUTTER_LIMIT_CUTS), MAIN_KB)
    if changed:
        save_state(st)

def update_counters(cuts=0):
    totals = get_history_totals()
    if totals:
        update_and_check_counters(
            totals.get("total_time", 0) / 3600.0,
            totals.get("total_filament_used", 0) / 1000.0,
            cuts)

# ==================== 8. ОТЧЕТЫ: РАСХОДНИКИ, СТАТИСТИКА, ДАЙДЖЕСТ ==
def analyze_history(history):
    now_ts = time.time()
    sec_7d, sec_30d, mm_7d, jobs_7d, ok_7d, ok_all = 0.0, 0.0, 0.0, 0, 0, 0
    mats_7d = {}
    for j in history:
        if j.get("status") == "completed": ok_all += 1
        st_time = j.get("start_time", 0) or 0
        dur = j.get("print_duration", 0) or 0
        if st_time >= now_ts - 30 * 86400: sec_30d += dur
        if st_time >= now_ts - 7 * 86400:
            sec_7d += dur
            jobs_7d += 1
            if j.get("status") == "completed": ok_7d += 1
            umm = j.get("filament_used", 0) or 0
            mm_7d += umm
            if umm > 0:
                types = detect_material_types(j.get("metadata") or {}, j.get("filename", ""))
                share = (umm / 1000.0) / len(types)
                for t in types: mats_7d[t] = mats_7d.get(t, 0.0) + share
    return sec_7d / 3600.0, sec_30d / 3600.0, mm_7d / 1000.0, jobs_7d, ok_7d, ok_all, mats_7d

def format_consumables():
    totals = get_history_totals()
    if not totals: return "⚠️ Не удалось получить данные расходников."
    h, m = totals.get("total_time", 0) / 3600.0, totals.get("total_filament_used", 0) / 1000.0
    st = load_state()
    return (
        "🔩 <b>Ресурс расходников и ТО:</b>\n"
        "• До планового ТО: <b>~%.1f ч</b>\n"
        "• Сопло: <b>%.1f / %d м</b>\n"
        "• Фильтр: <b>%.1f / %d ч</b>\n"
        "• PTFE-трубка: <b>%.1f / %d м</b>\n"
        "• Нож резака: <b>%d / %d резов</b>" % (
            max(0.0, (st.get("last_maint_hours", 0) + MAINT_INTERVAL_HOURS) - h),
            max(0.0, m - st.get("nozzle_start_meters", m)), NOZZLE_LIMIT_METERS,
            max(0.0, h - st.get("filter_start_hours", h)), FILTER_LIMIT_HOURS,
            max(0.0, m - st.get("ptfe_start_meters", m)), PTFE_LIMIT_METERS,
            st.get("cutter_cuts", 0), CUTTER_LIMIT_CUTS
        )
    )

def format_stats():
    totals = get_history_totals()
    if not totals: return "⚠️ Не удалось получить статистику."
    h, m = totals.get("total_time", 0) / 3600.0, totals.get("total_filament_used", 0) / 1000.0
    jobs, longest_h = totals.get("total_jobs", 0), totals.get("longest_print", 0) / 3600.0
    total_g = m * G_PER_M_DEFAULT
    elec = h * PRINTER_POWER_KW * ELEC_PRICE_KWH
    direct = (total_g / 1000.0) * FILAMENT_PRICE_KG + elec
    depr = h * DEPRECIATION_PER_HOUR
    history = get_history_list(500)
    h_7d, h_30d, _, _, _, ok_all, _ = analyze_history(history)
    hist_len = len(history)
    st = load_state()
    waste_g, waste_rub = st.get("waste_g", 0.0), st.get("waste_rub", 0.0)
    lines = [
        "📈 <b>Общая статистика принтера:</b>",
        "• Время печати: <b>%.1f ч</b>" % h,
        "• Расход филамента: <b>%.1f м</b> (~%.1f катушек)" % (m, m / M_PER_SPOOL),
        "• Всего печатей: <b>%d</b>" % jobs
    ]
    if hist_len > 0:
        lines.append("• Рейтинг надежности: <b>%.0f%%</b> (%d из %d без сбоев)" % (ok_all * 100.0 / hist_len, ok_all, hist_len))
    if waste_g > 0:
        w_pct = (waste_g * 100.0 / total_g) if total_g > 0 else 0
        lines.append("• Потери на брак: <b>%.0f г</b> (~%.0f руб. | %.1f%%)" % (waste_g, waste_rub, w_pct))
    lines.extend([
        "• Рекорд печати: <b>%.1f ч</b>" % longest_h,
        "• В среднем за печать: <b>~%.1f ч / ~%.0f г</b>" % ((h / jobs if jobs else 0), (total_g / jobs if jobs else 0)),
        "",
        "⏱ <b>Нагрузка принтера (КПД):</b>",
        "• За 7 дней: <b>%.1f ч</b> (~%.1f ч/день | КПД %.0f%%)" % (h_7d, h_7d / 7.0, min(100.0, h_7d * 100.0 / 168.0)),
        "• За 30 дней: <b>%.1f ч</b> (~%.1f ч/день | КПД %.0f%%)" % (h_30d, h_30d / 30.0, min(100.0, h_30d * 100.0 / 720.0))
    ])
    mats = st.get("materials_meters", {})
    if mats:
        lines.extend(["", "🧵 <b>Расход по типам пластика:</b>"])
        for k in sorted(mats, key=mats.get, reverse=True):
            lines.append("• %s: <b>%.1f м</b> (~%.1f катушек)" % (k, mats[k], mats[k] / M_PER_SPOOL))
    lines.extend([
        "",
        "💰 <b>Затраты за всё время:</b>",
        "• Прямые (пластик+свет): <b>~%.0f руб.</b> (свет ~%.0f руб.)" % (direct, elec),
        "• С учетом амортизации: <b>~%.0f руб.</b> (аморт. ~%.0f руб.)" % (direct + depr, depr)
    ])
    return "\n".join(lines)

def check_weekly_digest():
    local_now = datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET)
    if local_now.weekday() == 6 and local_now.hour == DIGEST_HOUR:
        today_str = local_now.strftime("%Y-%m-%d")
        st = load_state()
        if st.get("last_digest_date") == today_str: return
        h_7d, _, m_7d, jobs_7d, ok_7d, _, fallback_mats = analyze_history(get_history_list(500))
        week_mats = st.get("week_materials") or fallback_mats
        w_waste_g, w_waste_rub = st.get("week_waste_g", 0.0), st.get("week_waste_rub", 0.0)
        st["last_digest_date"], st["week_materials"] = today_str, {}
        st["week_waste_g"], st["week_waste_rub"] = 0.0, 0.0
        save_state(st)
        elec = h_7d * PRINTER_POWER_KW * ELEC_PRICE_KWH
        direct = ((m_7d * G_PER_M_DEFAULT) / 1000.0) * FILAMENT_PRICE_KG + elec
        full = direct + (h_7d * DEPRECIATION_PER_HOUR)
        rel_pct = (ok_7d * 100.0 / jobs_7d) if jobs_7d > 0 else 100.0
        lines = [
            "📅 <b>Еженедельный дайджест принтера:</b>",
            "• Напечатано за неделю: <b>%.1f ч</b> (КПД %.0f%%)" % (h_7d, min(100.0, h_7d * 100.0 / 168.0)),
            "• Надежность за неделю: <b>%.0f%%</b> (%d из %d завершено)" % (rel_pct, ok_7d, jobs_7d),
            "• Расход пластика: <b>%.1f м</b> (~%.1f катушек)" % (m_7d, m_7d / M_PER_SPOOL)
        ]
        if w_waste_g > 0:
            lines.append("• Потери на брак: <b>%.0f г</b> (~%.0f руб.)" % (w_waste_g, w_waste_rub))
        if week_mats:
            parts = ["%s: %.1f м (~%.1f кат.)" % (k, week_mats[k], week_mats[k] / M_PER_SPOOL) for k in sorted(week_mats, key=week_mats.get, reverse=True)]
            lines.append("• По материалам: <b>%s</b>" % ", ".join(parts))
        lines.append("• Затраты за неделю: <b>~%.0f руб.</b> (свет ~%.0f руб., с аморт. ~%.0f руб.)" % (direct, elec, full))
        tg_send("\n".join(lines), MAIN_KB)

# ==================== 9. ЗАВЕРШЕНИЕ ПЕЧАТИ (ОБЩАЯ ЛОГИКА) ==========
def process_completed_job(fname, meta, print_dur, used_mm, send_video=True):
    w, direct, full, elec, depr = calc_costs(meta, print_dur, used_mm)
    flush_g = get_flush_weight(meta)
    record_material_usage(meta, used_mm, fname)
    spool_notes = deduct_spool_weight(fname, meta, w)
    changes = meta.get("filament_change_count", 0) or 0
    final_cut = 1 if (HAS_CFS and used_mm > 0) else 0
    update_counters(changes + final_cut)
    lines = ["🏁 <b>Печать завершена:</b>", "• Файл: <b>%s</b>" % esc(fname), "• Время печати: <b>%s</b>" % format_dur(print_dur)]
    if w > 0:
        lines.append("• Расход пластика: <b>%.1f г</b>%s" % (w, " (промывка CFS: ~%.1f г)" % flush_g if flush_g > 0.1 else ""))
    if spool_notes: lines.append(spool_notes)
    if direct > 0:
        lines.extend(["", "💰 <b>Фактическая стоимость:</b>",
            "• Прямые (пластик+свет): <b>~%.1f руб.</b> (свет ~%.1f руб.)" % (direct, elec),
            "• С амортизацией: <b>~%.1f руб.</b> (аморт. ~%.1f руб.)" % (full, depr)])
    totals = get_history_totals()
    if totals: lines.extend(["", format_consumables()])
    if send_video:
        send_timelapse_async("\n".join(lines), MAIN_KB)
    else:
        tg_send("\n".join(lines), MAIN_KB)

def handle_missed_job_end(old_fname):
    try:
        job = next((j for j in get_history_list(5) if j.get("filename") == old_fname), None)
        if not job:
            log.warning("Пропущенная печать не найдена в истории: %s", old_fname)
            return
        status = job.get("status", "")
        dur = job.get("print_duration", 0) or 0
        umm = job.get("filament_used", 0) or 0
        meta = job.get("metadata") or {}
        log.info("Обработка пропущенного завершения: %s (статус %s)", old_fname, status)
        if status == "completed":
            process_completed_job(old_fname, meta, dur, umm, send_video=True)
            return
        record_material_usage(meta, umm, old_fname)
        w, direct, full, _, depr = calc_costs(meta, dur, umm)
        record_waste(w, direct)
        spool_notes = deduct_spool_weight(old_fname, meta, w)
        update_counters(0)
        lines = ["⏹ <b>Пропущенное завершение печати (статус: %s):</b>" % esc(status or "unknown"),
            "• Файл: <b>%s</b>" % esc(old_fname),
            "• Время печати: <b>%s</b>" % format_dur(dur)]
        if w > 0:
            lines.append("• Расход пластика: <b>%.1f г</b>" % w)
        if spool_notes:
            lines.extend(["", spool_notes])
        tg_send("\n".join(lines), MAIN_KB)
    except Exception:
        log.exception("Ошибка обработки пропущенного завершения")

# ==================== 10. СТАТУС, ОБСЛУЖИВАНИЕ И ДВИЖЕНИЕ ОСЕЙ ====
def format_dur(sec):
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    return "%d ч %d мин" % (h, m) if h > 0 else "%d мин" % m

def format_status():
    st = get_print_stats()
    if not st: return "⚠️ Принтер недоступен."
    ps, ds = st.get("print_stats", {}), st.get("display_status", {})
    state, fname = ps.get("state", "unknown"), ps.get("filename", "")
    prog, dur = (ds.get("progress", 0) or 0) * 100, ps.get("print_duration", 0) or 0
    info = ps.get("info") or {}
    s_map = {"printing": "🖨 Печатает", "paused": "⏸ На паузе", "complete": "🏁 Завершена", "standby": "💤 Ожидание", "error": "❌ Ошибка", "cancelled": "⏹ Отменена"}
    lines = ["📊 <b>Текущий статус:</b>", "• Статус: <b>%s</b>" % s_map.get(state, state)]
    if state in ("printing", "paused") and fname:
        lines.append("• Файл: <b>%s</b>" % esc(fname))
    p_str = "• Прогресс: <b>%.1f%%</b>" % prog
    if info.get("current_layer") is not None and info.get("total_layer"):
        p_str += " (слой %d из %d)" % (info["current_layer"], info["total_layer"])
    lines.append(p_str)
    est = get_file_meta(fname).get("estimated_time", 0)
    if est > dur:
        rem = est - dur
        end_dt = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET, seconds=rem)).strftime("%d.%m.%Y %H:%M")
        lines.extend(["• Осталось: <b>~%s</b>" % format_dur(rem), "• Окончание: <b>~%s</b>" % end_dt])
    return "\n".join(lines)

def do_reset(target):
    st = load_state()
    if target == "nozzle":
        val = get_history_totals().get("total_filament_used", 0) / 1000.0
        st["nozzle_start_meters"], st["nozzle_warned"] = val, False
        msg = "✅ Счетчик сопла сброшен (%.1f м)" % val
    elif target == "filter":
        val = get_history_totals().get("total_time", 0) / 3600.0
        st["filter_start_hours"], st["filter_warned"] = val, False
        msg = "✅ Счетчик фильтра сброшен (%.1f ч)" % val
    elif target == "ptfe":
        val = get_history_totals().get("total_filament_used", 0) / 1000.0
        st["ptfe_start_meters"], st["ptfe_warned"] = val, False
        msg = "✅ Счетчик PTFE сброшен (%.1f м)" % val
    elif target == "cutter":
        st["cutter_cuts"], st["cutter_warned"] = 0, False
        msg = "✅ Счетчик ножа сброшен (0 резов)"
    else:
        return ""
    save_state(st)
    return msg

def _lube_worker(axis):
    try:
        st = get_print_stats()
        if st.get("print_stats", {}).get("state") in ("printing", "paused"):
            tg_send("⛔ Нельзя двигать оси во время печати!", MAINT_KB)
            return
        th = st.get("toolhead", {})
        homed = th.get("homed_axes", "")
        a_min = list(th.get("axis_minimum") or (0, 0, 0))
        a_max = list(th.get("axis_maximum") or (250, 250, 250))
        idx = {"x": 0, "y": 1, "z": 2}.get(axis, 0)
        for _ in range(idx):
            a_min.pop(0); a_max.pop(0)
        lo = max(5.0, float(a_min.pop(0)) + 5.0)
        hi = max(20.0, float(a_max.pop(0)) - (10.0 if axis == "z" else 5.0))
        feed = "F1800" if axis == "z" else "F9000"
        ax_u = axis.upper()
        cmds = (["G28"] if ("x" not in homed or "y" not in homed or "z" not in homed) else []) + [
            "G90", "G1 %s%.1f %s" % (ax_u, hi, feed), "G1 %s%.1f %s" % (ax_u, lo, feed),
            "G1 %s%.1f %s" % (ax_u, hi, feed), "G1 %s%.1f %s" % (ax_u, lo, feed)
        ]
        url = MOONRAKER + "/printer/gcode/script"
        with urllib.request.urlopen(url, data=urllib.parse.urlencode({"script": "\n".join(cmds)}).encode(), timeout=300):
            pass
        tg_send("✅ Ось %s успешно прогнана!" % ax_u, MAINT_KB)
    except Exception as e:
        log.exception("Ошибка движения оси %s", axis.upper())
        tg_send("⚠️ Ошибка движения оси %s: %s" % (axis.upper(), e), MAINT_KB)

# ==================== 11. ОБРАБОТКА КОМАНД И КНОПОК ================
def check_commands():
    global last_update_id
    while True:
        try:
            # Long Polling: 25 секунд. Не грузит CPU, но быстро реагирует на сообщения.
            res = tg_api("getUpdates", {"offset": last_update_id + 1, "timeout": 25}, timeout=30).get("result", [])
            if not isinstance(res, list):
                time.sleep(5)
                continue
            for item in res:
                last_update_id = item["update_id"]
                try:
                    _handle_update(item)
                except Exception:
                    log.exception("Ошибка обработки апдейта %s", item.get("update_id"))
        except Exception:
            log.exception("Ошибка в цикле check_commands")
            time.sleep(5)

def _handle_update(item):
    global awaiting_spool, edit_spool_id
    cb = item.get("callback_query")
    if cb:
        msg = cb.get("message", {})
        if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID): return
        cb_id, msg_id, data = cb.get("id"), msg.get("message_id"), (cb.get("data") or "")
        out = lambda t, kb=None: out_edit_or_send(msg_id, t, kb)
        if data == "noop":
            tg_answer_cb(cb_id)
        elif data.startswith("sp_pg_"):
            raw_pg = data[6:]
            if "|" in raw_pg:
                pref, pg_s, b_cb = raw_pg.split("|", 2)
                tg_answer_cb(cb_id)
                out(msg.get("text") or "Выберите катушку:", build_spool_picker(pref, b_cb, int(pg_s)))
        elif data == "btn_status":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); send_usb_photo(format_status(), MAIN_KB)
        elif data == "btn_stats":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(format_stats(), MAIN_KB)
        elif data == "btn_spools":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); t, kb = build_spools_view(); out(t, kb)
        elif data == "sp_add":
            awaiting_spool = True; edit_spool_id = None; tg_answer_cb(cb_id)
            out("✍️ <b>Новая катушка:</b>\nОтправьте тип, цвет и вес (г).\nПример: <b>PLA Черный матовый 850</b>", {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "btn_spools"}]]})
        elif data == "sp_edit_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: t, kb = build_spools_view(); out(t, kb)
            else: out("✏️ <b>Редактирование катушки:</b>\nВыберите катушку из списка:", build_spool_picker("sp_edit_", "btn_spools", 0))
        elif data.startswith("sp_edit_"):
            sid = int(data.split("_").pop()); edit_spool_id = sid; awaiting_spool = False; tg_answer_cb(cb_id)
            sp = next((s for s in (load_state().get("spools") or []) if s.get("id") == sid), None)
            if sp: out("✏️ <b>Редактирование %s</b> (сейчас %.0f г):\nОтправьте <b>новый вес</b> (например: <b>650</b>) или <b>новое название</b> (например: <b>PLA Темно-зеленый 850</b>):" % (esc(sp_title(sp)), sp["rem_g"]), {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "btn_spools"}]]})
        elif data == "sp_ov_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            slots, _, _, _ = get_cfs_slots()
            rows = [[{"text": "Слот %s (%s)" % (let, hex_to_ru_name(slots[let]["hex"])), "callback_data": "sp_ovslot_%s" % let}] for let in ("A", "B", "C", "D") if let in slots]
            rows.append([{"text": "⬅️ Назад", "callback_data": "btn_spools"}])
            out("🔄 <b>Уточнение слота CFS:</b>", {"inline_keyboard": rows})
        elif data.startswith("sp_ovslot_"):
            let = data.split("_").pop(); tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: t, kb = build_spools_view(); out(t, kb)
            else:
                slots, _, _, _ = get_cfs_slots()
                p_mat = slots.get(let, {}).get("mat") if let in slots else None
                title_ov = "📥 <b>Выберите текущую катушку:</b>" if let == "SINGLE" else ("📥 <b>Выберите бобину для Слота %s:</b>" % let)
                out(title_ov, build_spool_picker("sp_ovset_%s_" % let, "btn_spools" if let == "SINGLE" else "sp_ov_menu", 0, p_mat))
        elif data.startswith("sp_ovset_"):
            nums = [p for p in data.split("_") if p.isdigit()]
            let = data.split("_")[2]
            sid = int(nums.pop())
            st = load_state()
            if let == "SINGLE":
                st["single_spool_id"] = sid
                save_state(st)
                tg_answer_cb(cb_id, "✅ Катушка выбрана")
            else:
                slots, _, _, _ = get_cfs_slots()
                if let in slots:
                    ov = st.get("slot_overrides") or {}
                    ov[let] = {"id": sid, "hex": slots[let]["hex"], "mat": slots[let]["mat"]}
                    st["slot_overrides"] = ov; save_state(st)
                    tg_answer_cb(cb_id, "✅ Слот %s уточнен" % let)
                t, kb = build_spools_view(); out(t, kb)
        elif data == "sp_del_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: t, kb = build_spools_view(); out(t, kb)
            else: out("🗑 <b>Удаление катушки:</b>\nВыберите катушку из списка:", build_spool_picker("sp_askdel_", "btn_spools", 0))
        elif data.startswith("sp_askdel_"):
            sid = int(data.split("_").pop()); tg_answer_cb(cb_id)
            sp = next((s for s in (load_state().get("spools") or []) if s.get("id") == sid), None)
            if sp: out("⚠️ <b>Вы уверены, что хотите удалить катушку?</b>\n• <b>%s</b> (%.0f г)" % (esc(sp_title(sp)), sp["rem_g"]), {"inline_keyboard": [[{"text": "✅ Да, удалить", "callback_data": "sp_confdel_%d" % sid}, {"text": "❌ Отмена", "callback_data": "btn_spools"}]]})
        elif data.startswith("sp_confdel_"):
            sid = int(data.split("_").pop()); st = load_state()
            st["spools"] = [s for s in (st.get("spools") or []) if s.get("id") != sid]
            ov = st.get("slot_overrides") or {}
            for k in [k for k, v in ov.items() if v.get("id") == sid]:
                del ov[k]
            st["slot_overrides"] = ov
            save_state(st); tg_answer_cb(cb_id, "🗑 Удалено"); t, kb = build_spools_view(); out(t, kb)
        elif data == "btn_cons": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(format_consumables(), MAIN_KB)
        elif data == "btn_maint": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out("🛠 <b>Обслуживание и смазка:</b>", MAINT_KB)
        elif data == "btn_resets": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out("🔄 <b>Сброс счетчиков:</b>\n" + format_consumables(), RESET_KB)
        elif data == "btn_back": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out("🤖 <b>Панель управления принтером:</b>", MAIN_KB)
        elif data.startswith("rst_"): r_txt = do_reset(data[4:]); tg_answer_cb(cb_id, r_txt); out("🔄 <b>Сброс счетчиков:</b>\n" + format_consumables(), RESET_KB)
        elif data.startswith("move_"): axis = data[5:]; tg_answer_cb(cb_id, "⏳ Прогон оси %s..." % axis.upper()); threading.Thread(target=_lube_worker, args=(axis,), daemon=True).start()
        return
    msg = item.get("message", {})
    if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID): return
    raw_txt = (msg.get("text") or "").strip(); txt = raw_txt.lower()
    if edit_spool_id is not None and not txt.startswith("/"):
        st = load_state()
        sp = next((s for s in (st.get("spools") or []) if s.get("id") == edit_spool_id), None)
        if sp:
            parts = raw_txt.split()
            try:
                new_g = round(max(0.0, float(raw_txt.replace(",", "."))), 1)
                sp["rem_g"] = new_g
            except Exception:
                try:
                    sp["rem_g"] = round(max(0.0, float(parts[-1].replace(",", "."))), 1)
                    parts.pop(-1)
                except Exception:
                    pass
            if parts and parts[:1] and parts[:1].pop().upper() in ("PLA", "PETG", "TPU", "ABS", "ASA", "PA-CF", "PC", "NYLON", "HIPS"):
                sp["mat"] = parts.pop(0).upper()
                new_name = " ".join(parts).strip()
                if new_name:
                    sp["name"] = new_name[:1].upper() + new_name[1:]
                    sp["group"] = normalize_color_group(sp["name"])
            save_state(st); edit_spool_id = None; t, kb = build_spools_view()
            tg_send("✅ Катушка <b>%s</b> (%.0f г) обновлена!\n" % (esc(sp_title(sp)), sp["rem_g"]) + t, kb)
            return
    if awaiting_spool and not txt.startswith("/"):
        parts = raw_txt.split()
        if len(parts) >= 3:
            try:
                mat = parts.pop(0).upper(); grams = float(parts.pop(-1).replace(",", "."))
                if grams <= 0 or grams > 10000: raise ValueError("bad grams")
                raw_name = " ".join(parts).strip()
                cname = raw_name[:1].upper() + raw_name[1:] if raw_name else "Неизвестный"
                grp = normalize_color_group(cname); st = load_state(); spools = st.get("spools") or []
                nid = max([s.get("id", 0) for s in spools], default=0) + 1
                new_sp = {"id": nid, "mat": mat, "name": cname, "group": grp, "hex": "", "rem_g": grams}
                spools.append(new_sp); st["spools"] = spools; save_state(st); awaiting_spool = False; t, kb = build_spools_view()
                tg_send("✅ Катушка <b>%s</b> (%.0f г) добавлена!\n" % (esc(sp_title(new_sp)), grams) + t, kb); return
            except Exception: pass
        tg_send("⚠️ Формат: <b>PLA Черный матовый 850</b>", {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "btn_spools"}]]}); return
    if txt.startswith("/stats"): tg_send(format_stats(), MAIN_KB)
    elif txt.startswith("/consumables"): tg_send(format_consumables(), MAIN_KB)
    elif txt.startswith("/status"): send_usb_photo(format_status(), MAIN_KB)
    elif txt.startswith("/spools"): t, kb = build_spools_view(); tg_send(t, kb)
    elif txt.startswith(("/start", "/help")): awaiting_spool = False; edit_spool_id = None; tg_send("🤖 <b>Панель управления принтером:</b>", MAIN_KB)

# ==================== 12. ВЕБ-СЕРВЕР MINI APP, ТУННЕЛЬ И OTA =======
WEBAPP_FILE = os.path.join(BASE_DIR, "webapp.html")
CLOUDFLARED_BIN = os.path.join(BASE_DIR, "cloudflared")
PUBLIC_WEBAPP_URL = ""
WEBAPP_SECRET = uuid.uuid4().hex
_hist_cache = {"ts": 0.0, "totals": {}, "h7": 0.0, "h30": 0.0, "ok": 0, "hlen": 0, "d_lbl": [], "d_hrs": []}

def _get_cached_stats():
    if time.time() - _hist_cache["ts"] < 60 and _hist_cache["totals"]:
        return _hist_cache
    totals = get_history_totals()
    history = get_history_list(300)
    h_7d, h_30d, _, _, _, ok_all, _ = analyze_history(history)
    tz = int(cfg("timezone_offset"))
    now_local = datetime.now(timezone.utc) + timedelta(hours=tz)
    days_labels, days_hours = [], []
    day_names = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
    # FIX: используем offset-aware epoch для корректного вычитания
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for shift in range(6, -1, -1):
        d_dt = now_local - timedelta(days=shift)
        days_labels.append("%s %02d" % (day_names[d_dt.weekday()], d_dt.day))
        d_start = d_dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=tz)
        ts0 = (d_start - epoch).total_seconds()
        sec_sum = sum((j.get("print_duration") or 0) for j in history if ts0 <= (j.get("start_time") or 0) < ts0 + 86400)
        days_hours.append(round(sec_sum / 3600.0, 2))
    _hist_cache.update({"ts": time.time(), "totals": totals, "h7": h_7d, "h30": h_30d, "ok": ok_all, "hlen": len(history), "d_lbl": days_labels, "d_hrs": days_hours})
    return _hist_cache

def verify_tg_init_data(init_data):
    if not init_data: return False
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recv_hash = parsed.pop("hash", "")
        if not recv_hash: return False
        data_check = "\n".join("%s=%s" % (k, v) for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, recv_hash): return False
        if "user" in parsed and parsed["user"]:
            user_obj = json.loads(parsed["user"])
            return str(user_obj.get("id", "")) == str(CHAT_ID)
        return True
    except Exception:
        return False

def build_webapp_payload():
    st = load_state()
    ps_all = get_print_stats()
    ps = ps_all.get("print_stats", {})
    ds = ps_all.get("display_status", {})
    info = ps.get("info") or {}
    cs = _get_cached_stats()
    totals = cs["totals"]
    h_7d, h_30d, ok_all, hlen = cs["h7"], cs["h30"], cs["ok"], cs["hlen"]
    days_labels, days_hours = cs["d_lbl"], cs["d_hrs"]
    slots_out = []
    cfs_temp, cfs_hum, active_let = "?", "?", "SINGLE"
    if HAS_CFS:
        slots, active_let, cfs_temp, cfs_hum = get_cfs_slots()
        for let in ("A", "B", "C", "D"):
            if let in slots:
                s_i = slots[let]
                sp = resolve_slot_spool(st, let, s_i["mat"], s_i["hex"], auto_create=False)
                slots_out.append({
                    "slot": let, "active": (let == active_let), "hex": "#" + s_i["hex"],
                    "mat": s_i["mat"], "color_name": hex_to_ru_name(s_i["hex"]),
                    "spool_id": sp["id"] if sp else None,
                    "spool_title": sp_title(sp) if sp else "Не назначена",
                    "rem_g": sp["rem_g"] if sp else 0
                })
    h_tot = totals.get("total_time", 0) / 3600.0
    m_tot = totals.get("total_filament_used", 0) / 1000.0
    mats_m = st.get("materials_meters") or {}
    if mats_m:
        fil_rub = 0.0
        for m_type, meters in mats_m.items():
            dens = DENSITY_G_PER_M.get(m_type, G_PER_M_DEFAULT)
            fil_rub += ((meters * dens) / 1000.0) * cfg_price(m_type)
        tracked_m = sum(mats_m.values())
        if m_tot > tracked_m:
            fil_rub += (((m_tot - tracked_m) * G_PER_M_DEFAULT) / 1000.0) * float(cfg("price_default"))
    else:
        fil_rub = ((m_tot * G_PER_M_DEFAULT) / 1000.0) * float(cfg("price_default"))
    elec = h_tot * float(cfg("power_kw")) * float(cfg("elec_price"))
    direct = fil_rub + elec
    depr = h_tot * float(cfg("depr_hour"))
    ext = ps_all.get("extruder") or {}
    bed = ps_all.get("heater_bed") or {}
    return {
        "version": VERSION,
        "has_cfs": HAS_CFS, "cfs_temp": cfs_temp, "cfs_hum": cfs_hum, "slots": slots_out,
        "single_spool_id": st.get("single_spool_id"),
        "spools": sorted([s for s in (st.get("spools") or []) if s.get("rem_g", 0) > 0], key=lambda x: x.get("id", 0)),
        "settings": {
            "spool_tracking": bool(cfg("spool_tracking")),
            "timezone_offset": cfg("timezone_offset"),
            "digest_hour": cfg("digest_hour"),
            "maint_hours": cfg("maint_hours"),
            "nozzle_meters": cfg("nozzle_meters"),
            "filter_hours": cfg("filter_hours"),
            "ptfe_meters": cfg("ptfe_meters"),
            "cutter_cuts": cfg("cutter_cuts"),
            "cfs_humidity": cfg("cfs_humidity"),
            "elec_price": cfg("elec_price"),
            "power_kw": cfg("power_kw"),
            "depr_hour": cfg("depr_hour"),
            "prices": cfg("prices")
        },
        "printer": {
            "state": ps.get("state", "unknown"), "filename": ps.get("filename", ""),
            "progress": round((ds.get("progress", 0) or 0) * 100, 1),
            "duration": ps.get("print_duration", 0) or 0,
            "layer": info.get("current_layer"), "total_layer": info.get("total_layer"),
            "nozzle_t": round(ext.get("temperature", 0) or 0, 1),
            "nozzle_target": round(ext.get("target", 0) or 0),
            "bed_t": round(bed.get("temperature", 0) or 0, 1),
            "bed_target": round(bed.get("target", 0) or 0)
        },
        "consumables": {
            "maint_left_h": round(max(0.0, (st.get("last_maint_hours", 0) + float(cfg("maint_hours"))) - h_tot), 1),
            "maint_max_h": float(cfg("maint_hours")),
            "nozzle_m": round(max(0.0, m_tot - st.get("nozzle_start_meters", m_tot)), 1), "nozzle_max": float(cfg("nozzle_meters")),
            "filter_h": round(max(0.0, h_tot - st.get("filter_start_hours", h_tot)), 1), "filter_max": float(cfg("filter_hours")),
            "ptfe_m": round(max(0.0, m_tot - st.get("ptfe_start_meters", m_tot)), 1), "ptfe_max": float(cfg("ptfe_meters")),
            "cutter": st.get("cutter_cuts", 0), "cutter_max": int(cfg("cutter_cuts"))
        },
        "stats": {
            "hours": round(h_tot, 1), "meters": round(m_tot, 1), "spools_cnt": round(m_tot / M_PER_SPOOL, 1),
            "jobs": totals.get("total_jobs", 0),
            "reliability": round((ok_all * 100.0 / hlen), 1) if hlen else 100.0,
            "h_7d": round(h_7d, 1), "h_30d": round(h_30d, 1),
            "fil_rub": round(fil_rub), "elec_rub": round(elec),
            "direct_rub": round(direct), "full_rub": round(direct + depr),
            "waste_g": round(st.get("waste_g", 0)), "waste_rub": round(st.get("waste_rub", 0)),
            "materials": mats_m,
            "days_labels": days_labels, "days_hours": days_hours
        }
    }

# --- OTA Update Logic ---
def check_for_updates():
    try:
        url = "%s/printer_tg_bot.py?v=%s" % (GITHUB_RAW_BASE, VERSION)
        with urllib.request.urlopen(url, timeout=15) as r:
            new_code = r.read().decode("utf-8")
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', new_code, re.MULTILINE)
        if not m:
            return None
        new_version = m.group(1)
        # FIX: сравниваем версии как кортежи чисел, чтобы не обновляться "вниз"
        def parse_version(v):
            try:
                return [int(x) for x in v.lstrip("v").split(".")]
            except Exception:
                return [0]
        current = parse_version(VERSION)
        new = parse_version(new_version)
        if new > current:
            log.info("Доступно обновление: %s -> %s", VERSION, new_version)
            return {"version": new_version, "code": new_code, "url": url}
        else:
            log.info("GitHub версия %s не выше текущей %s — пропускаем", new_version, VERSION)
            return None
    except Exception as e:
        log.warning("Ошибка проверки обновлений: %s", e)
        return None

def apply_update(update_info):
    try:
        new_path = os.path.join(BASE_DIR, "printer_tg_bot_new.py")
        with open(new_path, "w", encoding="utf-8") as f:
            f.write(update_info["code"])
        py_compile.compile(new_path, doraise=True)
        
        try:
            html_url = "%s/webapp.html?v=%s" % (GITHUB_RAW_BASE, update_info["version"])
            with urllib.request.urlopen(html_url, timeout=15) as r:
                html_code = r.read().decode("utf-8")
            html_path = os.path.join(BASE_DIR, "webapp_new.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html_code)
        except Exception as e:
            log.warning("Не удалось скачать webapp.html: %s", e)
        
        os.replace(new_path, os.path.join(BASE_DIR, "printer_tg_bot.py"))
        if os.path.exists(html_path):
            os.replace(html_path, WEBAPP_FILE)
        
        log.info("Обновление применено: %s -> %s. Перезапуск...", VERSION, update_info["version"])
        tg_send("✅ <b>Обновление применено:</b> v%s → v%s\nБот перезапускается..." % (VERSION, update_info["version"]), MAIN_KB)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log.exception("Ошибка применения обновления")
        tg_send("⚠️ <b>Ошибка обновления:</b> %s" % str(e), MAIN_KB)

class WebAppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass
    
    def _send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
        
    def _check_auth(self, params):
        ref = (self.headers.get("Referer") or "") + (self.headers.get("Origin") or "")
        if WEBAPP_SECRET and params.get("key") == WEBAPP_SECRET: return True
        if "trycloudflare.com" in ref: return True
        return verify_tg_init_data(params.get("initData", ""))
        
def _handle_api(self, path, params):
    try:
        if path == "/api/state":
            self._send_json(200, build_webapp_payload())
        elif path == "/api/spool/delta":
            sid = int(params.get("id", 0))
            delta = float(params.get("delta", 0))
            st = load_state()
            for s in (st.get("spools") or []):
                if s.get("id") == sid:
                    s["rem_g"] = round(max(0.0, s.get("rem_g", 0) + delta), 1)
                    break
            save_state(st)
            self._send_json(200, {"ok": True})
        elif path == "/api/settings/save":
            st = load_state()
            s = st.get("settings") or {}
            if "spool_tracking" in params:
                s["spool_tracking"] = str(params["spool_tracking"]).lower() in ("1", "true", "yes")
            for k, cast in (
                ("timezone_offset", int), ("digest_hour", int), ("maint_hours", float),
                ("nozzle_meters", float), ("filter_hours", float), ("ptfe_meters", float),
                ("cutter_cuts", int), ("cfs_humidity", int), ("elec_price", float),
                ("power_kw", float), ("depr_hour", float)
            ):
                if k in params and str(params[k]) != "":
                    s[k] = cast(params[k])
            if "prices_json" in params and params["prices_json"]:
                try:
                    s["prices"] = json.loads(params["prices_json"])
                except Exception:
                    pass
            st["settings"] = s
            save_state(st)
            self._send_json(200, {"ok": True})
        elif path == "/api/spool/save":
            st = load_state()
            spools = st.get("spools") or []
            sid = params.get("id")
            mat = str(params.get("mat") or "PLA").strip().upper()
            nm = str(params.get("name") or "").strip()
            if nm: nm = nm[:1].upper() + nm[1:]
            grams = round(max(0.0, float(params.get("rem_g", 1000))), 1)
            if sid:
                for s in spools:
                    if s.get("id") == int(sid):
                        s["mat"] = mat
                        if nm:
                            s["name"] = nm
                            s["group"] = normalize_color_group(nm)
                        s["rem_g"] = grams
            else:
                nid = max([s.get("id", 0) for s in spools], default=0) + 1
                spools.append({"id": nid, "mat": mat, "name": nm or "Новый", "group": normalize_color_group(nm), "hex": "", "rem_g": grams})
            st["spools"] = spools
            save_state(st)
            self._send_json(200, {"ok": True})
        elif path == "/api/spool/delete":
            sid = int(params.get("id", 0))
            st = load_state()
            st["spools"] = [s for s in (st.get("spools") or []) if s.get("id") != sid]
            ov = st.get("slot_overrides") or {}
            for k in [k for k, v in ov.items() if v.get("id") == sid]: del ov[k]
            st["slot_overrides"] = ov
            save_state(st)
            self._send_json(200, {"ok": True})
        elif path == "/api/slot/assign":
            let = str(params.get("slot") or "")
            sid = int(params.get("spool_id", 0))
            st = load_state()
            if let == "SINGLE":
                st["single_spool_id"] = sid
            else:
                slots, _, _, _ = get_cfs_slots()
                if let in slots:
                    ov = st.get("slot_overrides") or {}
                    ov[let] = {"id": sid, "hex": slots[let]["hex"], "mat": slots[let]["mat"]}
                    st["slot_overrides"] = ov
            save_state(st)
            self._send_json(200, {"ok": True})
        elif path == "/api/reset":
            do_reset(str(params.get("target") or ""))
            self._send_json(200, {"ok": True})
        elif path == "/api/move":
            ax = str(params.get("axis") or "")
            if ax in ("x", "y", "z"):
                threading.Thread(target=_lube_worker, args=(ax,), daemon=True).start()
            self._send_json(200, {"ok": True})
        elif path == "/api/printer/cmd":
            cmd = str(params.get("cmd") or "")
            ep = {"pause": "/printer/print/pause", "resume": "/printer/print/resume", "cancel": "/printer/print/cancel"}.get(cmd)
            if ep:
                try:
                    urllib.request.urlopen(MOONRAKER + ep, data=b"", timeout=5).close()
                except Exception:
                    pass
            self._send_json(200, {"ok": True})
        elif path == "/api/light/toggle":
            st = load_state()
            cur_on = bool(st.get("light_on", False))
            new_on = not cur_on
            st["light_on"] = new_on
            save_state(st)
            val = 1 if new_on else 0
            objs = moonraker_get("/printer/objects/list", 4).get("objects") or []
            cmds = []
            for o in objs:
                ol = o.lower()
                if (o.startswith("output_pin ") or o.startswith("led ")) and any(k in ol for k in ("light", "led", "lamp", "caselight")):
                    pin_name = o.split(" ", 1).pop()
                    if o.startswith("output_pin "):
                        cmds.append("SET_PIN PIN=%s VALUE=%d" % (pin_name, val))
                    else:
                        cmds.append("SET_LED LED=%s WHITE=%d RED=%d GREEN=%d BLUE=%d" % (pin_name, val, val, val, val))
            if cmds:
                try:
                    urllib.request.urlopen(MOONRAKER + "/printer/gcode/script", data=urllib.parse.urlencode({"script": "\n".join(cmds)}).encode(), timeout=5).close()
                except Exception:
                    pass
            self._send_json(200, {"ok": True, "light_on": new_on})
        elif path == "/api/update/check":
            update = check_for_updates()
            if update:
                self._send_json(200, {"ok": True, "has_update": True, "new_version": update["version"]})
            else:
                self._send_json(200, {"ok": True, "has_update": False, "current_version": VERSION})
        elif path == "/api/update/apply":
            update = check_for_updates()
            if update:
                threading.Thread(target=apply_update, args=(update,), daemon=True).start()
                self._send_json(200, {"ok": True, "message": "Updating to v%s..." % update["version"]})
            else:
                self._send_json(200, {"ok": True, "message": "Already up to date"})
        else:
            self._send_json(404, {"error": "unknown endpoint"})
    except Exception as e:
        log.exception("API Error on %s: %s", path, e)
        self._send_json(500, {"error": str(e)})

    def do_GET(self):
        global CAM_SNAPSHOT_URL, last_cam_fail_ts
        parts = self.path.split("?", 1); path = parts.pop(0); qs = dict(urllib.parse.parse_qsl(parts.pop(0))) if parts else {}
        if path in ("/", "/index.html"):
            if os.path.isfile(WEBAPP_FILE):
                with open(WEBAPP_FILE, "rb") as f: html = f.read()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(html))); self.end_headers(); self.wfile.write(html)
            else: self._send_json(404, {"error": "webapp.html not found"})
        elif path == "/api/cam.jpg":
            img = None
            if not CAM_SNAPSHOT_URL and time.time() - last_cam_fail_ts > 60: detect_environment()
            if CAM_SNAPSHOT_URL:
                try:
                    with urllib.request.urlopen(CAM_SNAPSHOT_URL, timeout=5) as r:
                        cand = r.read()
                        if len(cand) > 200: img = cand
                except Exception as e:
                    log.warning("WebApp cam err: %s", e); CAM_SNAPSHOT_URL = ""; last_cam_fail_ts = time.time()
            if img:
                self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(img))); self.end_headers(); self.wfile.write(img)
            else: self.send_response(503); self.end_headers()
        elif path.startswith("/api/"):
            if not self._check_auth(qs): self._send_json(403, {"error": "unauthorized"}); return
            self._handle_api(path, qs)
        else: self.send_response(404); self.end_headers()

    def do_POST(self):
        path = self.path.split("?", 1).pop(0)
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln > 0 else {}
        except Exception:
            self._send_json(400, {"error": "bad json"}); return
        if not self._check_auth(body): self._send_json(403, {"error": "unauthorized"}); return
        self._handle_api(path, body)

def _tunnel_worker():
    global PUBLIC_WEBAPP_URL
    if not USE_CLOUDFLARED:
        log.info("Cloudflared отключен в config.ini")
        return
    if not os.path.isfile(CLOUDFLARED_BIN):
        log.warning("cloudflared не найден: %s", CLOUDFLARED_BIN)
        return
    while True:
        try:
            proc = subprocess.Popen([CLOUDFLARED_BIN, "tunnel", "--url", "http://127.0.0.1:%d" % WEBAPP_PORT, "--protocol", "http2", "--no-autoupdate"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for raw_line in iter(proc.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="ignore")
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    url = m.group(0)
                    if url != PUBLIC_WEBAPP_URL:
                        PUBLIC_WEBAPP_URL = url
                        log.info("Mini App URL: %s", url)
                        res = tg_api("setChatMenuButton", {
                            "chat_id": CHAT_ID,
                            "menu_button": json.dumps({"type": "web_app", "text": "📱 Панель", "web_app": {"url": "%s/?key=%s" % (url, WEBAPP_SECRET)}})
                        })
                        log.info("setChatMenuButton: %s", res)
            proc.wait()
        except Exception as e:
            log.warning("Tunnel err: %s", e)
            time.sleep(10)

def start_webapp_services():
    try:
        if USE_CLOUDFLARED: os.system("killall cloudflared 2>/dev/null")
        srv = ThreadingHTTPServer(("127.0.0.1", WEBAPP_PORT), WebAppHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if USE_CLOUDFLARED: threading.Thread(target=_tunnel_worker, daemon=True).start()
        log.info("WebApp сервер запущен на порту %d", WEBAPP_PORT)
    except Exception as e:
        log.warning("WebApp start err: %s", e)

# ==================== 13. ОСНОВНОЙ ЦИКЛ МОНИТОРИНГА =================
detect_environment()
start_webapp_services()

def migrate_state(st):
    changed = False
    if "spools" not in st: st["spools"] = []; changed = True
    for sp in st["spools"]:
        if "mat" not in sp: sp["mat"] = "PLA"; changed = True
        if "group" not in sp: sp["group"] = normalize_color_group(sp.get("name") or sp.get("hex") or ""); changed = True
        if "rem_g" not in sp: sp["rem_g"] = 0.0; changed = True
    return changed

_st = load_state()
if migrate_state(_st):
    save_state(_st)
    log.info("Состояние мигрировано на новую схему")

mode_str = "CFS (4 слота)" if HAS_CFS else "Одиночная катушка"
tg_send("✅ <b>Мониторинг принтера запущен (v%s)</b>\nРежим: <b>%s</b>" % (VERSION, mode_str), MAIN_KB)

# КРИТИЧЕСКИЙ ФИКС: Опрос Telegram вынесен в отдельный поток, чтобы не блокировать основной цикл и WebApp
threading.Thread(target=check_commands, daemon=True).start()

while True:
    try:
        check_weekly_digest()
        check_cfs_humidity()
        st = get_print_stats()
        if not st:
            time.sleep(POLL_INTERVAL); continue
        ps = st.get("print_stats", {})
        state, fname, msg = ps.get("state"), ps.get("filename", ""), ps.get("message", "")
        used_mm, print_dur = ps.get("filament_used", 0) or 0, ps.get("print_duration", 0) or 0
        
        if last_state is None and state in ("printing", "paused"):
            last_state = state; last_fname = fname
            time.sleep(POLL_INTERVAL); continue
            
        if (state == "printing" and last_state == "printing" and fname and last_fname and fname != last_fname):
            log.warning("Пропущенное завершение: %s -> %s", last_fname, fname)
            handle_missed_job_end(last_fname)
            last_state = None
            
        if state == "printing" and last_state not in ("printing", "paused"):
            if TIMELAPSE_DIR and os.path.isdir(TIMELAPSE_DIR):
                print_start_files = set(f for f in os.listdir(TIMELAPSE_DIR) if f.endswith(".mp4"))
            meta = get_file_meta(fname); est = meta.get("estimated_time", 0) or 0
            w, direct, full, elec, depr = calc_costs(meta, est); flush_g = get_flush_weight(meta)
            lines = ["🖨 <b>Начало печати:</b>", "• Файл: <b>%s</b>" % esc(fname)]
            if est > 0:
                end_dt = (datetime.now(timezone.utc) + timedelta(hours=int(cfg("timezone_offset")), seconds=est)).strftime("%d.%m.%Y %H:%M")
                lines.extend(["• Длительность: <b>~%s</b>" % format_dur(est), "• Окончание: <b>~%s</b>" % end_dt])
            if w > 0:
                lines.append("• Расход пластика: <b>~%.1f г</b>%s" % (w, " (промывка CFS: ~%.1f г)" % flush_g if flush_g > 0.1 else ""))
            extra_btns = []
            if cfg("spool_tracking"):
                slots, active_let, _, _ = get_cfs_slots(); used_items = get_used_slots(fname, meta, slots, active_let, w); st_now = load_state()
                for let, part_g in used_items:
                    s_obj = slots.get(let) or {"mat": "PLA", "hex": "000000"}
                    sp = resolve_slot_spool(st_now, let, s_obj["mat"], s_obj["hex"], auto_create=HAS_CFS)
                    if sp:
                        if let == "SINGLE":
                            lines.append("• Катушка: <b>%s</b> (в наличии %.0f г)" % (esc(sp_title(sp)), sp["rem_g"]))
                            extra_btns.append([{"text": "🔄 Сменить катушку", "callback_data": "sp_ovslot_SINGLE"}])
                        else:
                            w_part_str = "нужно ~%.1f г | " % part_g if len(used_items) > 1 and part_g > 0 else ""
                            lines.append("• Слот %s: <b>%s</b> (%sв наличии %.0f г)" % (let, esc(sp_title(sp)), w_part_str, sp["rem_g"]))
                            extra_btns.append([{"text": "🔄 Сменить (Слот %s)" % let, "callback_data": "sp_ovslot_%s" % let}])
                        if part_g > 0 and sp["rem_g"] < part_g:
                            lines.append("⚠️ <i>Внимание: не хватит ~%.0f г до конца детали!</i>" % (part_g - sp["rem_g"]))
            if direct > 0:
                lines.extend(["", "💰 <b>Расчетная стоимость:</b>",
                    "• Прямые (пластик+свет): <b>~%.1f руб.</b> (свет ~%.1f руб.)" % (direct, elec),
                    "• С амортизацией: <b>~%.1f руб.</b> (аморт. ~%.1f руб.)" % (full, depr)])
            start_kb = {"inline_keyboard": MAIN_KB["inline_keyboard"] + extra_btns} if extra_btns else MAIN_KB
            send_thumbnail(meta, "\n".join(lines), start_kb)
            
        elif state == "paused" and last_state != "paused":
            p_msg = "⚠️ <b>Кончился филамент!</b>\nПечать на паузе.\n• Файл: <b>%s</b>" % esc(fname) if ("filament" in msg.lower() or "out of" in msg.lower()) else "⏸ <b>Печать на паузе</b>\n• Причина: %s\n• Файл: <b>%s</b>" % (esc(msg or "не указана"), esc(fname))
            send_usb_photo(p_msg, MAIN_KB)
            
        elif state == "complete" and last_state != "complete":
            process_completed_job(fname, get_file_meta(fname), print_dur, used_mm, send_video=True)
            
        elif (state == "error" and last_state != "error") or (state in ("standby", "cancelled") and last_state in ("printing", "paused")):
            meta = get_file_meta(fname); record_material_usage(meta, used_mm, fname)
            w, direct, full, _, depr = calc_costs(meta, print_dur, used_mm); record_waste(w, direct)
            spool_notes = deduct_spool_weight(fname, meta, w)
            lines = ["❌ <b>Ошибка печати:</b>" if state == "error" else "⏹ <b>Печать остановлена/отменена:</b>"]
            if state == "error" and msg: lines.append("• Причина: %s" % esc(msg))
            lines.append("• Файл: <b>%s</b>" % esc(fname))
            if w > 0.5 or direct > 0.5:
                lines.extend(["", "🗑 <b>Потери на брак:</b>",
                    "• Пластик: <b>%.1f г</b>" % w,
                    "• Прямые затраты: <b>~%.1f руб.</b> (с аморт. ~%.1f руб.)" % (direct, full)])
            if spool_notes: lines.extend(["", spool_notes])
            out_txt = "\n".join(lines)
            if state == "error": send_usb_photo(out_txt, MAIN_KB)
            else: tg_send(out_txt, MAIN_KB)
            update_counters(1 if (HAS_CFS and used_mm > 0) else 0)
            
        last_state = state; last_fname = fname
        
    except Exception:
        log.exception("Loop error")
    
    # КРИТИЧЕСКИЙ ФИКС: Пауза всегда в конце цикла, предотвращает 100% загрузку CPU
    time.sleep(POLL_INTERVAL)