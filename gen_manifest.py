#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генерирует manifest.json для OTA-обновлений.
Generates manifest.json used by the built-in OTA updater.

Запускать перед каждым релизом / run before every release:
    python3 gen_manifest.py [min_version]
"""
import hashlib
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
FILES_FILE = os.path.join(BASE, "manifest_files.txt")
DEFAULT_FILES = ("printer_tg_bot.py", "webapp.html")
CHUNK = 65536


def load_files():
    """Единый список файлов OTA из manifest_files.txt (или значения по умолчанию).
    Single OTA file list from manifest_files.txt (falls back to the defaults)."""
    try:
        with open(FILES_FILE, encoding="utf-8") as f:
            names = [ln.strip() for ln in f
                     if ln.strip() and not ln.strip().startswith("#")]
    except OSError:
        names = list(DEFAULT_FILES)
    unique = list(dict.fromkeys(names))
    return tuple(unique) or DEFAULT_FILES


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv):
    with open(os.path.join(BASE, "version.txt"), encoding="utf-8") as f:
        version = f.read().strip()
    if not version:
        raise SystemExit("version.txt is empty")
    min_version = argv[1] if len(argv) > 1 else "1.0.0"
    files = {}
    for name in load_files():
        path = os.path.join(BASE, name)
        files[name] = {"sha256": sha256_of(path), "size": os.path.getsize(path)}
    manifest = {"version": version, "min_version": min_version, "files": files}
    out = os.path.join(BASE, "manifest.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("manifest.json -> v%s, min_version=%s, %d file(s)" % (version, min_version, len(files)))
    for name, meta in files.items():
        print("  %-20s %8d  %s" % (name, meta["size"], meta["sha256"][:16]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
