#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Media File Cover Resizer Py
===========================

Shrinks the cover art embedded in media files to a given resolution and JPEG
quality. Only the pictures are touched; all other tags are preserved.

Supported formats:
  MP3, WAV, AIFF, DSF, TTA (ID3)  |  FLAC  |  M4A/M4B/MP4/M4V (MP4)
  OGG/OGA/OPUS/SPX (Vorbis comment)  |  WMA/ASF  |  APE/WV/MPC (APEv2)

Install dependencies:
    pip install mutagen pillow
"""
from __future__ import annotations

import base64
import errno
import io
import os
import re
import shutil
import struct
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

try:
    import mutagen
    from mutagen.apev2 import BINARY, APEv2, APEValue
    from mutagen.asf import ASF, ASFByteArrayAttribute
    from mutagen.flac import FLAC, Picture
    from mutagen.id3 import ID3
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.oggflac import OggFLAC
    from mutagen.oggopus import OggOpus
    from mutagen.oggspeex import OggSpeex
    from mutagen.oggvorbis import OggVorbis
    from PIL import Image, ImageOps, UnidentifiedImageError
    try:
        from PIL import ImageCms
    except ImportError:  # Pillow built without LittleCMS
        ImageCms = None
except ImportError as _e:  # pragma: no cover
    print(f"Required libraries are missing ({_e}).\n"
          "Install them with:  pip install mutagen pillow")
    try:
        input("\nPress Enter to exit...")
    except Exception:
        pass
    sys.exit(1)

# ─────────────────────────────── Constants ─────────────────────────────────

APP_NAME = "Media File Cover Resizer Py"
OUT_DIRNAME = "Cover_Resized"
STEPS = 4  # processing stages per file (drives the per-file progress bar)

MEDIA_EXTS = {
    ".mp3", ".flac", ".m4a", ".m4b", ".mp4", ".m4v", ".ogg", ".oga", ".opus",
    ".spx", ".wma", ".asf", ".wav", ".aif", ".aiff", ".ape", ".wv", ".mpc",
    ".tta", ".dsf",
}

RESAMPLE = getattr(Image, "Resampling", Image).LANCZOS

YES = {"y", "yes", "1", "+", "true", "on"}
NO = {"n", "no", "0", "-", "false", "off"}

MODE_TITLES = {
    1: "replace the original files",
    2: f'save to the "{OUT_DIRNAME}" folder',
    3: 'save next to the originals with a "_1" suffix',
}


class Colors:
    def __init__(self, enabled: bool):
        e = (lambda c: c) if enabled else (lambda c: "")
        self.end = e("\033[0m")
        self.bold = e("\033[1m")
        self.red = e("\033[31m")
        self.green = e("\033[32m")
        self.yellow = e("\033[33m")
        self.cyan = e("\033[36m")
        self.bg_green = e("\033[1;30;42m")
        self.bg_yellow = e("\033[1;30;43m")
        self.bg_red = e("\033[1;97;41m")


C = Colors(False)

# ─────────────────────────────── Data models ───────────────────────────────


@dataclass
class Config:
    root: str
    max_w: int = 300
    max_h: int = 300
    crop: bool = False
    quality: int = 83
    recursive: bool = True
    mode: int = 2
    workers: int = 1

    @property
    def out_dir(self) -> str:
        return os.path.join(self.root, OUT_DIRNAME)


@dataclass
class Img:
    data: bytes
    width: int
    height: int


@dataclass
class FileResult:
    rel: str
    status: str = "ok"          # ok | partial | skipped | error
    message: str = ""
    orig_bytes: int = 0
    new_bytes: int = 0


class Skip(Exception):
    """The file was skipped on purpose (no cover, etc.)."""


class ProcessError(Exception):
    """Processing error that already carries a human-readable description."""


# ───────────────────────────── Image processing ────────────────────────────

def process_image(raw: bytes, cfg: Config) -> Img:
    """Shrinks a cover and saves it as a maximally compatible JPEG."""
    im = Image.open(io.BytesIO(raw))
    im.load()
    icc = im.info.get("icc_profile")

    # Honor EXIF orientation (the EXIF data itself is not written to the result)
    try:
        im = ImageOps.exif_transpose(im)
    except Exception:
        pass

    # Transparency -> white background
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        im = bg

    # Convert to sRGB if the image has a foreign colour profile (keeps colours correct)
    if icc and ImageCms is not None and im.mode in ("RGB", "CMYK"):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            if "srgb" not in (ImageCms.getProfileDescription(src) or "").lower():
                im = ImageCms.profileToProfile(
                    im, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:
            pass

    if im.mode != "RGB":
        im = im.convert("RGB")

    box_w, box_h = cfg.max_w, cfg.max_h
    if cfg.crop:
        w, h = im.size
        side = min(w, h)
        left, top = (w - side) // 2, (h - side) // 2
        im = im.crop((left, top, left + side, top + side))
        box_w = box_h = min(cfg.max_w, cfg.max_h)

    # thumbnail() keeps the aspect ratio, never exceeds the box and never upscales
    im.thumbnail((box_w, box_h), RESAMPLE, reducing_gap=3.0)

    buf = io.BytesIO()
    im.save(
        buf, "JPEG",
        quality=cfg.quality,
        optimize=True,
        progressive=False,   # baseline JPEG: understood by every player
        subsampling=2,       # 4:2:0, the de-facto standard for cover art
        exif=b"",            # no EXIF
    )
    return Img(buf.getvalue(), im.width, im.height)


# ───────────────────────── Format handlers (read/write) ────────────────────

def _id3_major(path: str) -> int:
    try:
        with open(path, "rb") as f:
            head = f.read(10)
        if head[:3] == b"ID3":
            return head[3]
    except OSError:
        pass
    return 3


def _read_id3v1(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() < 128:
                return None
            f.seek(-128, os.SEEK_END)
            tail = f.read(128)
        return tail if tail[:3] == b"TAG" else None
    except OSError:
        return None


class ID3Handler:
    def extract(self, audio) -> List[bytes]:
        return [bytes(f.data) for f in audio.tags.getall("APIC")]

    def replace(self, audio, results):
        for frame, r in zip(audio.tags.getall("APIC"), results):
            if r:
                frame.data = r.data
                frame.mime = "image/jpeg"

    def save(self, audio, path):
        # Keep the original ID3v2 version (2.3 or 2.4)
        v2 = 4 if _id3_major(path) == 4 else 3
        if isinstance(audio, MP3):
            # Leave a legacy ID3v1 block untouched: it uses single-byte encodings,
            # and mutagen would rewrite non-Latin text in it as "?".
            tail = _read_id3v1(path)
            audio.save(v1=0, v2_version=v2)
            if tail:
                with open(path, "ab") as f:
                    f.write(tail)
        else:
            try:
                audio.save(v2_version=v2)
            except TypeError:
                audio.save()


class FlacHandler:
    def extract(self, audio):
        return [bytes(p.data) for p in audio.pictures]

    def replace(self, audio, results):
        for p, r in zip(audio.pictures, results):
            if r:
                p.data, p.mime = r.data, "image/jpeg"
                p.width, p.height, p.depth, p.colors = r.width, r.height, 24, 0

    def save(self, audio, path):
        audio.save()


class MP4Handler:
    def extract(self, audio):
        if not audio.tags:
            return []
        return [bytes(c) for c in audio.tags.get("covr", [])]

    def replace(self, audio, results):
        old = audio.tags.get("covr", [])
        audio.tags["covr"] = [
            MP4Cover(r.data, imageformat=MP4Cover.FORMAT_JPEG) if r else c
            for c, r in zip(old, results)
        ]

    def save(self, audio, path):
        audio.save()


class OggHandler:
    KEY = "metadata_block_picture"

    def _pics(self, audio):
        return [Picture(base64.b64decode(v)) for v in audio.tags.get(self.KEY, [])]

    def extract(self, audio):
        if not audio.tags:
            return []
        return [bytes(p.data) for p in self._pics(audio)]

    def replace(self, audio, results):
        out = []
        for p, r in zip(self._pics(audio), results):
            if r:
                p.data, p.mime = r.data, "image/jpeg"
                p.width, p.height, p.depth, p.colors = r.width, r.height, 24, 0
            out.append(base64.b64encode(p.write()).decode("ascii"))
        audio.tags[self.KEY] = out

    def save(self, audio, path):
        audio.save()


def _asf_parse(raw: bytes):
    ptype = raw[0]
    size = struct.unpack("<I", raw[1:5])[0]

    def zstr(pos):
        end = pos
        while end + 1 < len(raw) and raw[end:end + 2] != b"\x00\x00":
            end += 2
        return raw[pos:end].decode("utf-16-le", "replace"), end + 2

    mime, pos = zstr(5)
    desc, pos = zstr(pos)
    return ptype, mime, desc, raw[pos:pos + size]


def _asf_build(ptype, mime, desc, data) -> bytes:
    return (struct.pack("<BI", ptype, len(data))
            + mime.encode("utf-16-le") + b"\x00\x00"
            + desc.encode("utf-16-le") + b"\x00\x00" + data)


class ASFHandler:
    KEY = "WM/Picture"

    def extract(self, audio):
        if not audio.tags:
            return []
        return [_asf_parse(bytes(a.value))[3] for a in audio.tags.get(self.KEY, [])]

    def replace(self, audio, results):
        out = []
        for a, r in zip(audio.tags.get(self.KEY, []), results):
            if r:
                ptype, _mime, desc, _data = _asf_parse(bytes(a.value))
                out.append(ASFByteArrayAttribute(_asf_build(ptype, "image/jpeg", desc, r.data)))
            else:
                out.append(a)
        audio.tags[self.KEY] = out

    def save(self, audio, path):
        audio.save()


class APEHandler:
    def _keys(self, audio):
        return [k for k in audio.tags.keys()
                if k.lower().startswith("cover art")
                and getattr(audio.tags[k], "kind", None) == BINARY]

    @staticmethod
    def _split(raw: bytes):
        i = raw.find(b"\x00")
        return (raw[:i], raw[i + 1:]) if i >= 0 else (b"", raw)

    def extract(self, audio):
        return [self._split(bytes(audio.tags[k].value))[1] for k in self._keys(audio)]

    def replace(self, audio, results):
        for k, r in zip(self._keys(audio), results):
            if not r:
                continue
            name, _ = self._split(bytes(audio.tags[k].value))
            stem = os.path.splitext(name.decode("utf-8", "ignore"))[0] or "cover"
            audio.tags[k] = APEValue(stem.encode("utf-8") + b".jpg\x00" + r.data, BINARY)

    def save(self, audio, path):
        audio.save()


def pick_handler(audio):
    tags = getattr(audio, "tags", None)
    if isinstance(audio, FLAC):
        return FlacHandler()
    if isinstance(tags, ID3):
        return ID3Handler()
    if isinstance(audio, MP4):
        return MP4Handler()
    if isinstance(audio, (OggVorbis, OggOpus, OggFLAC, OggSpeex)):
        return OggHandler()
    if isinstance(audio, ASF):
        return ASFHandler()
    if isinstance(tags, APEv2):
        return APEHandler()
    return None


# ───────────────────────────── File processing ─────────────────────────────

def explain(e: BaseException) -> str:
    """Turns an exception into a human-readable reason."""
    if isinstance(e, ProcessError):
        return str(e)
    if isinstance(e, PermissionError):
        return ("Access denied: the file is in use by another program (player, torrent client), "
                f"is read-only, or you lack the required permissions. [{e}]")
    if isinstance(e, FileNotFoundError):
        return "File or folder not found (it may have been deleted or moved while the script was running)."
    if isinstance(e, UnidentifiedImageError):
        return "Could not recognize the cover image: the data is corrupted or the image format is not supported."
    if isinstance(e, Image.DecompressionBombError):
        return "The cover image has a suspiciously huge resolution and was rejected for safety reasons."
    if isinstance(e, MemoryError):
        return "Not enough memory to process the image."
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return "Not enough free disk space."
    if isinstance(e, mutagen.MutagenError):
        return ("Failed to read/write tags: the file is corrupted or has a non-standard structure. "
                f"[{type(e).__name__}: {e}]")
    return f"Unexpected error {type(e).__name__}: {e}"


def destination(cfg: Config, path: str) -> str:
    if cfg.mode == 1:
        return path
    if cfg.mode == 2:
        return os.path.join(cfg.out_dir, os.path.relpath(path, cfg.root))
    base, ext = os.path.splitext(path)
    return f"{base}_1{ext}"


def looks_like_copy(path: str) -> bool:
    """A file named 'name_1.ext' that has a sibling 'name.ext' is one of our earlier copies."""
    base, ext = os.path.splitext(path)
    return base.endswith("_1") and os.path.exists(base[:-2] + ext)


INTERRUPTED_MSG = "Processing was interrupted by the user before this file was started."


def process_file(path: str, cfg: Config, ui: "ProgressUI", stop: threading.Event) -> FileResult:
    rel = os.path.relpath(path, cfg.root)
    res = FileResult(rel=rel)
    if stop.is_set():
        res.status, res.message = "skipped", INTERRUPTED_MSG
        ui.count_only(res)
        return res

    key = threading.get_ident()
    ui.begin(key, rel)
    created = None
    dst = path
    try:
        dst = destination(cfg, path)
        if cfg.mode == 3:
            if looks_like_copy(path):
                raise Skip('The file looks like a copy created earlier (the original without "_1" exists); '
                           'skipped to avoid producing "_1_1" files.')
            if os.path.exists(dst):
                raise Skip(f'"{os.path.basename(dst)}" already exists and was not overwritten. '
                           "Delete or rename it if you want to create it again.")

        ui.step(key, 0, "reading tags")
        audio = mutagen.File(path)
        if audio is None:
            raise Skip("File format not recognized or the file is corrupted (its tags could not be opened).")
        handler = pick_handler(audio)
        if handler is None:
            if getattr(audio, "tags", None) is None:
                raise Skip("The file has no tags, so it has no embedded cover.")
            raise Skip(f'Tag type "{type(audio.tags).__name__}" is not supported for cover handling.')
        covers = handler.extract(audio)
        if not covers:
            raise Skip("No embedded cover found.")

        ui.step(key, 1, "resizing cover")
        results: List[Optional[Img]] = []
        warns: List[str] = []
        for i, raw in enumerate(covers, 1):
            try:
                img = process_image(raw, cfg)
                results.append(img)
                res.orig_bytes += len(raw)
                res.new_bytes += len(img.data)
            except Exception as e:  # noqa: BLE001
                results.append(None)
                warns.append(f"cover #{i}: {explain(e)}")
        if all(r is None for r in results):
            raise ProcessError("None of the covers could be processed: " + "; ".join(warns))

        if dst != path:
            ui.step(key, 2, "copying file")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(path, dst)
            created = dst
            audio = mutagen.File(dst)
            handler = pick_handler(audio)
        else:
            ui.step(key, 2, "preparing to write")

        ui.step(key, 3, "writing tags")
        handler.replace(audio, results)
        handler.save(audio, dst)
        created = None

        if warns:
            res.status = "partial"
            res.message = ("The file was processed partially. Problematic covers were left unchanged: "
                           + "; ".join(warns))
    except Skip as e:
        res.status, res.message = "skipped", str(e)
    except Exception as e:  # noqa: BLE001
        res.status, res.message = "error", explain(e)
        if created:
            try:
                os.remove(created)
                res.message += " The incomplete copy was removed."
            except OSError:
                pass
        elif dst == path:
            res.message += " If the error occurred while writing, please check the integrity of this file."
    finally:
        ui.finish(key, res)
    return res


# ───────────────────────────── Progress display ────────────────────────────

def dw(s: str) -> int:
    """Display width of a string in terminal cells (accounts for wide characters)."""
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in "WF" else 1)
               for c in s)


def fit(s: str, width: int) -> str:
    if width <= 0:
        return ""
    if dw(s) <= width:
        return s
    out, w = [], 0
    for ch in s:
        cw = dw(ch)
        if w + cw > width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


def fit_name(name: str, width: int) -> str:
    """Shortens a file name while keeping its extension."""
    if dw(name) <= width:
        return name
    stem, ext = os.path.splitext(name)
    if width > dw(ext) + 4:
        return fit(stem, width - dw(ext)) + ext
    return fit(name, width)


def bar(frac: float, width: int) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def fmt_time(sec: Optional[float]) -> str:
    if sec is None:
        return "--:--"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class ProgressUI:
    STATUS_MARK = {"ok": "✔", "partial": "⚠", "skipped": "⏭", "error": "✖"}

    def __init__(self, total: int, workers: int, tty: bool):
        self.total = total
        self.tty = tty
        self.lock = threading.RLock()
        self.active = {}
        self.counts = {"ok": 0, "partial": 0, "skipped": 0, "error": 0}
        self.done = 0
        self.start = time.time()
        self.drawn = 0
        self._last = 0.0
        rows = shutil.get_terminal_size((100, 30)).lines
        self.max_active = max(1, min(workers, rows - 6))
        self._stop = threading.Event()
        self._ticker = None

    # ---- events ------------------------------------------------------
    def begin(self, key, name):
        with self.lock:
            self.active[key] = [name, 0, "starting", time.time()]
            self.render()

    def step(self, key, stage, label):
        with self.lock:
            if key in self.active:
                self.active[key][1] = stage
                self.active[key][2] = label
            self.render()

    def finish(self, key, res: FileResult):
        with self.lock:
            self.active.pop(key, None)
            self._count(res)
            self.render()

    def count_only(self, res: FileResult):
        with self.lock:
            self._count(res)
            self.render()

    def _count(self, res):
        self.done += 1
        self.counts[res.status] += 1
        if not self.tty:
            print(f"[{self.done}/{self.total}] {self.STATUS_MARK[res.status]} {res.rel}", flush=True)

    # ---- drawing -----------------------------------------------------
    def open(self):
        if self.tty:
            self._ticker = threading.Thread(target=self._tick, daemon=True)
            self._ticker.start()
        self.render(force=True)

    def _tick(self):
        while not self._stop.wait(0.25):
            with self.lock:
                self.render(force=True)

    def close(self):
        self._stop.set()
        if self._ticker:
            self._ticker.join(timeout=1)
        with self.lock:
            self.render(force=True)

    def render(self, force=False):
        if not self.tty:
            return
        now = time.time()
        if not force and now - self._last < 0.07:
            return
        self._last = now
        cols = max(40, shutil.get_terminal_size((100, 30)).columns - 1)

        items = sorted(self.active.values(), key=lambda a: a[3])
        shown = items[: self.max_active]
        hidden = len(items) - len(shown)

        lines = []
        head = "Currently processing:" + (f"  (+{hidden} more)" if hidden > 0 else "")
        lines.append(f"{C.bold}{head}{C.end}")
        for name, stage, label, _t in shown:
            pref = f"  ► {bar(stage / STEPS, 10)} {int(stage / STEPS * 100):3d}%  "
            suff = f"  · {label}"
            room = max(8, cols - dw(pref) - dw(suff))
            lines.append(fit(pref + fit_name(name, room) + suff, cols))
        while len(lines) < self.max_active + 1:
            lines.append("")

        part = sum(a[1] / STEPS for a in self.active.values())
        frac = (self.done + part) / self.total if self.total else 1.0
        elapsed = now - self.start
        if self.done >= self.total:
            eta = 0
        elif frac > 0.01:
            eta = elapsed * (1 - frac) / frac
        else:
            eta = None
        speed = self.done / elapsed if elapsed > 0 and self.done else 0.0

        barw = max(10, min(40, cols - 34))
        lines.append(fit(f"Overall progress  {bar(frac, barw)} {frac * 100:5.1f}%   {self.done}/{self.total}", cols))

        c = self.counts
        tail = f"  │ elapsed {fmt_time(elapsed)}  │ ETA {fmt_time(eta)}  │ {speed:.1f} files/s"
        plain = f"  ✔ {c['ok']}  ⚠ {c['partial']}  ⏭ {c['skipped']}  ✖ {c['error']}" + tail
        if dw(plain) <= cols:
            lines.append(
                f"  {C.green}✔ {c['ok']}{C.end}  {C.yellow}⚠ {c['partial']}{C.end}  "
                f"{C.cyan}⏭ {c['skipped']}{C.end}  {C.red}✖ {c['error']}{C.end}" + tail)
        else:
            lines.append(fit(plain, cols))

        up = f"\033[{self.drawn}A\r" if self.drawn else ""
        sys.stdout.write(up + "".join(l + "\033[K\n" for l in lines))
        sys.stdout.flush()
        self.drawn = len(lines)


# ───────────────────────────── User input ──────────────────────────────────

RES_RE = re.compile(r"^(\d{1,5})\s*(?:[xX×*,]\s*(\d{1,5}))?$")


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        s = input(f"{prompt} [{hint}]: ").strip().lower()
        if not s:
            return default
        if s in YES:
            return True
        if s in NO:
            return False
        print("   Please enter 'y' (yes) or 'n' (no), or just press Enter.")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        s = input(f"{prompt} [{default}]: ").strip()
        if not s:
            return default
        if s.isdigit() and lo <= int(s) <= hi:
            return int(s)
        print(f"   Please enter a whole number from {lo} to {hi}, or just press Enter.")


def ask_resolution():
    while True:
        s = input("1) Maximum cover resolution, e.g. 300x300 [300x300]: ").strip()
        if not s:
            return 300, 300
        m = RES_RE.match(s)
        if m:
            w = int(m.group(1))
            h = int(m.group(2) or m.group(1))
            if 16 <= w <= 10000 and 16 <= h <= 10000:
                return w, h
        print("   Format: WIDTHxHEIGHT (e.g. 500x500), or a single number for a square. Allowed range: 16-10000.")


def ask_mode() -> int:
    print("5) How to save the results:")
    for k, v in MODE_TITLES.items():
        print(f"     {k} - {v}")
    while True:
        s = input("   Your choice [2]: ").strip()
        if not s:
            return 2
        if s in ("1", "2", "3"):
            return int(s)
        print("   Please enter 1, 2 or 3.")


def choose_folder_console(default: str) -> Optional[str]:
    while True:
        s = input(f"Folder to process (Enter = {default}): ").strip().strip('"')
        if not s:
            return default
        if os.path.isdir(s):
            return os.path.abspath(s)
        print("   That folder does not exist, please try again.")


def choose_folder(default: str) -> Optional[str]:
    """Shows a small window with a folder-picker button. Returns None if it is closed."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        win = tk.Tk()
    except Exception:
        return choose_folder_console(default)

    state = {"path": default, "ok": False}
    win.title(f"{APP_NAME} - choose folder")
    win.resizable(False, False)
    tk.Label(win, text="Folder with media files:", font=("TkDefaultFont", 10, "bold")
             ).pack(anchor="w", padx=14, pady=(14, 2))
    var = tk.StringVar(value=default)
    tk.Label(win, textvariable=var, wraplength=480, justify="left", anchor="w",
             relief="sunken", bg="white", padx=6, pady=6, width=64
             ).pack(fill="x", padx=14)

    def browse():
        d = filedialog.askdirectory(parent=win, initialdir=state["path"],
                                    mustexist=True, title="Select a folder")
        if d:
            state["path"] = os.path.normpath(d)
            var.set(state["path"])

    def go():
        state["ok"] = True
        win.destroy()

    row = tk.Frame(win)
    row.pack(pady=14)
    tk.Button(row, text="Choose another folder…", command=browse, width=22).pack(side="left", padx=5)
    tk.Button(row, text="Continue ▶", command=go, width=14, default="active").pack(side="left", padx=5)
    tk.Button(row, text="Exit", command=win.destroy, width=8).pack(side="left", padx=5)
    win.bind("<Return>", lambda e: go())
    win.bind("<Escape>", lambda e: win.destroy())

    win.update_idletasks()
    x = (win.winfo_screenwidth() - win.winfo_width()) // 2
    y = (win.winfo_screenheight() - win.winfo_height()) // 3
    win.geometry(f"+{x}+{y}")
    try:
        win.lift()
        win.attributes("-topmost", True)
        win.after(400, lambda: win.attributes("-topmost", False))
    except Exception:
        pass
    win.mainloop()
    return state["path"] if state["ok"] else None


# ───────────────────────────── Scanning ────────────────────────────────────

def scan_files(cfg: Config) -> List[str]:
    skip_dir = os.path.normcase(os.path.abspath(cfg.out_dir)) if cfg.mode == 2 else None
    found: List[str] = []
    for cur, dirs, files in os.walk(cfg.root):
        if skip_dir:
            dirs[:] = [d for d in dirs
                       if os.path.normcase(os.path.abspath(os.path.join(cur, d))) != skip_dir]
        dirs.sort(key=str.lower)
        for name in sorted(files, key=str.lower):
            if name.startswith("._"):  # macOS resource-fork files
                continue
            if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                found.append(os.path.join(cur, name))
        if not cfg.recursive:
            break
    return found


# ───────────────────────────── Log and summary ─────────────────────────────

def settings_text(cfg: Config) -> List[str]:
    return [
        f"Folder:               {cfg.root}",
        f"Subfolders:           {'included' if cfg.recursive else 'not included'}",
        f"Max resolution:       {cfg.max_w}x{cfg.max_h} (aspect ratio is preserved)",
        f"Crop to square:       {'yes' if cfg.crop else 'no'}",
        f"JPEG quality:         {cfg.quality}",
        f"Save mode:            {MODE_TITLES[cfg.mode]}",
        f"Threads:              {cfg.workers}",
    ]


def write_log(cfg: Config, results: List[FileResult], started: datetime,
              ended: datetime, interrupted: bool) -> Optional[str]:
    issues = [r for r in results if r.status != "ok"]
    order = {"error": 0, "partial": 1, "skipped": 2}
    issues.sort(key=lambda r: (order[r.status], r.rel.lower()))
    label = {"error": "ERROR", "partial": "PARTIAL", "skipped": "SKIPPED"}

    cnt = {k: sum(1 for r in results if r.status == k) for k in ("ok", "partial", "skipped", "error")}
    lines = [
        f"{APP_NAME} - issue report",
        "=" * 70,
        f"Started:  {started:%Y-%m-%d %H:%M:%S}",
        f"Finished: {ended:%Y-%m-%d %H:%M:%S}",
        "",
        "SETTINGS",
        *["  " + s for s in settings_text(cfg)],
        "",
        "SUMMARY",
        f"  Total files:           {len(results)}",
        f"  Succeeded:             {cnt['ok']}",
        f"  Partially processed:   {cnt['partial']}",
        f"  Skipped:               {cnt['skipped']}",
        f"  Errors:                {cnt['error']}",
    ]
    if interrupted:
        lines += ["", "!!! The run was interrupted by the user (Ctrl+C); some files were not processed."]
    lines += ["", "DETAILS", "=" * 70]
    for r in issues:
        lines += [f"[{label[r.status]}] {r.rel}", f"    Reason: {r.message}", ""]

    name = f"Cover_Resizer_{started:%Y-%m-%d_%H-%M-%S}.log"
    for folder in (cfg.root, os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.join(folder, name)
        try:
            with open(path, "w", encoding="utf-8-sig") as f:  # BOM so Windows Notepad detects UTF-8
                f.write("\n".join(lines))
            return path
        except OSError:
            continue
    return None


def beep():
    try:
        if os.name == "nt":
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
    except Exception:
        pass


def print_summary(cfg, results, elapsed, interrupted, log_path):
    cnt = {k: sum(1 for r in results if r.status == k) for k in ("ok", "partial", "skipped", "error")}
    issues = cnt["partial"] + cnt["skipped"] + cnt["error"]

    if interrupted:
        color, title = C.bg_yellow, "⏹  PROCESSING INTERRUPTED"
    elif cnt["error"]:
        color, title = C.bg_red, "✖  FINISHED WITH ERRORS"
    elif issues:
        color, title = C.bg_yellow, "⚠  FINISHED (SOME FILES WERE SKIPPED)"
    else:
        color, title = C.bg_green, "✔  PROCESSING COMPLETED SUCCESSFULLY"

    width = 56
    pad = width - dw(title)
    left = pad // 2
    print()
    print(f"{color}╔{'═' * width}╗{C.end}")
    print(f"{color}║{' ' * left}{title}{' ' * (pad - left)}║{C.end}")
    print(f"{color}╚{'═' * width}╝{C.end}")
    print()
    print(f"  Total files:           {len(results)}")
    print(f"  {C.green}✔ Succeeded:           {cnt['ok']}{C.end}")
    print(f"  {C.yellow}⚠ Partial:             {cnt['partial']}{C.end}")
    print(f"  {C.cyan}⏭ Skipped:             {cnt['skipped']}{C.end}")
    print(f"  {C.red}✖ Errors:              {cnt['error']}{C.end}")
    print(f"  Time elapsed:          {fmt_time(elapsed)}")
    orig = sum(r.orig_bytes for r in results)
    new = sum(r.new_bytes for r in results)
    if orig:
        print(f"  Cover data:            {fmt_size(orig)} → {fmt_size(new)}  (-{(1 - new / orig) * 100:.0f}%)")
    if cfg.mode == 2 and cnt["ok"] + cnt["partial"]:
        print(f"  Output folder:         {cfg.out_dir}")

    if issues:
        print(f"\n{C.yellow}Attention: some files were not processed or were processed incompletely.{C.end}")
        shown = [r for r in results if r.status in ("error", "partial")][:5]
        for r in shown:
            print(f"   • {r.rel}\n       {fit(r.message, 100)}")
        if log_path:
            print(f"\n  A detailed description of every case was saved to the log file:\n  {C.bold}{log_path}{C.end}")
        else:
            print(f"\n  {C.red}Could not create a log file (no write permission).{C.end}")
    print()
    beep()


# ───────────────────────────── Main flow ───────────────────────────────────

def run_session() -> bool:
    """Returns True if the 'start over / exit' question should be asked."""
    print(f"\n{C.bold}{C.cyan}═══ {APP_NAME} ═══{C.end}")
    print("Shrinks embedded cover art in media files (output is JPEG without EXIF)\n")

    print("A folder selection window will open...")
    root = choose_folder(os.getcwd())
    if root is None:
        print("Folder selection cancelled.")
        return False
    print(f"Folder: {root}\n")

    w, h = ask_resolution()
    crop = ask_yes_no("2) Crop to a centered 1:1 square if the cover is not square?", False)
    quality = ask_int("3) JPEG quality (1-100)", 83, 1, 100)
    recursive = ask_yes_no("4) Process subfolders?", True)
    mode = ask_mode()
    cpu = os.cpu_count() or 1
    workers = ask_int(f"6) Number of files to process simultaneously (threads, 1-64; CPU cores here: {cpu})", 1, 1, 64)

    cfg = Config(root, w, h, crop, quality, recursive, mode, workers)

    print("\nScanning folder...")
    files = scan_files(cfg)
    if not files:
        print(f"{C.yellow}No media files found in the selected folder.{C.end}")
        return True

    print(f"\n{C.bold}Run settings:{C.end}")
    for s in settings_text(cfg):
        print("  " + s)
    print(f"  Files found:          {len(files)}")
    if mode == 1:
        print(f"\n{C.yellow}Warning: the original files will be modified and cannot be restored.{C.end}")
    input("\nPress Enter to start (Ctrl+C to cancel)... ")
    print()

    tty = sys.stdout.isatty()
    ui = ProgressUI(len(files), cfg.workers, tty)
    stop = threading.Event()
    results: List[FileResult] = []
    started = datetime.now()
    t0 = time.time()
    interrupted = False

    ui.open()
    ex = ThreadPoolExecutor(max_workers=cfg.workers)
    futures = [ex.submit(process_file, p, cfg, ui, stop) for p in files]
    try:
        for fut in as_completed(futures):
            results.append(fut.result())
    except KeyboardInterrupt:
        interrupted = True
        stop.set()
        for f in futures:
            f.cancel()
    finally:
        ex.shutdown(wait=True)
        ui.close()

    done_rels = {r.rel for r in results}
    for f in futures:
        if f.done() and not f.cancelled():
            r = f.result()
            if r.rel not in done_rels:
                results.append(r)
                done_rels.add(r.rel)
    for p in files:
        rel = os.path.relpath(p, cfg.root)
        if rel not in done_rels:
            results.append(FileResult(rel, "skipped", INTERRUPTED_MSG))

    ended = datetime.now()
    log_path = None
    if any(r.status != "ok" for r in results):
        log_path = write_log(cfg, results, started, ended, interrupted)
    print_summary(cfg, results, time.time() - t0, interrupted, log_path)
    return True


def ask_restart() -> bool:
    print(f"{C.bold}What next?{C.end}")
    print("   1 - Start over")
    print("   2 - Exit")
    while True:
        s = input("Your choice [2]: ").strip().lower()
        if s in ("1", "s", "r", "start", "restart"):
            return True
        if s in ("", "2", "e", "q", "exit", "quit"):
            return False
        print("   Please enter 1 or 2.")


def main():
    global C
    if os.name == "nt":
        os.system("")  # enables ANSI escape sequences in the Windows 10+ console
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    C = Colors(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))

    try:
        while True:
            if run_session() and ask_restart():
                continue
            break
    except (KeyboardInterrupt, EOFError):
        print("\nExiting.")
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        try:
            input("\nAn unexpected error occurred. Press Enter to exit...")
        except Exception:
            pass
    print("Goodbye!")


if __name__ == "__main__":
    main()
