# 🖨 Klipper / Moonraker Telegram Bot & Mini App (Creality K2 Combo / CFS)

[🇬🇧 English](#-english) | [🇷🇺 Русский](#-русский)

---

## 🇬🇧 English

A lightweight, zero-dependency Telegram bot and built-in **Telegram Mini App** for **Klipper + Moonraker** 3D printers (including **Creality K2 Plus / K2 Combo with CFS** on OpenWrt).

Built purely on **Python 3 standard library** (no `pip` or external packages required) with minimal CPU and RAM footprint.

### ✨ Key Features

#### 📱 Built-in Telegram Mini App ("📱 Panel")
* **📊 Printer:** live USB webcam snapshots, real-time nozzle & bed temperatures, chamber light toggle (`💡`), layer progress bar, and print controls (`Pause` / `Resume` / `Stop`).
* **🧵 Spools & CFS:** visual 4-slot CFS status (colors, filament types, active slot, temperature & humidity), open spool inventory with quick **`-10g` / `+10g`** buttons, weight sorting, low-filament filter (`< 150g`), and **empty spool tare calculator**.
* **📈 Analytics & Costs:** total print hours, reliability score, filament usage (meters & spools), itemized direct costs (material-specific pricing + electricity) and depreciation, material breakdown donut chart, and 7-day print activity chart.
* **🔩 Maintenance & Wear:** progress bars for scheduled maintenance, nozzle, air filter, PTFE tube, and filament cutter with 1-click reset + `X`, `Y`, `Z` axis lubrication runs.
* **⚙️ Settings & OTA Updates:** edit electricity rates, filament prices, maintenance limits, language (`EN` / `RU`), timezone, and check/install updates directly from GitHub in 1 click without touching code.

#### 🤖 Telegram Chat Notifications
* Job start card with model thumbnail, estimated completion time, cost breakdown, and active CFS slots (with inline spool swap buttons).
* Automatic **timelapse (`.mp4`)** upload and actual cost summary upon print completion.
* Filament runout / pause alerts with webcam snapshot, failed print waste tracking, and weekly Sunday digest.

### 🚀 Quick Install (One-Line SSH Command)

Connect to your printer via SSH and run:

```
grep -q "8.8.8.8" /etc/resolv.conf 2>/dev/null || printf "\nnameserver 8.8.8.8\nnameserver 1.1.1.1\n" >> /etc/resolv.conf; python3 -c "import urllib.request; open('/tmp/install.sh','wb').write(urllib.request.urlopen('https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main/install.sh', timeout=15).read().replace(b'\r\n', b'\n'))" && sh /tmp/install.sh

```


The installer automatically downloads all files, fetches the matching `cloudflared` binary for your CPU architecture (`arm`, `arm64`, `amd64`), prompts for language, **TOKEN**, and **CHAT_ID**, and configures auto-start (`procd` on OpenWrt or `systemd` on Linux).

### 🗑 Uninstall

To completely remove the bot, Mini App, and background service without affecting Klipper or Moonraker files:

```sh
sh /root/printer_tg_bot/uninstall.sh
```

---

## 🇷🇺 Русский

Лёгкий автономный Telegram-бот и встроенное веб-приложение (**Telegram Mini App**) для 3D-принтеров на базе **Klipper + Moonraker** (включая **Creality K2 Plus / K2 Combo с CFS** на OpenWrt).

Работает исключительно на стандартных библиотеках **Python 3** (без `pip` и сторонних зависимостей), минимально нагружает процессор и оперативную память принтера.

### ✨ Основные возможности

#### 📱 Встроенный Telegram Mini App («📱 Панель»)
* **📊 Принтер:** живой кадр с USB-камеры, температуры сопла и стола в реальном времени, кнопка включения подсветки (`💡`), прогресс-бар слоя и кнопки управления (`Пауза` / `Продолжить` / `Стоп`).
* **🧵 Катушки и CFS:** визуальное отображение 4 слотов CFS (цвета, материалы, активный слот, температура и влажность), склад открытых бобин с быстрыми кнопками **`-10г` / `+10г`**, сортировкой по весу, фильтром заканчивающихся катушек (`< 150 г`) и **калькулятором вычета веса пустой шпули**.
* **📈 Графики и затраты:** сводка наработки, надёжности, расхода пластика (в метрах и катушках), раздельный расчёт прямых затрат (пластик по индивидуальным тарифам + электроэнергия) и амортизации, кольцевая диаграмма по типам пластика и столбчатый график загрузки за 7 дней.
* **🔩 ТО и обслуживание:** шкалы ресурса планового ТО, сопла, воздушного фильтра, PTFE-трубки и ножа резака со сбросом в 1 клик + прогон осей `X`, `Y`, `Z` для смазки.
* **⚙️ Настройки и обновления:** изменение всех тарифов, лимитов ТО, языка (`RU` / `EN`), часового пояса и обновление скрипта напрямую из GitHub одной кнопкой без редактирования кода.

#### 🤖 Уведомления в чате Telegram
* Карточка старта печати с превью модели, расчётом времени, стоимости и задействованных слотов CFS (с возможностью сменить катушку прямо под сообщением).
* Автоматическая отправка **таймлапса (`.mp4`)** и фактической себестоимости после завершения печати.
* Уведомления о паузе / обрыве филамента с фото с камеры, учёт потерь на брак и еженедельный воскресный дайджест.

### 🚀 Быстрая установка (одной командой по SSH)

Подключитесь к принтеру по SSH и выполните:

```
grep -q "8.8.8.8" /etc/resolv.conf 2>/dev/null || printf "\nnameserver 8.8.8.8\nnameserver 1.1.1.1\n" >> /etc/resolv.conf; python3 -c "import urllib.request; urls=['https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main/install.sh']; data=next((urllib.request.urlopen(u,timeout=8).read() for u in urls if not print('Trying:',u)),b''); open('/tmp/install.sh','wb').write(data.replace(b'\r\n',b'\n'))" && sh /tmp/install.sh

```

### 🗑 Удаление

Для полного удаления бота, Mini App и службы автозапуска без затрагивания файлов принтера выполните:

```
sh /root/printer_tg_bot/uninstall.sh
```

---

## 📁 Project Structure / Структура файлов

* `printer_tg_bot.py` — core monitoring daemon and embedded HTTP server for Telegram Mini App.
* `webapp.html` — responsive single-page Telegram Mini App UI.
* `config.ini` — default thresholds and pricing (safely updated during OTA updates).
* `secrets.ini` — your personal `token` and `chat_id` (created during setup, preserved across updates).
* `printer_state.json` — local spool inventory, usage stats, and custom settings from Mini App.
* `install.sh` / `uninstall.sh` — one-line installer and clean removal scripts.
