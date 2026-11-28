#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Загрузчик на чистом Python — curl/wget на принтере не нужны.
Pure-Python downloader: no curl/wget required on the printer.

Использование / usage:
    python3 fetch.py URL DEST [SHA256 [SIZE]]
    python3 fetch.py --manifest MANIFEST_URL DEST_DIR
"""
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request

UA = "klipper-tg-bot-fetch/1.0"


def _ssl_context():
    """Контекст с проверкой сертификата; понятная ошибка, если CA нет.
    Verifying TLS context with a readable error when CA certs are missing."""
    return ssl.create_default_context()


def http_get(url, timeout=45, retries=3):
    """GET с повторами; возвращает bytes.
    GET with retries; returns raw bytes."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - нужен любой сбой сети / any network failure
            last = exc
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise RuntimeError(
                    "нет CA-сертификатов / no CA bundle, выполните: opkg install ca-bundle")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("GET failed: %s (%s)" % (url, last))


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def download(url, dest, digest=None, size=None):
    """Скачивает файл, сверяет размер и sha256, пишет атомарно.
    Downloads a file, verifies size and sha256, writes it atomically."""
    data = http_get(url)
    if size and len(data) != size:
        raise RuntimeError("size mismatch for %s: %d != %d" % (dest, len(data), size))
    if digest and sha256_hex(data) != digest:
        raise RuntimeError("sha256 mismatch for %s" % dest)
    # Скрипты для OpenWrt должны быть с LF / shell scripts must use LF on OpenWrt.
    if dest.endswith(".sh"):
        data = data.replace(b"\r\n", b"\n")
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    return len(data)


def _safe_name(name):
    name = str(name or "").strip()
    if (not name or name.startswith((".", "/", "\\"))
            or "/" in name or "\\" in name or ".." in name):
        raise RuntimeError("unsafe file name: %r" % name)
    return name


def fetch_manifest(manifest_url, dest_dir):
    """Скачивает все файлы из манифеста с проверкой sha256.
    Downloads every manifest entry and verifies its sha256."""
    raw = http_get(manifest_url, timeout=30)
    manifest = json.loads(raw.decode("utf-8"))
    version = str(manifest.get("version") or "")
    files = manifest.get("files") or {}
    if not version or not isinstance(files, dict) or not files:
        raise RuntimeError("bad manifest")
    base = manifest_url.rsplit("/", 1)[0]
    total = 0
    for name, meta in files.items():
        safe = _safe_name(name)
        meta = meta or {}
        url = "%s/%s?v=%s" % (base, safe, version)
        written = download(url, os.path.join(dest_dir, safe),
                           meta.get("sha256"), meta.get("size"))
        print("   OK %-24s %8d bytes" % (safe, written))
        total += written
    return version, len(files), total


def main(argv):
    if len(argv) >= 3 and argv[1] == "--manifest":
        dest_dir = argv[3] if len(argv) > 3 else "."
        version, count, total = fetch_manifest(argv[2], dest_dir)
        print("-> v%s: %d file(s), %.1f KB" % (version, count, total / 1024.0))
        return 0
    if len(argv) >= 3:
        size = int(argv[4]) if len(argv) > 4 else None
        written = download(argv[1], argv[2], argv[3] if len(argv) > 3 else None, size)
        print("-> %s (%d bytes)" % (argv[2], written))
        return 0
    print("usage: fetch.py URL DEST [SHA256 [SIZE]] | fetch.py --manifest URL DIR")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:  # noqa: BLE001 - CLI: печатаем причину и выходим
        sys.stderr.write("ERROR: %s\n" % exc)
        sys.exit(1)
