#!/usr/bin/env python3
"""Recoge predicciones y observaciones y calcula el acierto de cada modelo.

Sin dependencias externas. Cada fase es independiente: si AEMET falla o no hay
clave, las predicciones se siguen guardando.
"""
import json
import math
import os
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MODELS = [
    "ecmwf_ifs", "icon_seamless", "gfs_global", "ukmo_global_deterministic_10km",
    "meteofrance_arpege_europe", "meteofrance_arome_france", "gem_global",
    "ecmwf_aifs025_single",
]
HOURLY = "temperature_2m,precipitation,wind_gusts_10m"
MIN_HOURS = 20
RAIN_MM = 1.0
SNAPSHOT_FROM_UTC_HOUR = 8  # con las ejecuciones de 00 UTC ya disponibles
BUCKETS = {"1": (1, 1), "2-3": (2, 3), "4-6": (4, 6)}
AEMET = "https://opendata.aemet.es/opendata/api"
MAX_STATION_TRIES = 6


def log(*a):
    print(*a, flush=True)


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return "-".join("".join(c if c.isalnum() else " " for c in s).split())


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n", encoding="utf-8")


def http(url, headers=None, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "isobara", **(headers or {})})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin-1")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(20 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(10)
                continue
            raise


# ---------- Municipios ----------
def geocode(name):
    q = urllib.parse.urlencode({"name": name, "count": 10, "language": "es", "countryCode": "ES"})
    res = json.loads(http("https://geocoding-api.open-meteo.com/v1/search?" + q)).get("results") or []
    if not res:
        return None
    exact = [r for r in res if slugify(r["name"]) == slugify(name)] or res
    return exact[0]


def resolve_places():
    wanted = read_json(ROOT / "municipios.json", [])
    known = {p["slug"]: p for p in read_json(DATA / "lugares.json", [])}
    out = []
    for item in wanted:
        name = item if isinstance(item, str) else item["nombre"]
        slug = slugify(name)
        p = known.get(slug)
        if not p:
            if isinstance(item, dict) and "lat" in item:
                p = {"name": name, "slug": slug, "sub": item.get("sub", ""), "lat": item["lat"], "lon": item["lon"], "tz": item.get("tz", "Europe/Madrid"), "elev": item.get("elev")}
            else:
                g = geocode(name)
                if not g:
                    log(f"AVISO: no encuentro el municipio «{name}»; se omite")
                    continue
                p = {"name": name, "slug": slug, "sub": (g.get("admin2") or g.get("admin1") or "").replace("Provincia de ", ""), "lat": g["latitude"], "lon": g["longitude"], "tz": g.get("timezone", "Europe/Madrid"), "elev": g.get("elevation")}
        out.append(p)
    return out


# ---------- Predicciones ----------
def daily_from_hourly(hourly):
    """Agrega por día local. Un día sin horas suficientes de una variable queda sin ella."""
    days = {}
    for i, t in enumerate(hourly.get("time", [])):
        d = days.setdefault(t[:10], {"t": [], "p": [], "g": []})
        for key, var in (("t", "temperature_2m"), ("p", "precipitation"), ("g", "wind_gusts_10m")):
            v = (hourly.get(var) or [None] * (i + 1))[i]
            if v is not None:
                d[key].append(v)
    out = {}
    for day, d in days.items():
        row = {}
        if len(d["t"]) >= MIN_HOURS:
            row["tmax"], row["tmin"] = max(d["t"]), min(d["t"])
        if len(d["p"]) >= MIN_HOURS:
            row["rain"] = round(sum(d["p"]), 2)
        if len(d["g"]) >= MIN_HOURS:
            row["gust"] = max(d["g"])
        if row:
            out[day] = row
    return out


def collect_forecasts(places, now, force=False):
    if now.hour < SNAPSHOT_FROM_UTC_HOUR and not force:
        log("Predicciones: aún no es la hora de la foto diaria")
        return
    issue = now.astimezone(ZoneInfo("Europe/Madrid")).date().isoformat()
    for p in places:
        path = DATA / "predicciones" / f"{p['slug']}.json"
        store = read_json(path, {})
        if issue in store:
            continue
        snap = {}
        for m in MODELS:
            q = urllib.parse.urlencode({"latitude": p["lat"], "longitude": p["lon"], "hourly": HOURLY, "models": m, "forecast_days": 7, "timezone": "auto"})
            try:
                j = json.loads(http("https://api.open-meteo.com/v1/forecast?" + q))
                d = daily_from_hourly(j.get("hourly") or {})
                if d:
                    snap[m] = d
            except urllib.error.HTTPError as e:
                if e.code != 400:  # 400 = modelo sin cobertura aquí
                    log(f"AVISO: {p['name']} / {m}: HTTP {e.code}")
            except Exception as e:  # un modelo no debe parar la foto del resto
                log(f"AVISO: {p['name']} / {m}: {e}")
        if snap:
            store[issue] = snap
            write_json(path, store)
            log(f"Predicciones: {p['name']} {issue}, {len(snap)} modelos")


# ---------- AEMET ----------
def aemet_get(path, key):
    meta = json.loads(http(f"{AEMET}{path}", headers={"api_key": key, "Accept": "application/json"}))
    if meta.get("estado") != 200 or not meta.get("datos"):
        raise RuntimeError(f"AEMET {path}: estado {meta.get('estado')} {meta.get('descripcion')}")
    time.sleep(1.5)
    return json.loads(http(meta["datos"]))


def parse_coord(s):
    """AEMET da p. ej. 402304N: grados, minutos, segundos y hemisferio."""
    s = s.strip()
    val = int(s[:-5]) + int(s[-5:-3]) / 60 + int(s[-3:-1]) / 3600
    return -val if s[-1] in "SW" else val


def haversine(a, b, c, d):
    r = math.radians
    h = math.sin(r(c - a) / 2) ** 2 + math.cos(r(a)) * math.cos(r(c)) * math.sin(r(d - b) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", ".").strip()
    if s.lower() in ("ip", "acum"):  # inapreciable
        return 0.0 if s.lower() == "ip" else None
    try:
        return float(s)
    except ValueError:
        return None


def obs_row(o):
    """Convierte una fila de AEMET. El viento llega en m/s y se guarda en km/h."""
    vmax = num(o.get("vmax"))
    return {
        "ta": num(o.get("ta")), "tamax": num(o.get("tamax")), "tamin": num(o.get("tamin")),
        "prec": num(o.get("prec")), "gust": None if vmax is None else round(vmax * 3.6, 1),
    }


def collect_obs(places, key):
    if not key:
        log("Observaciones: falta el secreto AEMET_API_KEY; se omite esta fase")
        return
    assign = read_json(DATA / "estaciones.json", {})
    inventory = None
    for p in places:
        rows, st = None, assign.get(p["slug"])
        try:
            if st:
                rows = aemet_get(f"/observacion/convencional/datos/estacion/{st['id']}", key)
            else:
                if inventory is None:
                    inventory = aemet_get("/valores/climatologicos/inventarioestaciones/todasestaciones", key)
                near = []
                for s in inventory:
                    try:
                        near.append((haversine(p["lat"], p["lon"], parse_coord(s["latitud"]), parse_coord(s["longitud"])), s))
                    except (KeyError, ValueError):
                        continue
                near.sort(key=lambda x: x[0])
                for dist, s in near[:MAX_STATION_TRIES]:
                    log(f"Probando {s.get('nombre')} ({s['indicativo']}), {dist:.1f} km, {s.get('altitud')} m")
                    try:
                        r = aemet_get(f"/observacion/convencional/datos/estacion/{s['indicativo']}", key)
                    except Exception as e:
                        log(f"  descartada: {e}")
                        continue
                    if any(num(o.get("ta")) is not None for o in r):
                        rows = r
                        st = {"id": s["indicativo"], "name": s["nombre"].title(), "km": round(dist, 1), "alt": num(s.get("altitud")), "dalt": None if p.get("elev") is None or num(s.get("altitud")) is None else round(num(s["altitud"]) - p["elev"])}
                        assign[p["slug"]] = st
                        write_json(DATA / "estaciones.json", assign)
                        break
                if not rows:
                    log(f"AVISO: sin estación con datos cerca de {p['name']}")
                    continue
        except Exception as e:
            log(f"AVISO: observaciones de {p['name']}: {e}")
            continue
        path = DATA / "observaciones" / f"{p['slug']}.json"
        store = read_json(path, {})
        added = 0
        for o in rows:
            fint = o.get("fint")
            if fint and fint not in store:
                added += 1
            if fint:
                store[fint] = obs_row(o)
        write_json(path, store)
        log(f"Observaciones: {p['name']} ← {st['name']} ({st['km']} km), {added} horas nuevas")
        time.sleep(1.5)


def parse_fint(s):
    s = s.replace("Z", "+0000")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def daily_obs(store, tz):
    z, days = ZoneInfo(tz), {}
    for fint, o in store.items():
        t = parse_fint(fint)
        if not t:
            continue
        # La observación de las HH:00 resume la hora anterior: se asigna a su inicio.
        day = (t - timedelta(hours=1)).astimezone(z).date().isoformat()
        d = days.setdefault(day, {"hi": [], "lo": [], "p": [], "g": []})
        hi = o["tamax"] if o.get("tamax") is not None else o.get("ta")
        lo = o["tamin"] if o.get("tamin") is not None else o.get("ta")
        if hi is not None:
            d["hi"].append(hi)
        if lo is not None:
            d["lo"].append(lo)
        if o.get("prec") is not None:
            d["p"].append(o["prec"])
        if o.get("gust") is not None:
            d["g"].append(o["gust"])
    out = {}
    for day, d in days.items():
        row = {}
        if len(d["hi"]) >= MIN_HOURS and len(d["lo"]) >= MIN_HOURS:
            row["tmax"], row["tmin"] = max(d["hi"]), min(d["lo"])
        if len(d["p"]) >= MIN_HOURS:
            row["rain"] = round(sum(d["p"]), 2)
        if len(d["g"]) >= MIN_HOURS:
            row["gust"] = max(d["g"])
        if row:
            out[day] = row
    return out


# ---------- Puntuación ----------
def score_place(fc, obs):
    acc = {b: {} for b in BUCKETS}

    def add(bucket, who, var, err):
        acc[bucket].setdefault(who, {}).setdefault(var, []).append(err)

    def add_all(bucket, who, f, o):
        for v in ("tmax", "tmin", "rain", "gust"):
            if f.get(v) is not None and o.get(v) is not None:
                add(bucket, who, v, abs(f[v] - o[v]))
        if f.get("rain") is not None and o.get("rain") is not None:
            add(bucket, who, "wet", 1.0 if (f["rain"] >= RAIN_MM) == (o["rain"] >= RAIN_MM) else 0.0)

    for issue, snap in fc.items():
        i = date.fromisoformat(issue)
        targets = {t for m in snap.values() for t in m}
        for tgt in targets:
            lead = (date.fromisoformat(tgt) - i).days
            b = next((k for k, (a, z) in BUCKETS.items() if a <= lead <= z), None)
            if b is None or tgt not in obs:
                continue
            for m, days in snap.items():
                if tgt in days:
                    add_all(b, m, days[tgt], obs[tgt])
            cons = {}
            for v in ("tmax", "tmin", "rain", "gust"):
                vals = [d[tgt][v] for d in snap.values() if tgt in d and d[tgt].get(v) is not None]
                if vals:
                    cons[v] = statistics.median(vals)
            add_all(b, "consenso", cons, obs[tgt])
    return {b: {who: {v: {"e": round(sum(x) / len(x), 2), "n": len(x)} for v, x in vs.items()} for who, vs in whos.items()} for b, whos in acc.items()}


def build_scores(places):
    stations = read_json(DATA / "estaciones.json", {})
    out = {"actualizado": datetime.now(timezone.utc).isoformat(timespec="minutes"), "lugares": {}}
    for p in places:
        fc = read_json(DATA / "predicciones" / f"{p['slug']}.json", {})
        obs = daily_obs(read_json(DATA / "observaciones" / f"{p['slug']}.json", {}), p["tz"])
        out["lugares"][p["slug"]] = {
            "estacion": stations.get(p["slug"]), "desde": min(fc) if fc else None,
            "fotos": len(fc), "dias_observados": len(obs), "cubos": score_place(fc, obs),
        }
    write_json(DATA / "aciertos.json", out)


def main():
    places = resolve_places()
    write_json(DATA / "lugares.json", places)
    log("Municipios:", ", ".join(f"{p['name']} ({p['lat']}, {p['lon']}; {p['sub']})" for p in places))
    collect_forecasts(places, datetime.now(timezone.utc), force=os.environ.get("FORZAR_FOTO") == "1")
    collect_obs(places, os.environ.get("AEMET_API_KEY", "").strip())
    build_scores(places)


if __name__ == "__main__":
    sys.exit(main())
