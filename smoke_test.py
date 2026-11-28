#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Быстрые проверки проекта без запуска бота (stdlib only).

Smoke checks for the project without starting the bot (stdlib only):
  * every .py file parses;
  * RU/EN translation keys are in parity and every t("...") key exists;
  * manifest.json matches version.txt, manifest_files.txt and real hashes;
  * webapp.html has RU/EN parity and all data-i18n keys;
  * no leftover currency_rate conversion;
  * feature invariants: authenticated thumbnails, no base64 in the state,
    atomic pre-flight storage, new panel blocks;
  * shell scripts pass `sh -n` when a shell is available.

Запуск / run:  python3 smoke_test.py
"""
import ast
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY_FILES = ("printer_tg_bot.py", "fetch.py", "gen_manifest.py", "smoke_test.py")
errors = []
checks = 0


def ok(name):
    global checks
    checks += 1
    print("  ok   %s" % name)


def fail(name, detail):
    errors.append("%s: %s" % (name, detail))
    print("  FAIL %s: %s" % (name, detail))


def read(path):
    with io.open(os.path.join(BASE, path), encoding="utf-8") as f:
        return f.read()


def check_python():
    for name in PY_FILES:
        try:
            ast.parse(read(name))
            ok("parse %s" % name)
        except Exception as e:
            fail("parse %s" % name, e)


def check_i18n():
    tree = ast.parse(read("printer_tg_bot.py"))
    lang = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(tg, ast.Name) and tg.id == "LANG" for tg in node.targets):
            lang = ast.literal_eval(node.value)
            break
    if not isinstance(lang, dict) or "ru" not in lang or "en" not in lang:
        fail("i18n", "LANG dictionary not found")
        return
    ru, en = set(lang["ru"]), set(lang["en"])
    if ru == en:
        ok("i18n parity (%d keys)" % len(ru))
    else:
        fail("i18n parity", "ru-only %s, en-only %s"
             % (sorted(ru - en)[:5], sorted(en - ru)[:5]))

    used = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("t", "plural") and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            used.add(node.args[0].value)
    missing = sorted(k for k in used if k not in ru or k not in en)
    if missing:
        fail("i18n used keys", "missing: %s" % missing[:10])
    else:
        ok("i18n used keys (%d)" % len(used))


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_manifest():
    try:
        with io.open(os.path.join(BASE, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        version = read("version.txt").strip()
    except Exception as e:
        fail("manifest", e)
        return
    if manifest.get("version") != version:
        fail("manifest version", "%r != version.txt %r" % (manifest.get("version"), version))
    else:
        ok("manifest version %s" % version)

    expected = None
    try:
        names = [ln.strip() for ln in read("manifest_files.txt").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        expected = list(dict.fromkeys(names))
    except OSError:
        pass
    files = manifest.get("files") or {}
    if expected and sorted(files) != sorted(expected):
        fail("manifest file list", "%s != manifest_files.txt %s"
             % (sorted(files), sorted(expected)))
    else:
        ok("manifest file list")

    bad = []
    for name, meta in files.items():
        path = os.path.join(BASE, name)
        if not os.path.isfile(path):
            bad.append("%s missing" % name)
            continue
        if os.path.getsize(path) != int(meta.get("size") or 0):
            bad.append("%s size" % name)
        if sha256_of(path) != meta.get("sha256"):
            bad.append("%s sha256" % name)
    if bad:
        fail("manifest hashes", ", ".join(bad) + " (run: python gen_manifest.py)")
    else:
        ok("manifest hashes (%d files)" % len(files))


def check_webapp():
    html = read("webapp.html")
    m = re.search(r"<script>(.*)</script>", html, re.S)
    script = m.group(1) if m else ""
    ru_m = re.search(r"ru:\s*\{(.*?)\n\s*\},\n\s*en:", script, re.S)
    en_m = re.search(r"en:\s*\{(.*?)\n\s*\}\n\s*\}", script, re.S)
    if not (ru_m and en_m):
        fail("webapp i18n", "I18N dictionaries not found")
        return
    kr = set(re.findall(r'"([a-z0-9_]+)"\s*:', ru_m.group(1)))
    ke = set(re.findall(r'"([a-z0-9_]+)"\s*:', en_m.group(1)))
    if kr == ke:
        ok("webapp i18n parity (%d keys)" % len(kr))
    else:
        fail("webapp i18n parity", "ru-only %s, en-only %s"
             % (sorted(kr - ke)[:5], sorted(ke - kr)[:5]))
    used = set(re.findall(r'data-i18n="([^"]+)"', html))
    missing = sorted(used - kr - ke)
    if missing:
        fail("webapp data-i18n", "missing: %s" % missing[:10])
    else:
        ok("webapp data-i18n (%d)" % len(used))


def check_currency():
    py = read("printer_tg_bot.py")
    if re.search(r"def currency_rate\(", py):
        fail("currency_rate", "conversion helper is still defined")
    else:
        ok("no currency conversion helper")
    if "currency_rate" in read("webapp.html"):
        fail("webapp currency_rate", "stale field in the panel")
    else:
        ok("no currency_rate in webapp")
    if "authQuery" in read("webapp.html"):
        fail("webapp auth", "key still passed in the URL")
    else:
        ok("panel auth uses headers")


def check_features():
    py = read("printer_tg_bot.py")
    html = read("webapp.html")
    # Превью модели отдаётся только авторизованным клиентам.
    # Model thumbnails are served only to authenticated clients.
    block = re.search(r'elif path == "/api/thumb":(.*?)\n\s*elif ', py, re.S)
    if block and "_check_auth" in block.group(1):
        ok("thumb endpoint requires auth")
    else:
        fail("thumb endpoint", "missing or unauthenticated")
    # В состоянии не должно быть base64-картинок (память OpenWrt).
    # The state must not contain base64 images (OpenWrt memory).
    if re.search(r"^\s*(import base64|from base64 import)|base64\.", py, re.M):
        fail("no base64 in backend", "base64 used; thumbnails must stay out of the state")
    else:
        ok("no base64 in backend")
    # Результат pre-flight пишется атомарно.
    # Pre-flight results are stored atomically.
    if re.search(r"def preflight_and_notify\(.*?mutate_state", py, re.S):
        ok("preflight uses mutate_state")
    else:
        fail("preflight state", "mutate_state not used")
    # Ключевые поля снимка панели.
    # Key panel payload fields.
    missing = [name for name in ('"job": job', '"archive":', '"maintenance_forecast":')
               if name not in py]
    if missing:
        fail("payload fields", ", ".join(missing))
    else:
        ok("payload fields (job, archive, forecast)")
    # Новые блоки интерфейса и их ключи локализации.
    # New UI blocks and their localization keys.
    absent = [el_id for el_id, key in (
        ("job-card", "job_title"), ("archive-list", "archive_title"),
        ("eff-list", "eff_title"), ("maint_forecast", "maint_forecast"))
        if el_id not in html or key not in html]
    if absent:
        fail("webapp new blocks", ", ".join(absent))
    else:
        ok("webapp new blocks (job, archive, efficiency, forecast)")


def check_paths():
    """OTA не должен принимать выход за пределы каталога установки.
    OTA must never accept paths outside the install directory."""
    good = ("webapp.html", "core/state.py", "a/b/c.py")
    bad = ("../x", "/etc/passwd", "a/../../b", ".hidden/x", "", "a//b", "a/b/c/d/e.py")
    try:
        import fetch
        for name in good:
            if fetch._safe_name(name) != name:
                raise AssertionError("fetch rejected %r" % name)
        for name in bad:
            try:
                fetch._safe_name(name)
            except RuntimeError:
                continue
            raise AssertionError("fetch accepted %r" % name)
        ok("fetch safe paths")
    except Exception as e:
        fail("fetch safe paths", e)

    tree = ast.parse(read("printer_tg_bot.py"))
    fn = next((node for node in tree.body
               if isinstance(node, ast.FunctionDef) and node.name == "_safe_relpath"), None)
    if fn is None:
        fail("bot safe paths", "_safe_relpath not found")
        return
    module = ast.Module(body=[fn], type_ignores=[])
    namespace = {"re": re}
    exec(compile(module, "printer_tg_bot.py", "exec"), namespace)
    safe = namespace["_safe_relpath"]
    problems = [name for name in good if safe(name) != name]
    problems += [name for name in bad if safe(name) is not None]
    if problems:
        fail("bot safe paths", "wrong result for %s" % problems)
    else:
        ok("bot safe paths")


def check_shell():
    sh = shutil.which("sh")
    if not sh:
        print("  skip sh -n (no sh on this system)")
        return
    for name in ("install.sh", "uninstall.sh"):
        proc = subprocess.run([sh, "-n", os.path.join(BASE, name)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode == 0:
            ok("sh -n %s" % name)
        else:
            fail("sh -n %s" % name, proc.stdout.decode("utf-8", "ignore").strip())


def main():
    print("Smoke test: %s" % BASE)
    check_python()
    check_i18n()
    check_manifest()
    check_webapp()
    check_currency()
    check_features()
    check_paths()
    check_shell()
    print("")
    if errors:
        print("FAILED: %d check(s), %d error(s)" % (checks, len(errors)))
        for item in errors:
            print("  - %s" % item)
        return 1
    print("OK: %d check(s) passed" % checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
