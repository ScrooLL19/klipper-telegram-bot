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
import re
import ssl
import sys
import time
import urllib.request

UA = "klipper-tg-bot-fetch/1.0"
CHUNK = 65536
BAR_WIDTH = 20
NAME_WIDTH = 26


def _ssl_context():
    """Контекст с проверкой сертификата; понятная ошибка, если CA нет.
    Verifying TLS context with a readable error when CA certs are missing."""
    return ssl.create_default_context()


def _fmt_mb(value):
    return value / 1048576.0


def _progress(done, total, started, label):
    """Рисует прогресс-бар в одной строке.
    Renders an in-place progress bar."""
    elapsed = max(0.001, time.time() - started)
    speed = _fmt_mb(done) / elapsed
    name = os.path.basename(label)[:NAME_WIDTH]
    if total and total > 0:
        pct = min(100, int(done * 100 / total))
        filled = pct * BAR_WIDTH // 100
        bar = "#" * filled + "-" * (BAR_WIDTH - filled)
        line = "   [%s] %3d%%  %6.1f/%6.1f MB  %5.1f MB/s  %s" % (
            bar, pct, _fmt_mb(done), _fmt_mb(total), speed, name)
    else:
        line = "   %8.1f MB  %5.1f MB/s  %s" % (_fmt_mb(done), speed, name)
    sys.stdout.write("\r" + line[:100])
    sys.stdout.flush()


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


def _normalize_script(path):
    """Приводит .sh к LF — иначе OpenWrt ругается на CRLF.
    Converts .sh files to LF, otherwise OpenWrt fails on CRLF."""
    with open(path, "rb") as f:
        data = f.read()
    if b"\r\n" in data:
        with open(path, "wb") as f:
            f.write(data.replace(b"\r\n", b"\n"))


def download(url, dest, digest=None, size=None, timeout=45, retries=3):
    """Скачивает потоком с прогресс-баром, сверяет размер и sha256.
    Streams the file with a progress bar and verifies size and sha256."""
    last = None
    for attempt in range(retries):
        tmp = dest + ".part"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
                total = int(resp.headers.get("Content-Length") or 0) or int(size or 0)
                hasher = hashlib.sha256()
                done = 0
                started = time.time()
                os.makedirs(os.path.dirname(tmp) or ".", exist_ok=True)
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        hasher.update(chunk)
                        done += len(chunk)
                        _progress(done, total, started, dest)
                    f.flush()
                    os.fsync(f.fileno())
            sys.stdout.write("\n")
            sys.stdout.flush()
            if size and done != size:
                raise RuntimeError("size mismatch for %s: %d != %d" % (dest, done, size))
            if digest and hasher.hexdigest() != digest:
                raise RuntimeError("sha256 mismatch for %s" % dest)
            os.replace(tmp, dest)
            if dest.endswith(".sh"):
                _normalize_script(dest)
            return done
        except Exception as exc:  # noqa: BLE001 - повторяем любой сетевой сбой
            sys.stdout.write("\n")
            sys.stdout.flush()
            last = exc
            try:
                os.remove(tmp)
            except OSError:
                pass
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise RuntimeError(
                    "нет CA-сертификатов / no CA bundle, выполните: opkg install ca-bundle")
            if "mismatch" in str(exc):
                # Битый файл не лечится повтором — сразу на другое зеркало.
                # A corrupted file will not fix itself: fail fast for the next mirror.
                break
            if attempt < retries - 1:
                sys.stdout.write("   retry %d/%d: %s\n" % (attempt + 2, retries, exc))
                sys.stdout.flush()
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("download failed: %s (%s)" % (url, last))


def _safe_name(name):
    """Разрешает относительные пути, включая подкаталоги (core/state.py).
    Allows relative paths, subdirectories included (core/state.py)."""
    raw = str(name or "").strip().replace("\\", "/")
    parts = raw.split("/")
    if not raw or raw.startswith("/") or len(parts) > 4 or any(not p for p in parts):
        raise RuntimeError("unsafe file name: %r" % name)
    for part in parts:
        if part.startswith(".") or not re.match(r"^[A-Za-z0-9_.-]+$", part):
            raise RuntimeError("unsafe file name: %r" % name)
    return "/".join(parts)


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
    total_bytes = 0
    for name, meta in files.items():
        safe = _safe_name(name)
        meta = meta or {}
        url = "%s/%s?v=%s" % (base, safe, version)
        written = download(url, os.path.join(dest_dir, safe),
                           meta.get("sha256"), meta.get("size"))
        total_bytes += written
        print("   OK %-24s %8d bytes, sha256 verified" % (safe, written))
    return version, len(files), total_bytes


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
