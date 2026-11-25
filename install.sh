#!/bin/sh
set -e

# Автодобавление DNS если resolv.conf доступен и в нём нет 8.8.8.8
if [ -w /etc/resolv.conf ] && ! grep -q "8.8.8.8" /etc/resolv.conf 2>/dev/null; then
  printf "\nnameserver 8.8.8.8\nnameserver 1.1.1.1\n" >> /etc/resolv.conf
fi

REPO_RAW="https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main"
INSTALL_DIR="/root/printer_tg_bot"
BACKUP_FILE="/root/printer_tg_bot_backup.json"

if [ ! -w "/root" ]; then
  INSTALL_DIR="$HOME/printer_tg_bot"
  BACKUP_FILE="$HOME/printer_tg_bot_backup.json"
fi

# Функция скачивания с fallback на jsDelivr и докачкой по Range
dl() {
  python3 -c "import urllib.request, time, sys, os
u1 = '$1'
# Если это GitHub — добавляем jsDelivr как fallback
if 'github.com' in u1 and 'cdn.jsdelivr.net' not in u1 and 'raw.githubusercontent.com' in u1:
    u2 = u1.replace('https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main', 'https://cdn.jsdelivr.net/gh/ScrooLL19/klipper-telegram-bot@main/')
elif 'github.com' in u1 and 'cdn.jsdelivr.net' not in u1:
    u2 = u1.replace('https://github.com/', 'https://cdn.jsdelivr.net/gh/').replace('/releases/latest/download/', '@latest/releases/latest/download/')
else:
    u2 = None
urls = [u1, u2] if u2 else [u1]
dst = '$2'
tmp = dst + '.part'
if os.path.exists(tmp): os.remove(tmp)
for i in range(6):
    u = urls[i % len(urls)]
    pos = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    try:
        req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
        if pos > 0: req.add_header('Range', 'bytes=%d-' % pos)
        with urllib.request.urlopen(req, timeout=45) as r:
            code = getattr(r, 'status', 200)
            if code != 206: pos = 0
            rem = int(r.headers.get('Content-Length') or 0)
            tot = pos + rem
            mode = 'ab' if (pos > 0 and code == 206) else 'wb'
            with open(tmp, mode) as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk: break
                    f.write(chunk)
                    pos += len(chunk)
            if tot > 0:
                pct = min(100, int(pos * 100 / tot))
                sys.stdout.write('\r   [%-20s] %3d%% (%.1f / %.1f MB) from %s' % ('#' * (pct // 5), pct, pos / 1048576.0, tot / 1048576.0, u.split('/')[-1]))
                sys.stdout.flush()
                sys.stdout.write('\n')
            os.replace(tmp, dst)
            print('   Downloaded OK from:', u)
            break
    except Exception as e:
        sys.stdout.write('\n')
        if i == 5:
            if os.path.exists(tmp): os.remove(tmp)
            raise
        print('   Resuming download (attempt %d/6, %s)...' % (i + 2, e))
        time.sleep(3)"
}

echo "=========================================="
echo "  Installing Telegram Bot & Mini App"
echo "  for Klipper / Moonraker / Creality CFS"
echo "=========================================="

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# Восстановление бэкапа если есть
if [ -f "$BACKUP_FILE" ] && [ ! -f "$INSTALL_DIR/printer_state.json" ]; then
  echo "-> Found previous backup! Restoring spools and counters..."
  mv "$BACKUP_FILE" "$INSTALL_DIR/printer_state.json"
fi

echo "-> Downloading project files..."
dl "$REPO_RAW/printer_tg_bot.py" "printer_tg_bot.py"
dl "$REPO_RAW/webapp.html" "webapp.html"
dl "$REPO_RAW/uninstall.sh" "uninstall.sh"
tr -d '\r' < uninstall.sh > uninstall.sh.tmp && mv uninstall.sh.tmp uninstall.sh

if [ ! -f config.ini ]; then
  dl "$REPO_RAW/config.ini" "config.ini"
fi

chmod +x printer_tg_bot.py uninstall.sh

# Создание secrets.ini если его нет
if [ ! -f secrets.ini ]; then
  echo ""
  printf "Enter your Telegram Bot TOKEN from @BotFather: "
  read BOT_TOKEN
  BOT_TOKEN=$(printf "%s" "$BOT_TOKEN" | tr -cd '0-9A-Za-z_:-')
  printf "Enter your Telegram CHAT_ID: "
  read BOT_CHAT_ID
  BOT_CHAT_ID=$(printf "%s" "$BOT_CHAT_ID" | tr -cd '0-9-')
  cat << EOF > secrets.ini
[telegram]
token = $BOT_TOKEN
chat_id = $BOT_CHAT_ID
EOF
  chmod 600 secrets.ini
  echo "-> File secrets.ini created."
else
  echo "-> Existing secrets.ini found, keeping it intact."
fi

# Скачивание cloudflared с несколькими зеркалами
echo ""
printf "Install cloudflared for Telegram Mini App? [Y/n]: "
read USE_CF
if [ "$USE_CF" != "n" ] && [ "$USE_CF" != "N" ]; then
  if [ ! -f cloudflared ]; then
    ARCH=$(uname -m)
    CF_ARCH="arm"
    case "$ARCH" in
      aarch64|arm64) CF_ARCH="arm64" ;;
      x86_64|amd64)  CF_ARCH="amd64" ;;
      armv7*|arm*)   CF_ARCH="arm" ;;
    esac
    CF_NAME="cloudflared-linux-$CF_ARCH"
    echo "-> Downloading cloudflared ($CF_ARCH)..."
    # Пробуем несколько зеркал по очереди
    CF_URLS="https://github.com/cloudflare/cloudflared/releases/latest/download/$CF_NAME https://cdn.jsdelivr.net/gh/cloudflare/cloudflared@latest/releases/latest/download/$CF_NAME"
    CF_DOWNLOADED=0
    for CF_URL in $CF_URLS; do
      echo "   Trying: $CF_URL"
      if dl "$CF_URL" "cloudflared"; then
        CF_DOWNLOADED=1
        break
      fi
    done
    if [ "$CF_DOWNLOADED" -eq 0 ]; then
      echo "⚠️ Failed to download cloudflared from all mirrors."
      echo "   You can download it manually and place it in $INSTALL_DIR/cloudflared"
    else
      chmod +x cloudflared
    fi
  fi
fi

# Настройка автозапуска
if [ -d /etc/init.d ] && command -v procd >/dev/null 2>&1 || [ -f /etc/openwrt_release ]; then
  echo "-> Configuring OpenWrt auto-start (/etc/init.d/printer_tg_bot)..."
  cat << EOF > /etc/init.d/printer_tg_bot
#!/bin/sh /etc/rc.common
START=99
STOP=10
USE_PROCD=1
start_service() {
  procd_open_instance
  procd_set_param command /usr/bin/python3 $INSTALL_DIR/printer_tg_bot.py
  procd_set_param respawn 3600 5 0
  procd_close_instance
}
EOF
  tr -d '\r' < /etc/init.d/printer_tg_bot > /tmp/init_b && mv /tmp/init_b /etc/init.d/printer_tg_bot
  chmod +x /etc/init.d/printer_tg_bot
  /etc/init.d/printer_tg_bot enable
  /etc/init.d/printer_tg_bot restart
elif command -v systemctl >/dev/null 2>&1; then
  echo "-> Configuring systemd auto-start..."
  cat << EOF > /etc/systemd/system/printer_tg_bot.service
[Unit]
Description=Klipper Moonraker Telegram Bot & Mini App
After=network-online.target moonraker.service

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/printer_tg_bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable printer_tg_bot
  systemctl restart printer_tg_bot
else
  echo "-> Starting in background..."
  nohup python3 "$INSTALL_DIR/printer_tg_bot.py" >/dev/null 2>&1 &
fi

rm -f /tmp/install.sh
echo ""
echo "✅ Installation complete! Check your Telegram bot."