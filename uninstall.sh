#!/bin/sh
echo "=========================================="
echo "  Uninstalling Telegram Bot & Mini App"
echo "=========================================="

INSTALL_DIR="/root/printer_tg_bot"
BACKUP_FILE="/root/printer_tg_bot_backup.json"
if [ ! -d "$INSTALL_DIR" ]; then
    INSTALL_DIR="$HOME/printer_tg_bot"
    BACKUP_FILE="$HOME/printer_tg_bot_backup.json"
fi

if [ -f "$INSTALL_DIR/printer_state.json" ]; then
    printf "Keep backup of spools, counters and settings in %s? [y/N]: " "$BACKUP_FILE"
    read KEEP_DATA
    if [ "$KEEP_DATA" = "y" ] || [ "$KEEP_DATA" = "Y" ]; then
        cp "$INSTALL_DIR/printer_state.json" "$BACKUP_FILE"
        echo "-> Backup saved to $BACKUP_FILE"
    fi
fi

if [ -f /etc/init.d/printer_tg_bot ]; then
    echo "-> Stopping OpenWrt service..."
    /etc/init.d/printer_tg_bot stop 2>/dev/null || true
    /etc/init.d/printer_tg_bot disable 2>/dev/null || true
    rm -f /etc/init.d/printer_tg_bot
fi

if command -v systemctl >/dev/null 2>&1 && [ -f /etc/systemd/system/printer_tg_bot.service ]; then
    echo "-> Stopping systemd service..."
    systemctl stop printer_tg_bot 2>/dev/null || true
    systemctl disable printer_tg_bot 2>/dev/null || true
    rm -f /etc/systemd/system/printer_tg_bot.service
    systemctl daemon-reload 2>/dev/null || true
fi

killall cloudflared 2>/dev/null || true
pkill -f "printer_tg_bot.py" 2>/dev/null || true

if [ -d "$INSTALL_DIR" ]; then
    echo "-> Removing directory $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
fi

echo "✅ Bot and all its files have been completely removed. Printer system files were untouched."
