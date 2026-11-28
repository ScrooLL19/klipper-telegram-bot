#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, os, sys, time, uuid, threading, urllib.request, urllib.parse, logging, configparser
import hmac, hashlib, subprocess, re, shutil, copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

try:
    import fcntl
except ImportError:  # Windows: блокировка второго экземпляра недоступна / no flock on Windows
    fcntl = None

VERSION = "1.7.0"

# ==================== STATE CACHE / КЭШ СОСТОЯНИЯ ====================
# Единственный безопасный способ изменить состояние — mutate_state().
# Прямое «load_state() -> правка -> save_state()» теряет данные, если в этот
# момент состояние пишет другой поток (главный цикл и Mini App одновременно).
# mutate_state() is the only safe way to change state: plain
# load_state() -> edit -> save_state() loses updates when another thread
# (monitor loop and Mini App) writes state at the same time.
log = logging.getLogger("printer_tg_bot")
state_lock = threading.RLock()
_state_cache = {"data": None, "sig": None}


def _state_signature():
    """Отпечаток файла состояния для инвалидации кэша.
    File signature used to invalidate the in-memory cache."""
    try:
        st = os.stat(STATE_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _read_state_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("state root must be a JSON object")
    return data


def _write_state_file(path, data):
    """Атомарная запись: tmp -> fsync -> .bak -> replace -> fsync(dir).
    Atomic write with backup, safe against power loss."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def load_state():
    """Читает состояние из кэша или с диска (с откатом на .bak).
    Reads state from cache or disk, falling back to the .bak copy."""
    with state_lock:
        sig = _state_signature()
        if _state_cache["data"] is not None and _state_cache["sig"] == sig:
            return copy.deepcopy(_state_cache["data"])
        data = {}
        if sig is not None:
            try:
                data = _read_state_file(STATE_FILE)
            except Exception as e:
                log.error("Не удалось прочитать %s: %s / cannot read state: %s", STATE_FILE, e, e)
                try:
                    data = _read_state_file(STATE_FILE + ".bak")
                    log.warning("Состояние восстановлено из .bak / state restored from backup")
                except Exception:
                    data = {}
        _state_cache["data"] = data
        _state_cache["sig"] = sig
        return copy.deepcopy(data)


def save_state(data):
    """Атомарно сохраняет состояние, не скрывая ошибок.
    Persists state atomically and never hides write errors."""
    with state_lock:
        try:
            _write_state_file(STATE_FILE, data)
        except Exception as e:
            log.exception("Не удалось сохранить состояние / cannot save state: %s", e)
            return False
        _state_cache["data"] = copy.deepcopy(data)
        _state_cache["sig"] = _state_signature()
        return True


def mutate_state(mutator):
    """Атомарно: прочитать -> изменить -> записать под одним локом.
    Atomically read-modify-write the state under a single lock."""
    with state_lock:
        data = load_state()
        result = mutator(data)
        save_state(data)
        return result
# ==================== МУЛЬТИЯЗЫЧНОСТЬ (RU / EN) ====================
LANG = {
    "ru": {
        # Меню Telegram
        "menu_status": "📊 Статус",
        "menu_stats": "📈 Статистика",
        "menu_spools": "🧵 Катушки",
        "menu_cons": "🔩 Расходники",
        "menu_maint": "🛠 Обслуживание",
        "maint_x": "↔️ Прогнать ось X",
        "maint_y": "↕️ Прогнать ось Y",
        "maint_z": "⬆️ Прогнать ось Z (винты)",
        "maint_reset": "🔄 Сброс счетчиков",
        "maint_back": "⬅️ Назад в меню",
        "rst_nozzle": "Сбросить сопло",
        "rst_filter": "Сбросить фильтр",
        "rst_ptfe": "Сбросить PTFE",
        "rst_cutter": "Сбросить нож",
        "rst_back": "⬅️ Назад в обслуживание",
        # Уведомления
        "print_started": "🖨 <b>Начало печати:</b>",
        "print_completed": "🏁 <b>Печать завершена:</b>",
        "print_paused": "⏸ <b>Печать на паузе</b>",
        "filament_out": "⚠️ <b>Кончился филамент!</b>\nПечать на паузе.",
        "print_error": "❌ <b>Ошибка печати:</b>",
        "print_cancelled": "⏹ <b>Печать остановлена/отменена:</b>",
        "missed_end": "⏹ <b>Пропущенное завершение печати (статус: %s):</b>",
        "file": "• Файл: <b>%s</b>",
        "duration": "• Длительность: <b>~%s</b>",
        "eta": "• Окончание: <b>~%s</b>",
        "remaining": "• Осталось: <b>~%s</b>",
        "filament_used": "• Расход пластика: <b>~%.1f г</b>",
        "cfs_flush": " (промывка CFS: ~%.1f г)",
        "cost_title": "💰 <b>Расчетная стоимость:</b>",
        "cost_direct": "• Прямые (пластик+свет): <b>~%s</b> (свет ~%s)",
        "cost_depr": "• С амортизацией: <b>~%s</b> (аморт. ~%s)",
        "spool": "• Катушка: <b>%s</b> (в наличии %.0f г)",
        "slot": "• Слот %s: <b>%s</b> (%sв наличии %.0f г)",
        "slot_need": "нужно ~%.1f г | ",
        "low_filament": "⚠️ <i>Внимание: не хватит ~%.0f г до конца детали!</i>",
        "change_spool": "🔄 Сменить катушку",
        "change_slot": "🔄 Сменить (Слот %s)",
        "reason": "• Причина: %s",
        "waste_title": "🗑 <b>Потери на брак:</b>",
        "waste_filament": "• Пластик: <b>%.1f г</b>",
        "waste_cost": "• Прямые затраты: <b>~%s</b> (с аморт. ~%s)",
        "spool_remaining": "%s: осталось <b>%.0f г</b>",
        "spool_prefix": "• Катушка ",
        "spool_tracking_off": "🧵 <b>Учет склада катушек отключен.</b>\nВключить его можно в <b>📱 Панель → Катушки / Настройки</b>.",
        "spools_cfs_title": "🧵 <b>Склад катушек и CFS (%s°C | влажн. %s%%):</b>",
        "spools_cfs_current": "📦 <b>Сейчас в CFS:</b>",
        "spools_cfs_empty": "• Слоты CFS пусты",
        "spools_cfs_slot": "• Слот %s%s: <b>%s</b> — %.0f г",
        "spools_cfs_slot_empty": "• Слот %s%s: <i>не назначена</i>",
        "spools_single_title": "🧵 <b>Склад катушек принтера:</b>",
        "spools_single_current": "📦 <b>Установлена сейчас:</b>",
        "spools_single_active": "• Активная: <b>%s</b> — %.0f г",
        "spools_single_none": "• Катушка не выбрана (добавьте её ниже)",
        "spools_all_title": "🗄 <b>Все открытые катушки (%d шт.):</b>",
        "spools_all_item": "• %s: <b>%.0f г</b>",
        "spool_add": "➕ Добавить",
        "spool_edit": "✏️ Редактировать",
        "spool_refine": "🔄 Уточнить слот",
        "spool_select": "🔄 Выбрать текущую",
        "spool_delete": "🗑 Удалить",
        "back_menu": "⬅️ Назад в меню",
        "new_spool_prompt": "✍️ <b>Новая катушка:</b>\nОтправьте тип, цвет и вес (г).\nПример: <b>PLA Черный матовый 850</b>",
        "cancel": "❌ Отмена",
        "edit_spool_prompt": "✏️ <b>Редактирование катушки:</b>\nВыберите катушку из списка:",
        "edit_spool_detail": "✏️ <b>Редактирование %s</b> (сейчас %.0f г):\nОтправьте <b>новый вес</b> (например: <b>650</b>) или <b>новое название</b> (например: <b>PLA Темно-зеленый 850</b>):",
        "refine_cfs": "🔄 <b>Уточнение слота CFS:</b>",
        "select_current": "📥 <b>Выберите текущую катушку:</b>",
        "select_slot": "📥 <b>Выберите бобину для Слота %s:</b>",
        "spool_selected": "✅ Катушка выбрана",
        "slot_refined": "✅ Слот %s уточнен",
        "delete_prompt": "🗑 <b>Удаление катушки:</b>\nВыберите катушку из списка:",
        "delete_confirm": "⚠️ <b>Вы уверены, что хотите удалить катушку?</b>\n• <b>%s</b> (%.0f г)",
        "delete_yes": "✅ Да, удалить",
        "deleted": "🗑 Удалено",
        "consumables_view": "🛠 <b>Обслуживание и смазка:</b>",
        "reset_counters": "🔄 <b>Сброс счетчиков:</b>",
        "panel_title": "🤖 <b>Панель управления принтером:</b>",
        "spool_updated": "✅ Катушка <b>%s</b> (%.0f г) обновлена!\n",
        "spool_added": "✅ Катушка <b>%s</b> (%.0f г) добавлена!\n",
        "spool_format": "⚠️ Формат: <b>PLA Черный матовый 850</b>",
        "cfs_humidity_warn": "💧 <b>Повышенная влажность в CFS: %d%%</b> (темп. %s°C)\nПора заменить силикагель или включить сушку филамента.",
        "maint_due": "🛠 <b>Пора провести ТО принтера!</b>\nНаработка: <b>%.1f ч</b>\nОчистите и смажьте направляющие/винты, проверьте ремни.",
        "nozzle_warn": "⚠️ <b>Ресурс сопла выработан!</b>\nПробег: <b>%.1f / %d м</b>.\nПосле замены сбросьте счетчик в меню Обслуживание.",
        "filter_warn": "🌬 <b>Пора заменить фильтр воздуха!</b>\nНаработка фильтра: <b>%.1f / %d ч</b>.\nПосле замены сбросьте счетчик в меню Обслуживание.",
        "ptfe_warn": "➰ <b>Проверьте PTFE-трубку!</b>\nПробег филамента через трубку: <b>%.1f / %d м</b>.\nПосле замены сбросьте счетчик в меню Обслуживание.",
        "cutter_warn": "✂️ <b>Пора проверить лезвие резака филамента!</b>\nВыполнено резов: <b>%d / %d</b>.\nПосле замены сбросьте счетчик в меню Обслуживание.",
        "consumables_title": "🔩 <b>Ресурс расходников и ТО:</b>",
        "consumables_maint": "• До планового ТО: <b>~%.1f ч</b>",
        "consumables_nozzle": "• Сопло: <b>%.1f / %d м</b>",
        "consumables_filter": "• Фильтр: <b>%.1f / %d ч</b>",
        "consumables_ptfe": "• PTFE-трубка: <b>%.1f / %d м</b>",
        "consumables_cutter": "• Нож резака: <b>%d / %d резов</b>",
        "stats_title": "📈 <b>Общая статистика принтера:</b>",
        "stats_time": "• Время печати: <b>%.1f ч</b>",
        "stats_filament": "• Расход филамента: <b>%.1f м</b> (~%.1f катушек)",
        "stats_jobs": "• Всего печатей: <b>%d</b>",
        "stats_reliability": "• Рейтинг надежности: <b>%.0f%%</b> (%d из %d без сбоев)",
        "stats_waste": "• Потери на брак: <b>%.0f г</b> (~%s | %.1f%%)",
        "stats_longest": "• Рекорд печати: <b>%.1f ч</b>",
        "stats_avg": "• В среднем за печать: <b>~%.1f ч / ~%.0f г</b>",
        "stats_load_title": "⏱ <b>Нагрузка принтера (КПД):</b>",
        "stats_7d": "• За 7 дней: <b>%.1f ч</b> (~%.1f ч/день | КПД %.0f%%)",
        "stats_30d": "• За 30 дней: <b>%.1f ч</b> (~%.1f ч/день | КПД %.0f%%)",
        "stats_mats_title": "🧵 <b>Расход по типам пластика:</b>",
        "stats_mat_item": "• %s: <b>%.1f м</b> (~%.1f катушек)",
        "stats_costs_title": "💰 <b>Затраты за всё время:</b>",
        "stats_costs_direct": "• Прямые (пластик+свет): <b>~%s</b> (свет ~%s)",
        "stats_costs_depr": "• С учетом амортизации: <b>~%s</b> (аморт. ~%s)",
        "digest_title": "📅 <b>Еженедельный дайджест принтера:</b>",
        "digest_printed": "• Напечатано за неделю: <b>%.1f ч</b> (КПД %.0f%%)",
        "digest_reliability": "• Надежность за неделю: <b>%.0f%%</b> (%d из %d завершено)",
        "digest_filament": "• Расход пластика: <b>%.1f м</b> (~%.1f катушек)",
        "digest_waste": "• Потери на брак: <b>%.0f г</b> (~%s)",
        "digest_mats": "• По материалам: <b>%s</b>",
        "digest_costs": "• Затраты за неделю: <b>~%s</b> (свет ~%s, с аморт. ~%s)",
        "digest_mat_item": "%s: %.1f м (~%.1f кат.)",
        "bot_started": "✅ <b>Мониторинг принтера запущен (v%s)</b>\nРежим: <b>%s</b>",
        "mode_cfs": "CFS (4 слота)",
        "mode_single": "Одиночная катушка",
        "axis_move_busy": "⛔ Нельзя двигать оси во время печати!",
        "axis_move_ok": "✅ Ось %s успешно прогнана!",
        "axis_move_err": "⚠️ Ошибка движения оси %s: %s",
        "moving_axis": "⏳ Прогон оси %s...",
        "reset_nozzle": "✅ Счетчик сопла сброшен (%.1f м)",
        "reset_filter": "✅ Счетчик фильтра сброшен (%.1f ч)",
        "reset_ptfe": "✅ Счетчик PTFE сброшен (%.1f м)",
        "reset_cutter": "✅ Счетчик ножа сброшен (0 резов)",
        "printer_unavailable": "⚠️ Принтер недоступен.",
        "status_title": "📊 <b>Текущий статус:</b>",
        "status_label": "• Статус: <b>%s</b>",
        "status_file": "• Файл: <b>%s</b>",
        "status_progress": "• Прогресс: <b>%.1f%%</b>",
        "status_layer": " (слой %d из %d)",
        "state_printing": "🖨 Печатает",
        "state_paused": "⏸ На паузе",
        "state_complete": "🏁 Завершена",
        "state_standby": "💤 Ожидание",
        "state_error": "❌ Ошибка",
        "state_cancelled": "⏹ Отменена",
        "state_unknown": "Неизвестно",
        "timelapse_big": "\n🎥 <i>Таймлапс записан (%.1f МБ), но превышает лимит Telegram 50 МБ.</i>",
        "page": "Стр. %d / %d",
        "back": "⬅️ Назад",
        "select_spool": "Выберите катушку:",
        "update_applied": "✅ <b>Обновление применено:</b> v%s → v%s\nБот перезапускается...",
        "update_error": "⚠️ <b>Ошибка обновления:</b> %s",
        "update_checking": "Updating to v%s...",
        "update_uptodate": "Already up to date",
        "missed_not_found": "Пропущенная печать не найдена в истории: %s",
        "missed_processing": "Обработка пропущенного завершения: %s (статус %s)",
        "missed_error": "Ошибка обработки пропущенного завершения",
        "axis_error": "Ошибка движения оси %s",
        "loop_error": "Loop error",
        "state_migrated": "Состояние мигрировано на новую схему",
        "unknown": "Неизвестный",
        "not_assigned": "Не назначена",
        "new_spool": "Новый",
        "webapp_panel": "📱 Панель",
        "language_ru": "🇷🇺 Русский",
        "language_en": "🇬🇧 English",
        "print_time": "• Время печати: <b>%s</b>",
        "actual_cost": "💰 <b>Фактическая стоимость:</b>",
        "reason_none": "не указана",
        "days_short": "Пн,Вт,Ср,Чт,Пт,Сб,Вс",
        "dur_hm": "%d ч %d мин",
        "dur_m": "%d мин",
        "update_started": "🔄 <b>Обновление запущено:</b> v%s → v%s",
        "update_stage_download": "📥 Скачивание %d/%d: <code>%s</code>",
        "update_stage_verify": "🔐 Проверка целостности (sha256)...",
        "update_stage_backup": "💾 Резервная копия текущих файлов...",
        "update_stage_apply": "⚙️ Применение обновления...",
        "update_stage_restart": "🔁 Перезапуск на v%s...",
        "update_done": "✅ <b>Обновление завершено:</b> v%s → v%s\nТекущая версия: <b>v%s</b>",
        "update_rollback": "↩️ Откат / rollback: %s",
        "consumables_unavailable": "⚠️ Не удалось получить данные расходников.",
        "stats_unavailable": "⚠️ Не удалось получить статистику.",
        "slot_button": "Слот %s (%s)",
        "error_short": "❌ Ошибка",
        # Pre-flight (проверка перед печатью)
        "preflight_title": "⚠️ <b>Проверка перед печатью</b>",
        "preflight_low_filament": "• Слот %s: не хватает <b>%.0f г</b> (остаток %.0f г).",
        "preflight_stock_yes": "На складе есть катушка #%d — %.0f г.",
        "preflight_stock_no": "Подходящей катушки на складе нет.",
        "preflight_wrong_mat": "• Слот %s: G-code требует <b>%s</b>, в слоте <b>%s</b>.",
        "preflight_maint": "• %s: осталось <b>%.1f ч</b>, задание ~%.1f ч.",
        "preflight_maint_m": "• %s: осталось <b>%.1f м</b>, задание ~%.1f м.",
        "preflight_item_maint": "Плановое ТО",
        "preflight_item_nozzle": "Сопло",
        "preflight_item_filter": "Фильтр",
        "preflight_item_ptfe": "PTFE-трубка",
        "preflight_note": "<i>Печать не остановлена — это предупреждение.</i>",
    },
    "en": {
        "menu_status": "📊 Status",
        "menu_stats": "📈 Stats",
        "menu_spools": "🧵 Spools",
        "menu_cons": "🔩 Consumables",
        "menu_maint": "🛠 Maintenance",
        "maint_x": "↔️ Move X axis",
        "maint_y": "↕️ Move Y axis",
        "maint_z": "⬆️ Move Z axis (screws)",
        "maint_reset": "🔄 Reset counters",
        "maint_back": "⬅️ Back to menu",
        "rst_nozzle": "Reset nozzle",
        "rst_filter": "Reset filter",
        "rst_ptfe": "Reset PTFE",
        "rst_cutter": "Reset cutter",
        "rst_back": "⬅️ Back to maintenance",
        "print_started": "🖨 <b>Print started:</b>",
        "print_completed": "🏁 <b>Print completed:</b>",
        "print_paused": "⏸ <b>Print paused</b>",
        "filament_out": "⚠️ <b>Filament out!</b>\nPrint paused.",
        "print_error": "❌ <b>Print error:</b>",
        "print_cancelled": "⏹ <b>Print stopped/cancelled:</b>",
        "missed_end": "⏹ <b>Missed print completion (status: %s):</b>",
        "file": "• File: <b>%s</b>",
        "duration": "• Duration: <b>~%s</b>",
        "eta": "• ETA: <b>~%s</b>",
        "remaining": "• Remaining: <b>~%s</b>",
        "filament_used": "• Filament: <b>~%.1f g</b>",
        "cfs_flush": " (CFS flush: ~%.1f g)",
        "cost_title": "💰 <b>Estimated cost:</b>",
        "cost_direct": "• Direct (filament+electricity): <b>~%s</b> (electricity ~%s)",
        "cost_depr": "• With depreciation: <b>~%s</b> (depr. ~%s)",
        "spool": "• Spool: <b>%s</b> (%.0f g left)",
        "slot": "• Slot %s: <b>%s</b> (%s%.0f g left)",
        "slot_need": "Need ~%.1f g | ",
        "low_filament": "⚠️ <i>Warning: not enough filament (~%.0f g short)!</i>",
        "change_spool": "🔄 Change spool",
        "change_slot": "🔄 Change (Slot %s)",
        "reason": "• Reason: %s",
        "waste_title": "🗑 <b>Waste:</b>",
        "waste_filament": "• Filament: <b>%.1f g</b>",
        "waste_cost": "• Cost loss: <b>~%s</b> (with depr. ~%s)",
        "spool_remaining": "%s: <b>%.0f g</b> left",
        "spool_prefix": "• Spool ",
        "spool_tracking_off": "🧵 <b>Spool tracking disabled.</b>\nEnable it in <b>📱 Panel → Spools / Settings</b>.",
        "spools_cfs_title": "🧵 <b>Spool inventory & CFS (%s°C | humidity %s%%):</b>",
        "spools_cfs_current": "📦 <b>Currently in CFS:</b>",
        "spools_cfs_empty": "• CFS slots are empty",
        "spools_cfs_slot": "• Slot %s%s: <b>%s</b> — %.0f g",
        "spools_cfs_slot_empty": "• Slot %s%s: <i>not assigned</i>",
        "spools_single_title": "🧵 <b>Printer spool inventory:</b>",
        "spools_single_current": "📦 <b>Currently loaded:</b>",
        "spools_single_active": "• Active: <b>%s</b> — %.0f g",
        "spools_single_none": "• No spool selected (add one below)",
        "spools_all_title": "🗄 <b>All open spools (%d):</b>",
        "spools_all_item": "• %s: <b>%.0f g</b>",
        "spool_add": "➕ Add",
        "spool_edit": "✏️ Edit",
        "spool_refine": "🔄 Refine slot",
        "spool_select": "🔄 Select current",
        "spool_delete": "🗑 Delete",
        "back_menu": "⬅️ Back to menu",
        "new_spool_prompt": "✍️ <b>New spool:</b>\nSend type, color and weight (g).\nExample: <b>PLA Black Matte 850</b>",
        "cancel": "❌ Cancel",
        "edit_spool_prompt": "✏️ <b>Edit spool:</b>\nSelect from list:",
        "edit_spool_detail": "✏️ <b>Edit %s</b> (currently %.0f g):\nSend <b>new weight</b> (e.g. <b>650</b>) or <b>new name</b> (e.g. <b>PLA Dark Green 850</b>):",
        "refine_cfs": "🔄 <b>Refine CFS slot:</b>",
        "select_current": "📥 <b>Select current spool:</b>",
        "select_slot": "📥 <b>Select spool for Slot %s:</b>",
        "spool_selected": "✅ Spool selected",
        "slot_refined": "✅ Slot %s refined",
        "delete_prompt": "🗑 <b>Delete spool:</b>\nSelect from list:",
        "delete_confirm": "⚠️ <b>Are you sure you want to delete this spool?</b>\n• <b>%s</b> (%.0f g)",
        "delete_yes": "✅ Yes, delete",
        "deleted": "🗑 Deleted",
        "consumables_view": "🛠 <b>Maintenance & lubrication:</b>",
        "reset_counters": "🔄 <b>Reset counters:</b>",
        "panel_title": "🤖 <b>Printer control panel:</b>",
        "spool_updated": "✅ Spool <b>%s</b> (%.0f g) updated!\n",
        "spool_added": "✅ Spool <b>%s</b> (%.0f g) added!\n",
        "spool_format": "⚠️ Format: <b>PLA Black Matte 850</b>",
        "cfs_humidity_warn": "💧 <b>High humidity in CFS: %d%%</b>\nTemp: %s°C\nPlease replace silica gel or enable drying.",
        "maint_due": "🛠 <b>Printer maintenance due!</b>\nRuntime: <b>%.1f h</b>\nClean and lubricate rails/screws, check belts.",
        "nozzle_warn": "⚠️ <b>Nozzle life exhausted!</b>\nUsage: <b>%.1f / %d m</b>.\nReset counter in Maintenance menu after replacement.",
        "filter_warn": "🌬 <b>Replace air filter!</b>\nFilter runtime: <b>%.1f / %d h</b>.\nReset counter in Maintenance menu after replacement.",
        "ptfe_warn": "➰ <b>Check PTFE tube!</b>\nFilament travel: <b>%.1f / %d m</b>.\nReset counter in Maintenance menu after replacement.",
        "cutter_warn": "✂️ <b>Check filament cutter blade!</b>\nCuts performed: <b>%d / %d</b>.\nReset counter in Maintenance menu after replacement.",
        "consumables_title": "🔩 <b>Consumables & Maintenance:</b>",
        "consumables_maint": "• Until next maintenance: <b>~%.1f h</b>",
        "consumables_nozzle": "• Nozzle: <b>%.1f / %d m</b>",
        "consumables_filter": "• Filter: <b>%.1f / %d h</b>",
        "consumables_ptfe": "• PTFE tube: <b>%.1f / %d m</b>",
        "consumables_cutter": "• Cutter blade: <b>%d / %d cuts</b>",
        "stats_title": "📈 <b>Printer statistics:</b>",
        "stats_time": "• Print time: <b>%.1f h</b>",
        "stats_filament": "• Filament used: <b>%.1f m</b> (~%.1f spools)",
        "stats_jobs": "• Total prints: <b>%d</b>",
        "stats_reliability": "• Reliability: <b>%.0f%%</b> (%d of %d without failures)",
        "stats_waste": "• Waste: <b>%.0f g</b> (~%s | %.1f%%)",
        "stats_longest": "• Longest print: <b>%.1f h</b>",
        "stats_avg": "• Average per print: <b>~%.1f h / ~%.0f g</b>",
        "stats_load_title": "⏱ <b>Printer load (efficiency):</b>",
        "stats_7d": "• Last 7 days: <b>%.1f h</b> (~%.1f h/day | efficiency %.0f%%)",
        "stats_30d": "• Last 30 days: <b>%.1f h</b> (~%.1f h/day | efficiency %.0f%%)",
        "stats_mats_title": "🧵 <b>Usage by material type:</b>",
        "stats_mat_item": "• %s: <b>%.1f m</b> (~%.1f spools)",
        "stats_costs_title": "💰 <b>Total costs:</b>",
        "stats_costs_direct": "• Direct (filament+electricity): <b>~%s</b> (electricity ~%s)",
        "stats_costs_depr": "• With depreciation: <b>~%s</b> (depr. ~%s)",
        "digest_title": "📅 <b>Weekly printer digest:</b>",
        "digest_printed": "• Printed this week: <b>%.1f h</b> (efficiency %.0f%%)",
        "digest_reliability": "• Reliability this week: <b>%.0f%%</b> (%d of %d completed)",
        "digest_filament": "• Filament used: <b>%.1f m</b> (~%.1f spools)",
        "digest_waste": "• Waste: <b>%.0f g</b> (~%s)",
        "digest_mats": "• By material: <b>%s</b>",
        "digest_costs": "• Weekly costs: <b>~%s</b> (electricity ~%s, with depr. ~%s)",
        "digest_mat_item": "%s: %.1f m (~%.1f spools)",
        "bot_started": "✅ <b>Printer monitor started (v%s)</b>\nMode: <b>%s</b>",
        "mode_cfs": "CFS (4 slots)",
        "mode_single": "Single spool",
        "axis_move_busy": "⛔ Cannot move axes during printing!",
        "axis_move_ok": "✅ Axis %s moved successfully!",
        "axis_move_err": "⚠️ Error moving axis %s: %s",
        "moving_axis": "⏳ Moving axis %s...",
        "reset_nozzle": "✅ Nozzle counter reset (%.1f m)",
        "reset_filter": "✅ Filter counter reset (%.1f h)",
        "reset_ptfe": "✅ PTFE counter reset (%.1f m)",
        "reset_cutter": "✅ Cutter counter reset (0 cuts)",
        "printer_unavailable": "⚠️ Printer unavailable.",
        "status_title": "📊 <b>Current status:</b>",
        "status_label": "• Status: <b>%s</b>",
        "status_file": "• File: <b>%s</b>",
        "status_progress": "• Progress: <b>%.1f%%</b>",
        "status_layer": " (layer %d of %d)",
        "state_printing": "🖨 Printing",
        "state_paused": "⏸ Paused",
        "state_complete": "🏁 Completed",
        "state_standby": "💤 Standby",
        "state_error": "❌ Error",
        "state_cancelled": "⏹ Cancelled",
        "state_unknown": "Unknown",
        "timelapse_big": "\n🎥 <i>Timelapse recorded (%.1f MB), but exceeds Telegram 50 MB limit.</i>",
        "page": "Page %d / %d",
        "back": "⬅️ Back",
        "select_spool": "Select spool:",
        "update_applied": "✅ <b>Update applied:</b> v%s → v%s\nBot restarting...",
        "update_error": "⚠️ <b>Update error:</b> %s",
        "update_checking": "Updating to v%s...",
        "update_uptodate": "Already up to date",
        "missed_not_found": "Missed print not found in history: %s",
        "missed_processing": "Processing missed completion: %s (status %s)",
        "missed_error": "Error processing missed completion",
        "axis_error": "Error moving axis %s",
        "loop_error": "Loop error",
        "state_migrated": "State migrated to new schema",
        "unknown": "Unknown",
        "not_assigned": "Not assigned",
        "new_spool": "New",
        "webapp_panel": "📱 Panel",
        "language_ru": "🇷🇺 Русский",
        "language_en": "🇬🇧 English",
        "print_time": "• Print time: <b>%s</b>",
        "actual_cost": "💰 <b>Actual cost:</b>",
        "reason_none": "not specified",
        "days_short": "Mon,Tue,Wed,Thu,Fri,Sat,Sun",
        "dur_hm": "%d h %d min",
        "dur_m": "%d min",
        "update_started": "🔄 <b>Update started:</b> v%s → v%s",
        "update_stage_download": "📥 Downloading %d/%d: <code>%s</code>",
        "update_stage_verify": "🔐 Verifying integrity (sha256)...",
        "update_stage_backup": "💾 Backing up current files...",
        "update_stage_apply": "⚙️ Applying the update...",
        "update_stage_restart": "🔁 Restarting on v%s...",
        "update_done": "✅ <b>Update complete:</b> v%s → v%s\nCurrent version: <b>v%s</b>",
        "update_rollback": "↩️ Rollback: %s",
        "consumables_unavailable": "⚠️ Failed to read maintenance data.",
        "stats_unavailable": "⚠️ Failed to read statistics.",
        "slot_button": "Slot %s (%s)",
        "error_short": "❌ Error",
        # Pre-flight checks
        "preflight_title": "⚠️ <b>Pre-flight check</b>",
        "preflight_low_filament": "• Slot %s: <b>%.0f g</b> short (%.0f g left).",
        "preflight_stock_yes": "In stock: spool #%d — %.0f g.",
        "preflight_stock_no": "No suitable spool in stock.",
        "preflight_wrong_mat": "• Slot %s: G-code expects <b>%s</b>, slot has <b>%s</b>.",
        "preflight_maint": "• %s: <b>%.1f h</b> left, job ~%.1f h.",
        "preflight_maint_m": "• %s: <b>%.1f m</b> left, job ~%.1f m.",
        "preflight_item_maint": "Scheduled maintenance",
        "preflight_item_nozzle": "Nozzle",
        "preflight_item_filter": "Filter",
        "preflight_item_ptfe": "PTFE tube",
        "preflight_note": "<i>The print was not stopped — this is a warning.</i>",
    }
}

def _set_language(lang):
    """Сохраняет язык и пересобирает все клавиатуры.
    Persists the language and rebuilds every keyboard."""
    lang = "en" if lang == "en" else "ru"

    def _apply(st):
        settings = st.get("settings") or {}
        settings["language"] = lang
        st["settings"] = settings

    mutate_state(_apply)
    global MAIN_KB, WELCOME_KB, MAINT_KB, RESET_KB
    MAIN_KB = get_main_kb()
    WELCOME_KB = get_welcome_kb()
    MAINT_KB = get_maint_kb()
    RESET_KB = get_reset_kb()
    _refresh_menu_button()
    return lang


def _refresh_menu_button():
    """Обновляет кнопку Mini App в чате под текущий язык.
    Refreshes the chat Mini App button for the current language."""
    url = globals().get("PUBLIC_WEBAPP_URL") or ""
    if not url:
        return
    try:
        res = tg_api("setChatMenuButton", {
            "chat_id": CHAT_ID,
            "menu_button": json.dumps({
                "type": "web_app",
                "text": t("webapp_panel"),
                # Ключ не передаём в URL: он попадает в логи прокси и браузера.
                # Панель авторизуется подписанным initData, ключ — только заголовком.
                # The key is not put into the URL (it leaks into proxy/browser logs):
                # the panel authenticates with signed initData, the key is header-only.
                "web_app": {"url": "%s/" % url},
            }),
        })
        log.info("setChatMenuButton: %s", res)
    except Exception as e:
        log.warning("setChatMenuButton failed: %s", e)


def get_language():
    """Возвращает текущий язык из настроек (по умолчанию 'ru')."""
    st = load_state()
    return (st.get("settings") or {}).get("language", "ru")

_last_kb_lang = [None]

_missing_keys = set()


def _i18n_selfcheck():
    """Проверяет паритет ключей ru/en и пишет расхождения в лог.
    Verifies ru/en key parity and logs the differences."""
    ru_keys, en_keys = set(LANG.get("ru", {})), set(LANG.get("en", {}))
    for key in sorted(ru_keys - en_keys):
        log.warning("i18n: нет перевода EN / missing EN key: %s", key)
    for key in sorted(en_keys - ru_keys):
        log.warning("i18n: нет перевода RU / missing RU key: %s", key)
    return not (ru_keys ^ en_keys)


def plural(count, one, few, many):
    """Русская плюрализация; для EN используются формы one/many.
    Russian pluralization; EN falls back to the one/many forms."""
    n = abs(int(count))
    if get_language() == "en":
        return t(one) if n == 1 else t(many)
    if n % 10 == 1 and n % 100 != 11:
        return t(one)
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return t(few)
    return t(many)


def t(key, *args):
    """Перевод: текущий язык -> EN -> ключ (с записью пропуска в лог).
    Lookup order: current language -> EN -> key (missing keys are logged)."""
    lang = get_language()
    if lang != _last_kb_lang[0]:
        _last_kb_lang[0] = lang
        global MAIN_KB, WELCOME_KB, MAINT_KB, RESET_KB
        try:
            MAIN_KB = get_main_kb()
            WELCOME_KB = get_welcome_kb()
            MAINT_KB = get_maint_kb()
            RESET_KB = get_reset_kb()
        except Exception:
            pass
    table = LANG.get(lang) or LANG.get("en") or {}
    text = table.get(key)
    if text is None:
        text = LANG.get("en", {}).get(key)
    if text is None:
        if key not in _missing_keys:
            _missing_keys.add(key)
            log.warning("i18n: нет ключа / missing key: %s", key)
        return key
    if args:
        try:
            return text % args
        except Exception:
            return text
    return text


# ==================== 1. КОНФИГУРАЦИЯ ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.ini")
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.ini")
STATE_FILE = os.path.join(BASE_DIR, "printer_state.json")

TOKEN = ""
CHAT_ID = ""
ALLOWED_USERS = ""
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
DEFAULT_SPOOL_WEIGHT = 1000.0
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
GITHUB_MIRROR_BASE = "https://cdn.jsdelivr.net/gh/ScrooLL19/klipper-telegram-bot@main"
MANIFEST_FILE = os.path.join(BASE_DIR, "manifest.json")
PENDING_UPDATE_FILE = os.path.join(BASE_DIR, "pending_update.json")
UPDATE_STAGING = os.path.join(BASE_DIR, "update_staging")
UPDATE_BACKUP = os.path.join(BASE_DIR, "update_backup")

# Сетевые таймауты в секундах — без магических чисел по коду.
# Network timeouts in seconds — no magic numbers scattered around.
TIMEOUT_TG = 15
TIMEOUT_MOONRAKER = 8
TIMEOUT_MOONRAKER_FAST = 4
TIMEOUT_MOONRAKER_SLOW = 10
TIMEOUT_METADATA = 5
TIMEOUT_HISTORY_TOTALS = 5
TIMEOUT_HISTORY_LIST = 8
TIMEOUT_CAMERA = 5
TIMEOUT_CAMERA_PROBE = 2
TIMEOUT_THUMBNAIL = 10
TIMEOUT_GCODE_LONG = 300
TIMEOUT_OTA_VERSION = 10
TIMEOUT_OTA_CODE = 25
TIMEOUT_OTA_MANIFEST = 15
TIMEOUT_OTA_DOWNLOAD = 60
TIMEOUT_OTA_HTTP = 20
TIMEOUT_TG_CALLBACK = 10
TIMEOUT_TG_MEDIA = 30
TIMEOUT_TG_LONG = 30

if os.path.exists(SECRETS_FILE) or os.path.exists(CONFIG_FILE):
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)
    config.read(SECRETS_FILE)
    if config.has_option("telegram", "token"): TOKEN = config.get("telegram", "token")
    if config.has_option("telegram", "chat_id"): CHAT_ID = config.get("telegram", "chat_id")
    if config.has_option("telegram", "allowed_users"): ALLOWED_USERS = config.get("telegram", "allowed_users")
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

log = logging.getLogger("printer_tg_bot")
log.setLevel(logging.INFO)
_handler = RotatingFileHandler(os.path.join(BASE_DIR, "printer_tg_bot.log"), maxBytes=5*1024*1024, backupCount=3)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_handler)
_i18n_selfcheck()

# Источники обновлений: raw первым (без кэша), jsDelivr как fallback.
# Если в config.ini задан собственный github_raw_base (форк или локальный
# сервер), используется только он — чтобы не смешивать версии из разных
# репозиториев. Раньше конфиг с jsDelivr полностью вытеснял raw, и из-за
# кэша jsDelivr (до 12 ч) обновления "не находились".
# Update sources: raw first (uncached), jsDelivr as fallback. A custom
# github_raw_base in config.ini replaces the whole list.
CANONICAL_RAW = "https://raw.githubusercontent.com/ScrooLL19/klipper-telegram-bot/main"
CANONICAL_MIRROR = "https://cdn.jsdelivr.net/gh/ScrooLL19/klipper-telegram-bot@main"
_cfg_base = (GITHUB_RAW_BASE or "").rstrip("/")
if "ScrooLL19/klipper-telegram-bot" in _cfg_base:
    _candidates = (CANONICAL_RAW, CANONICAL_MIRROR, _cfg_base)
else:
    _candidates = (_cfg_base,)
UPDATE_BASES = []
for _base in _candidates:
    _base = (_base or "").rstrip("/")
    if _base and _base not in UPDATE_BASES:
        UPDATE_BASES.append(_base)


def _startup_fail(msg_ru, msg_en):
    """Понятная ошибка конфигурации вместо молчаливого запуска.
    Fail fast with a readable message instead of starting misconfigured."""
    log.critical("%s / %s", msg_ru, msg_en)
    print("ERROR: %s\nERROR: %s" % (msg_ru, msg_en))
    sys.exit(1)


if not TOKEN or not CHAT_ID:
    _startup_fail("в secrets.ini не заданы token или chat_id",
                  "token or chat_id is missing in secrets.ini")
if not re.match(r"^\d+:[A-Za-z0-9_-]{20,}$", TOKEN):
    _startup_fail("token не похож на токен из @BotFather",
                  "token does not look like a @BotFather token")
if not re.match(r"^-?\d+$", str(CHAT_ID)):
    _startup_fail("chat_id должен быть числом (для групп — отрицательным)",
                  "chat_id must be a number (negative for groups)")

# ==================== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ====================
last_state = None
last_fname = ""
last_update_id = 0
awaiting_spool = False
edit_spool_id = None
print_start_files = set()
last_cam_fail_ts = 0.0


# ==================== 2. МЕНЮ TELEGRAM ====================
def get_main_kb():
    return {
        "inline_keyboard": [
            [{"text": t("menu_status"), "callback_data": "btn_status"}, {"text": t("menu_stats"), "callback_data": "btn_stats"}],
            [{"text": t("menu_spools"), "callback_data": "btn_spools"}, {"text": t("menu_cons"), "callback_data": "btn_cons"}],
            [{"text": t("menu_maint"), "callback_data": "btn_maint"}]
        ]
    }

def get_welcome_kb():
    return {
        "inline_keyboard": [
            [{"text": t("menu_status"), "callback_data": "btn_status"}, {"text": t("menu_stats"), "callback_data": "btn_stats"}],
            [{"text": t("menu_spools"), "callback_data": "btn_spools"}, {"text": t("menu_cons"), "callback_data": "btn_cons"}],
            [{"text": t("menu_maint"), "callback_data": "btn_maint"}]
        ]
    }

def get_maint_kb():
    return {
        "inline_keyboard": [
            [{"text": t("maint_x"), "callback_data": "move_x"}, {"text": t("maint_y"), "callback_data": "move_y"}],
            [{"text": t("maint_z"), "callback_data": "move_z"}],
            [{"text": t("maint_reset"), "callback_data": "btn_resets"}],
            [{"text": t("maint_back"), "callback_data": "btn_back"}]
        ]
    }

def get_reset_kb():
    return {
        "inline_keyboard": [
            [{"text": t("rst_nozzle"), "callback_data": "rst_nozzle"}, {"text": t("rst_filter"), "callback_data": "rst_filter"}],
            [{"text": t("rst_ptfe"), "callback_data": "rst_ptfe"}, {"text": t("rst_cutter"), "callback_data": "rst_cutter"}],
            [{"text": t("rst_back"), "callback_data": "btn_maint"}]
        ]
    }

MAIN_KB = get_main_kb()
WELCOME_KB = get_welcome_kb()
MAINT_KB = get_maint_kb()
RESET_KB = get_reset_kb()

# ==================== 3. TELEGRAM API ====================
def tg_api(method, payload, timeout=TIMEOUT_TG):
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
    tg_api("answerCallbackQuery", p, timeout=TIMEOUT_TG_CALLBACK)

def tg_send_media(is_video, file_bytes, filename, mime, caption, kb=None, timeout=TIMEOUT_TG_MEDIA):
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
    if CAM_SNAPSHOT_URL:
        try:
            with urllib.request.urlopen(CAM_SNAPSHOT_URL, timeout=TIMEOUT_CAMERA) as r:
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
                with urllib.request.urlopen(MOONRAKER + "/server/files/gcodes/" + urllib.parse.quote(rel),
                                            timeout=TIMEOUT_THUMBNAIL) as r:
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
                    tg_send(caption + t("timelapse_big", size_mb), kb)
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

# ==================== 4. MOONRAKER API ====================
class MoonrakerClient:
    """Единая точка доступа к Moonraker: базовый URL, таймауты, лог ошибок.
    Single access point to Moonraker: base URL, timeouts, error logging."""

    def __init__(self, base=None):
        self.base = str(base or MOONRAKER).rstrip("/")

    def url(self, path):
        return self.base + "/" + str(path or "").lstrip("/")

    def get(self, path, timeout=TIMEOUT_MOONRAKER):
        try:
            with urllib.request.urlopen(self.url(path), timeout=timeout) as r:
                return json.loads(r.read().decode()).get("result", {})
        except Exception as e:
            log.warning("Moonraker GET %s: %s", path, e)
            return {}

    def post(self, path, payload=b"", timeout=TIMEOUT_MOONRAKER_FAST):
        req = urllib.request.Request(self.url(path), data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    def gcode(self, script, timeout=TIMEOUT_GCODE_LONG):
        data = urllib.parse.urlencode({"script": script}).encode()
        return self.post("/printer/gcode/script", data, timeout)


MOONRAKER_CLIENT = MoonrakerClient()


def moonraker_get(path, timeout=TIMEOUT_MOONRAKER):
    return MOONRAKER_CLIENT.get(path, timeout)

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
                with urllib.request.urlopen(u, timeout=TIMEOUT_CAMERA_PROBE) as r:
                    if r.read(16).startswith(b"\xff\xd8"):
                        CAM_SNAPSHOT_URL = u
                        break
            except Exception:
                pass
    objs = moonraker_get("/printer/objects/list", 4).get("objects") or []
    HAS_CFS = ("box" in objs)

def get_print_stats():
    return moonraker_get("/printer/objects/query?print_stats&display_status&toolhead&extruder&heater_bed",
                         TIMEOUT_MOONRAKER_SLOW).get("status", {})

def get_cfs_status():
    return moonraker_get("/printer/objects/query?box&filament_rack",
                         TIMEOUT_MOONRAKER).get("status", {}) if HAS_CFS else {}

def get_file_meta(filename):
    return moonraker_get("/server/files/metadata?filename=" + urllib.parse.quote(filename),
                         TIMEOUT_METADATA) if filename else {}

_GCODE_WEIGHTS_CACHE = {}


def get_gcode_slot_weights(filename, meta=None):
    """Вес по слотам с кэшем по (имя, размер, mtime) — панель обновляется часто.
    Per-slot weights cached by (name, size, mtime) — the panel refreshes often."""
    cache_key = None
    if filename and GCODES_DIR:
        path = os.path.join(GCODES_DIR, filename)
        try:
            info = os.stat(path)
            cache_key = (filename, info.st_size, info.st_mtime)
            if cache_key in _GCODE_WEIGHTS_CACHE:
                return list(_GCODE_WEIGHTS_CACHE[cache_key])
        except OSError:
            cache_key = None
    result = _read_gcode_slot_weights(filename, meta)
    if cache_key and result:
        if len(_GCODE_WEIGHTS_CACHE) > 8:
            _GCODE_WEIGHTS_CACHE.clear()
        _GCODE_WEIGHTS_CACHE[cache_key] = list(result)
    return result


def _read_gcode_slot_weights(filename, meta=None):
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
    return moonraker_get("/server/history/totals", TIMEOUT_HISTORY_TOTALS).get("job_totals", {})

def get_history_list(limit=500):
    return moonraker_get("/server/history/list?limit=%d" % limit, TIMEOUT_HISTORY_LIST).get("jobs", [])

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

# ==================== 5. УЧЕТ КАТУШЕК ====================


def cfg(key, st=None):
    """Настройка из переданного снимка состояния или с диска.
    Reads a setting from the given state snapshot or from disk."""
    if st is None:
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
        "currency": "",
    }
    return defaults.get(key)

def cfg_price(mat, st=None):
    prices = cfg("prices", st) or FILAMENT_PRICE_BY_TYPE
    return float(prices.get(str(mat).upper(), cfg("price_default", st)))


# Суммы хранятся в валюте, в которой их ввёл пользователь; символ — только
# оформление. Курсов и конвертации нет: русскоязычный вводит рубли,
# англоязычный — доллары, и ничего не пересчитывается.
# Amounts are stored in the currency the user entered them in; the symbol is
# formatting only. There are no rates or conversion: a Russian-speaking user
# enters rubles, an English-speaking user enters dollars, nothing is converted.
CURRENCY_SYMBOLS = {"RUB": "₽", "USD": "$", "EUR": "€"}
DEFAULT_CURRENCY_BY_LANG = {"ru": "RUB", "en": "USD"}


def currency_code():
    """Валюта пользователя: из настроек, иначе по языку (ru→RUB, en→USD).
    User currency from settings, otherwise derived from the language."""
    code = str(cfg("currency") or "").upper()
    if code in CURRENCY_SYMBOLS:
        return code
    return DEFAULT_CURRENCY_BY_LANG.get(get_language(), "RUB")


def money(value, decimals=0):
    """Сумма в валюте пользователя без пересчёта: 1234 ₽ / $1200.
    Formats an amount in the user's currency without conversion."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.0
    fmt = "%." + str(int(decimals)) + "f"
    text = fmt % value
    code = currency_code()
    if code == "RUB":
        return text + " " + CURRENCY_SYMBOLS[code]
    return CURRENCY_SYMBOLS[code] + text

COLOR_PALETTE = (
    ((228, 238, 155), "Светло-желтый", "Желтый"),
    ((252, 130, 115), "Коралловый", "Розовый"),
    ((15, 105, 250), "Синий", "Синий"),
    ((85, 235, 95), "Салатовый", "Салатовый"),
    ((240, 125, 40), "Оранжевый", "Оранжевый"),
    ((255, 20, 15), "Красный", "Красный"),
    ((245, 245, 245), "Белый", "Белый"),
    ((195, 210, 225), "Серый", "Серый"),
    ((155, 65, 240), "Фиолетовый", "Фиолетовый"),
    ((10, 195, 255), "Голубой", "Голубой"),
    ((15, 235, 210), "Бирюзовый", "Бирюзовый"),
    ((245, 190, 235), "Светло-розовый", "Розовый"),
    ((255, 165, 10), "Оранжевый", "Оранжевый"),
    ((15, 30, 235), "Темно-синий", "Синий"),
    ((22, 24, 30), "Черный", "Черный"),
    ((110, 175, 240), "Голубой", "Голубой"),
    ((25, 145, 250), "Синий", "Синий"),
    ((65, 185, 45), "Зеленый", "Зеленый"),
    ((10, 235, 115), "Зеленый", "Зеленый"),
    ((240, 55, 235), "Розовый", "Розовый"),
    ((235, 245, 20), "Желтый", "Желтый"),
    ((205, 195, 245), "Сиреневый", "Фиолетовый"),
    ((65, 35, 245), "Фиолетовый", "Фиолетовый"),
    ((250, 205, 15), "Желтый", "Желтый"),
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

COLOR_EN = {
    "Светло-желтый": "Light yellow", "Желтый": "Yellow", "Коралловый": "Coral",
    "Розовый": "Pink", "Синий": "Blue", "Салатовый": "Lime", "Оранжевый": "Orange",
    "Красный": "Red", "Белый": "White", "Серый": "Grey", "Фиолетовый": "Purple",
    "Голубой": "Light blue", "Бирюзовый": "Turquoise", "Светло-розовый": "Light pink",
    "Темно-синий": "Dark blue", "Черный": "Black", "Зеленый": "Green",
    "Сиреневый": "Lilac", "Серебряный": "Silver", "Коричневый": "Brown",
    "Прозрачный": "Clear", "Неизвестный": "Unknown",
}


def hex_to_ru_name(raw_hex):
    """Название цвета по hex на языке пользователя (внутри — канонический RU).
    Colour name for a hex value, localized for display; canonical RU is kept
    internally for grouping and matching."""
    res = _nearest_palette(raw_hex)
    name = list(res).pop(0) if res else "Неизвестный"
    if get_language() == "en":
        return COLOR_EN.get(name, name)
    return name

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
    if not HAS_CFS:
        return
    try:
        _, _, temp, hum = get_cfs_slots()
        hum_val = int(float(str(hum)))
        if hum_val <= CFS_HUMIDITY_LIMIT:
            return
        should_warn = [False]

        def _apply(st):
            if time.time() - st.get("last_hum_warn_ts", 0) < 86400:
                return
            st["last_hum_warn_ts"] = time.time()
            should_warn[0] = True

        mutate_state(_apply)
        if should_warn[0]:
            tg_send(t("cfs_humidity_warn", hum_val, temp), MAIN_KB)
    except Exception:
        pass

def resolve_slot_spool(st, letter, mat="PLA", hx="000000", auto_create=True):
    """Ищет/создаёт катушку для слота, работая только с переданным состоянием.
    Функция не сохраняет состояние: запись делает вызывающий через mutate_state().
    Resolves or creates the spool for a slot using the given state only.
    It never persists: the caller writes through mutate_state()."""
    spools = st.get("spools") or []
    if letter == "SINGLE":
        cur_id = st.get("single_spool_id")
        sp = next((s for s in spools if s.get("id") == cur_id and s.get("rem_g", 0) > 0), None)
        if sp:
            return sp
        avail = [s for s in spools if s.get("rem_g", 0) > 0]
        if avail:
            avail.sort(key=lambda x: x.get("id", 0))
            st["single_spool_id"] = avail[0]["id"]
            return next((s for s in spools if s.get("id") == st["single_spool_id"]), None)
        return None
    ov_map = st.get("slot_overrides") or {}
    ov = ov_map.get(letter)
    if ov:
        if ov.get("hex") == hx and ov.get("mat") == mat:
            sp = next((s for s in spools if s.get("id") == ov.get("id") and s.get("rem_g", 0) > 0), None)
            if sp:
                return sp
        del ov_map[letter]
        st["slot_overrides"] = ov_map
    exact = hex_to_ru_name(hx)
    grp = normalize_color_group(hx)
    matches = [s for s in spools if s.get("mat") == mat and normalize_color_group(s.get("group") or s.get("name") or s.get("hex")) == grp and s.get("rem_g", 0) > 0]
    if matches:
        matches.sort(key=lambda x: (0 if exact.lower() in str(x.get("name") or "").lower() else 1, x.get("id", 0)))
        return matches[0]
    if not auto_create:
        return None
    next_id = max([s.get("id", 0) for s in spools], default=0) + 1
    new_sp = {"id": next_id, "mat": mat, "name": exact, "group": grp, "hex": hx, "rem_g": DEFAULT_SPOOL_WEIGHT}
    spools.append(new_sp)
    st["spools"] = spools
    return new_sp

def deduct_spool_weight(filename, meta, weight_g):
    """Списывает пластик с катушек атомарно (одна транзакция состояния).
    Deducts filament weight in a single state transaction."""
    if weight_g <= 0 or not cfg("spool_tracking"):
        return ""
    slots, active_let, _, _ = get_cfs_slots()
    used = get_used_slots(filename, meta, slots, active_let, weight_g)
    raw_sum = sum(g for _, g in used)
    scale = (weight_g / raw_sum) if raw_sum > 0 else 1.0
    notes = []

    def _apply(st):
        for let, part_g in used:
            slot = slots.get(let) or {"mat": "PLA", "hex": "000000"}
            left_to_sub = part_g * scale
            guard = 0
            while left_to_sub > 0.1 and guard < 8:
                guard += 1
                sp = resolve_slot_spool(st, let, slot["mat"], slot["hex"], auto_create=HAS_CFS)
                if not sp:
                    break
                take = min(sp["rem_g"], left_to_sub)
                sp["rem_g"] = round(max(0.0, sp["rem_g"] - take), 1)
                left_to_sub -= take
                notes.append("%s: осталось <b>%.0f г</b>" % (sp_title(sp), sp["rem_g"]))
        overrides = st.get("slot_overrides") or {}
        for let, _ in used:
            overrides.pop(let, None)
        st["slot_overrides"] = overrides

    mutate_state(_apply)
    return "\n".join("• Катушка " + n for n in notes)

def build_spools_view():
    """Экран склада катушек на языке пользователя.
    Spool inventory screen in the user's language."""
    st = load_state()
    if not cfg("spool_tracking"):
        return t("spool_tracking_off"), {"inline_keyboard": [[{"text": t("back_menu"), "callback_data": "btn_back"}]]}
    if HAS_CFS:
        slots, active_let, temp, hum = get_cfs_slots()
        lines = [t("spools_cfs_title", temp, hum), "", t("spools_cfs_current")]
        if not slots:
            lines.append(t("spools_cfs_empty"))
        else:
            for let in ("A", "B", "C", "D"):
                if let in slots:
                    s_info = slots[let]
                    sp = resolve_slot_spool(st, let, s_info["mat"], s_info["hex"], auto_create=False)
                    mark = " ▶️" if let == active_let else ""
                    if sp:
                        lines.append(t("spools_cfs_slot", let, mark, sp_title(sp), sp["rem_g"]))
                    else:
                        lines.append(t("spools_cfs_slot_empty", let, mark))
        ov_btn = {"text": t("spool_refine"), "callback_data": "sp_ov_menu"}
    else:
        lines = [t("spools_single_title"), "", t("spools_single_current")]
        sp = resolve_slot_spool(st, "SINGLE")
        if sp:
            lines.append(t("spools_single_active", sp_title(sp), sp["rem_g"]))
        else:
            lines.append(t("spools_single_none"))
        ov_btn = {"text": t("spool_select"), "callback_data": "sp_ovslot_SINGLE"}
    spools = [s for s in (st.get("spools") or []) if s.get("rem_g", 0) > 0]
    if spools:
        lines.extend(["", t("spools_all_title", len(spools))])
        for sp in sorted(spools, key=lambda x: x.get("rem_g", 0)):
            lines.append(t("spools_all_item", sp_title(sp), sp["rem_g"]))
    kb = {
        "inline_keyboard": [
            [{"text": t("spool_add"), "callback_data": "sp_add"},
             {"text": t("spool_edit"), "callback_data": "sp_edit_menu"}],
            [ov_btn, {"text": t("spool_delete"), "callback_data": "sp_del_menu"}],
            [{"text": t("back_menu"), "callback_data": "btn_back"}]
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

# ==================== 6. СЧЕТЧИКИ РАСХОДНИКОВ ====================
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
    """Учитывает расход пластика по типам атомарно.
    Records filament usage per material in one transaction."""
    if used_mm <= 0: used_mm = meta.get("filament_total", 0) or 0
    if used_mm <= 0: return
    types = detect_material_types(meta, fname)
    share = (used_mm / 1000.0) / len(types)

    def _apply(st):
        mats = st.get("materials_meters", {})
        w_mats = st.get("week_materials", {})
        for mat in types:
            mats[mat] = mats.get(mat, 0.0) + share
            w_mats[mat] = w_mats.get(mat, 0.0) + share
        st["materials_meters"], st["week_materials"] = mats, w_mats

    mutate_state(_apply)

def record_waste(weight_g, direct_rub):
    """Учитывает потери на брак атомарно.
    Records failed-print waste in one transaction."""
    if weight_g <= 0.5 and direct_rub <= 0.5: return

    def _apply(st):
        st["waste_g"] = round(st.get("waste_g", 0.0) + weight_g, 1)
        st["waste_rub"] = round(st.get("waste_rub", 0.0) + direct_rub, 1)
        st["week_waste_g"] = round(st.get("week_waste_g", 0.0) + weight_g, 1)
        st["week_waste_rub"] = round(st.get("week_waste_rub", 0.0) + direct_rub, 1)

    mutate_state(_apply)

def update_and_check_counters(total_hours, total_meters, add_cuts=0):
    """Обновляет счётчики ТО и собирает уведомления для отправки после записи.
    Updates maintenance counters and returns notifications to send afterwards."""
    messages = []

    def _apply(st):
        last_m = st.get("last_maint_hours", 0)
        if last_m == 0 and total_hours >= MAINT_INTERVAL_HOURS:
            st["last_maint_hours"] = (int(total_hours) // MAINT_INTERVAL_HOURS) * MAINT_INTERVAL_HOURS
        elif total_hours - last_m >= MAINT_INTERVAL_HOURS:
            st["last_maint_hours"] = (int(total_hours) // MAINT_INTERVAL_HOURS) * MAINT_INTERVAL_HOURS
            messages.append(t("maint_due", total_hours))
        checks = [
            ("nozzle_start_meters", "nozzle_warned", total_meters, NOZZLE_LIMIT_METERS, "⚠️ <b>Ресурс сопла выработан!</b>\nПробег: <b>%.1f / %d м</b>.\nПосле замены сбросьте счетчик в меню Обслуживание."),
            ("filter_start_hours", "filter_warned", total_hours, FILTER_LIMIT_HOURS, "🌬 <b>Пора заменить фильтр воздуха!</b>\nНаработка фильтра: <b>%.1f / %d ч</b>.\nПосле замены сбросьте счетчик в меню Обслуживание."),
            ("ptfe_start_meters", "ptfe_warned", total_meters, PTFE_LIMIT_METERS, "➰ <b>Проверьте PTFE-трубку!</b>\nПробег филамента через трубку: <b>%.1f / %d м</b>.\nПосле замены сбросьте счетчик в меню Обслуживание.")
        ]
        for start_k, warn_k, cur_val, limit_val, msg_tpl in checks:
            if start_k not in st:
                st[start_k], st[warn_k] = cur_val, False
            else:
                used = max(0.0, cur_val - st[start_k])
                if used >= limit_val and not st.get(warn_k, False):
                    st[warn_k] = True
                    messages.append(msg_tpl % (used, limit_val))
        if add_cuts > 0:
            st["cutter_cuts"] = st.get("cutter_cuts", 0) + add_cuts
            cuts = st["cutter_cuts"]
            if cuts >= CUTTER_LIMIT_CUTS and not st.get("cutter_warned", False):
                st["cutter_warned"] = True
                messages.append("✂️ <b>Пора проверить лезвие резака филамента!</b>\nВыполнено резов: <b>%d / %d</b>.\nПосле замены сбросьте счетчик в меню Обслуживание." % (cuts, CUTTER_LIMIT_CUTS))

    mutate_state(_apply)
    for msg in messages:
        try:
            tg_send(msg, MAIN_KB)
        except Exception:
            log.warning("Не удалось отправить уведомление ТО / maintenance notify failed")

def update_counters(cuts=0):
    totals = get_history_totals()
    if totals:
        update_and_check_counters(
            totals.get("total_time", 0) / 3600.0,
            totals.get("total_filament_used", 0) / 1000.0,
            cuts)

# ==================== 7. ОТЧЕТЫ ====================
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
    """Ресурс расходников на языке пользователя.
    Maintenance counters in the user's language."""
    totals = get_history_totals()
    if not totals:
        return t("consumables_unavailable")
    h, m = totals.get("total_time", 0) / 3600.0, totals.get("total_filament_used", 0) / 1000.0
    st = load_state()
    return "\n".join([
        t("consumables_title"),
        t("consumables_maint", max(0.0, (st.get("last_maint_hours", 0) + MAINT_INTERVAL_HOURS) - h)),
        t("consumables_nozzle", max(0.0, m - st.get("nozzle_start_meters", m)), NOZZLE_LIMIT_METERS),
        t("consumables_filter", max(0.0, h - st.get("filter_start_hours", h)), FILTER_LIMIT_HOURS),
        t("consumables_ptfe", max(0.0, m - st.get("ptfe_start_meters", m)), PTFE_LIMIT_METERS),
        t("consumables_cutter", st.get("cutter_cuts", 0), CUTTER_LIMIT_CUTS),
    ])

def format_stats():
    totals = get_history_totals()
    if not totals: return t("stats_unavailable")
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
        t("stats_title"),
        t("stats_time", h),
        t("stats_filament", m, m / M_PER_SPOOL),
        t("stats_jobs", jobs),
    ]
    if hist_len > 0:
        lines.append(t("stats_reliability", ok_all * 100.0 / hist_len, ok_all, hist_len))
    if waste_g > 0:
        w_pct = (waste_g * 100.0 / total_g) if total_g > 0 else 0
        lines.append(t("stats_waste", waste_g, money(waste_rub), w_pct))
    lines.extend([
        t("stats_longest", longest_h),
        t("stats_avg", (h / jobs if jobs else 0), (total_g / jobs if jobs else 0)),
        "",
        t("stats_load_title"),
        t("stats_7d", h_7d, h_7d / 7.0, min(100.0, h_7d * 100.0 / 168.0)),
        t("stats_30d", h_30d, h_30d / 30.0, min(100.0, h_30d * 100.0 / 720.0)),
    ])
    mats = st.get("materials_meters", {})
    if mats:
        lines.extend(["", t("stats_mats_title")])
        for k in sorted(mats, key=mats.get, reverse=True):
            lines.append(t("stats_mat_item", k, mats[k], mats[k] / M_PER_SPOOL))
    lines.extend([
        "",
        t("stats_costs_title"),
        t("stats_costs_direct", money(direct), money(elec)),
        t("stats_costs_depr", money(direct + depr), money(depr)),
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

        def _reset_week(state):
            # Повторная проверка под локом: дайджест мог уйти из другого потока.
            # Re-check under the lock: another thread may have sent the digest.
            if state.get("last_digest_date") == today_str:
                return
            state["last_digest_date"] = today_str
            state["week_materials"] = {}
            state["week_waste_g"], state["week_waste_rub"] = 0.0, 0.0

        mutate_state(_reset_week)
        elec = h_7d * PRINTER_POWER_KW * ELEC_PRICE_KWH
        direct = ((m_7d * G_PER_M_DEFAULT) / 1000.0) * FILAMENT_PRICE_KG + elec
        full = direct + (h_7d * DEPRECIATION_PER_HOUR)
        rel_pct = (ok_7d * 100.0 / jobs_7d) if jobs_7d > 0 else 100.0
        lines = [
            t("digest_title"),
            t("digest_printed", h_7d, min(100.0, h_7d * 100.0 / 168.0)),
            t("digest_reliability", rel_pct, ok_7d, jobs_7d),
            t("digest_filament", m_7d, m_7d / M_PER_SPOOL),
        ]
        if w_waste_g > 0:
            lines.append(t("digest_waste", w_waste_g, money(w_waste_rub)))
        if week_mats:
            parts = [t("digest_mat_item", k, week_mats[k], week_mats[k] / M_PER_SPOOL)
                     for k in sorted(week_mats, key=week_mats.get, reverse=True)]
            lines.append(t("digest_mats", ", ".join(parts)))
        lines.append(t("digest_costs", money(direct), money(elec), money(full)))
        tg_send("\n".join(lines), MAIN_KB)

# ==================== 8. ЗАВЕРШЕНИЕ ПЕЧАТИ ====================
def process_completed_job(fname, meta, print_dur, used_mm, send_video=True):
    w, direct, full, elec, depr = calc_costs(meta, print_dur, used_mm)
    flush_g = get_flush_weight(meta)
    record_material_usage(meta, used_mm, fname)
    spool_notes = deduct_spool_weight(fname, meta, w)
    changes = meta.get("filament_change_count", 0) or 0
    final_cut = 1 if (HAS_CFS and used_mm > 0) else 0
    update_counters(changes + final_cut)
    lines = [t("print_completed"), t("file", esc(fname)), t("print_time", format_dur(print_dur))]
    if w > 0:
        flush_txt = t("cfs_flush", flush_g) if flush_g > 0.1 else ""
        lines.append(t("filament_used", w) + flush_txt)
    if spool_notes: lines.append(spool_notes)
    if direct > 0:
        lines.extend(["", t("actual_cost"),
            t("cost_direct", money(direct), money(elec)),
            t("cost_depr", money(full), money(depr))])
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
        lines = [t("missed_end", esc(status or t("unknown"))),
            t("file", esc(old_fname)),
            t("print_time", format_dur(dur))]
        if w > 0:
            lines.append(t("filament_used", w))
        if spool_notes:
            lines.extend(["", spool_notes])
        tg_send("\n".join(lines), MAIN_KB)
    except Exception:
        log.exception("Ошибка обработки пропущенного завершения")

# ==================== 9. СТАТУС И ОБСЛУЖИВАНИЕ ====================
def format_dur(sec):
    """Длительность на языке пользователя.
    Human-readable duration in the user's language."""
    h, m = int(sec // 3600), int((sec % 3600) // 60)
    return t("dur_hm", h, m) if h > 0 else t("dur_m", m)

def format_status():
    st = get_print_stats()
    if not st: return t("printer_unavailable")
    ps, ds = st.get("print_stats", {}), st.get("display_status", {})
    state, fname = ps.get("state", "unknown"), ps.get("filename", "")
    prog, dur = (ds.get("progress", 0) or 0) * 100, ps.get("print_duration", 0) or 0
    info = ps.get("info") or {}
    s_map = {
        "printing": t("state_printing"),
        "paused": t("state_paused"),
        "complete": t("state_complete"),
        "standby": t("state_standby"),
        "error": t("state_error"),
        "cancelled": t("state_cancelled"),
    }
    lines = [t("status_title"), t("status_label", s_map.get(state, t("state_unknown")))]
    if state in ("printing", "paused") and fname:
        lines.append(t("status_file", esc(fname)))
    p_str = t("status_progress", prog)
    if info.get("current_layer") is not None and info.get("total_layer"):
        p_str += t("status_layer", info["current_layer"], info["total_layer"])
    lines.append(p_str)
    est = get_file_meta(fname).get("estimated_time", 0)
    if est > dur:
        rem = est - dur
        end_dt = (datetime.now(timezone.utc) + timedelta(hours=TIMEZONE_OFFSET, seconds=rem)).strftime("%d.%m.%Y %H:%M")
        lines.extend([t("remaining", format_dur(rem)), t("eta", end_dt)])
    return "\n".join(lines)

def do_reset(target):
    """Сброс счётчика ТО. Сеть читаем до лока, запись — в mutate_state().
    Resets a maintenance counter; network reads happen before the lock."""
    if target not in ("nozzle", "filter", "ptfe", "cutter"):
        return ""
    totals = get_history_totals()
    meters = totals.get("total_filament_used", 0) / 1000.0
    hours = totals.get("total_time", 0) / 3600.0
    result = {"msg": ""}

    def _apply(st):
        if target == "nozzle":
            st["nozzle_start_meters"], st["nozzle_warned"] = meters, False
            result["msg"] = t("reset_nozzle", meters)
        elif target == "filter":
            st["filter_start_hours"], st["filter_warned"] = hours, False
            result["msg"] = t("reset_filter", hours)
        elif target == "ptfe":
            st["ptfe_start_meters"], st["ptfe_warned"] = meters, False
            result["msg"] = t("reset_ptfe", meters)
        else:
            st["cutter_cuts"], st["cutter_warned"] = 0, False
            result["msg"] = t("reset_cutter")

    mutate_state(_apply)
    return result["msg"]

def _lube_worker(axis):
    try:
        st = get_print_stats()
        if st.get("print_stats", {}).get("state") in ("printing", "paused"):
            tg_send(t("axis_move_busy"), MAINT_KB)
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
        MOONRAKER_CLIENT.gcode("\n".join(cmds), TIMEOUT_GCODE_LONG)
        tg_send("✅ Ось %s успешно прогнана!" % ax_u, MAINT_KB)
    except Exception as e:
        log.exception("Ошибка движения оси %s", axis.upper())
        tg_send("⚠️ Ошибка движения оси %s: %s" % (axis.upper(), e), MAINT_KB)

# ==================== 10. ОБРАБОТКА КОМАНД (ОТДЕЛЬНЫЙ ПОТОК) ====================
def check_commands():
    global last_update_id
    log.info("✅ check_commands thread started")
    while True:
        try:
            res = tg_api("getUpdates", {"offset": last_update_id + 1, "timeout": 25},
                         timeout=TIMEOUT_TG_LONG).get("result", [])
            if not isinstance(res, list) or not res:
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
                out(msg.get("text") or t("select_spool"), build_spool_picker(pref, b_cb, int(pg_s)))
        elif data == "lang_en":
            _set_language("en")
            tg_answer_cb(cb_id, "Language switched to English")
            out(t("panel_title"), MAIN_KB)
            return
        elif data == "lang_ru":
            _set_language("ru")
            tg_answer_cb(cb_id, "Язык изменён на русский")
            out(t("panel_title"), MAIN_KB)
            return        
        elif data == "btn_status":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); send_usb_photo(format_status(), MAIN_KB)
        elif data == "btn_stats":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(format_stats(), MAIN_KB)
        elif data == "btn_spools":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); view, kb = build_spools_view(); out(view, kb)
        elif data == "sp_add":
            awaiting_spool = True; edit_spool_id = None; tg_answer_cb(cb_id)
            out(t("new_spool_prompt"), {"inline_keyboard": [[{"text": t("cancel"), "callback_data": "btn_spools"}]]})
        elif data == "sp_edit_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: view, kb = build_spools_view(); out(view, kb)
            else: out(t("edit_spool_prompt"), build_spool_picker("sp_edit_", "btn_spools", 0))
        elif data.startswith("sp_edit_"):
            sid = int(data.split("_").pop()); edit_spool_id = sid; awaiting_spool = False; tg_answer_cb(cb_id)
            sp = next((s for s in (load_state().get("spools") or []) if s.get("id") == sid), None)
            if sp: out(t("edit_spool_detail", esc(sp_title(sp)), sp["rem_g"]), {"inline_keyboard": [[{"text": t("cancel"), "callback_data": "btn_spools"}]]})
        elif data == "sp_ov_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            slots, _, _, _ = get_cfs_slots()
            rows = [[{"text": t("slot_button", let, hex_to_ru_name(slots[let]["hex"])), "callback_data": "sp_ovslot_%s" % let}] for let in ("A", "B", "C", "D") if let in slots]
            rows.append([{"text": t("back"), "callback_data": "btn_spools"}])
            out(t("refine_cfs"), {"inline_keyboard": rows})
        elif data.startswith("sp_ovslot_"):
            let = data.split("_").pop(); tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: view, kb = build_spools_view(); out(view, kb)
            else:
                slots, _, _, _ = get_cfs_slots()
                p_mat = slots.get(let, {}).get("mat") if let in slots else None
                title_ov = t("select_current") if let == "SINGLE" else t("select_slot", let)
                out(title_ov, build_spool_picker("sp_ovset_%s_" % let, "btn_spools" if let == "SINGLE" else "sp_ov_menu", 0, p_mat))
        elif data.startswith("sp_ovset_"):
            nums = [p for p in data.split("_") if p.isdigit()]
            let = data.split("_")[2]
            if not nums:
                tg_answer_cb(cb_id, t("error_short"))
                return
            sid = int(nums.pop())
            slots = get_cfs_slots()[0] if let != "SINGLE" else {}

            def _apply(st):
                if let == "SINGLE":
                    st["single_spool_id"] = sid
                    return True
                if let not in slots:
                    return False
                overrides = st.get("slot_overrides") or {}
                overrides[let] = {"id": sid, "hex": slots[let]["hex"], "mat": slots[let]["mat"]}
                st["slot_overrides"] = overrides
                return True

            if mutate_state(_apply):
                tg_answer_cb(cb_id, t("spool_selected") if let == "SINGLE" else t("slot_refined", let))
            view, kb = build_spools_view(); out(view, kb)
        elif data == "sp_del_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: view, kb = build_spools_view(); out(view, kb)
            else: out(t("delete_prompt"), build_spool_picker("sp_askdel_", "btn_spools", 0))
        elif data.startswith("sp_askdel_"):
            sid = int(data.split("_").pop()); tg_answer_cb(cb_id)
            sp = next((s for s in (load_state().get("spools") or []) if s.get("id") == sid), None)
            if sp: out(t("delete_confirm", esc(sp_title(sp)), sp["rem_g"]), {"inline_keyboard": [[{"text": t("delete_yes"), "callback_data": "sp_confdel_%d" % sid}, {"text": t("cancel"), "callback_data": "btn_spools"}]]})
        elif data.startswith("sp_confdel_"):
            sid = int(data.split("_").pop())

            def _apply(st):
                st["spools"] = [s for s in (st.get("spools") or []) if s.get("id") != sid]
                overrides = st.get("slot_overrides") or {}
                for key in [k for k, v in overrides.items() if (v or {}).get("id") == sid]:
                    del overrides[key]
                st["slot_overrides"] = overrides
                if st.get("single_spool_id") == sid:
                    st["single_spool_id"] = None

            mutate_state(_apply)
            tg_answer_cb(cb_id, t("deleted")); view, kb = build_spools_view(); out(view, kb)
        elif data == "btn_cons": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(format_consumables(), MAIN_KB)
        elif data == "btn_maint": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(t("consumables_view"), MAINT_KB)
        elif data == "btn_resets": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(t("reset_counters") + "\n" + format_consumables(), RESET_KB)
        elif data == "btn_back": awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id); out(t("panel_title"), MAIN_KB)
        elif data.startswith("rst_"): r_txt = do_reset(data[4:]); tg_answer_cb(cb_id, r_txt); out(t("reset_counters") + "\n" + format_consumables(), RESET_KB)
        elif data.startswith("move_"): axis = data[5:]; tg_answer_cb(cb_id, t("moving_axis", axis.upper())); threading.Thread(target=_lube_worker, args=(axis,), daemon=True).start()
        return
    msg = item.get("message", {})
    if str(msg.get("chat", {}).get("id", "")) != str(CHAT_ID): return
    raw_txt = (msg.get("text") or "").strip(); txt = raw_txt.lower()
    if edit_spool_id is not None and not txt.startswith("/"):
        parts = raw_txt.split()
        new_g = None
        try:
            new_g = round(max(0.0, float(raw_txt.replace(",", "."))), 1)
        except Exception:
            try:
                new_g = round(max(0.0, float(parts[-1].replace(",", "."))), 1)
                parts.pop(-1)
            except Exception:
                new_g = None
        new_mat = None
        if parts and parts[0].upper() in ("PLA", "PETG", "TPU", "ABS", "ASA", "PA-CF", "PC", "NYLON", "HIPS"):
            new_mat = parts.pop(0).upper()
        new_name = " ".join(parts).strip()
        updated = {}
        target_id = edit_spool_id

        def _apply(st):
            sp = next((s for s in (st.get("spools") or []) if s.get("id") == target_id), None)
            if not sp:
                return False
            if new_g is not None:
                sp["rem_g"] = new_g
            if new_mat:
                sp["mat"] = new_mat
            if new_name:
                sp["name"] = new_name[:1].upper() + new_name[1:]
                sp["group"] = normalize_color_group(sp["name"])
            updated.update(sp)
            return True

        if mutate_state(_apply):
            edit_spool_id = None
            view, kb = build_spools_view()
            tg_send(t("spool_updated", esc(sp_title(updated)), updated.get("rem_g", 0)) + view, kb)
            return
    if awaiting_spool and not txt.startswith("/"):
        parts = raw_txt.split()
        if len(parts) >= 3:
            try:
                mat = parts.pop(0).upper()
                grams = float(parts.pop(-1).replace(",", "."))
                if grams <= 0 or grams > 10000:
                    raise ValueError("bad grams")
                raw_name = " ".join(parts).strip()
                cname = raw_name[:1].upper() + raw_name[1:] if raw_name else t("unknown")
                grp = normalize_color_group(cname)
                created = {}

                def _apply(st):
                    spools = st.get("spools") or []
                    new_id = max([s.get("id", 0) for s in spools], default=0) + 1
                    new_sp = {"id": new_id, "mat": mat, "name": cname, "group": grp,
                              "hex": "", "rem_g": grams}
                    spools.append(new_sp)
                    st["spools"] = spools
                    created.update(new_sp)

                mutate_state(_apply)
                awaiting_spool = False
                view, kb = build_spools_view()
                tg_send(t("spool_added", esc(sp_title(created)), grams) + view, kb)
                return
            except Exception:
                pass
        tg_send(t("spool_format"), {"inline_keyboard": [[{"text": t("cancel"), "callback_data": "btn_spools"}]]})
        return
        tg_send(t("spool_format"), {"inline_keyboard": [[{"text": t("cancel"), "callback_data": "btn_spools"}]]}); return
    if txt.startswith("/stats"): tg_send(format_stats(), MAIN_KB)
    elif txt.startswith("/consumables"): tg_send(format_consumables(), MAIN_KB)
    elif txt.startswith("/status"): send_usb_photo(format_status(), MAIN_KB)
    elif txt.startswith("/spools"): view, kb = build_spools_view(); tg_send(view, kb)
    elif txt.startswith(("/start", "/help")): awaiting_spool = False; edit_spool_id = None; tg_send(t("panel_title"), MAIN_KB)

# ==================== 11. WEB-СЕРВЕР MINI APP ====================
WEBAPP_FILE = os.path.join(BASE_DIR, "webapp.html")
CLOUDFLARED_BIN = os.path.join(BASE_DIR, "cloudflared")
PUBLIC_WEBAPP_URL = ""
WEBAPP_SECRET = uuid.uuid4().hex
_hist_cache = {"ts": 0.0, "totals": {}, "h7": 0.0, "h30": 0.0, "ok": 0, "hlen": 0, "d_lbl": [], "d_hrs": []}

ARCHIVE_LIMIT = 12
FORECAST_MIN_JOBS = 3
FORECAST_WINDOW_DAYS = 30


def _job_display_name(fname):
    """Короткое имя модели из имени файла.
    Short model name derived from the G-code file name."""
    base = os.path.basename(str(fname or "")).strip()
    for ext in (".gcode", ".gco", ".g"):
        if base.lower().endswith(ext):
            base = base[:-len(ext)]
            break
    base = base.replace("_", " ").strip()
    return base[:48] or str(fname or "")


def build_archive(history, limit=ARCHIVE_LIMIT):
    """Компактный архив последних моделей: без тяжёлых полей истории.
    Compact archive of recent models without heavy history fields."""
    rows = []
    for job in history or []:
        ts = job.get("start_time") or 0
        if ts <= 0:
            continue
        meta = job.get("metadata") or {}
        fname = str(job.get("filename") or meta.get("filename") or "")
        dur = job.get("print_duration") or 0
        used_mm = job.get("filament_used") or 0
        try:
            grams = (used_mm / 1000.0) * density_for(meta, fname) if used_mm > 0 else 0.0
            _, direct, _, _, _ = calc_costs(meta, dur, used_mm)
        except Exception:
            grams, direct = 0.0, 0.0
        rows.append({
            "name": _job_display_name(fname),
            "file": fname,
            "status": str(job.get("status") or ""),
            "ts": int(ts),
            "duration": int(dur),
            "grams": round(grams, 1),
            "cost": round(direct),
        })
    rows.sort(key=lambda x: x["ts"], reverse=True)
    counts = {}
    for row in rows:
        key = row["name"].lower()
        counts[key] = counts.get(key, 0) + 1
    out, used = [], set()
    for row in rows:
        key = row["name"].lower()
        if key in used:
            continue
        used.add(key)
        row["repeat"] = counts.get(key, 1)
        out.append(row)
        if len(out) >= limit:
            break
    return out


def _get_cached_stats():
    if time.time() - _hist_cache["ts"] < 60 and _hist_cache["totals"]:
        return _hist_cache
    totals = get_history_totals()
    history = get_history_list(300)
    h_7d, h_30d, _, _, _, ok_all, _ = analyze_history(history)
    # Темп эксплуатации за окно прогноза: часы и метры в день.
    # Usage rate for the forecast window: hours and meters per day.
    now_ts = time.time()
    window = [j for j in history
              if (j.get("start_time") or 0) >= now_ts - FORECAST_WINDOW_DAYS * 86400]
    enough = len(window) >= FORECAST_MIN_JOBS
    if enough:
        first_ts = min((j.get("start_time") or now_ts) for j in window)
        span_days = max(1.0, min(float(FORECAST_WINDOW_DAYS), (now_ts - first_ts) / 86400.0))
        rate_h = sum((j.get("print_duration") or 0) for j in window) / span_days / 3600.0
        rate_m = sum((j.get("filament_used") or 0) for j in window) / span_days / 1000.0
    else:
        span_days, rate_h, rate_m = 0.0, 0.0, 0.0
    tz = int(cfg("timezone_offset"))
    now_local = datetime.now(timezone.utc) + timedelta(hours=tz)
    days_labels, days_hours = [], []
    day_names = tuple(t("days_short").split(","))
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for shift in range(6, -1, -1):
        d_dt = now_local - timedelta(days=shift)
        days_labels.append("%s %02d" % (day_names[d_dt.weekday()], d_dt.day))
        d_start = d_dt.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=tz)
        ts0 = (d_start - epoch).total_seconds()
        sec_sum = sum((j.get("print_duration") or 0) for j in history if ts0 <= (j.get("start_time") or 0) < ts0 + 86400)
        days_hours.append(round(sec_sum / 3600.0, 2))
    _hist_cache.update({
        "ts": time.time(), "totals": totals, "h7": h_7d, "h30": h_30d,
        "ok": ok_all, "hlen": len(history), "d_lbl": days_labels, "d_hrs": days_hours,
        "rate_h": rate_h, "rate_m": rate_m, "jobs30": len(window),
        "enough30": enough, "span_days": round(span_days, 1),
        "archive": build_archive(history),
    })
    return _hist_cache

def build_maintenance_forecast(st, cs):
    """Остаток ресурса ТО в днях и дата замены по темпу печати.
    Maintenance life left in days and an estimated replacement date."""
    enough = bool(cs.get("enough30"))
    rate_h = float(cs.get("rate_h") or 0)
    rate_m = float(cs.get("rate_m") or 0)
    totals = cs.get("totals") or {}
    h_tot = (totals.get("total_time", 0) or 0) / 3600.0
    m_tot = (totals.get("total_filament_used", 0) or 0) / 1000.0
    tz = int(cfg("timezone_offset", st) or 0)
    now_local = datetime.now(timezone.utc) + timedelta(hours=tz)
    maint_left = max(0.0, (st.get("last_maint_hours", 0) + float(cfg("maint_hours", st))) - h_tot)
    nozzle_left = max(0.0, float(cfg("nozzle_meters", st))
                      - max(0.0, m_tot - st.get("nozzle_start_meters", m_tot)))
    filter_left = max(0.0, float(cfg("filter_hours", st))
                      - max(0.0, h_tot - st.get("filter_start_hours", h_tot)))
    ptfe_left = max(0.0, float(cfg("ptfe_meters", st))
                    - max(0.0, m_tot - st.get("ptfe_start_meters", m_tot)))
    cutter_left = max(0.0, float(cfg("cutter_cuts", st)) - float(st.get("cutter_cuts", 0) or 0))
    items = []
    for key, left, unit, rate in (
            ("maint", maint_left, "h", rate_h),
            ("nozzle", nozzle_left, "m", rate_m),
            ("filter", filter_left, "h", rate_h),
            ("ptfe", ptfe_left, "m", rate_m),
            ("cutter", cutter_left, "cuts", 0.0)):
        days, date = None, ""
        if enough and rate > 0 and left > 0:
            days = int(round(left / rate))
            date = (now_local + timedelta(days=days)).strftime("%Y-%m-%d")
        items.append({"key": key, "left": round(left, 1), "unit": unit,
                      "days": days, "date": date})
    return {
        "enough_data": enough,
        "rate_hours": round(rate_h, 2),
        "rate_meters": round(rate_m, 1),
        "jobs": int(cs.get("jobs30") or 0),
        "span_days": cs.get("span_days") or 0,
        "items": items,
    }


PREFLIGHT_ITEM_KEYS = {
    "maint": "preflight_item_maint",
    "nozzle": "preflight_item_nozzle",
    "filter": "preflight_item_filter",
    "ptfe": "preflight_item_ptfe",
}


def _find_backup_spool(spools, mat, group, need_g, exclude_id=None):
    """Запасная катушка того же материала/цвета с достатком граммов.
    A spare spool of the same material/color with enough grams."""
    best = None
    for sp in spools or []:
        if exclude_id is not None and sp.get("id") == exclude_id:
            continue
        if str(sp.get("mat") or "").upper() != str(mat or "").upper():
            continue
        sp_group = normalize_color_group(sp.get("group") or sp.get("name") or sp.get("hex"))
        if group and sp_group != group:
            continue
        if float(sp.get("rem_g", 0) or 0) + 0.5 < need_g:
            continue
        if best is None or float(sp.get("rem_g", 0)) < float(best.get("rem_g", 0)):
            best = sp
    if not best:
        return None
    return int(best.get("id", 0)), float(best.get("rem_g", 0))


def run_preflight(fname, meta, total_w):
    """Проверки перед печатью: пластик, тип пластика, ресурс ТО.
    Pre-flight checks: filament, filament type, maintenance life."""
    warnings = []
    st = load_state()
    slots, active_let = {}, "SINGLE"
    if HAS_CFS:
        slots, active_let, _, _ = get_cfs_slots()
    used = get_used_slots(fname, meta, slots, active_let, total_w) if total_w > 0 else []
    tracking = bool(cfg("spool_tracking", st))
    if tracking and used:
        spools = st.get("spools") or []
        for let, need in used:
            slot = slots.get(let) or {"mat": "PLA", "hex": "000000"}
            sp = resolve_slot_spool(st, let, slot.get("mat", "PLA"),
                                    slot.get("hex", "000000"), auto_create=False)
            rem = float((sp or {}).get("rem_g", 0) or 0)
            if need > 0.5 and rem + 0.5 < need:
                backup = _find_backup_spool(
                    spools, (sp or {}).get("mat") or slot.get("mat", "PLA"),
                    (sp or {}).get("group"), need - rem, (sp or {}).get("id"))
                warnings.append({"code": "low_filament",
                                 "args": [let, round(need - rem, 1), round(rem, 1),
                                          int(backup[0]) if backup else 0,
                                          round(backup[1], 1) if backup else 0]})
    raw_types = str(meta.get("filament_type") or "").strip()
    if raw_types:
        gcode_mats = [x.strip().upper() for x in raw_types.replace(",", ";").split(";") if x.strip()]
        for let, _need in used:
            if let not in slots:
                continue
            slot_mat = str(slots[let].get("mat") or "").upper()
            if slot_mat and gcode_mats and slot_mat not in gcode_mats:
                warnings.append({"code": "wrong_material",
                                 "args": [let, ", ".join(gcode_mats), slot_mat]})
    cs = _get_cached_stats()
    totals = cs.get("totals") or {}
    h_tot = (totals.get("total_time", 0) or 0) / 3600.0
    m_tot = (totals.get("total_filament_used", 0) or 0) / 1000.0
    est_h = float(meta.get("estimated_time") or 0) / 3600.0
    est_m = float(meta.get("filament_total") or 0) / 1000.0
    checks = (
        ("maint", (st.get("last_maint_hours", 0) + float(cfg("maint_hours", st))) - h_tot, est_h, "h"),
        ("nozzle", (st.get("nozzle_start_meters", m_tot) + float(cfg("nozzle_meters", st))) - m_tot, est_m, "m"),
        ("filter", (st.get("filter_start_hours", h_tot) + float(cfg("filter_hours", st))) - h_tot, est_h, "h"),
        ("ptfe", (st.get("ptfe_start_meters", m_tot) + float(cfg("ptfe_meters", st))) - m_tot, est_m, "m"),
    )
    for key, left, need, unit in checks:
        if left > 0 and need > 0 and left < need:
            warnings.append({"code": "maintenance",
                             "args": [key, round(left, 1), round(need, 1), unit]})
    return warnings


def render_preflight_warning(warn):
    """Текст одного предупреждения pre-flight на языке пользователя.
    Renders a single pre-flight warning in the user's language."""
    code = warn.get("code")
    args = warn.get("args") or []
    if code == "low_filament" and len(args) >= 5:
        let, need, rem, backup_id, backup_g = args
        text = t("preflight_low_filament", let, need, rem)
        text += "\n  " + (t("preflight_stock_yes", backup_id, backup_g)
                          if backup_id else t("preflight_stock_no"))
        return text
    if code == "wrong_material" and len(args) >= 3:
        return t("preflight_wrong_mat", args[0], args[1], args[2])
    if code == "maintenance" and len(args) >= 4:
        name = t(PREFLIGHT_ITEM_KEYS.get(args[0], "preflight_item_maint"))
        key = "preflight_maint_m" if args[3] == "m" else "preflight_maint"
        return t(key, name, args[1], args[2])
    return ""


def preflight_and_notify(fname, meta, total_w):
    """Сохраняет результат pre-flight и предупреждает в чат только при рисках.
    Stores pre-flight results and warns in the chat only when risks are found."""
    try:
        warnings = run_preflight(fname, meta, total_w)
    except Exception:
        log.exception("Pre-flight check failed / ошибка pre-flight")
        return

    def _apply(st):
        st["preflight"] = {"file": fname, "ts": time.time(), "warnings": warnings}

    mutate_state(_apply)
    if not warnings:
        log.info("Pre-flight OK: %s", fname)
        return
    lines = [t("preflight_title"), t("file", esc(fname))]
    for warn in warnings:
        text = render_preflight_warning(warn)
        if text:
            lines.append(text)
    lines.extend(["", t("preflight_note")])
    tg_send("\n".join(lines), MAIN_KB)


INITDATA_MAX_AGE = 86400  # сек / seconds: срок жизни initData


def _allowed_user_ids():
    """Разрешённые Telegram user id: владелец чата + список из secrets.ini.
    Allowed Telegram user ids: the chat owner plus the secrets.ini list."""
    ids = {str(CHAT_ID).strip()}
    for part in str(ALLOWED_USERS or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(part)
    return ids


def verify_tg_init_data(init_data):
    """Проверяет подпись initData, срок жизни и владельца.
    Validates the initData HMAC, its age and the user id."""
    if not init_data:
        return False
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        recv_hash = parsed.pop("hash", "")
        if not recv_hash:
            return False
        data_check = "\n".join("%s=%s" % (k, v) for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, recv_hash):
            log.warning("initData: подпись не совпала / bad signature")
            return False
        try:
            auth_date = int(parsed.get("auth_date") or 0)
        except (TypeError, ValueError):
            return False
        age = time.time() - auth_date
        if auth_date <= 0 or age > INITDATA_MAX_AGE:
            log.warning("initData: устарел на %d с / expired", int(age))
            return False
        if not parsed.get("user"):
            return False
        user_obj = json.loads(parsed["user"])
        return str(user_obj.get("id", "")) in _allowed_user_ids()
    except Exception as e:
        log.warning("initData: ошибка проверки / validation error: %s", e)
        return False

_meta_cache = {"key": None, "ts": 0.0, "data": {}}


def get_file_meta_cached(filename, ttl=30.0):
    """Метаданные G-code с коротким кэшем для панели.
    G-code metadata with a short cache for the panel."""
    now = time.time()
    if _meta_cache["key"] == filename and now - _meta_cache["ts"] < ttl:
        return _meta_cache["data"]
    data = get_file_meta(filename)
    _meta_cache.update({"key": filename, "ts": now, "data": data})
    return data


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
    slots, active_let = {}, "SINGLE"
    cfs_temp, cfs_hum = "?", "?"
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
                    "spool_title": sp_title(sp) if sp else t("not_assigned"),
                    "rem_g": sp["rem_g"] if sp else 0
                })
    h_tot = totals.get("total_time", 0) / 3600.0
    m_tot = totals.get("total_filament_used", 0) / 1000.0
    # Карточка текущей печати: план, факт и ETA по реальному прогрессу.
    # Current print card: plan, spent so far and ETA from real progress.
    job = None
    state = str(ps.get("state") or "unknown")
    fname = str(ps.get("filename") or "")
    total_dur_all = float(ps.get("total_duration", 0) or 0)
    print_dur_all = float(ps.get("print_duration", 0) or 0)
    if state in ("printing", "paused") and fname:
        meta = get_file_meta_cached(fname)
        progress = float(ds.get("progress", 0) or 0)
        est = float(meta.get("estimated_time") or 0)
        planned_w, planned_direct, planned_full, planned_elec, planned_depr = calc_costs(meta, est)
        thumb_rel = ""
        thumbs = meta.get("thumbnails") or []
        if thumbs:
            best_thumb = max(thumbs, key=lambda x: (x.get("width", 0) * x.get("height", 0)))
            thumb_rel = str(best_thumb.get("relative_path") or "")
        plan_g = []
        if planned_w > 0:
            for let, part_g in get_used_slots(fname, meta, slots, active_let, planned_w):
                slot = slots.get(let) or {}
                sp = resolve_slot_spool(st, let, slot.get("mat", "PLA"),
                                        slot.get("hex", "000000"), auto_create=False)
                plan_g.append({
                    "slot": let,
                    "mat": (sp or {}).get("mat") or slot.get("mat", ""),
                    "g": round(part_g, 1),
                    "rem_g": round(float((sp or {}).get("rem_g", 0) or 0), 1),
                })
        eta_ts = None
        if progress >= 0.02 and print_dur_all > 0:
            eta_ts = int(time.time() + print_dur_all * (1.0 - progress) / progress)
        preflight = st.get("preflight") or {}
        warnings = preflight.get("warnings") if preflight.get("file") == fname else []
        job = {
            "active": True, "name": _job_display_name(fname), "file": fname, "state": state,
            "progress": round(progress * 100, 1),
            "elapsed": int(print_dur_all), "total_duration": int(total_dur_all),
            "eta_ts": eta_ts,
            "thumb": ("/api/thumb?path=" + urllib.parse.quote(thumb_rel)) if thumb_rel else "",
            "plan_g": plan_g,
            "cost": {
                "planned": {
                    "filament": round(planned_w / 1000.0 * price_for(meta, fname)),
                    "elec": round(planned_elec), "depr": round(planned_depr),
                    "total": round(planned_full),
                },
                "spent": {"total": round(planned_full * progress)},
            },
            "preflight": warnings or [],
        }
    print_ratio = round(print_dur_all * 100.0 / total_dur_all, 1) if total_dur_all > 0 else None
    prep_seconds = int(max(0.0, total_dur_all - print_dur_all))
    mats_m = st.get("materials_meters") or {}
    if mats_m:
        fil_rub = 0.0
        for m_type, meters in mats_m.items():
            dens = DENSITY_G_PER_M.get(m_type, G_PER_M_DEFAULT)
            fil_rub += ((meters * dens) / 1000.0) * cfg_price(m_type, st)
        tracked_m = sum(mats_m.values())
        if m_tot > tracked_m:
            fil_rub += (((m_tot - tracked_m) * G_PER_M_DEFAULT) / 1000.0) * float(cfg("price_default", st))
    else:
        fil_rub = ((m_tot * G_PER_M_DEFAULT) / 1000.0) * float(cfg("price_default", st))
    elec = h_tot * float(cfg("power_kw", st)) * float(cfg("elec_price", st))
    direct = fil_rub + elec
    depr = h_tot * float(cfg("depr_hour", st))
    ext = ps_all.get("extruder") or {}
    bed = ps_all.get("heater_bed") or {}
    return {
        "version": VERSION,
        "has_cfs": HAS_CFS, "cfs_temp": cfs_temp, "cfs_hum": cfs_hum, "slots": slots_out,
        "single_spool_id": st.get("single_spool_id"),
        "spools": sorted([s for s in (st.get("spools") or []) if s.get("rem_g", 0) > 0], key=lambda x: x.get("id", 0)),
        "settings": {
            "language": get_language(),
            "spool_tracking": bool(cfg("spool_tracking", st)),
            "timezone_offset": cfg("timezone_offset", st),
            "digest_hour": cfg("digest_hour", st),
            "maint_hours": cfg("maint_hours", st),
            "nozzle_meters": cfg("nozzle_meters", st),
            "filter_hours": cfg("filter_hours", st),
            "ptfe_meters": cfg("ptfe_meters", st),
            "cutter_cuts": cfg("cutter_cuts", st),
            "cfs_humidity": cfg("cfs_humidity", st),
            "elec_price": cfg("elec_price", st),
            "power_kw": cfg("power_kw", st),
            "depr_hour": cfg("depr_hour", st),
            "prices": cfg("prices", st),
            "currency": currency_code(),
            "currency_symbol": CURRENCY_SYMBOLS[currency_code()],
        },
        "job": job,
        "maintenance_forecast": build_maintenance_forecast(st, cs),
        "archive": cs.get("archive") or [],
        "efficiency": {"print_ratio": print_ratio, "prep_seconds": prep_seconds},
        "printer": {
            "state": state, "filename": fname,
            "progress": round((ds.get("progress", 0) or 0) * 100, 1),
            "duration": int(print_dur_all),
            "total_duration": int(total_dur_all),
            "prep_seconds": prep_seconds,
            "print_ratio": print_ratio,
            "layer": info.get("current_layer"), "total_layer": info.get("total_layer"),
            "nozzle_t": round(ext.get("temperature", 0) or 0, 1),
            "nozzle_target": round(ext.get("target", 0) or 0),
            "bed_t": round(bed.get("temperature", 0) or 0, 1),
            "bed_target": round(bed.get("target", 0) or 0)
        },
        "consumables": {
            "maint_left_h": round(max(0.0, (st.get("last_maint_hours", 0) + float(cfg("maint_hours", st))) - h_tot), 1),
            "maint_max_h": float(cfg("maint_hours", st)),
            "nozzle_m": round(max(0.0, m_tot - st.get("nozzle_start_meters", m_tot)), 1), "nozzle_max": float(cfg("nozzle_meters", st)),
            "filter_h": round(max(0.0, h_tot - st.get("filter_start_hours", h_tot)), 1), "filter_max": float(cfg("filter_hours", st)),
            "ptfe_m": round(max(0.0, m_tot - st.get("ptfe_start_meters", m_tot)), 1), "ptfe_max": float(cfg("ptfe_meters", st)),
            "cutter": st.get("cutter_cuts", 0), "cutter_max": int(cfg("cutter_cuts", st))
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

def _update_bases():
    return list(UPDATE_BASES)


def _http_get(url, timeout=TIMEOUT_OTA_HTTP):
    """GET с таймаутом и явным User-Agent.
    Plain GET with a timeout and an explicit User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": "klipper-tg-bot/%s" % VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_first(path, timeout=TIMEOUT_OTA_HTTP):
    """Скачивает path с первого ответившего зеркала.
    Downloads path from the first mirror that responds."""
    errors = []
    for base in _update_bases():
        url = "%s/%s" % (base, path.lstrip("/"))
        try:
            data = _http_get(url, timeout)
            if data:
                return data, url
        except Exception as e:
            errors.append("%s: %s" % (url, e))
            log.warning("Зеркало недоступно / mirror failed: %s", errors[-1])
    return None, "; ".join(errors) or "no update sources"


def _parse_version(v):
    try:
        return [int(x) for x in str(v).lstrip("vV").strip().split(".")]
    except Exception:
        return [0]


def _sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _safe_relpath(name):
    """Разрешает относительные пути внутри BASE_DIR, включая подкаталоги.
    Allows relative paths inside BASE_DIR, subdirectories included."""
    raw = str(name or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/"):
        return None
    parts = raw.split("/")
    if len(parts) > 4 or any(not part for part in parts):
        return None
    for part in parts:
        if part.startswith(".") or not re.match(r"^[A-Za-z0-9_.-]+$", part):
            return None
    return "/".join(parts)


def _load_manifest(raw):
    """Разбирает и валидирует manifest.json.
    Parses and validates manifest.json."""
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be an object")
    version = str(data.get("version") or "").strip()
    files = data.get("files") or {}
    if not version or not isinstance(files, dict) or not files:
        raise ValueError("manifest must contain version and files")
    clean = {}
    for name, meta in files.items():
        safe = _safe_relpath(name)
        if not safe:
            raise ValueError("unsafe file name in manifest: %r" % name)
        meta = meta or {}
        digest = str(meta.get("sha256") or "").strip().lower()
        if not re.match(r"^[0-9a-f]{64}$", digest):
            raise ValueError("bad sha256 for %s" % name)
        clean[safe] = {"sha256": digest, "size": int(meta.get("size") or 0)}
    return {
        "version": version,
        "min_version": str(data.get("min_version") or "0"),
        "files": clean,
    }


_last_check = {"error": "", "version": "", "ts": 0.0}


def _remote_version_from_code(raw):
    """Резервный путь: версия из исходника, если version.txt нет.
    Fallback: read VERSION from the source when version.txt is missing."""
    try:
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', raw.decode("utf-8", "ignore"),
                      re.MULTILINE)
        return m.group(1) if m else ""
    except Exception:
        return ""


def check_for_updates():
    """Проверка обновлений: version.txt (fallback — VERSION в .py), затем манифест.
    Update check: version.txt (fallback to VERSION in .py), then the manifest.
    Причина сбоя сохраняется в _last_check["error"] и показывается в панели."""
    _last_check["error"] = ""
    _last_check["ts"] = time.time()
    errors = []
    best_version, best_base = None, None
    for base in _update_bases():
        stamp = int(time.time())
        remote = ""
        try:
            raw = _http_get("%s/version.txt?v=%d" % (base, stamp), TIMEOUT_OTA_VERSION)
            remote = raw.decode("utf-8", "ignore").strip().splitlines()[0].strip() if raw else ""
        except Exception as e:
            errors.append("version.txt @ %s: %s" % (base, e))
        if not remote:
            # Старые репозитории без version.txt / repositories without version.txt.
            try:
                raw = _http_get("%s/printer_tg_bot.py?v=%d" % (base, stamp), TIMEOUT_OTA_CODE)
                remote = _remote_version_from_code(raw)
            except Exception as e:
                errors.append("printer_tg_bot.py @ %s: %s" % (base, e))
        if not remote:
            continue
        if best_version is None or _parse_version(remote) > _parse_version(best_version):
            best_version, best_base = remote, base
    if not best_version:
        _last_check["error"] = "источники недоступны / sources unavailable: " + "; ".join(errors[:2])
        log.warning("OTA: %s", _last_check["error"])
        return None
    _last_check["version"] = best_version
    if _parse_version(best_version) <= _parse_version(VERSION):
        log.info("Обновлений нет: %s (локально %s) / up to date", best_version, VERSION)
        return None
    raw_manifest, manifest_src = _fetch_first("manifest.json?v=%s" % best_version, TIMEOUT_OTA_MANIFEST)
    if not raw_manifest:
        _last_check["error"] = "manifest.json: %s" % manifest_src
        log.warning("OTA: %s", _last_check["error"])
        return None
    try:
        manifest = _load_manifest(raw_manifest)
    except Exception as e:
        _last_check["error"] = "manifest.json: %s" % e
        log.warning("OTA: битый manifest.json / bad manifest: %s", e)
        return None
    if manifest["version"] != best_version:
        _last_check["error"] = "манифест %s != version.txt %s" % (manifest["version"], best_version)
        log.warning("OTA: %s", _last_check["error"])
        return None
    if _parse_version(VERSION) < _parse_version(manifest["min_version"]):
        _last_check["error"] = "требуется версия не ниже %s" % manifest["min_version"]
        log.warning("OTA: %s", _last_check["error"])
        return None
    log.info("Доступно обновление: %s -> %s (источник %s)",
             VERSION, manifest["version"], best_base)
    return {"version": manifest["version"], "manifest": manifest}


_update_lock = threading.Lock()
_update_running = [False]
_update_stage = {"stage": "idle", "message": ""}


def _update_set_stage(stage, message=""):
    """Фиксирует текущий этап обновления для панели и лога.
    Records the current update stage for the panel and the log."""
    _update_stage["stage"] = stage
    _update_stage["message"] = message
    log.info("OTA stage=%s %s", stage, message)


def _update_start_message(text):
    """Отправляет первое сообщение об обновлении и возвращает его id.
    Sends the first update message and returns its message id."""
    res = tg_api("sendMessage", {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"})
    try:
        return res.get("result", {}).get("message_id")
    except Exception:
        return None


def _update_edit(msg_id, text):
    """Редактирует сообщение о ходе обновления (или отправляет новое).
    Edits the update progress message, or sends a new one."""
    try:
        if msg_id:
            tg_edit(msg_id, text)
        else:
            tg_send(text)
    except Exception:
        pass


def _download_verified(name, meta, dest):
    """Скачивает файл и сверяет sha256, перебирая все зеркала.
    Downloads a file and verifies sha256, trying every mirror."""
    last_err = None
    for base in _update_bases():
        url = "%s/%s?v=%s" % (base, name, meta.get("sha256", "")[:12])
        try:
            data = _http_get(url, TIMEOUT_OTA_DOWNLOAD)
        except Exception as e:
            last_err = "%s: %s" % (url, e)
            continue
        if meta.get("size") and len(data) != meta["size"]:
            last_err = "%s: size %d != %d" % (url, len(data), meta["size"])
            continue
        if _sha256_hex(data) != meta["sha256"]:
            last_err = "%s: sha256 mismatch" % url
            log.error("Хеш не совпал / hash mismatch: %s", last_err)
            continue
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return True
    log.error("Не удалось скачать %s / download failed: %s", name, last_err)
    return False


def apply_update(update_info):
    """Поэтапное обновление с уведомлениями в чат.
    Staged update with progress notifications in the chat."""
    with _update_lock:
        if _update_running[0]:
            log.warning("Обновление уже выполняется / update already running")
            return
        _update_running[0] = True

    manifest = update_info.get("manifest") or {}
    files = manifest.get("files") or {}
    new_version = str(manifest.get("version") or update_info.get("version") or "")
    replaced = []
    msg_id = None
    stage = "prepare"
    try:
        if not files:
            raise RuntimeError("empty manifest")

        msg_id = _update_start_message(t("update_started", VERSION, new_version))
        _update_set_stage(stage)

        shutil.rmtree(UPDATE_STAGING, ignore_errors=True)
        shutil.rmtree(UPDATE_BACKUP, ignore_errors=True)
        os.makedirs(UPDATE_STAGING)
        os.makedirs(UPDATE_BACKUP)

        # 1. Скачивание / download.
        total = len(files)
        for index, (name, meta) in enumerate(files.items(), 1):
            stage = "download"
            _update_set_stage(stage, name)
            _update_edit(msg_id, t("update_stage_download", index, total, name))
            if not _download_verified(name, meta, os.path.join(UPDATE_STAGING, name)):
                raise RuntimeError("не удалось скачать %s" % name)

        # 2. Проверка целостности / integrity check.
        stage = "verify"
        _update_set_stage(stage)
        _update_edit(msg_id, t("update_stage_verify"))
        for name in files:
            path = os.path.join(UPDATE_STAGING, name)
            if name.endswith(".py"):
                with open(path, "r", encoding="utf-8") as f:
                    compile(f.read(), path, "exec")
            elif name.endswith(".html"):
                with open(path, "r", encoding="utf-8") as f:
                    if "</html>" not in f.read():
                        raise RuntimeError("webapp.html повреждён")

        # 3. Резервная копия / backup.
        stage = "backup"
        _update_set_stage(stage)
        _update_edit(msg_id, t("update_stage_backup"))
        for name in files:
            current = os.path.join(BASE_DIR, name)
            if os.path.exists(current):
                backup = os.path.join(UPDATE_BACKUP, name)
                os.makedirs(os.path.dirname(backup) or UPDATE_BACKUP, exist_ok=True)
                shutil.copyfile(current, backup)

        # 4. Применение / apply.
        stage = "apply"
        _update_set_stage(stage)
        _update_edit(msg_id, t("update_stage_apply"))
        for name in files:
            dest = os.path.join(BASE_DIR, name)
            os.makedirs(os.path.dirname(dest) or BASE_DIR, exist_ok=True)
            os.replace(os.path.join(UPDATE_STAGING, name), dest)
            replaced.append(name)
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        # 5. Перезапуск / restart.
        stage = "restart"
        _update_set_stage(stage)
        _update_edit(msg_id, t("update_stage_restart", new_version))
        try:
            with open(PENDING_UPDATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"from": VERSION, "to": new_version}, f)
        except OSError:
            pass
        log.info("Обновление применено: %s -> %s (%s)",
                 VERSION, new_version, ", ".join(replaced))
        shutil.rmtree(UPDATE_STAGING, ignore_errors=True)
        shutil.rmtree(UPDATE_BACKUP, ignore_errors=True)
        time.sleep(2)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log.exception("Ошибка обновления / update failed: %s", e)
        restored = []
        for name in replaced:
            backup = os.path.join(UPDATE_BACKUP, name)
            if os.path.exists(backup):
                try:
                    os.replace(backup, os.path.join(BASE_DIR, name))
                    restored.append(name)
                except OSError:
                    pass
        shutil.rmtree(UPDATE_STAGING, ignore_errors=True)
        text = t("update_error", esc(str(e)))
        if restored:
            text += "\n" + t("update_rollback", ", ".join(restored))
        _update_edit(msg_id, text)
        _update_set_stage("failed", stage)
    finally:
        with _update_lock:
            _update_running[0] = False


def _confirm_pending_update():
    """Сообщает об успешном обновлении после перезапуска.
    Reports a successful update after the restart."""
    if not os.path.exists(PENDING_UPDATE_FILE):
        return
    try:
        with open(PENDING_UPDATE_FILE, "r", encoding="utf-8") as f:
            info = json.load(f)
        os.remove(PENDING_UPDATE_FILE)
    except Exception as e:
        log.warning("Не удалось прочитать флаг обновления / pending update: %s", e)
        return
    try:
        tg_send(t("update_done", info.get("from", "?"), info.get("to", VERSION), VERSION), MAIN_KB)
    except Exception:
        pass


# Кэш ответа /api/state: панель открывается мгновенно, а обновление идёт в
# фоне и не блокирует HTTP-поток запросами к Moonraker.
# /api/state cache: the panel opens instantly while a background thread
# refreshes it, so HTTP requests never wait for Moonraker.
PANEL_CACHE_TTL = 5.0
_panel_cache = {"ts": 0.0, "data": None}
_panel_lock = threading.Lock()

# Кэш превью в оперативной памяти: без base64 в printer_state.json.
# Thumbnail cache in RAM: never base64 inside printer_state.json.
THUMB_CACHE_MAX_ITEMS = 6
THUMB_CACHE_MAX_BYTES = 2 * 1024 * 1024
_thumb_cache = {"order": [], "items": {}, "bytes": 0}


def _thumb_cache_get(key):
    item = _thumb_cache["items"].get(key)
    if not item:
        return None
    try:
        _thumb_cache["order"].remove(key)
    except ValueError:
        pass
    _thumb_cache["order"].append(key)
    return item


def _thumb_cache_put(key, data):
    if len(data) > THUMB_CACHE_MAX_BYTES:
        return
    old = _thumb_cache["items"].pop(key, None)
    if old:
        _thumb_cache["bytes"] -= len(old)
        try:
            _thumb_cache["order"].remove(key)
        except ValueError:
            pass
    _thumb_cache["items"][key] = data
    _thumb_cache["order"].append(key)
    _thumb_cache["bytes"] += len(data)
    while _thumb_cache["order"] and (
            len(_thumb_cache["order"]) > THUMB_CACHE_MAX_ITEMS
            or _thumb_cache["bytes"] > THUMB_CACHE_MAX_BYTES):
        old_key = _thumb_cache["order"].pop(0)
        old_data = _thumb_cache["items"].pop(old_key, None)
        if old_data:
            _thumb_cache["bytes"] -= len(old_data)


def _safe_thumb_path(path):
    """Разрешает только превью внутри каталога gcodes.
    Allows only thumbnails inside the gcodes directory."""
    rel = str(path or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    if parts[0] not in (".thumbs", ".thumbnails", "thumbnails"):
        return None
    return "/".join(parts)


def _refresh_panel_cache():
    data = build_webapp_payload()
    with _panel_lock:
        _panel_cache["data"] = data
        _panel_cache["ts"] = time.time()
    return data


def get_panel_payload():
    """Снимок панели из кэша (первый запрос строит его синхронно).
    Panel snapshot from cache; the very first request builds it synchronously."""
    with _panel_lock:
        data = _panel_cache["data"]
        ts = _panel_cache["ts"]
    if data is None:
        data = _refresh_panel_cache()
        with _panel_lock:
            ts = _panel_cache["ts"]
    age = max(0.0, time.time() - (ts or 0.0))
    out = dict(data)
    out["cached_age"] = round(age, 1)
    out["stale"] = age > PANEL_CACHE_TTL * 3
    return out


def _panel_worker():
    """Обновляет кэш панели в фоне / refreshes the panel cache in background."""
    while True:
        try:
            _refresh_panel_cache()
        except Exception:
            log.exception("Не удалось обновить кэш панели / panel cache refresh failed")
        time.sleep(PANEL_CACHE_TTL)


def _env_worker():
    """Определяет окружение (камера, CFS) вне HTTP-потоков.
    Detects the environment (camera, CFS) outside HTTP threads."""
    while True:
        try:
            if not CAM_SNAPSHOT_URL or not HAS_CFS:
                detect_environment()
        except Exception:
            log.exception("detect_environment failed")
        time.sleep(60)


def start_background_workers():
    """Запускает фоновые воркеры панели и окружения (один раз).
    Starts the panel and environment workers once."""
    for target in (_panel_worker, _env_worker):
        threading.Thread(target=target, daemon=True).start()


class WebAppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _check_auth(self, params):
        """Только секрет панели или подписанный initData.
        Referer/Origin не считаются доверенными. Ключ и initData принимаются
        из заголовков (приоритет) или из query — для совместимости.
        Only the panel secret or signed initData is accepted;
        Referer/Origin headers are untrusted and ignored. The key and initData
        come from headers (preferred) or the query string (compatibility)."""
        key = str(self.headers.get("X-App-Key") or params.get("key") or "")
        if WEBAPP_SECRET and key and hmac.compare_digest(key, str(WEBAPP_SECRET)):
            return True
        init_data = str(self.headers.get("X-Tg-Init-Data") or params.get("initData") or "")
        return verify_tg_init_data(init_data)

    def _handle_api(self, path, params):
        try:
            if path == "/api/state":
                self._send_json(200, get_panel_payload())
            elif path == "/api/spool/delta":
                self._handle_spool_delta(params)
            elif path == "/api/settings/save":
                self._handle_settings_save(params)
            elif path == "/api/spool/save":
                self._handle_spool_save(params)
            elif path == "/api/spool/delete":
                self._handle_spool_delete(params)
            elif path == "/api/slot/assign":
                self._handle_slot_assign(params)
            elif path == "/api/reset":
                do_reset(str(params.get("target") or ""))
                self._send_json(200, {"ok": True})
            elif path == "/api/move":
                axis = str(params.get("axis") or "").lower()
                if axis in ("x", "y", "z"):
                    threading.Thread(target=_lube_worker, args=(axis,), daemon=True).start()
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(400, {"error": "bad axis"})
            elif path == "/api/printer/cmd":
                self._handle_printer_cmd(params)
            elif path == "/api/light/toggle":
                self._handle_light_toggle()
            elif path == "/api/settings/language":
                self._handle_language(params)
            elif path == "/api/update/status":
                self._send_json(200, {
                    "ok": True,
                    "running": bool(_update_running[0]),
                    "stage": _update_stage.get("stage", "idle"),
                    "message": _update_stage.get("message", ""),
                    "current_version": VERSION,
                })
            elif path == "/api/update/check":
                update = check_for_updates()
                if update:
                    self._send_json(200, {"ok": True, "has_update": True,
                                          "new_version": update["version"]})
                else:
                    self._send_json(200, {"ok": True, "has_update": False,
                                          "current_version": VERSION,
                                          "error": _last_check.get("error", "")})
            elif path == "/api/update/apply":
                update = check_for_updates()
                if update:
                    threading.Thread(target=apply_update, args=(update,), daemon=True).start()
                    self._send_json(200, {"ok": True,
                                          "message": "Updating to v%s..." % update["version"]})
                else:
                    self._send_json(200, {"ok": True, "message": "Already up to date"})
            else:
                self._send_json(404, {"error": "unknown endpoint"})
        except Exception as e:
            log.exception("API Error on %s: %s", path, e)
            # Внутренние детали не отдаём клиенту / do not leak internals to the client
            self._send_json(500, {"error": "internal error"})

    def _handle_spool_delta(self, params):
        sid = _as_int(params.get("id"))
        delta = _as_float(params.get("delta"))
        if sid is None or delta is None:
            self._send_json(400, {"error": "bad id or delta"})
            return

        def _apply(st):
            for sp in (st.get("spools") or []):
                if sp.get("id") == sid:
                    sp["rem_g"] = round(max(0.0, float(sp.get("rem_g") or 0) + delta), 1)
                    return True
            return False

        if not mutate_state(_apply):
            self._send_json(404, {"error": "spool not found"})
        else:
            self._send_json(200, {"ok": True})

    def _handle_settings_save(self, params):
        int_keys = ("timezone_offset", "digest_hour", "cutter_cuts", "cfs_humidity")
        float_keys = ("maint_hours", "nozzle_meters", "filter_hours", "ptfe_meters",
                      "elec_price", "power_kw", "depr_hour")
        bad = []

        def _apply(st):
            settings = st.get("settings") or {}
            if "spool_tracking" in params:
                settings["spool_tracking"] = _as_bool(params["spool_tracking"])
            if "currency" in params:
                code = str(params["currency"] or "").strip().upper()
                if code in CURRENCY_SYMBOLS:
                    settings["currency"] = code
                else:
                    bad.append("currency")
            for key in int_keys:
                if key in params and str(params[key]).strip() != "":
                    val = _as_int(params[key])
                    if val is None:
                        bad.append(key)
                    else:
                        settings[key] = val
            for key in float_keys:
                if key in params and str(params[key]).strip() != "":
                    val = _as_float(params[key])
                    if val is None or val < 0:
                        bad.append(key)
                    else:
                        settings[key] = val
            if params.get("prices_json"):
                try:
                    raw = json.loads(params["prices_json"])
                except Exception:
                    raw = None
                if not isinstance(raw, dict):
                    bad.append("prices_json")
                else:
                    prices = {}
                    for mat, price in raw.items():
                        val = _as_float(price)
                        if val is None or val < 0:
                            bad.append("prices.%s" % mat)
                            continue
                        prices[str(mat).upper()] = val
                    if prices:
                        settings["prices"] = prices
            st["settings"] = settings

        mutate_state(_apply)
        if bad:
            self._send_json(400, {"ok": False, "error": "bad values", "fields": bad})
        else:
            self._send_json(200, {"ok": True})

    def _handle_spool_save(self, params):
        mat = str(params.get("mat") or "PLA").strip().upper()[:16] or "PLA"
        name = str(params.get("name") or "").strip()[:40]
        if name:
            name = name[:1].upper() + name[1:]
        grams = _as_float(params.get("rem_g"), 1000.0)
        if grams is None or grams < 0:
            self._send_json(400, {"error": "bad rem_g"})
            return
        sid = _as_int(params.get("id"))

        def _apply(st):
            spools = st.get("spools") or []
            if sid:
                for sp in spools:
                    if sp.get("id") == sid:
                        sp["mat"] = mat
                        if name:
                            sp["name"] = name
                            sp["group"] = normalize_color_group(name)
                        sp["rem_g"] = round(grams, 1)
                        st["spools"] = spools
                        return True
                return False
            new_id = max([int(sp.get("id") or 0) for sp in spools] or [0]) + 1
            spools.append({
                "id": new_id, "mat": mat, "name": name or t("new_spool"),
                "group": normalize_color_group(name), "hex": "", "rem_g": round(grams, 1),
            })
            st["spools"] = spools
            return True

        if not mutate_state(_apply):
            self._send_json(404, {"error": "spool not found"})
        else:
            self._send_json(200, {"ok": True})

    def _handle_spool_delete(self, params):
        sid = _as_int(params.get("id"))
        if sid is None:
            self._send_json(400, {"error": "bad id"})
            return

        def _apply(st):
            before = len(st.get("spools") or [])
            st["spools"] = [sp for sp in (st.get("spools") or []) if sp.get("id") != sid]
            overrides = st.get("slot_overrides") or {}
            for key in [k for k, v in overrides.items() if (v or {}).get("id") == sid]:
                del overrides[key]
            st["slot_overrides"] = overrides
            if st.get("single_spool_id") == sid:
                st["single_spool_id"] = None
            return len(st["spools"]) != before

        if not mutate_state(_apply):
            self._send_json(404, {"error": "spool not found"})
        else:
            self._send_json(200, {"ok": True})

    def _handle_slot_assign(self, params):
        letter = str(params.get("slot") or "")
        sid = _as_int(params.get("spool_id"))
        if sid is None:
            self._send_json(400, {"error": "bad spool_id"})
            return

        def _apply(st):
            if letter == "SINGLE":
                st["single_spool_id"] = sid
                return True
            slots, _, _, _ = get_cfs_slots()
            if letter not in slots:
                return False
            overrides = st.get("slot_overrides") or {}
            overrides[letter] = {"id": sid, "hex": slots[letter]["hex"], "mat": slots[letter]["mat"]}
            st["slot_overrides"] = overrides
            return True

        if not mutate_state(_apply):
            self._send_json(404, {"error": "unknown slot"})
        else:
            self._send_json(200, {"ok": True})

    def _handle_printer_cmd(self, params):
        cmd = str(params.get("cmd") or "")
        endpoint = {"pause": "/printer/print/pause",
                    "resume": "/printer/print/resume",
                    "cancel": "/printer/print/cancel"}.get(cmd)
        if not endpoint:
            self._send_json(400, {"error": "bad cmd"})
            return
        ok = True
        try:
            MOONRAKER_CLIENT.post(endpoint, b"", TIMEOUT_METADATA)
        except Exception as e:
            log.warning("Printer cmd %s failed: %s", cmd, e)
            ok = False
        self._send_json(200 if ok else 502, {"ok": ok})

    def _handle_thumb(self, params):
        """Отдаёт превью модели из кэша RAM или из Moonraker.
        Serves a model thumbnail from the RAM cache or from Moonraker."""
        safe = _safe_thumb_path(params.get("path"))
        if not safe:
            self._send_json(400, {"error": "bad path"})
            return
        data = _thumb_cache_get(safe)
        if data is None:
            try:
                with urllib.request.urlopen(
                        MOONRAKER + "/server/files/gcodes/" + urllib.parse.quote(safe),
                        timeout=TIMEOUT_THUMBNAIL) as r:
                    data = r.read()
            except Exception as e:
                log.warning("Thumbnail fetch failed (%s): %s", safe, e)
                data = None
            if data:
                _thumb_cache_put(safe, data)
        if not data:
            self._send_json(404, {"error": "no thumbnail"})
            return
        ctype = "image/png" if data[:8].startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_light_toggle(self):
        result = {}

        def _apply(st):
            result["on"] = not bool(st.get("light_on", False))
            st["light_on"] = result["on"]

        mutate_state(_apply)
        new_on = bool(result.get("on"))
        value = 1 if new_on else 0
        try:
            objects = moonraker_get("/printer/objects/list", TIMEOUT_MOONRAKER_FAST).get("objects") or []
            commands = []
            for obj in objects:
                low = obj.lower()
                if (obj.startswith("output_pin ") or obj.startswith("led ")) and any(
                        key in low for key in ("light", "led", "lamp", "caselight")):
                    pin = obj.split(" ", 1).pop()
                    if obj.startswith("output_pin "):
                        commands.append("SET_PIN PIN=%s VALUE=%d" % (pin, value))
                    else:
                        commands.append("SET_LED LED=%s WHITE=%d RED=%d GREEN=%d BLUE=%d"
                                        % (pin, value, value, value, value))
            if commands:
                MOONRAKER_CLIENT.gcode("\n".join(commands), TIMEOUT_METADATA)
        except Exception as e:
            log.warning("Light toggle failed: %s", e)
        self._send_json(200, {"ok": True, "light_on": new_on})

    def _handle_language(self, params):
        lang = str(params.get("language", "ru")).lower()
        if lang not in ("ru", "en"):
            lang = "ru"

        def _apply(st):
            settings = st.get("settings") or {}
            settings["language"] = lang
            st["settings"] = settings

        mutate_state(_apply)
        # Пересобираем все клавиатуры, включая приветствие.
        # Rebuild every keyboard, the welcome one included.
        global MAIN_KB, WELCOME_KB, MAINT_KB, RESET_KB
        MAIN_KB = get_main_kb()
        WELCOME_KB = get_welcome_kb()
        MAINT_KB = get_maint_kb()
        RESET_KB = get_reset_kb()
        lang_msg = "🌐 Language switched to English" if lang == "en" else "🌐 Язык изменён на русский"
        try:
            tg_send(lang_msg, MAIN_KB)
            log.info("🌐 Language switched to: %s", lang)
        except Exception as e:
            log.warning("Failed to send language switch message: %s", e)
        self._send_json(200, {"ok": True, "language": lang})

    def do_GET(self):
        global CAM_SNAPSHOT_URL, last_cam_fail_ts
        parts = self.path.split("?", 1)
        path = parts.pop(0)
        qs = dict(urllib.parse.parse_qsl(parts.pop(0))) if parts else {}
        if path in ("/", "/index.html"):
            if os.path.isfile(WEBAPP_FILE):
                with open(WEBAPP_FILE, "rb") as f: html = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            else:
                self._send_json(404, {"error": "webapp.html not found"})
        elif path == "/api/thumb":
            # Превью модели — только после авторизации.
            # Model thumbnails are served only to authenticated clients.
            if not self._check_auth(qs):
                self._send_json(403, {"error": "unauthorized"}); return
            self._handle_thumb(qs)
        elif path == "/api/cam.jpg":
            # Камера — приватные данные: без авторизации не отдаём.
            # Camera frames are private: never serve them unauthenticated.
            if not self._check_auth(qs):
                self._send_json(403, {"error": "unauthorized"}); return
            img = None
            # detect_environment() работает в фоновом потоке (_env_worker),
            # чтобы запрос кадра не ждал перебора URL камеры.
            # detect_environment() runs in the background (_env_worker) so a
            # frame request never waits for camera URL probing.
            if CAM_SNAPSHOT_URL:
                try:
                    with urllib.request.urlopen(CAM_SNAPSHOT_URL, timeout=TIMEOUT_CAMERA) as r:
                        cand = r.read()
                        if len(cand) > 200: img = cand
                except Exception as e:
                    log.warning("WebApp cam err: %s", e); CAM_SNAPSHOT_URL = ""; last_cam_fail_ts = time.time()
            if img:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(img)))
                self.end_headers()
                self.wfile.write(img)
            else:
                self.send_response(503); self.end_headers()
        elif path.startswith("/api/"):
            if not self._check_auth(qs):
                self._send_json(403, {"error": "unauthorized"}); return
            self._handle_api(path, qs)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        parts = self.path.split("?", 1)
        path = parts.pop(0)
        qs = dict(urllib.parse.parse_qsl(parts.pop(0))) if parts else {}
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln > 0 else {}
        except Exception:
            self._send_json(400, {"error": "bad json"}); return
        if not isinstance(body, dict):
            self._send_json(400, {"error": "bad json"}); return
        # Параметры из query (key/initData) добавляем в тело, чтобы POST
        # проходил ту же проверку авторизации, что и GET.
        # Merge query params into the body so POST uses the same auth path as GET.
        for key, value in qs.items():
            body.setdefault(key, value)
        if not self._check_auth(body):
            self._send_json(403, {"error": "unauthorized"}); return
        self._handle_api(path, body)

_tunnel_proc = None
TUNNEL_PID_FILE = os.path.join(BASE_DIR, "cloudflared.pid")


def _stop_tunnel():
    """Останавливает туннель без внешних утилит (без killall/pkill).
    Stops the tunnel without external tools (no killall/pkill)."""
    global _tunnel_proc
    proc, _tunnel_proc = _tunnel_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        with open(TUNNEL_PID_FILE, "r") as f:
            pid = int((f.read() or "0").strip())
        if pid > 1 and pid != os.getpid():
            os.kill(pid, 15)
            log.info("Остановлен осиротевший cloudflared pid=%d / stopped orphan", pid)
    except (OSError, ValueError):
        pass
    try:
        os.remove(TUNNEL_PID_FILE)
    except OSError:
        pass


def _tunnel_worker():
    global PUBLIC_WEBAPP_URL, _tunnel_proc
    if not USE_CLOUDFLARED:
        log.info("Cloudflared отключен в config.ini")
        return
    if not os.path.isfile(CLOUDFLARED_BIN):
        log.warning("cloudflared не найден: %s", CLOUDFLARED_BIN)
        return
    while True:
        try:
            proc = subprocess.Popen([CLOUDFLARED_BIN, "tunnel", "--url", "http://127.0.0.1:%d" % WEBAPP_PORT, "--protocol", "http2", "--no-autoupdate"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            _tunnel_proc = proc
            try:
                with open(TUNNEL_PID_FILE, "w") as f:
                    f.write(str(proc.pid))
            except OSError:
                pass
            for raw_line in iter(proc.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="ignore")
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", line)
                if m:
                    url = m.group(0)
                    if url != PUBLIC_WEBAPP_URL:
                        PUBLIC_WEBAPP_URL = url
                        log.info("Mini App URL: %s", url)
                        _refresh_menu_button()
            proc.wait()
            _tunnel_proc = None
        except Exception as e:
            log.warning("Tunnel err: %s", e)
            time.sleep(10)

def start_webapp_services():
    try:
        if USE_CLOUDFLARED: _stop_tunnel()
        srv = ThreadingHTTPServer(("127.0.0.1", WEBAPP_PORT), WebAppHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        if USE_CLOUDFLARED: threading.Thread(target=_tunnel_worker, daemon=True).start()
        log.info("WebApp сервер запущен на порту %d", WEBAPP_PORT)
    except Exception as e:
        log.warning("WebApp start err: %s", e)

# ==================== 12. ОСНОВНОЙ ЦИКЛ МОНИТОРИНГА ====================
LOCK_FILE = os.path.join(BASE_DIR, "printer_tg_bot.lock")
_lock_fd = None
if fcntl is not None:
    try:
        _lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except (IOError, OSError):
        log.critical("Уже запущен другой экземпляр / another instance is already running")
        print("Another instance is already running! Exiting.")
        sys.exit(0)
else:
    # Windows/без fcntl: блокировка недоступна, запускаемся без неё.
    # No flock available (Windows): start without the lock.
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    log.warning("fcntl недоступен: блокировка второго экземпляра отключена / flock unavailable")

detect_environment()
start_webapp_services()
start_background_workers()

STATE_SCHEMA_VERSION = 2


def migrate_state(st):
    """Приводит состояние к актуальной схеме, не теряя данные.
    Migrates state to the current schema without losing data."""
    changed = False
    if not isinstance(st.get("spools"), list):
        st["spools"] = []
        changed = True
    clean = [sp for sp in st["spools"] if isinstance(sp, dict)]
    if len(clean) != len(st["spools"]):
        st["spools"] = clean
        changed = True
    for sp in st["spools"]:
        if "mat" not in sp: sp["mat"] = "PLA"; changed = True
        if "group" not in sp: sp["group"] = normalize_color_group(sp.get("name") or sp.get("hex") or ""); changed = True
        if "rem_g" not in sp:
            # Раньше отсутствующий вес превращался в 0, и катушка выпадала из списка.
            # Previously a missing weight became 0 and the spool disappeared from the list.
            sp["rem_g"] = DEFAULT_SPOOL_WEIGHT
            changed = True
    settings = st.get("settings")
    if not isinstance(settings, dict):
        settings = {}
        st["settings"] = settings
        changed = True
    # Курс валюты больше не используется: суммы хранятся в валюте пользователя.
    # The exchange rate is gone: amounts are stored in the user's currency.
    if settings.pop("currency_rate", None) is not None:
        changed = True
    if settings.get("currency") not in CURRENCY_SYMBOLS:
        settings["currency"] = DEFAULT_CURRENCY_BY_LANG.get(settings.get("language", "ru"), "RUB")
        changed = True
    if st.get("schema_version") != STATE_SCHEMA_VERSION:
        st["schema_version"] = STATE_SCHEMA_VERSION
        changed = True
    return changed

_st = load_state()
if migrate_state(_st):
    save_state(_st)
    log.info("Состояние мигрировано на новую схему")

mode_str = t("mode_cfs") if HAS_CFS else t("mode_single")
tg_send(t("bot_started", VERSION, mode_str), WELCOME_KB)
_confirm_pending_update()

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
            lines = [t("print_started"), t("file", esc(fname))]
            if est > 0:
                end_dt = (datetime.now(timezone.utc) + timedelta(hours=int(cfg("timezone_offset")), seconds=est)).strftime("%d.%m.%Y %H:%M")
                lines.extend([t("duration", format_dur(est)), t("eta", end_dt)])
            if w > 0:
                flush_txt = t("cfs_flush", flush_g) if flush_g > 0.1 else ""
                lines.append(t("filament_used", w) + flush_txt)
            extra_btns = []
            if cfg("spool_tracking"):
                slots, active_let, _, _ = get_cfs_slots()
                used_items = get_used_slots(fname, meta, slots, active_let, w)
                spool_rows = []

                def _resolve_slots(st):
                    # Автосоздание катушек пишем в состояние, а не в локальную копию.
                    # Auto-created spools must be persisted, not written to a copy.
                    spool_rows.clear()
                    for let, part_g in used_items:
                        s_obj = slots.get(let) or {"mat": "PLA", "hex": "000000"}
                        sp = resolve_slot_spool(st, let, s_obj["mat"], s_obj["hex"], auto_create=HAS_CFS)
                        if sp:
                            spool_rows.append((let, part_g, sp_title(sp), sp["rem_g"]))

                mutate_state(_resolve_slots)
                for let, part_g, title, rem_g in spool_rows:
                    if let == "SINGLE":
                        lines.append(t("spool", esc(title), rem_g))
                        extra_btns.append([{"text": t("change_spool"), "callback_data": "sp_ovslot_SINGLE"}])
                    else:
                        need = t("slot_need", part_g) if len(used_items) > 1 and part_g > 0 else ""
                        lines.append(t("slot", let, esc(title), need, rem_g))
                        extra_btns.append([{"text": t("change_slot", let), "callback_data": "sp_ovslot_%s" % let}])
                    if part_g > 0 and rem_g < part_g:
                        lines.append(t("low_filament", part_g - rem_g))
            if direct > 0:
                lines.extend(["", t("cost_title"),
                    t("cost_direct", money(direct), money(elec)),
                    t("cost_depr", money(full), money(depr))])
            start_kb = {"inline_keyboard": MAIN_KB["inline_keyboard"] + extra_btns} if extra_btns else MAIN_KB
            send_thumbnail(meta, "\n".join(lines), start_kb)
            # Предполётный аудит: молчит, если рисков нет.
            # Pre-flight audit: stays silent when there are no risks.
            preflight_and_notify(fname, meta, w)
        elif state == "paused" and last_state != "paused":
            if "filament" in msg.lower() or "out of" in msg.lower():
                p_msg = "%s\n%s" % (t("filament_out"), t("file", esc(fname)))
            else:
                p_msg = "%s\n%s\n%s" % (t("print_paused"),
                                         t("reason", esc(msg or t("reason_none"))),
                                         t("file", esc(fname)))
            send_usb_photo(p_msg, MAIN_KB)
        elif state == "complete" and last_state != "complete":
            process_completed_job(fname, get_file_meta(fname), print_dur, used_mm, send_video=True)
        elif (state == "error" and last_state != "error") or (state in ("standby", "cancelled") and last_state in ("printing", "paused")):
            meta = get_file_meta(fname); record_material_usage(meta, used_mm, fname)
            w, direct, full, _, depr = calc_costs(meta, print_dur, used_mm); record_waste(w, direct)
            spool_notes = deduct_spool_weight(fname, meta, w)
            lines = [t("print_error") if state == "error" else t("print_cancelled")]
            if state == "error" and msg:
                lines.append(t("reason", esc(msg)))
            lines.append(t("file", esc(fname)))
            if w > 0.5 or direct > 0.5:
                lines.extend(["", t("waste_title"),
                    t("waste_filament", w),
                    t("waste_cost", money(direct), money(full))])
            if spool_notes: lines.extend(["", spool_notes])
            out_txt = "\n".join(lines)
            if state == "error": send_usb_photo(out_txt, MAIN_KB)
            else: tg_send(out_txt, MAIN_KB)
            update_counters(1 if (HAS_CFS and used_mm > 0) else 0)
        last_state = state; last_fname = fname
    except Exception:
        log.exception("Loop error")
    time.sleep(POLL_INTERVAL)