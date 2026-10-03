#!/usr/bin/env python3
"""
bc250-monitor — thermal and performance telemetry for the AMD BC-250.
Standard library only. Samples sensors, stores them in SQLite, serves a dashboard.
"""
import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

BASE = os.path.dirname(os.path.abspath(__file__))
# systemd sets STATE_DIRECTORY when the unit declares StateDirectory=.
DB = os.environ.get("BC250_MON_DB") or os.path.join(
    os.environ.get("STATE_DIRECTORY") or BASE, "metrics.db")
INTERVAL = float(os.environ.get("BC250_MON_INTERVAL", "3"))       # seconds between samples
RETENTION_DAYS = int(os.environ.get("BC250_MON_RETENTION_DAYS", "14"))
BIND = os.environ.get("BC250_MON_BIND", "0.0.0.0")
PORT = int(os.environ.get("BC250_MON_PORT", "9095"))
OLLAMA = os.environ.get("BC250_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# --- Thermal guard ---------------------------------------------------------
# The BC-250's cooling runs at its ceiling: sustained load is enough to go past
# 100 C. The guard unloads models from VRAM, which removes the heat source
# without touching any service.
GUARD_ENABLED = os.environ.get("BC250_GUARD", "1") != "0"
GUARD_WARN = float(os.environ.get("BC250_GUARD_WARN", "88"))
GUARD_CRIT = float(os.environ.get("BC250_GUARD_CRIT", "93"))
GUARD_CONSEC = 2          # consecutive samples before tripping
GUARD_COOLDOWN = 120      # seconds before the guard may trip again

# --------------------------------------------------------------------------
# Resolve hwmon paths by name (hwmonN numbering changes between boots)
# --------------------------------------------------------------------------

def resolve_hwmon():
    found = {}
    for entry in sorted(os.listdir("/sys/class/hwmon")):
        path = os.path.join("/sys/class/hwmon", entry)
        try:
            with open(os.path.join(path, "name")) as f:
                found.setdefault(f.read().strip(), path)
        except OSError:
            continue
    return found


def find_drm_device():
    for card in sorted(os.listdir("/sys/class/drm")):
        if re.fullmatch(r"card\d+", card):
            dev = f"/sys/class/drm/{card}/device"
            if os.path.exists(os.path.join(dev, "pp_dpm_sclk")):
                return dev
    return None


HWMON = resolve_hwmon()
DRM = find_drm_device()


def read_raw(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def read_int(base, name, div=1):
    if not base:
        return None
    v = read_raw(os.path.join(base, name))
    if v is None:
        return None
    try:
        return int(v) / div
    except ValueError:
        return None


def current_sclk():
    """Active GPU clock in MHz, from pp_dpm_sclk (the line marked '*')."""
    if not DRM:
        return None
    raw = read_raw(os.path.join(DRM, "pp_dpm_sclk"))
    if not raw:
        return None
    for line in raw.splitlines():
        if "*" in line:
            m = re.search(r"(\d+)\s*Mhz", line, re.I)
            if m:
                return int(m.group(1))
    return None


def ollama_models():
    """Models currently loaded by Ollama. Empty list if Ollama is unreachable."""
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/ps", timeout=2) as r:
            return json.load(r).get("models") or []
    except Exception:
        return []


def ollama_state():
    """Loaded models and total VRAM use, for the dashboard."""
    models = ollama_models()
    names = [m.get("name") for m in models]
    return {
        "model": ", ".join(names) if names else None,
        "models": names,
        "vram_mb": round(sum(m.get("size_vram", 0) for m in models) / 1048576),
        "ctx": models[0].get("context_length") if models else None,
    }


def unload_models():
    """Unload every model from VRAM: removes the GPU load without killing a service."""
    freed = []
    for m in ollama_models():
        body = json.dumps({"model": m.get("name"), "keep_alive": 0}).encode()
        req = urllib.request.Request(f"{OLLAMA}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
            freed.append(m.get("name"))
        except Exception:
            pass
    return freed


def log_event(con, level, temp, detail):
    con.execute("INSERT INTO events (ts, level, temp, detail) VALUES (?,?,?,?)",
                (int(time.time()), level, temp, detail))
    con.commit()
    print(f"[guard:{level}] {temp:.1f}C — {detail}", flush=True)


def sample():
    global HWMON, DRM
    amd = HWMON.get("amdgpu")
    k10 = HWMON.get("k10temp")
    nct = HWMON.get("nct6686") or HWMON.get("nct6687")

    s = {
        "ts": int(time.time()),
        "gpu_edge": read_int(amd, "temp1_input", 1000),
        "tctl": read_int(k10, "temp1_input", 1000),
        "tsi": read_int(nct, "temp1_input", 1000),
        "therm14": read_int(nct, "temp2_input", 1000),
        "therm15": read_int(nct, "temp3_input", 1000),
        "power_w": read_int(amd, "power1_input", 1000000),
        "vddgfx": read_int(amd, "in0_input"),
        "fan_rpm": read_int(nct, "fan2_input"),
        "pwm": read_int(nct, "pwm2"),
        "sclk": current_sclk(),
        "nvme": read_int(HWMON.get("nvme"), "temp1_input", 1000),
    }
    # Sensors vanish when a module is reloaded: resolve them again.
    if s["gpu_edge"] is None or s["tsi"] is None:
        HWMON = resolve_hwmon()
        DRM = find_drm_device()
    return s


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

COLUMNS = ["ts", "gpu_edge", "tctl", "tsi", "therm14", "therm15",
           "power_w", "vddgfx", "fan_rpm", "pwm", "sclk", "nvme"]
# When several samples are merged into one chart point, keep the peak for these
# (a short excursion must stay visible on the 7-day view) and average the rest.
PEAK_COLUMNS = {"gpu_edge", "tctl", "tsi", "therm14", "therm15", "power_w", "nvme"}


def connect():
    con = sqlite3.connect(DB, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = connect()
    con.execute(f"""CREATE TABLE IF NOT EXISTS metrics (
        {', '.join(c + (' INTEGER PRIMARY KEY' if c == 'ts' else ' REAL') for c in COLUMNS)}
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON metrics(ts)")
    con.execute("""CREATE TABLE IF NOT EXISTS events (
        ts INTEGER, level TEXT, temp REAL, detail TEXT)""")
    con.commit()
    con.close()


def sampler_loop():
    con = connect()
    placeholders = ",".join("?" * len(COLUMNS))
    stmt = f"INSERT OR REPLACE INTO metrics ({','.join(COLUMNS)}) VALUES ({placeholders})"
    last_prune = 0
    hot_streak = 0
    warned = False
    last_trip = 0
    while True:
        try:
            s = sample()
            con.execute(stmt, [s.get(c) for c in COLUMNS])
            con.commit()
            now = time.time()

            # Thermal guard: act on the hottest of the three sensors
            temps = [s[k] for k in ("gpu_edge", "tctl", "tsi") if s.get(k) is not None]
            peak = max(temps) if temps else None
            if GUARD_ENABLED and peak is not None:
                if peak >= GUARD_CRIT:
                    hot_streak += 1
                    if hot_streak >= GUARD_CONSEC and now - last_trip > GUARD_COOLDOWN:
                        freed = unload_models()
                        log_event(con, "critical", peak,
                                  f"{GUARD_CRIT}C threshold crossed — unloaded: {', '.join(freed)}"
                                  if freed else
                                  f"{GUARD_CRIT}C threshold crossed — no model to unload")
                        last_trip = now
                        hot_streak = 0
                else:
                    hot_streak = 0
                if peak >= GUARD_WARN and not warned:
                    log_event(con, "warning", peak, f"{GUARD_WARN}C warning threshold crossed")
                    warned = True
                elif peak < GUARD_WARN - 3:
                    warned = False

            if now - last_prune > 3600:
                cutoff = int(now - RETENTION_DAYS * 86400)
                con.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
                con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                con.commit()
                last_prune = now
        except Exception as e:
            print(f"[sampler] {e}", flush=True)
        time.sleep(INTERVAL)


def query(seconds, max_points=420):
    """History over the last `seconds`, merged in SQL into at most max_points buckets."""
    cutoff = int(time.time() - seconds)
    width = max(seconds / max_points, 1)
    cols = ",".join(
        f"{'MAX' if c in PEAK_COLUMNS else 'AVG'}({c})" for c in COLUMNS if c != "ts")
    con = connect()
    rows = con.execute(
        f"SELECT MIN(ts),{cols} FROM metrics WHERE ts >= ? "
        f"GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY 1",
        (cutoff, cutoff, width)).fetchall()
    con.close()
    return {c: [r[i] for r in rows] for i, c in enumerate(COLUMNS)}


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        url = urlsplit(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(BASE, "dashboard.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"dashboard.html not found", "text/plain")

        elif path == "/api/current":
            s = sample()
            s["ollama"] = ollama_state()
            uptime = read_raw("/proc/uptime")
            s["uptime"] = uptime.split()[0] if uptime else None
            self._json(s)

        elif path == "/api/history":
            seconds = RANGES.get(qs.get("range", ["1h"])[0], 3600)
            self._json(query(seconds))

        elif path == "/api/events":
            con = connect()
            rows = con.execute(
                "SELECT ts, level, temp, detail FROM events ORDER BY ts DESC LIMIT 50").fetchall()
            con.close()
            self._json([{"ts": r[0], "level": r[1], "temp": r[2], "detail": r[3]} for r in rows])

        elif path == "/api/health":
            self._json({
                "ok": True, "guard": GUARD_ENABLED,
                "warn": GUARD_WARN, "crit": GUARD_CRIT,
                "interval": INTERVAL, "retention_days": RETENTION_DAYS,
            })

        else:
            self._send(404, b"not found", "text/plain")


def main():
    init_db()
    threading.Thread(target=sampler_loop, daemon=True).start()
    srv = ThreadingHTTPServer((BIND, PORT), Handler)
    guard = f"warn {GUARD_WARN}C / unload {GUARD_CRIT}C" if GUARD_ENABLED else "DISABLED"
    print(f"bc250-monitor on {BIND}:{PORT} — db={DB} hwmon={list(HWMON)} drm={DRM} "
          f"— thermal guard: {guard}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
