#!/bin/sh
# Полное удаление бота, Mini App и службы автозапуска.
# Removes the bot, the Mini App and the autostart service.
# Файлы Klipper/Moonraker не затрагиваются.
# Klipper/Moonraker files are never touched.
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
  KEEP_DATA="n"
  if [ -t 0 ]; then read KEEP_DATA || KEEP_DATA="n"; fi
  case "$KEEP_DATA" in
    y|Y)
      cp "$INSTALL_DIR/printer_state.json" "$BACKUP_FILE"
      echo "-> Backup saved to $BACKUP_FILE"
      ;;
  esac
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

# Останавливаем по PID из lock-файлов: без killall/pkill.
# Stop by the PID from the lock files: no killall/pkill needed.
kill_pid_file() {
  [ -f "$1" ] || return 0
  PID="$(cat "$1" 2>/dev/null | tr -cd '0-9')"
  if [ -n "$PID" ] && [ "$PID" -gt 1 ]; then
    kill "$PID" 2>/dev/null || true
    sleep 1
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f "$1"
}
kill_pid_file "$INSTALL_DIR/cloudflared.pid"
kill_pid_file "$INSTALL_DIR/printer_tg_bot.lock"

if [ -d "$INSTALL_DIR" ]; then
  echo "-> Removing directory $INSTALL_DIR..."
  rm -rf "$INSTALL_DIR"
fi

echo "Bot and all its files have been completely removed. Printer system files were untouched."
