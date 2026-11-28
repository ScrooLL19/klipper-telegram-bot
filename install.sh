#!/bin/sh
# ============================================================
#  Telegram Bot & Mini App installer
#  Klipper / Moonraker / Creality K2 / CFS
#  Установщик бота и Mini App.
#  Требуется только python3 — curl/wget не используются.
#  Requires python3 only — curl/wget are never used.
# ============================================================
set -e

REPO_RAW="https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main"
REPO_MIRROR="https://cdn.jsdelivr.net/gh/ScrooLL19/klipper-telegram-bot@main"
INSTALL_DIR="/root/printer_tg_bot"
BACKUP_FILE="/root/printer_tg_bot_backup.json"

if [ ! -w "/root" ]; then
  INSTALL_DIR="$HOME/printer_tg_bot"
  BACKUP_FILE="$HOME/printer_tg_bot_backup.json"
fi

say() { printf '%s\n' "$*"; }

say "=========================================="
say "  Telegram Bot & Mini App installer"
say "  Klipper / Moonraker / Creality CFS"
say "=========================================="

# ---------- 1. Проверка Python / Python preflight ----------
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  say "ERROR: python3 не найден / python3 not found"
  say "OpenWrt: opkg update && opkg install python3-light"
  exit 1
fi

if ! "$PY" - <<'PYCHECK'
import sys

missing = []
for mod in ("urllib.request", "hashlib", "hmac", "http.server",
            "logging.handlers", "configparser", "json", "shutil"):
    try:
        __import__(mod)
    except Exception:
        missing.append(mod)
try:
    import hashlib
    hashlib.sha256(b"x").hexdigest()
except Exception:
    missing.append("hashlib.sha256 (python3-openssl)")
if missing:
    sys.stderr.write("MISSING: %s\n" % ", ".join(missing))
    sys.exit(1)
PYCHECK
then
  say "ERROR: не хватает модулей Python / missing Python modules (см. выше)"
  say "OpenWrt: opkg update && opkg install python3-light python3-urllib python3-openssl python3-logging"
  exit 1
fi

# ---------- 2. Сеть и сертификаты / network and CA ----------
NET_ERR="$("$PY" - <<'PYNET' 2>&1
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("https://api.telegram.org", timeout=15).close()
except urllib.error.HTTPError:
    pass
PYNET
)" || true

case "$NET_ERR" in
  *CERTIFICATE_VERIFY_FAILED*)
    say "WARN: нет CA-сертификатов / no CA bundle"
    say "      OpenWrt: opkg update && opkg install ca-bundle"
    ;;
  *Traceback*)
    say "WARN: нет доступа к https://api.telegram.org — проверьте сеть и DNS"
    ;;
esac

# ---------- 3. DNS (по желанию) / optional DNS ----------
if [ -w /etc/resolv.conf ] && ! grep -q "8.8.8.8" /etc/resolv.conf 2>/dev/null; then
  printf "Добавить DNS 8.8.8.8 / 1.1.1.1 в /etc/resolv.conf? [y/N]: "
  ADD_DNS="n"
  if [ -t 0 ]; then read ADD_DNS || ADD_DNS="n"; fi
  case "$ADD_DNS" in
    y|Y)
      cp /etc/resolv.conf /etc/resolv.conf.bak 2>/dev/null || true
      printf "\nnameserver 8.8.8.8\nnameserver 1.1.1.1\n" >> /etc/resolv.conf
      say "-> DNS добавлен, бэкап: /etc/resolv.conf.bak"
      ;;
  esac
fi

# ---------- 4. Каталог и восстановление данных / dir and restore ----------
[ -n "$INSTALL_DIR" ] || INSTALL_DIR="/root/printer_tg_bot"
if ! mkdir -p "$INSTALL_DIR"; then
  say "ERROR: не удалось создать каталог / cannot create $INSTALL_DIR"
  exit 1
fi
if ! cd "$INSTALL_DIR"; then
  say "ERROR: не удалось перейти в каталог / cannot enter $INSTALL_DIR"
  exit 1
fi
# Склеивать пути внутри heredoc нельзя: ошибка в переменной даёт битый
# ExecStart. Собираем один раз и дальше используем только $ENTRY / $FETCH.
# Never concatenate paths inside a heredoc: one bad variable breaks ExecStart.
ENTRY="$INSTALL_DIR/printer_tg_bot.py"
FETCH="$INSTALL_DIR/fetch.py"
say "-> Install dir:  $INSTALL_DIR"
say "-> Entry point:  $ENTRY"

if [ -f "$BACKUP_FILE" ] && [ ! -f "$INSTALL_DIR/printer_state.json" ]; then
  say "-> Found previous backup, restoring spool data..."
  mv "$BACKUP_FILE" "$INSTALL_DIR/printer_state.json"
fi

# ---------- 5. Загрузка файлов на чистом Python / pure-Python download ----------
say "-> Downloading project files (python only)..."

BOOT_OK=0
for BASE in "$REPO_RAW" "$REPO_MIRROR"; do
  if "$PY" -c "
import sys, urllib.request
url, dst = sys.argv[1], sys.argv[2]
req = urllib.request.Request(url, headers={'User-Agent': 'installer'})
data = urllib.request.urlopen(req, timeout=30).read()
if b'def download' not in data:
    raise SystemExit('unexpected fetch.py content')
open(dst, 'wb').write(data)
" "$BASE/fetch.py" "$FETCH"; then
    BOOT_OK=1
    break
  fi
done
if [ "$BOOT_OK" -ne 1 ]; then
  say "ERROR: не удалось скачать fetch.py / cannot download fetch.py"
  exit 1
fi

FETCH_OK=0
for BASE in "$REPO_RAW" "$REPO_MIRROR"; do
  if "$PY" "$FETCH" --manifest "$BASE/manifest.json" "$INSTALL_DIR"; then
    FETCH_OK=1
    break
  fi
done
if [ "$FETCH_OK" -ne 1 ]; then
  say "ERROR: не удалось скачать файлы проекта / cannot download project files"
  exit 1
fi

if [ ! -f uninstall.sh ]; then
  "$PY" "$FETCH" "$REPO_RAW/uninstall.sh" "$INSTALL_DIR/uninstall.sh" || \
    "$PY" "$FETCH" "$REPO_MIRROR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
fi

# config.ini никогда не перезаписываем — в нём настройки пользователя.
# config.ini is never overwritten: it holds user settings.
if [ ! -f config.ini ]; then
  "$PY" "$FETCH" "$REPO_RAW/config.ini" "$INSTALL_DIR/config.ini" || \
    "$PY" "$FETCH" "$REPO_MIRROR/config.ini" "$INSTALL_DIR/config.ini"
fi

chmod +x "$ENTRY" "$FETCH" 2>/dev/null || true
[ -f "$INSTALL_DIR/uninstall.sh" ] && chmod +x "$INSTALL_DIR/uninstall.sh" 2>/dev/null || true

# ---------- 5a. Проверка целостности установки / installation self-check ----------
for need in "$ENTRY" "$INSTALL_DIR/webapp.html"; do
  if [ ! -s "$need" ]; then
    say "ERROR: файл не установлен / missing or empty: $need"
    exit 1
  fi
done
if ! "$PY" -c "import ast, io, sys; ast.parse(io.open(sys.argv[1], encoding='utf-8').read())" "$ENTRY"; then
  say "ERROR: printer_tg_bot.py повреждён / backend file is corrupted"
  exit 1
fi
say "-> Files verified in $INSTALL_DIR"

# ---------- 6. secrets.ini ----------
if [ ! -f secrets.ini ]; then
  BOT_TOKEN="${BOT_TOKEN:-}"
  BOT_CHAT_ID="${BOT_CHAT_ID:-}"
  if [ -z "$BOT_TOKEN" ] && [ -t 0 ]; then
    printf "Enter your Telegram Bot TOKEN from @BotFather: "
    read BOT_TOKEN || BOT_TOKEN=""
  fi
  if [ -z "$BOT_CHAT_ID" ] && [ -t 0 ]; then
    printf "Enter your Telegram CHAT_ID: "
    read BOT_CHAT_ID || BOT_CHAT_ID=""
  fi
  BOT_TOKEN="$("$PY" -c "import re,sys; print(re.sub(r'[^0-9A-Za-z_:-]', '', sys.argv[1]))" "$BOT_TOKEN")"
  BOT_CHAT_ID="$("$PY" -c "import re,sys; print(re.sub(r'[^0-9-]', '', sys.argv[1]))" "$BOT_CHAT_ID")"
  if [ -z "$BOT_TOKEN" ] || [ -z "$BOT_CHAT_ID" ]; then
    say "ERROR: token/chat_id не заданы / token or chat_id is missing"
    say "       BOT_TOKEN=... BOT_CHAT_ID=... sh install.sh"
    exit 1
  fi
  cat > secrets.ini <<EOF
[telegram]
token = $BOT_TOKEN
chat_id = $BOT_CHAT_ID
EOF
  chmod 600 secrets.ini
  say "-> secrets.ini created"
else
  say "-> Existing secrets.ini found, keeping it intact"
fi

# ---------- 7. cloudflared (необязательно) / optional ----------
USE_CF="y"
if [ -t 0 ]; then
  printf "Install cloudflared for Telegram Mini App? [Y/n]: "
  read USE_CF || USE_CF="y"
fi
case "$USE_CF" in
  n|N) say "-> cloudflared skipped" ;;
  *)
    if [ ! -f cloudflared ]; then
      CF_ARCH="$("$PY" -c "import platform; m = platform.machine().lower(); print('arm64' if m in ('aarch64', 'arm64') else 'amd64' if m in ('x86_64', 'amd64') else 'arm')")"
      CF_NAME="cloudflared-linux-$CF_ARCH"
      say "-> Downloading cloudflared ($CF_ARCH)..."
      if "$PY" "$FETCH" "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF_NAME" "$INSTALL_DIR/cloudflared" || \
         "$PY" "$FETCH" "https://cdn.jsdelivr.net/gh/cloudflare/cloudflared@latest/releases/latest/download/$CF_NAME" "$INSTALL_DIR/cloudflared"; then
        chmod +x cloudflared
      else
        say "WARN: cloudflared не скачан — Mini App будет доступен только в локальной сети"
      fi
    fi
    ;;
esac

# ---------- 8. Автозапуск / autostart ----------
if [ -f /etc/openwrt_release ] || command -v procd >/dev/null 2>&1; then
  say "-> Configuring OpenWrt auto-start (/etc/init.d/printer_tg_bot)..."
  cat > /etc/init.d/printer_tg_bot <<EOF
#!/bin/sh /etc/rc.common
START=99
STOP=10
USE_PROCD=1
start_service() {
  procd_open_instance
  procd_set_param command "$PY" "$ENTRY"
  procd_set_param respawn 3600 5 0
  procd_set_param stdout 1
  procd_set_param stderr 1
  procd_close_instance
}
EOF
  chmod +x /etc/init.d/printer_tg_bot
  say "-> Service command: $PY $ENTRY"
  /etc/init.d/printer_tg_bot enable
  /etc/init.d/printer_tg_bot restart
elif command -v systemctl >/dev/null 2>&1; then
  say "-> Configuring systemd auto-start..."
  cat > /etc/systemd/system/printer_tg_bot.service <<EOF
[Unit]
Description=Klipper Moonraker Telegram Bot & Mini App
After=network-online.target moonraker.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$PY "$ENTRY"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  say "-> Service command: $PY $ENTRY"
  systemctl daemon-reload
  systemctl enable printer_tg_bot
  systemctl restart printer_tg_bot
else
  say "-> Starting in background..."
  nohup "$PY" "$ENTRY" >/dev/null 2>&1 &
fi

rm -f /tmp/install.sh
say ""
say "Installation complete. Check your Telegram bot."
