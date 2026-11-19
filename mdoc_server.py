#!/usr/bin/env python3
"""
mdoc_server.py - serve mdoc Stage Map and your SerialEM data folders to a browser.

The viewer page (index.html) must sit next to this script, or be given with --html.
Only the folders you list are reachable. Everything is read-only except session
files and movies: the viewer can save its state as a .json file, and an acquisition
movie as an .mp4 file, inside those folders (turn that off with --read-only).
Movies are encoded by ffmpeg on this machine, if it is installed.

Usage
    python3 mdoc_server.py /path/to/data [/another/root ...] [--port 8765] [--host 127.0.0.1]

Then open  http://localhost:8765  in Chrome, Edge or Firefox.

Examples
    python3 mdoc_server.py /data/krios
    python3 mdoc_server.py D:\\SerialEM\\Sessions --port 9000          (Windows)
    python3 mdoc_server.py /data/krios --host 0.0.0.0                 (reachable from other machines)
    python3 mdoc_server.py /data/krios --read-only                    (never write session files)

To view data on a remote workstation without opening a port to the network,
start the server there on 127.0.0.1 and tunnel it:
    ssh -L 8765:localhost:8765 user@workstation
then open http://localhost:8765 on your own computer.

Requires Python 3.7+. If numpy is installed, the server shrinks each image piece
before sending it (about 80x less data over the network); without numpy the viewer
reads full pieces and shrinks them itself.
"""
import argparse
import errno
import getpass
import socket
import atexit
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import numpy as np
except ImportError:
    np = None

APP = "mdoc-stage-map"
VERSION = "1.3"
MRC_EXT = (".mrc", ".st", ".map", ".mrcs", ".ali", ".preali")
CHUNK = 1 << 20          # 1 MiB per write when streaming a file
LIST_LIMIT = 20000       # most mdoc files returned by one recursive listing
TGTS_RE = re.compile(r"_tgts(_p\d+)?\.txt$", re.I)   # PACEtomo target files

SAVE_EXT = (".json",)    # the only files the server will write
SAVE_LIMIT = 64 << 20    # largest session file accepted, in bytes

MOVIES = {}              # id -> {"proc", "dir", "out", "log", "n", "t"}: movies being encoded by ffmpeg
MOVIE_LOCK = threading.Lock()
MOVIE_MAX_FRAMES = 30000 # about 16 minutes at 30 frames per second
MOVIE_MAX_FRAME = 16 << 20

ROOTS = {}               # name -> absolute real path
HTML_PATH = None
VERBOSE = False
WRITABLE = True


def norm(p):
    # compare paths as written, without resolving links, so symlinked, junction and
    # network-drive subfolders inside a shared folder work; '..' is rejected separately
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def inside(path, root):
    try:
        return os.path.commonpath([norm(path), norm(root)]) == norm(root)
    except ValueError:   # different drives on Windows
        return False


def resolve(p):
    """Map an API path to (real filesystem path, API path). Accepts 'root/sub/file' or an absolute path."""
    p = (p or "").strip()
    if p in ("", "/"):
        return None, ""
    if os.path.isabs(p) or re.match(r"^[A-Za-z]:[\\/]", p):
        if ".." in re.split(r"[\\/]", p):
            raise PermissionError("'..' is not allowed in paths.")
        real = os.path.normpath(os.path.abspath(p))
        for name, root in ROOTS.items():
            if inside(real, root):
                rel = os.path.relpath(real, root).replace(os.sep, "/")
                return real, name if rel == "." else f"{name}/{rel}"
        raise PermissionError("That path is outside the folders this server shares.")
    parts = [x for x in p.replace("\\", "/").split("/") if x not in ("", ".")]
    if not parts or parts[0] not in ROOTS:
        raise FileNotFoundError(f"Unknown root '{parts[0] if parts else ''}'.")
    if ".." in parts:
        raise PermissionError("'..' is not allowed in paths.")
    root = ROOTS[parts[0]]
    real = os.path.normpath(os.path.join(root, *parts[1:]))
    if not inside(real, root):
        raise PermissionError("That path is outside the folders this server shares.")
    return real, "/".join(parts)


def read_binned_section(real, z, binf):
    """Read MRC section z and average bin x bin blocks. Returns (float32 bytes, width, height)."""
    with open(real, "rb") as f:
        h = f.read(1024)
    nx, ny, nz, mode = struct.unpack("<4i", h[:16])
    if not (0 < nx < 200000 and 0 < ny < 200000):
        raise ValueError("not a little-endian MRC file")
    nxt = max(0, struct.unpack("<i", h[92:96])[0])
    stamp, flags = struct.unpack("<2i", h[152:160])
    signed = stamp == 1146047817 and (flags & 1) == 1
    dt = {0: np.int8 if signed else np.uint8, 1: np.dtype("<i2"), 2: np.dtype("<f4"),
          6: np.dtype("<u2"), 12: np.dtype("<f2")}.get(mode)
    if dt is None:
        raise ValueError(f"MRC mode {mode} is not supported")
    if not 0 <= z < nz:
        raise ValueError(f"section {z} is outside the file (nz = {nz})")
    item = np.dtype(dt).itemsize
    a = np.fromfile(real, dtype=dt, count=nx * ny, offset=1024 + nxt + z * nx * ny * item)
    if a.size < nx * ny:
        raise ValueError(f"file is shorter than expected at section {z}")
    a = a.reshape(ny, nx)
    bw, bh = nx // binf, ny // binf
    a = a[:bh * binf, :bw * binf].astype(np.float32).reshape(bh, binf, bw, binf).mean(axis=(1, 3))
    return a.astype("<f4").tobytes(), bw, bh


FFMPEG = "auto"          # --ffmpeg: a path, "auto" (look on PATH) or "none"


def ffmpeg_path():
    if FFMPEG == "none":
        return None
    if FFMPEG != "auto":
        return FFMPEG if os.path.isfile(FFMPEG) else shutil.which(FFMPEG)
    exe = shutil.which("ffmpeg")
    if not exe:
        try:                                   # the copy that comes with the imageio-ffmpeg Python package
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            exe = None
    return exe


def ffmpeg_encoder(exe):
    """An H.264 encoder this ffmpeg really has (some builds lack libx264), else MPEG-4."""
    try:
        out = subprocess.run([exe, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        out = ""
    table = [("libx264", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]),
             ("h264_videotoolbox", ["-c:v", "h264_videotoolbox", "-b:v", "20M", "-allow_sw", "1"]),
             ("libopenh264", ["-c:v", "libopenh264", "-b:v", "20M"]),
             ("h264_nvenc", ["-c:v", "h264_nvenc", "-cq", "19", "-b:v", "0"])]
    for name, args in table:
        if f" {name} " in out:
            return name, args
    return "mpeg4", ["-c:v", "mpeg4", "-q:v", "2"]


def movie_drop(mid):
    """Stop the encoder of a movie and delete its temporary folder."""
    with MOVIE_LOCK:
        m = MOVIES.pop(mid, None)
    if not m:
        return
    try:
        if m["proc"].poll() is None:
            m["proc"].kill()
    except Exception:
        pass
    shutil.rmtree(m["dir"], ignore_errors=True)


def movie_sweep():
    """Forget movies nobody touched for an hour."""
    now = time.time()
    for mid in [k for k, m in list(MOVIES.items()) if now - m["t"] > 3600]:
        movie_drop(mid)


def listed(name):
    return name.lower().endswith(".mdoc") or bool(TGTS_RE.search(name))


def ext_filter(spec):
    """Build a file-name test from ?ext=: comma-separated extensions, 'tgts' for PACEtomo
    target files, '*' for every file. Empty means the default: mdoc and target files."""
    toks = [t.strip().lower().lstrip(".") for t in (spec or "").split(",") if t.strip()]
    if not toks:
        return listed
    if "*" in toks:
        return lambda n: True
    exts = tuple("." + t for t in toks if t != "tgts")
    tg = "tgts" in toks
    return lambda n: (bool(exts) and n.lower().endswith(exts)) or (tg and bool(TGTS_RE.search(n)))


def has_mrc(real_mdoc):
    base = real_mdoc[:-5]            # strip ".mdoc"
    if not base.lower().endswith(MRC_EXT):
        return False
    if os.path.isfile(base):
        return True
    stem, ext = os.path.splitext(base)  # TS_001.mrc.mdoc may go with TS_001.st, and the other way round
    alt = {".mrc": ".st", ".st": ".mrc"}.get(ext.lower())
    return bool(alt) and os.path.isfile(stem + alt)


class Handler(BaseHTTPRequestHandler):
    server_version = f"mdocStageMap/{VERSION}"

    def log_message(self, fmt, *args):
        if VERBOSE:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- responses ----------
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_err(self, status, msg):
        self.send_json({"error": msg}, status)

    def send_path(self, real, ctype="application/octet-stream"):
        size = os.path.getsize(real)
        start, end = 0, size - 1
        status = 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if not m or (m.group(1) == "" and m.group(2) == ""):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if m.group(1) == "":                      # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            else:
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(real, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                buf = f.read(min(CHUNK, left))
                if not buf:
                    break
                self.wfile.write(buf)
                left -= len(buf)

    # ---------- routes ----------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        arg = lambda k, d="": q.get(k, [d])[0]
        try:
            if url.path in ("/", "/index.html"):
                if not HTML_PATH or not os.path.isfile(HTML_PATH):
                    return self.send_err(404, "index.html not found. Put the viewer next to mdoc_server.py or pass --html.")
                return self.send_path(HTML_PATH, "text/html; charset=utf-8")
            if url.path == "/api/info":
                start = next(iter(ROOTS)) if len(ROOTS) == 1 else ""
                return self.send_json({"app": APP, "version": VERSION, "start": start, "numpy": np is not None,
                                       "write": WRITABLE, "sep": os.sep, "ffmpeg": ffmpeg_path() is not None,
                                       "roots": [{"name": n, "path": r} for n, r in ROOTS.items()]})
            if url.path == "/api/list":
                want = ext_filter(arg("ext"))
                names = {n.strip().lower() for n in arg("names").split(",") if n.strip()}
                if names:                       # only these file names (used to find tilt series anywhere below a folder)
                    want = lambda n, _w=want: n.lower() in names and _w(n)
                return self.api_list(arg("path"), arg("recursive") == "1", want)
            if url.path == "/api/stat":
                real, api = resolve(arg("path"))
                if real and os.path.islink(real) and not os.path.isfile(real):
                    # say why a symbolic link cannot be used, instead of a plain "not found"
                    target = os.readlink(real)
                    full = os.path.normpath(os.path.join(os.path.dirname(real), target))
                    why = "does not exist on this computer" if not os.path.exists(full) else "is not a file"
                    return self.send_json({"error": f"{os.path.basename(real)} is a symbolic link to {target}, which {why}.", "link": target}, 404)
                if not real or not os.path.isfile(real):
                    return self.send_err(404, "Not found.")
                if not os.access(real, os.R_OK):
                    return self.send_err(403, f"{os.path.basename(real)} cannot be read by the server (permissions)" + (f"; it is a symbolic link to {os.readlink(real)}." if os.path.islink(real) else "."))
                return self.send_json({"path": api, "size": os.path.getsize(real)})
            if url.path == "/api/section":
                if np is None:
                    return self.send_err(501, "numpy is not installed on the server.")
                real, _ = resolve(arg("path"))
                if not real or not os.path.isfile(real):
                    return self.send_err(404, "Not found.")
                body, bw, bh = read_binned_section(real, int(arg("z", "0")), max(1, int(arg("bin", "1"))))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Bw", str(bw))
                self.send_header("X-Bh", str(bh))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                return
            if url.path == "/api/file":
                real, _ = resolve(arg("path"))
                if not real or not os.path.isfile(real):
                    return self.send_err(404, "Not found.")
                ctype = "text/plain; charset=utf-8" if real.lower().endswith(".mdoc") else "application/octet-stream"
                return self.send_path(real, ctype)
            if url.path == "/api/movie/file":
                m = MOVIES.get(arg("id"))
                if not m or not m.get("done") or not os.path.isfile(m["out"]):
                    return self.send_err(404, "That movie is not available (any more).")
                m["t"] = time.time()
                return self.send_path(m["out"], "video/mp4")
            return self.send_err(404, "Unknown address.")
        except PermissionError as e:
            return self.send_err(403, str(e))
        except FileNotFoundError as e:
            return self.send_err(404, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # report instead of dropping the connection
            try:
                return self.send_err(500, f"{type(e).__name__}: {e}")
            except Exception:
                return

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/api/save":
                return self.api_save(q.get("path", [""])[0], q.get("overwrite", ["0"])[0] == "1")
            if url.path.startswith("/api/movie/"):
                return self.api_movie(url.path[len("/api/movie/"):], q)
            return self.send_err(404, "Unknown address.")
        except PermissionError as e:
            return self.send_err(403, str(e))
        except FileNotFoundError as e:
            return self.send_err(404, str(e))
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                return self.send_err(500, f"{type(e).__name__}: {e}")
            except Exception:
                return

    # ---------- movies: the page sends JPEG frames, ffmpeg on this machine encodes them ----------
    def body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            return None
        return self.rfile.read(n)

    def api_movie(self, action, q):
        arg = lambda k, d="": q.get(k, [d])[0]
        ctype = (self.headers.get("Content-Type") or "").lower()
        if action == "start":
            # JSON content type: a page from another site cannot start an encoder here (CORS preflight is never answered)
            if not ctype.startswith("application/json"):
                return self.send_err(415, "Send application/json.")
            exe = ffmpeg_path()
            if not exe:
                return self.send_err(501, "ffmpeg is not installed on the server.")
            try:
                o = json.loads((self.body(1 << 16) or b"{}").decode("utf-8"))
                w, h, fps = int(o.get("w", 1920)), int(o.get("h", 1080)), float(o.get("fps", 30))
            except Exception:
                return self.send_err(400, "Bad movie settings.")
            if not (64 <= w <= 4096 and 64 <= h <= 4096 and w % 2 == 0 and h % 2 == 0 and 1 <= fps <= 60):
                return self.send_err(400, "Movie size must be even, 64 to 4096 px, at 1 to 60 frames per second.")
            movie_sweep()
            if sum(1 for m in MOVIES.values() if not m.get("done")) >= 2:
                return self.send_err(429, "Two movies are already being encoded on this server.")
            d = tempfile.mkdtemp(prefix="mdoc_movie_")
            out, log = os.path.join(d, "movie.mp4"), open(os.path.join(d, "ffmpeg.log"), "wb")
            enc, args = ffmpeg_encoder(exe)
            proc = subprocess.Popen([exe, "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", f"{fps:g}", "-i", "-"]
                                    + args + ["-r", f"{fps:g}", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
            mid = secrets.token_urlsafe(18)
            MOVIES[mid] = {"proc": proc, "dir": d, "out": out, "log": log, "n": 0, "t": time.time(), "lock": threading.Lock()}
            return self.send_json({"id": mid, "encoder": enc})
        m = MOVIES.get(arg("id"))
        if not m:
            return self.send_err(404, "Unknown movie.")
        m["t"] = time.time()

        def why():
            try:
                m["log"].flush()
                return open(os.path.join(m["dir"], "ffmpeg.log"), "rb").read()[-400:].decode("utf-8", "replace").strip()
            except Exception:
                return ""
        if action == "frame":
            if not ctype.startswith("image/jpeg"):
                return self.send_err(415, "Send each frame as image/jpeg.")
            data = self.body(MOVIE_MAX_FRAME)
            if not data or data[:2] != b"\xff\xd8":
                return self.send_err(400, "Not a JPEG frame.")
            if m.get("done") or m["n"] >= MOVIE_MAX_FRAMES:
                return self.send_err(400, "This movie takes no more frames.")
            try:
                with m["lock"]:
                    m["proc"].stdin.write(data)
                    m["n"] += 1
            except (BrokenPipeError, OSError):
                return self.send_err(500, "ffmpeg stopped: " + (why() or "no message"))
            return self.send_json({"n": m["n"]})
        if action == "end":
            try:
                m["proc"].stdin.close()
            except Exception:
                pass
            try:
                code = m["proc"].wait(timeout=900)
            except subprocess.TimeoutExpired:
                movie_drop(arg("id"))
                return self.send_err(500, "ffmpeg did not finish.")
            if code != 0 or not os.path.isfile(m["out"]):
                msg = why()
                movie_drop(arg("id"))
                return self.send_err(500, "ffmpeg failed: " + (msg or f"exit code {code}"))
            m["done"] = True
            return self.send_json({"frames": m["n"], "size": os.path.getsize(m["out"])})
        if action == "cancel":
            movie_drop(arg("id"))
            return self.send_json({"ok": True})
        if action == "save":
            # copy the finished movie into a shared folder: .mp4 only, same rules as session files
            if not WRITABLE:
                raise PermissionError("This server was started with --read-only, so it cannot save files.")
            if not ctype.startswith("application/json"):
                return self.send_err(415, "Send application/json.")
            if not m.get("done"):
                return self.send_err(400, "The movie is not finished.")
            real, api = resolve(arg("path"))
            if not real or "/" not in api:
                raise PermissionError("Choose a folder inside one of the shared folders.")
            if not real.lower().endswith(".mp4"):
                raise PermissionError("Movies are saved as .mp4 files.")
            folder = os.path.dirname(real)
            if not os.path.isdir(folder):
                raise FileNotFoundError("That folder does not exist.")
            if os.path.islink(real) or os.path.isdir(real):
                raise PermissionError("Will not write there.")
            if os.path.exists(real) and arg("overwrite") != "1":
                return self.send_json({"error": "exists", "path": api}, 409)
            tmp = os.path.join(folder, f".{os.path.basename(real)}.{os.getpid()}.tmp")
            try:
                shutil.copyfile(m["out"], tmp)
                os.replace(tmp, real)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            return self.send_json({"path": api, "size": os.path.getsize(real)})
        return self.send_err(404, "Unknown address.")

    def api_save(self, path, overwrite):
        """Write a session .json inside a shared folder: never another file type, never outside the roots."""
        if not WRITABLE:
            raise PermissionError("This server was started with --read-only, so it cannot save files.")
        # a JSON content type makes browsers send a CORS preflight, which this server never
        # answers, so a page from another site cannot make your browser save files here
        if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
            return self.send_err(415, "Send the session as application/json.")
        n =int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > SAVE_LIMIT:
            return self.send_err(413 if n > 0 else 400, "The session file is empty or too large.")
        body = self.rfile.read(n)
        real, api = resolve(path)
        if not real or "/" not in api:
            raise PermissionError("Choose a folder inside one of the shared folders.")
        if not real.lower().endswith(SAVE_EXT):
            raise PermissionError("Only .json session files can be saved.")
        folder = os.path.dirname(real)
        if not os.path.isdir(folder):
            raise FileNotFoundError("That folder does not exist.")
        if os.path.islink(real):
            raise PermissionError("Will not write through a link.")
        if os.path.isdir(real):
            raise PermissionError("A folder has that name.")
        if os.path.exists(real) and not overwrite:
            return self.send_json({"error": "exists", "path": api}, 409)
        try:
            json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_err(400, "The data is not valid JSON.")
        tmp = os.path.join(folder, f".{os.path.basename(real)}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, real)       # atomic: a crash never leaves half a file
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return self.send_json({"path": api, "size": len(body)})

    def api_list(self, path, recursive, want=listed):
        real, api = resolve(path)
        if real is None:                      # top level: the shared roots
            return self.send_json({"path": "", "parent": None,
                                   "dirs": [{"name": n, "path": n} for n in ROOTS], "files": []})
        if os.path.isfile(real):              # a file path lists its folder
            real, api = os.path.dirname(real), api.rsplit("/", 1)[0]
        if not os.path.isdir(real):
            return self.send_err(404, "Folder not found.")
        parent = api.rsplit("/", 1)[0] if "/" in api else ""
        dirs, files, truncated = [], [], False
        if recursive:
            seen = set()
            for dp, dn, fn in os.walk(real, followlinks=True):
                key = os.path.normcase(os.path.realpath(dp))
                if key in seen:          # a link loop: do not walk the same folder twice
                    dn[:] = []
                    continue
                seen.add(key)
                dn[:] = sorted(d for d in dn if not d.startswith("."))
                for f in sorted(fn):
                    if want(f):
                        full = os.path.join(dp, f)
                        rel = os.path.relpath(dp, real).replace(os.sep, "/")
                        files.append({"name": f, "path": f"{api}/{os.path.relpath(full, real).replace(os.sep, '/')}",
                                      "rel": "" if rel == "." else rel, "mrc": f.lower().endswith(".mdoc") and has_mrc(full)})
                        if len(files) >= LIST_LIMIT:
                            truncated = True
                            break
                if truncated:
                    break
        else:
            with os.scandir(real) as it:
                for e in sorted(it, key=lambda e: e.name.lower()):
                    if e.name.startswith("."):
                        continue
                    if e.is_dir():
                        dirs.append({"name": e.name, "path": f"{api}/{e.name}"})
                    elif want(e.name):
                        files.append({"name": e.name, "path": f"{api}/{e.name}", "rel": "", "mrc": e.name.lower().endswith(".mdoc") and has_mrc(e.path)})
        return self.send_json({"path": api, "parent": parent, "dirs": dirs, "files": files, "truncated": truncated})


DEFAULT_PORT = 8765


def how_to_open(host, port):
    """What to open, and (like Jupyter) the SSH command that forwards the port from your own computer."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "you"
    name = socket.getfqdn() or socket.gethostname()
    if name.startswith("localhost") or "." not in name:
        name = socket.gethostname() or name
    ssh = os.environ.get("SSH_CONNECTION", "").split()
    addr = ssh[2] if len(ssh) >= 4 else None          # the address you reached this computer at
    target = f"{user}@{name}"
    local = 8765 if port == DEFAULT_PORT else port
    out = []
    if ssh:
        out.append("\n  You are logged in over SSH. To open the viewer from your own computer, forward the port")
        out.append("  (the same as for a Jupyter notebook). On YOUR computer, in a new terminal, run:\n")
    else:
        out.append(f"\n  On this computer, open  http://localhost:{port}\n")
        out.append("  From another computer, forward the port over SSH (the same as for a Jupyter notebook).")
        out.append("  On that computer, in a terminal, run:\n")
    out.append(f"      ssh -N -L {local}:localhost:{port} {target}")
    if addr and addr != name:
        out.append(f"      (or: ssh -N -L {local}:localhost:{port} {user}@{addr})")
    out.append(f"\n  leave it running, then open  http://localhost:{local}  in your browser there.")
    out.append(f"  Through a gateway, add  -J you@gateway.  If port {local} is busy on your computer, use another")
    out.append(f"  local port, e.g.  ssh -N -L 9000:localhost:{port} {target}  and open http://localhost:9000.")
    if host in ("0.0.0.0", ""):
        out.append(f"\n  Without SSH, on the same network:  http://{name}:{port}")
    elif host not in ("127.0.0.1", "localhost"):
        out.append(f"\n  Without SSH, on the same network:  http://{host}:{port}")
    if ssh:
        out.append(f"\n  (On this computer itself: http://localhost:{port})")
    out.append("")
    return "\n".join(out)


def main():
    global HTML_PATH, VERBOSE, WRITABLE, FFMPEG
    ap = argparse.ArgumentParser(description="Serve mdoc Stage Map and SerialEM data folders (read-only except session .json files).")
    ap.add_argument("roots", nargs="+", help="data folders to share")
    ap.add_argument("--port", type=int, default=None, help="port to listen on (default 8765, or the next free one)")
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 (default) = this computer only; 0.0.0.0 = whole network")
    ap.add_argument("--html", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
                    help="path to the viewer page (default: index.html next to this script)")
    ap.add_argument("--read-only", action="store_true", help="never write anything, not even session .json files")
    ap.add_argument("--ffmpeg", default="auto", help="ffmpeg used to encode acquisition movies: a path, 'auto' (default: look on PATH) or 'none'")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every request")
    a = ap.parse_args()

    for r in a.roots:
        real = os.path.normpath(os.path.abspath(os.path.expanduser(r)))
        if not os.path.isdir(real):
            sys.exit(f"Not a folder: {r}")
        name = os.path.basename(real.rstrip("\\/")) or "root"
        name = re.sub(r"[^\w.-]", "_", name)
        base, k = name, 2
        while name in ROOTS:
            name, k = f"{base}_{k}", k + 1
        ROOTS[name] = real
    HTML_PATH = os.path.realpath(a.html)
    VERBOSE = a.verbose
    WRITABLE = not a.read_only
    FFMPEG = a.ffmpeg
    if not os.path.isfile(HTML_PATH):
        print(f"Warning: viewer page not found at {HTML_PATH}", file=sys.stderr)

    atexit.register(lambda: [movie_drop(k) for k in list(MOVIES)])     # no temporary movie folders left behind
    srv, port = None, a.port or DEFAULT_PORT
    # like Jupyter: if the default port is taken, use the next free one; a port asked for explicitly must be free
    for p in ([a.port] if a.port else range(DEFAULT_PORT, DEFAULT_PORT + 30)):
        try:
            srv, port = ThreadingHTTPServer((a.host, p), Handler), p
            break
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) and "in use" not in str(e).lower():
                raise
            if a.port:
                sys.exit(f"Port {a.port} is already in use on this computer (another mdoc_server.py or Jupyter?). "
                         f"Pick another with --port, or leave --port out to use the next free one.")
    if srv is None:
        sys.exit(f"Ports {DEFAULT_PORT}-{DEFAULT_PORT + 29} are all in use. Pick one with --port.")
    srv.daemon_threads = True
    print(f"mdoc Stage Map server {VERSION}  (numpy {'found: pieces are shrunk on the server' if np is not None else 'not found: full pieces are sent'})")
    for n, r in ROOTS.items():
        print(f"  sharing  {n:<16} {r}")
    print("  movies   " + (f"encoded with {ffmpeg_path()}" if ffmpeg_path() else "ffmpeg not found (pass --ffmpeg PATH): the browser encodes them instead"))
    if port != (a.port or DEFAULT_PORT):
        print(f"  port     {DEFAULT_PORT} is in use, so {port} is used")
    print(how_to_open(a.host, port))
    if a.host not in ("127.0.0.1", "localhost"):
        print("  Note: other computers on the network can read the shared folders"
              + (" and save session .json files in them.\n" if WRITABLE else ".\n"))
    print("  Press Ctrl+C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
