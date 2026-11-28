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

VERSION = "1.1.0"

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
        "cost_direct": "• Прямые (пластик+свет): <b>~%.1f руб.</b> (свет ~%.1f руб.)",
        "cost_depr": "• С амортизацией: <b>~%.1f руб.</b> (аморт. ~%.1f руб.)",
        "spool": "• Катушка: <b>%s</b> (в наличии %.0f г)",
        "slot": "• Слот %s: <b>%s</b> (%sв наличии %.0f г)",
        "slot_need": "нужно ~%.1f г | ",
        "low_filament": "⚠️ <i>Внимание: не хватит ~%.0f г до конца детали!</i>",
        "change_spool": "🔄 Сменить катушку",
        "change_slot": "🔄 Сменить (Слот %s)",
        "reason": "• Причина: %s",
        "waste_title": "🗑 <b>Потери на брак:</b>",
        "waste_filament": "• Пластик: <b>%.1f г</b>",
        "waste_cost": "• Прямые затраты: <b>~%.1f руб.</b> (с аморт. ~%.1f руб.)",
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
        "stats_waste": "• Потери на брак: <b>%.0f г</b> (~%.0f руб. | %.1f%%)",
        "stats_longest": "• Рекорд печати: <b>%.1f ч</b>",
        "stats_avg": "• В среднем за печать: <b>~%.1f ч / ~%.0f г</b>",
        "stats_load_title": "⏱ <b>Нагрузка принтера (КПД):</b>",
        "stats_7d": "• За 7 дней: <b>%.1f ч</b> (~%.1f ч/день | КПД %.0f%%)",
        "stats_30d": "• За 30 дней: <b>%.1f ч</b> (~%.1f ч/день | КПД %.0f%%)",
        "stats_mats_title": "🧵 <b>Расход по типам пластика:</b>",
        "stats_mat_item": "• %s: <b>%.1f м</b> (~%.1f катушек)",
        "stats_costs_title": "💰 <b>Затраты за всё время:</b>",
        "stats_costs_direct": "• Прямые (пластик+свет): <b>~%.0f руб.</b> (свет ~%.0f руб.)",
        "stats_costs_depr": "• С учетом амортизации: <b>~%.0f руб.</b> (аморт. ~%.0f руб.)",
        "digest_title": "📅 <b>Еженедельный дайджест принтера:</b>",
        "digest_printed": "• Напечатано за неделю: <b>%.1f ч</b> (КПД %.0f%%)",
        "digest_reliability": "• Надежность за неделю: <b>%.0f%%</b> (%d из %d завершено)",
        "digest_filament": "• Расход пластика: <b>%.1f м</b> (~%.1f катушек)",
        "digest_waste": "• Потери на брак: <b>%.0f г</b> (~%.0f руб.)",
        "digest_mats": "• По материалам: <b>%s</b>",
        "digest_costs": "• Затраты за неделю: <b>~%.0f руб.</b> (свет ~%.0f руб., с аморт. ~%.0f руб.)",
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
        "cost_direct": "• Direct (filament+electricity): <b>~%.1f RUB</b> (electricity ~%.1f RUB)",
        "cost_depr": "• With depreciation: <b>~%.1f RUB</b> (depr. ~%.1f RUB)",
        "spool": "• Spool: <b>%s</b> (%.0f g left)",
        "slot": "• Slot %s: <b>%s</b> (%s%.0f g left)",
        "slot_need": "Need ~%.1f g | ",
        "low_filament": "⚠️ <i>Warning: not enough filament (~%.0f g short)!</i>",
        "change_spool": "🔄 Change spool",
        "change_slot": "🔄 Change (Slot %s)",
        "reason": "• Reason: %s",
        "waste_title": "🗑 <b>Waste:</b>",
        "waste_filament": "• Filament: <b>%.1f g</b>",
        "waste_cost": "• Cost loss: <b>~%.1f RUB</b> (with depr. ~%.1f RUB)",
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
        "stats_waste": "• Waste: <b>%.0f g</b> (~%.0f RUB | %.1f%%)",
        "stats_longest": "• Longest print: <b>%.1f h</b>",
        "stats_avg": "• Average per print: <b>~%.1f h / ~%.0f g</b>",
        "stats_load_title": "⏱ <b>Printer load (efficiency):</b>",
        "stats_7d": "• Last 7 days: <b>%.1f h</b> (~%.1f h/day | efficiency %.0f%%)",
        "stats_30d": "• Last 30 days: <b>%.1f h</b> (~%.1f h/day | efficiency %.0f%%)",
        "stats_mats_title": "🧵 <b>Usage by material type:</b>",
        "stats_mat_item": "• %s: <b>%.1f m</b> (~%.1f spools)",
        "stats_costs_title": "💰 <b>Total costs:</b>",
        "stats_costs_direct": "• Direct (filament+electricity): <b>~%.0f RUB</b> (electricity ~%.0f RUB)",
        "stats_costs_depr": "• With depreciation: <b>~%.0f RUB</b> (depr. ~%.0f RUB)",
        "digest_title": "📅 <b>Weekly printer digest:</b>",
        "digest_printed": "• Printed this week: <b>%.1f h</b> (efficiency %.0f%%)",
        "digest_reliability": "• Reliability this week: <b>%.0f%%</b> (%d of %d completed)",
        "digest_filament": "• Filament used: <b>%.1f m</b> (~%.1f spools)",
        "digest_waste": "• Waste: <b>%.0f g</b> (~%.0f RUB)",
        "digest_mats": "• By material: <b>%s</b>",
        "digest_costs": "• Weekly costs: <b>~%.0f RUB</b> (electricity ~%.0f RUB, with depr. ~%.0f RUB)",
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
    return lang


def get_language():
    """Возвращает текущий язык из настроек (по умолчанию 'ru')."""
    st = load_state()
    return (st.get("settings") or {}).get("language", "ru")

_last_kb_lang = [None]

def t(key, *args):
    """Перевод строки. Если ключа нет — возвращает русский текст как fallback."""
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
    text = LANG.get(lang, LANG["ru"]).get(key, LANG["ru"].get(key, key))
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
UPDATE_STAGING = os.path.join(BASE_DIR, "update_staging")
UPDATE_BACKUP = os.path.join(BASE_DIR, "update_backup")

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

# Источники обновлений: raw первым (свежий), jsDelivr как fallback (доступность).
# Update sources: raw first (fresh), jsDelivr as fallback (reachability).
UPDATE_BASES = []
for _base in (GITHUB_RAW_BASE, GITHUB_MIRROR_BASE):
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

# ==================== 4. MOONRAKER API ====================
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

# ==================== 5. УЧЕТ КАТУШЕК ====================


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

# ==================== 8. ЗАВЕРШЕНИЕ ПЕЧАТИ ====================
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

# ==================== 9. СТАТУС И ОБСЛУЖИВАНИЕ ====================
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
    s_map = {
        "printing": t("state_printing"),
        "paused": t("state_paused"),
        "complete": t("state_complete"),
        "standby": t("state_standby"),
        "error": t("state_error"),
        "cancelled": t("state_cancelled"),
    }
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
            result["msg"] = "✅ Счетчик сопла сброшен (%.1f м)" % meters
        elif target == "filter":
            st["filter_start_hours"], st["filter_warned"] = hours, False
            result["msg"] = "✅ Счетчик фильтра сброшен (%.1f ч)" % hours
        elif target == "ptfe":
            st["ptfe_start_meters"], st["ptfe_warned"] = meters, False
            result["msg"] = "✅ Счетчик PTFE сброшен (%.1f м)" % meters
        else:
            st["cutter_cuts"], st["cutter_warned"] = 0, False
            result["msg"] = "✅ Счетчик ножа сброшен (0 резов)"

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
        url = MOONRAKER + "/printer/gcode/script"
        with urllib.request.urlopen(url, data=urllib.parse.urlencode({"script": "\n".join(cmds)}).encode(), timeout=300):
            pass
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
            res = tg_api("getUpdates", {"offset": last_update_id + 1, "timeout": 25}, timeout=30).get("result", [])
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
                out(msg.get("text") or "Выберите катушку:", build_spool_picker(pref, b_cb, int(pg_s)))
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
            out("✍️ <b>Новая катушка:</b>\nОтправьте тип, цвет и вес (г).\nПример: <b>PLA Черный матовый 850</b>", {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "btn_spools"}]]})
        elif data == "sp_edit_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: view, kb = build_spools_view(); out(view, kb)
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
            if not spools: view, kb = build_spools_view(); out(view, kb)
            else:
                slots, _, _, _ = get_cfs_slots()
                p_mat = slots.get(let, {}).get("mat") if let in slots else None
                title_ov = "📥 <b>Выберите текущую катушку:</b>" if let == "SINGLE" else ("📥 <b>Выберите бобину для Слота %s:</b>" % let)
                out(title_ov, build_spool_picker("sp_ovset_%s_" % let, "btn_spools" if let == "SINGLE" else "sp_ov_menu", 0, p_mat))
        elif data.startswith("sp_ovset_"):
            nums = [p for p in data.split("_") if p.isdigit()]
            let = data.split("_")[2]
            if not nums:
                tg_answer_cb(cb_id, "❌ Ошибка")
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
                tg_answer_cb(cb_id, "✅ Катушка выбрана" if let == "SINGLE" else "✅ Слот %s уточнен" % let)
            view, kb = build_spools_view(); out(view, kb)
        elif data == "sp_del_menu":
            awaiting_spool = False; edit_spool_id = None; tg_answer_cb(cb_id)
            spools = [s for s in (load_state().get("spools") or []) if s.get("rem_g", 0) > 0]
            if not spools: view, kb = build_spools_view(); out(view, kb)
            else: out("🗑 <b>Удаление катушки:</b>\nВыберите катушку из списка:", build_spool_picker("sp_askdel_", "btn_spools", 0))
        elif data.startswith("sp_askdel_"):
            sid = int(data.split("_").pop()); tg_answer_cb(cb_id)
            sp = next((s for s in (load_state().get("spools") or []) if s.get("id") == sid), None)
            if sp: out("⚠️ <b>Вы уверены, что хотите удалить катушку?</b>\n• <b>%s</b> (%.0f г)" % (esc(sp_title(sp)), sp["rem_g"]), {"inline_keyboard": [[{"text": "✅ Да, удалить", "callback_data": "sp_confdel_%d" % sid}, {"text": "❌ Отмена", "callback_data": "btn_spools"}]]})
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
            tg_answer_cb(cb_id, "🗑 Удалено"); view, kb = build_spools_view(); out(view, kb)
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
            tg_send("✅ Катушка <b>%s</b> (%.0f г) обновлена!\n" % (esc(sp_title(updated)), updated.get("rem_g", 0)) + view, kb)
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
                tg_send("✅ Катушка <b>%s</b> (%.0f г) добавлена!\n" % (esc(sp_title(created)), grams) + view, kb)
                return
            except Exception:
                pass
        tg_send("⚠️ Формат: <b>PLA Черный матовый 850</b>", {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "btn_spools"}]]})
        return
        tg_send("⚠️ Формат: <b>PLA Черный матовый 850</b>", {"inline_keyboard": [[{"text": "❌ Отмена", "callback_data": "btn_spools"}]]}); return
    if txt.startswith("/stats"): tg_send(format_stats(), MAIN_KB)
    elif txt.startswith("/consumables"): tg_send(format_consumables(), MAIN_KB)
    elif txt.startswith("/status"): send_usb_photo(format_status(), MAIN_KB)
    elif txt.startswith("/spools"): view, kb = build_spools_view(); tg_send(view, kb)
    elif txt.startswith(("/start", "/help")): awaiting_spool = False; edit_spool_id = None; tg_send("🤖 <b>Панель управления принтером:</b>", MAIN_KB)

# ==================== 11. WEB-СЕРВЕР MINI APP ====================
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
            "language": get_language(),
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

def _update_bases():
    return list(UPDATE_BASES)


def _http_get(url, timeout=20):
    """GET с таймаутом и явным User-Agent.
    Plain GET with a timeout and an explicit User-Agent."""
    req = urllib.request.Request(url, headers={"User-Agent": "klipper-tg-bot/%s" % VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch_first(path, timeout=20):
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
    """Разрешает только простые имена файлов внутри BASE_DIR.
    Allows only plain file names inside BASE_DIR (no traversal)."""
    name = str(name or "").strip()
    if (not name or name.startswith((".", "/", "\\"))
            or "/" in name or "\\" in name or ".." in name):
        return None
    return name


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


def check_for_updates():
    """Проверка обновлений: маленький version.txt, затем манифест.
    Update check: fetch the tiny version.txt first, then the manifest."""
    try:
        best_version, best_base = None, None
        for base in _update_bases():
            url = "%s/version.txt?v=%d" % (base, int(time.time()))
            try:
                raw = _http_get(url, 10).decode("utf-8", "ignore").strip()
            except Exception as e:
                log.warning("version.txt недоступен / unavailable: %s (%s)", url, e)
                continue
            remote = raw.splitlines()[0].strip() if raw else ""
            if not remote:
                continue
            if best_version is None or _parse_version(remote) > _parse_version(best_version):
                best_version, best_base = remote, base
        if not best_version:
            return None
        if _parse_version(best_version) <= _parse_version(VERSION):
            log.info("Обновлений нет: %s (локально %s) / up to date", best_version, VERSION)
            return None
        raw_manifest, manifest_src = _fetch_first("manifest.json?v=%s" % best_version, 15)
        if not raw_manifest:
            log.warning("manifest.json недоступен / manifest unavailable: %s", manifest_src)
            return None
        manifest = _load_manifest(raw_manifest)
        if manifest["version"] != best_version:
            log.warning("Версия манифеста %s не совпадает с version.txt %s",
                        manifest["version"], best_version)
            return None
        if _parse_version(VERSION) < _parse_version(manifest["min_version"]):
            log.warning("Обновление требует минимум v%s — нужна переустановка",
                        manifest["min_version"])
            return None
        log.info("Доступно обновление: %s -> %s (источник %s)",
                 VERSION, manifest["version"], best_base)
        return {"version": manifest["version"], "manifest": manifest}
    except Exception as e:
        log.warning("Ошибка проверки обновлений / update check failed: %s", e)
        return None


def _download_verified(name, meta, dest):
    """Скачивает файл и сверяет sha256, перебирая все зеркала.
    Downloads a file and verifies sha256, trying every mirror."""
    last_err = None
    for base in _update_bases():
        url = "%s/%s?v=%s" % (base, name, meta.get("sha256", "")[:12])
        try:
            data = _http_get(url, 60)
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
        with open(dest, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return True
    log.error("Не удалось скачать %s / download failed: %s", name, last_err)
    return False


_update_lock = threading.Lock()
_update_running = [False]


def apply_update(update_info):
    """Поэтапное обновление: скачать -> проверить -> забэкапить -> заменить.
    При любой ошибке — откат к предыдущей версии.
    Staged update with hash verification and rollback on failure."""
    with _update_lock:
        if _update_running[0]:
            log.warning("Обновление уже выполняется / update already running")
            return
        _update_running[0] = True
    manifest = update_info.get("manifest") or {}
    files = manifest.get("files") or {}
    new_version = str(manifest.get("version") or update_info.get("version") or "")
    replaced = []
    try:
        if not files:
            raise RuntimeError("empty manifest")
        shutil.rmtree(UPDATE_STAGING, ignore_errors=True)
        shutil.rmtree(UPDATE_BACKUP, ignore_errors=True)
        os.makedirs(UPDATE_STAGING)
        os.makedirs(UPDATE_BACKUP)

        # 1. Скачиваем во временную папку и проверяем целостность
        #    Download into staging and verify integrity.
        for name, meta in files.items():
            dest = os.path.join(UPDATE_STAGING, name)
            if not _download_verified(name, meta, dest):
                raise RuntimeError("не удалось скачать %s" % name)
            if name.endswith(".py"):
                with open(dest, "r", encoding="utf-8") as f:
                    compile(f.read(), dest, "exec")
            elif name.endswith(".html"):
                with open(dest, "r", encoding="utf-8") as f:
                    if "</html>" not in f.read():
                        raise RuntimeError("webapp.html повреждён")

        # 2. Бэкап текущих файлов / back up current files.
        for name in files:
            current = os.path.join(BASE_DIR, name)
            if os.path.exists(current):
                shutil.copyfile(current, os.path.join(UPDATE_BACKUP, name))

        # 3. Атомарная замена / atomic replace.
        for name in files:
            os.replace(os.path.join(UPDATE_STAGING, name), os.path.join(BASE_DIR, name))
            replaced.append(name)
        with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        log.info("Обновление применено: %s -> %s (%s)",
                 VERSION, new_version, ", ".join(replaced))
        tg_send("✅ <b>Обновление применено:</b> v%s → v%s\nБот перезапускается..."
                % (VERSION, new_version), MAIN_KB)
        shutil.rmtree(UPDATE_STAGING, ignore_errors=True)
        shutil.rmtree(UPDATE_BACKUP, ignore_errors=True)
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
        tg_send("⚠️ <b>Ошибка обновления:</b> %s\nОткат / rollback: %s"
                % (esc(str(e)), ", ".join(restored) or "—"), MAIN_KB)
    finally:
        with _update_lock:
            _update_running[0] = False


def _as_int(value, default=None):
    """Безопасное приведение к int, None при ошибке.
    Safe int conversion; returns the default on failure."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_float(value, default=None):
    """Безопасное приведение к float (запятая как разделитель тоже).
    Safe float conversion; accepts a comma as the decimal separator."""
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


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
        Referer/Origin не считаются доверенными.
        Only the panel secret or signed initData is accepted;
        Referer/Origin headers are untrusted and ignored."""
        key = str(params.get("key") or "")
        if WEBAPP_SECRET and key and hmac.compare_digest(key, str(WEBAPP_SECRET)):
            return True
        return verify_tg_init_data(params.get("initData", ""))

    def _handle_api(self, path, params):
        try:
            if path == "/api/state":
                self._send_json(200, build_webapp_payload())
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
            elif path == "/api/update/check":
                update = check_for_updates()
                if update:
                    self._send_json(200, {"ok": True, "has_update": True,
                                          "new_version": update["version"]})
                else:
                    self._send_json(200, {"ok": True, "has_update": False,
                                          "current_version": VERSION})
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
            urllib.request.urlopen(MOONRAKER + endpoint, data=b"", timeout=5).close()
        except Exception as e:
            log.warning("Printer cmd %s failed: %s", cmd, e)
            ok = False
        self._send_json(200 if ok else 502, {"ok": ok})

    def _handle_light_toggle(self):
        result = {}

        def _apply(st):
            result["on"] = not bool(st.get("light_on", False))
            st["light_on"] = result["on"]

        mutate_state(_apply)
        new_on = bool(result.get("on"))
        value = 1 if new_on else 0
        try:
            objects = moonraker_get("/printer/objects/list", 4).get("objects") or []
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
                urllib.request.urlopen(
                    MOONRAKER + "/printer/gcode/script",
                    data=urllib.parse.urlencode({"script": "\n".join(commands)}).encode(),
                    timeout=5).close()
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
        elif path == "/api/cam.jpg":
            # Камера — приватные данные: без авторизации не отдаём.
            # Camera frames are private: never serve them unauthenticated.
            if not self._check_auth(qs):
                self._send_json(403, {"error": "unauthorized"}); return
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
                        res = tg_api("setChatMenuButton", {
                            "chat_id": CHAT_ID,
                            "menu_button": json.dumps({"type": "web_app", "text": "📱 Панель", "web_app": {"url": "%s/?key=%s" % (url, WEBAPP_SECRET)}})
                        })
                        log.info("setChatMenuButton: %s", res)
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
    if st.get("schema_version") != STATE_SCHEMA_VERSION:
        st["schema_version"] = STATE_SCHEMA_VERSION
        changed = True
    return changed

_st = load_state()
if migrate_state(_st):
    save_state(_st)
    log.info("Состояние мигрировано на новую схему")

mode_str = "CFS (4 слота)" if HAS_CFS else "Одиночная катушка"
tg_send("✅ <b>Мониторинг принтера запущен (v%s)</b>\nРежим: <b>%s</b>" % (VERSION, mode_str), WELCOME_KB)

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
                        lines.append("• Катушка: <b>%s</b> (в наличии %.0f г)" % (esc(title), rem_g))
                        extra_btns.append([{"text": "🔄 Сменить катушку", "callback_data": "sp_ovslot_SINGLE"}])
                    else:
                        w_part_str = "нужно ~%.1f г | " % part_g if len(used_items) > 1 and part_g > 0 else ""
                        lines.append("• Слот %s: <b>%s</b> (%sв наличии %.0f г)" % (let, esc(title), w_part_str, rem_g))
                        extra_btns.append([{"text": "🔄 Сменить (Слот %s)" % let, "callback_data": "sp_ovslot_%s" % let}])
                    if part_g > 0 and rem_g < part_g:
                        lines.append("⚠️ <i>Внимание: не хватит ~%.0f г до конца детали!</i>" % (part_g - rem_g))
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
    time.sleep(POLL_INTERVAL)