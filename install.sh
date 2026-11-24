#!/bin/sh
set -e
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

dl() {
    python3 -c "import urllib.request, time
for i in range(5):
    try:
        urllib.request.urlretrieve('$1', '$2')
        break
    except Exception:
        if i == 4: raise
        time.sleep(2)"
}

echo "=========================================="
echo "  Installing Telegram Bot & Mini App"
echo "  for Klipper / Moonraker / Creality CFS"
echo "=========================================="

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

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

if [ ! -f secrets.ini ]; then
    echo ""
    printf "Enter your Telegram Bot TOKEN from @BotFather: "
    read BOT_TOKEN
    printf "Enter your Telegram CHAT_ID: "
    read BOT_CHAT_ID
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
        CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
        echo "-> Downloading cloudflared ($CF_ARCH)..."
        dl "$CF_URL" "cloudflared"
        chmod +x cloudflared
    fi
fi

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
