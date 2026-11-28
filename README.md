# 🖨 Klipper / Moonraker Telegram Bot + Mini App (Creality K2 / CFS)

[🇬🇧 English](#-english) | [🇷🇺 Русский](#-русский)

> Python 3 **stdlib only** · no `pip`, no `curl`, no `wget` · OpenWrt / Linux · current version: see [`version.txt`](version.txt)

---

## 🇬🇧 English

A lightweight Telegram bot with a built-in **Telegram Mini App** for **Klipper + Moonraker** printers, including **Creality K2 Plus / K2 Combo with CFS**. Runs on the printer itself (OpenWrt or any Linux), uses only the Python standard library, and stores all data in a single JSON file.

### ✨ What it does

**🤖 Telegram chat**
- **Inline menu:** `📊 Status` (photo + temperatures + progress), `📈 Stats`, `🧵 Spools`, `🧰 Consumables`, `🔧 Maintenance` (axis `X`/`Y`/`Z` lubrication runs, counter resets).
- **Commands:** `/start`, `/help`, `/status`, `/stats`, `/spools`, `/consumables`.
- **Spool management in chat:** add, edit weight/material/name, delete, assign a spool to a CFS slot or to the single-spool mode.
- **Notifications:**
  - job start card with model thumbnail, ETA, cost breakdown and active CFS slots (with inline spool swap);
  - **pre-flight check** at job start: filament shortage (with a spare spool from the inventory), wrong filament type and maintenance life shorter than the job. Silent when there are no risks;
  - filament runout / pause alert with a camera snapshot;
  - completion message with **timelapse `.mp4`** and actual cost;
  - failed-print waste tracking;
  - weekly Sunday digest;
  - CFS humidity warning and maintenance/cutter limit warnings.
- **Language:** `🇷🇺 Русский` / `🇬🇧 English`, switchable in chat or in the Mini App; the chat menu button follows the language.

**📱 Mini App ("📱 Panel" button in the chat)**
- **Printer:** live USB camera snapshot, nozzle/bed temperatures, layer & progress bars, `Pause` / `Resume` / `Stop`, chamber light toggle `💡`.
- **Current print card:** model thumbnail, planned vs spent cost, planned grams per CFS slot, ETA from real progress and pre-flight warnings.
- **Spools & CFS:** 4 CFS slots (color, material, active slot, temperature, humidity), inventory with `-10 g` / `+10 g`, sorting by ID/weight, low-filament filter, empty-spool **tare calculator**.
- **Analytics:** total hours, meters, spool count, jobs, reliability, direct costs (material prices per type + electricity) and depreciation, material donut chart, 7-day activity chart, time efficiency (pure printing vs preparation) and a compact archive of recent models.
- **Maintenance:** progress bars for scheduled maintenance, nozzle, filter, PTFE tube and cutter, one-click resets, axis lubrication runs; a forecast in days and a replacement date based on the last 30 days usage rate.
- **Settings:** electricity price, power, depreciation, filament price per material, maintenance limits, CFS humidity threshold, timezone, digest hour, language, **currency (RUB / USD / EUR — a label only, no conversion)**, and **OTA update** with progress and rollback.
- **Currency:** all amounts are stored exactly as entered; the selected currency is only a display symbol. A Russian-speaking user enters rubles, an English-speaking user enters dollars — nothing is recalculated.

**🛡 Reliability**
- Atomic state writes: `tmp → fsync → .bak → replace`, reads fall back to `.bak`.
- All read-modify-write operations go through a single lock (`mutate_state`).
- Mini App auth: panel key or signed Telegram `initData` (24 h lifetime, `chat_id` / `allowed_users` check). `Referer` is never trusted. The key and `initData` travel in headers, not in the URL.
- OTA: `version.txt` → `manifest.json` (sha256 per file) → staging → verify → backup → replace → **automatic rollback** on any error. Files in subdirectories are supported.

### 📦 Requirements

- `python3` with modules: `urllib`, `hashlib`/`openssl`, `logging`, `configparser`, `http.server`.
  OpenWrt: `opkg update && opkg install python3-light python3-urllib python3-openssl python3-logging ca-bundle`
- Running **Moonraker** (default `http://localhost:7125`).
- Optional: `cloudflared` for a public HTTPS URL of the Mini App (installed automatically).

### 🚀 Install

SSH into the printer and run one line:

```sh
python3 -c "import urllib.request as u; open('/tmp/install.sh','wb').write(u.urlopen('https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main/install.sh',timeout=15).read().replace(b'\r\n',b'\n'))" && sh /tmp/install.sh
```

The installer:
1. checks Python modules, network and CA certificates (offers DNS `8.8.8.8` / `1.1.1.1` as opt-in);
2. downloads files by `manifest.json` with sha256 verification (raw GitHub first, jsDelivr as fallback);
3. asks for **Bot TOKEN** and **CHAT_ID** (or use `BOT_TOKEN=... BOT_CHAT_ID=... sh install.sh`), writes `secrets.ini`;
4. optionally installs `cloudflared` (matches `arm` / `arm64` / `amd64`);
5. refreshes `uninstall.sh` and strips CRLF from shell scripts;
6. verifies the install and enables autostart (`procd` on OpenWrt, `systemd` on Linux).

Default directory: `/root/printer_tg_bot` (falls back to `$HOME/printer_tg_bot`).

### 🔄 Update

- **From the Mini App (recommended):** `⚙️ Settings → 🚀 Bot update → 🔄 Check → ⬇️ Install`. Progress is reported both in the panel and in the chat; the backend is compiled in memory before replacing files, and a failed update rolls back automatically.
- **By re-running the installer:** same command as above. `secrets.ini`, `config.ini` and `printer_state.json` are preserved.

### 🗑 Uninstall

```sh
sh /root/printer_tg_bot/uninstall.sh
```

Stops and removes the service, stops `cloudflared` by PID, removes the install directory. It offers to keep a backup of spools/counters/settings in `/root/printer_tg_bot_backup.json`. Klipper/Moonraker files are never touched.

### 🩺 Troubleshooting

| Symptom | Fix |
|---|---|
| `can't open file '/root/printer_tg_bot.py'` | Wrong autostart path — re-run the installer or fix the service command |
| `syntax error: unexpected word (expecting "in")` in `uninstall.sh` | Windows line endings (CRLF). Fix: `python3 -c "p='/root/printer_tg_bot/uninstall.sh'; d=open(p,'rb').read(); open(p,'wb').write(d.replace(b'\r\n',b'\n'))" && sh /root/printer_tg_bot/uninstall.sh` |
| `CERTIFICATE_VERIFY_FAILED` | `opkg install ca-bundle` |
| `MISSING: ...` at install | Install `python3-urllib`, `python3-openssl`, `python3-logging` |
| No updates found | jsDelivr cache — raw GitHub is queried first; check `printer_tg_bot.log` for the reason shown in the panel |
| Mini App not opening | `cloudflared` missing or blocked; check the log and `cloudflared.pid` |

### 📁 Files

| File | Purpose |
|---|---|
| `printer_tg_bot.py` | bot, monitor loop and embedded HTTP server |
| `webapp.html` | Mini App UI (single page, RU/EN) |
| `config.ini` | default thresholds and prices (never overwritten by updates) |
| `secrets.ini` | `token`, `chat_id`, optional `allowed_users` |
| `printer_state.json` | spools, counters, usage and settings (+ `.bak`) |
| `install.sh` / `uninstall.sh` | install and clean removal |
| `fetch.py` | pure-Python downloader with sha256 and progress bar |
| `manifest.json` / `version.txt` | OTA manifest and current version |
| `manifest_files.txt` | single OTA file list used by the generator |
| `gen_manifest.py` | regenerate the manifest before a release |
| `smoke_test.py` | quick checks: i18n parity, manifest hashes, safe paths, line endings |

### 🔐 Security notes

- Mini App accepts only the panel key or signed `initData`; `initData` older than 24 h is rejected.
- OTA verifies sha256 from `manifest.json`, allows only safe relative paths and rolls back on mismatch.
- `secrets.ini`, `printer_state.json` and logs are excluded from git (see `.gitignore`).

---

## 🇷🇺 Русский

Лёгкий Telegram-бот со встроенным **Telegram Mini App** для принтеров на **Klipper + Moonraker**, включая **Creality K2 Plus / K2 Combo с CFS**. Работает на самом принтере (OpenWrt или любой Linux), использует только стандартную библиотеку Python и хранит все данные в одном JSON-файле.

### ✨ Что умеет

**🤖 Чат Telegram**
- **Меню на кнопках:** `📊 Статус` (фото + температуры + прогресс), `📈 Статистика`, `🧵 Катушки`, `🧰 Расходники`, `🔧 ТО` (прогон осей `X`/`Y`/`Z`, сброс счётчиков).
- **Команды:** `/start`, `/help`, `/status`, `/stats`, `/spools`, `/consumables`.
- **Управление катушками в чате:** добавить, изменить вес/материал/название, удалить, назначить на слот CFS или на одиночную катушку.
- **Уведомления:**
  - карточка старта печати с превью модели, временем окончания, расчётом стоимости и активными слотами CFS (со сменой катушки прямо под сообщением);
  - **pre-flight проверка** при старте: нехватка пластика (с поиском запасной катушки на складе), чужой тип пластика и ресурс ТО меньше задания. Если рисков нет — сообщение не отправляется;
  - пауза / обрыв филамента с фото с камеры;
  - завершение печати с **таймлапсом `.mp4`** и фактической себестоимостью;
  - учёт потерь на брак;
  - еженедельный воскресный дайджест;
  - предупреждения о влажности CFS и лимитах ТО/ножа.
- **Язык:** `🇷🇺 Русский` / `🇬🇧 English` — переключается в чате и в панели; кнопка Mini App в чате тоже меняет язык.

**📱 Mini App (кнопка «📱 Панель» в чате)**
- **Принтер:** живой кадр с USB-камеры, температуры сопла и стола, прогресс и слои, `Пауза` / `Продолжить` / `Стоп`, подсветка `💡`.
- **Карточка текущей печати:** превью модели, план и потраченная сумма, плановый расход по слотам CFS, ETA по реальному прогрессу и предупреждения pre-flight.
- **Катушки и CFS:** 4 слота (цвет, материал, активный слот, температура, влажность), склад с `-10 г` / `+10 г`, сортировка по номеру/остатку, фильтр заканчивающихся катушек, **калькулятор веса пустой шпули**.
- **Аналитика:** наработка, метры, число катушек, количество печатей, надёжность, прямые затраты (пластик по типам + электроэнергия) и амортизация, кольцевая диаграмма по материалам, график за 7 дней, эффективность времени (чистая печать vs подготовка) и компактный архив последних моделей.
- **ТО:** шкалы ресурса планового ТО, сопла, фильтра, PTFE-трубки и ножа, сброс в 1 клик, прогон осей для смазки; прогноз в днях и дата замены по темпу печати за последние 30 дней.
- **Настройки:** цена электричества, мощность, амортизация, цены пластика по типам, лимиты ТО, порог влажности CFS, часовой пояс, час дайджеста, язык, **валюта (RUB / USD / EUR — только подпись, без пересчёта)** и **обновление (OTA)** с прогрессом и откатом.
- **Валюта:** суммы хранятся ровно так, как их ввели; выбранная валюта — только символ отображения. Русскоязычный пользователь вводит рубли, англоязычный — доллары, ничего не пересчитывается.

**🛡 Надёжность**
- Атомарная запись состояния: `tmp → fsync → .bak → replace`, чтение с откатом на `.bak`.
- Все операции «прочитать → изменить → записать» идут под одним локом (`mutate_state`).
- Авторизация Mini App: ключ панели или подписанный Telegram `initData` (срок 24 ч, проверка `chat_id` / `allowed_users`). `Referer` не используется. Ключ и `initData` передаются заголовками, а не в URL.
- OTA: `version.txt` → `manifest.json` (sha256 по каждому файлу) → staging → проверка → бэкап → замена → **автоматический откат** при любой ошибке. Поддерживаются файлы в подкаталогах.

### 📦 Требования

- `python3` с модулями: `urllib`, `hashlib`/`openssl`, `logging`, `configparser`, `http.server`.
  OpenWrt: `opkg update && opkg install python3-light python3-urllib python3-openssl python3-logging ca-bundle`
- Запущенный **Moonraker** (по умолчанию `http://localhost:7125`).
- Опционально: `cloudflared` для публичного HTTPS-адреса Mini App (ставится автоматически).

### 🚀 Установка

Подключитесь к принтеру по SSH и выполните одну строку:

```sh
python3 -c "import urllib.request as u; open('/tmp/install.sh','wb').write(u.urlopen('https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main/install.sh',timeout=15).read().replace(b'\r\n',b'\n'))" && sh /tmp/install.sh
```

Установщик:
1. проверяет модули Python, сеть и CA-сертификаты (предлагает добавить DNS `8.8.8.8` / `1.1.1.1`);
2. скачивает файлы по `manifest.json` со сверкой sha256 (сначала raw GitHub, затем jsDelivr);
3. спрашивает **TOKEN** бота и **CHAT_ID** (можно передать `BOT_TOKEN=... BOT_CHAT_ID=... sh install.sh`) и создаёт `secrets.ini`;
4. по желанию ставит `cloudflared` под вашу архитектуру (`arm` / `arm64` / `amd64`);
5. обновляет `uninstall.sh` и убирает CRLF из shell-скриптов;
6. проверяет установку и включает автозапуск (`procd` на OpenWrt, `systemd` на Linux).

Каталог по умолчанию: `/root/printer_tg_bot` (иначе `$HOME/printer_tg_bot`).

### 🔄 Обновление

- **Из Mini App (рекомендуется):** `⚙️ Настройки → 🚀 Обновление бота → 🔄 Проверить → ⬇️ Установить`. Прогресс виден и в панели, и в чате; перед заменой бэкенд компилируется в памяти, при ошибке выполняется откат.
- **Повторным запуском установщика:** та же команда. `secrets.ini`, `config.ini` и `printer_state.json` сохраняются.

### 🗑 Удаление

```sh
sh /root/printer_tg_bot/uninstall.sh
```

Останавливает и удаляет службу, гасит `cloudflared` по PID, удаляет каталог установки. Предлагает сохранить бэкап катушек/счётчиков/настроек в `/root/printer_tg_bot_backup.json`. Файлы Klipper/Moonraker не затрагиваются.

### 🩺 Решение проблем

| Симптом | Решение |
|---|---|
| `can't open file '/root/printer_tg_bot.py'` | Неверный путь автозапуска — перезапустите установщик или поправьте службу |
| `syntax error: unexpected word (expecting "in")` в `uninstall.sh` | Windows-переводы строк (CRLF). Исправление: `python3 -c "p='/root/printer_tg_bot/uninstall.sh'; d=open(p,'rb').read(); open(p,'wb').write(d.replace(b'\r\n',b'\n'))" && sh /root/printer_tg_bot/uninstall.sh` |
| `CERTIFICATE_VERIFY_FAILED` | `opkg install ca-bundle` |
| `MISSING: ...` при установке | Доставьте `python3-urllib`, `python3-openssl`, `python3-logging` |
| Обновления не находятся | Кэш jsDelivr — raw GitHub опрашивается первым; причина видна в панели и в `printer_tg_bot.log` |
| Mini App не открывается | Нет или заблокирован `cloudflared`; проверьте лог и `cloudflared.pid` |

### 📁 Файлы

| Файл | Назначение |
|---|---|
| `printer_tg_bot.py` | бот, цикл мониторинга и встроенный HTTP-сервер |
| `webapp.html` | интерфейс Mini App (одна страница, RU/EN) |
| `config.ini` | пороги и цены по умолчанию (обновлениями не перезаписывается) |
| `secrets.ini` | `token`, `chat_id`, опционально `allowed_users` |
| `printer_state.json` | катушки, счётчики, расход и настройки (+ `.bak`) |
| `install.sh` / `uninstall.sh` | установка и полное удаление |
| `fetch.py` | загрузчик на чистом Python с sha256 и прогресс-баром |
| `manifest.json` / `version.txt` | манифест OTA и текущая версия |
| `manifest_files.txt` | единый список файлов для OTA и генератора |
| `gen_manifest.py` | пересборка манифеста перед релизом |
| `smoke_test.py` | быстрые проверки: паритет i18n, хеши манифеста, безопасные пути, переводы строк |

### 🔐 Безопасность

- Mini App принимает только ключ панели или подписанный `initData`; `initData` старше 24 ч отклоняется.
- OTA сверяет sha256 из `manifest.json`, допускает только безопасные относительные пути и откатывается при несовпадении.
- `secrets.ini`, `printer_state.json` и логи исключены из git (см. `.gitignore`).
