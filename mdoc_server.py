#!/usr/bin/env python3
"""
mdoc_server.py - serve mdoc Stage Map and your SerialEM data folders to a browser.

The viewer page (index.html) must sit next to this script, or be given with --html.
Only the folders you list are reachable. Everything is read-only except session
files: the viewer can save its state as a .json file inside those folders (turn
that off with --read-only).

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
import json
import os
import re
import struct
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import numpy as np
except ImportError:
    np = None

APP = "mdoc-stage-map"
VERSION = "1.2"
MRC_EXT = (".mrc", ".st", ".map", ".mrcs", ".ali", ".preali")
CHUNK = 1 << 20          # 1 MiB per write when streaming a file
LIST_LIMIT = 20000       # most mdoc files returned by one recursive listing
TGTS_RE = re.compile(r"_tgts(_p\d+)?\.txt$", re.I)   # PACEtomo target files

SAVE_EXT = (".json",)    # the only files the server will write
SAVE_LIMIT = 64 << 20    # largest session file accepted, in bytes

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
    return base.lower().endswith(MRC_EXT) and os.path.isfile(base)


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
                                       "write": WRITABLE, "sep": os.sep,
                                       "roots": [{"name": n, "path": r} for n, r in ROOTS.items()]})
            if url.path == "/api/list":
                return self.api_list(arg("path"), arg("recursive") == "1", ext_filter(arg("ext")))
            if url.path == "/api/stat":
                real, api = resolve(arg("path"))
                if not real or not os.path.isfile(real):
                    return self.send_err(404, "Not found.")
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


def main():
    global HTML_PATH, VERBOSE, WRITABLE
    ap = argparse.ArgumentParser(description="Serve mdoc Stage Map and SerialEM data folders (read-only except session .json files).")
    ap.add_argument("roots", nargs="+", help="data folders to share")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="127.0.0.1 (default) = this computer only; 0.0.0.0 = whole network")
    ap.add_argument("--html", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"),
                    help="path to the viewer page (default: index.html next to this script)")
    ap.add_argument("--read-only", action="store_true", help="never write anything, not even session .json files")
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
    if not os.path.isfile(HTML_PATH):
        print(f"Warning: viewer page not found at {HTML_PATH}", file=sys.stderr)

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    shown = "localhost" if a.host in ("127.0.0.1", "0.0.0.0", "") else a.host
    print(f"mdoc Stage Map server {VERSION}  (numpy {'found: pieces are shrunk on the server' if np is not None else 'not found: full pieces are sent'})")
    for n, r in ROOTS.items():
        print(f"  sharing  {n:<16} {r}")
    print(f"\n  Open  http://{shown}:{a.port}\n")
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
