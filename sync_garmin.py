#!/usr/bin/env python3
"""Sincroniza tus datos de Garmin (solo lectura) a notas markdown + data.json.

Uso:
  python sync_garmin.py --login            # una vez, en una Terminal real
  python sync_garmin.py --days 3           # baja los ultimos 3 dias
  python sync_garmin.py --days 3 --dry-run # muestra sin escribir archivos

Construido sobre python-garminconnect (cyberjunky):
https://github.com/cyberjunky/python-garminconnect
Nunca escribe nada en tu cuenta Garmin. La clave nunca se guarda ni se imprime.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import stat
import sys
from pathlib import Path

TOKEN_DIR = Path(os.environ.get("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser()
DEFAULT_OUT = Path(__file__).resolve().parent / "garmin"


def _import_garmin():
    try:
        from garminconnect import Garmin  # type: ignore
    except ImportError:
        sys.exit("Falta la libreria. Ejecuta: pip install -r requirements.txt")
    return Garmin


def _lock_down_tokens() -> None:
    """Deja la carpeta de tokens legible solo por ti."""
    if not TOKEN_DIR.exists():
        return
    os.chmod(TOKEN_DIR, stat.S_IRWXU)
    for p in TOKEN_DIR.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


# ---------------------------------------------------------------- login
def do_login() -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("Por seguridad, --login solo corre en una Terminal real (para ocultar tu clave).")
    Garmin = _import_garmin()
    email = input("Correo de Garmin: ").strip()
    password = getpass.getpass("Clave de Garmin (no se ve mientras escribes): ")

    def ask_mfa() -> str:
        return input("Codigo 2FA que te envio Garmin: ").strip()

    try:
        client = Garmin(email=email, password=password, prompt_mfa=ask_mfa)
    except TypeError:
        client = Garmin(email, password)
    password = None  # noqa: F841  (no la mantenemos en memoria mas de lo necesario)

    TOKEN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        result = client.login(str(TOKEN_DIR))
        # Algunas versiones devuelven ("needs_mfa", estado) en vez de preguntar
        if isinstance(result, tuple) and result and result[0] == "needs_mfa":
            client.resume_login(result[1], ask_mfa())
            client.login(str(TOKEN_DIR))
    except Exception as e:  # sin imprimir datos sensibles
        sys.exit(f"No se pudo iniciar sesion ({type(e).__name__}). Revisa correo/clave/2FA e intenta de nuevo.")
    _lock_down_tokens()
    print(f"Listo: sesion guardada de forma privada en {TOKEN_DIR} (dura ~1 ano).")


def _import_ci_token() -> None:
    """En GitHub Actions: reconstruye la sesion desde el secreto GARMIN_TOKEN_B64."""
    b64 = os.environ.get("GARMIN_TOKEN_B64", "").strip()
    if not b64 or (TOKEN_DIR.exists() and any(TOKEN_DIR.iterdir())):
        return
    import base64, io, tarfile
    TOKEN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz") as t:
        for m in t.getmembers():
            if m.isfile() and "/" not in m.name and ".." not in m.name:
                (TOKEN_DIR / m.name).write_bytes(t.extractfile(m).read())


def export_ci_token() -> None:
    """Empaqueta la sesion en garmin-ci-token.txt para pegarla como secreto en GitHub."""
    import base64, io, tarfile
    if not TOKEN_DIR.exists() or not any(TOKEN_DIR.iterdir()):
        sys.exit("No hay sesion guardada. Ejecuta primero --login")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p in TOKEN_DIR.iterdir():
            if p.is_file():
                t.add(p, arcname=p.name)
    out = Path(__file__).resolve().parent / "garmin-ci-token.txt"
    out.write_text(base64.b64encode(buf.getvalue()).decode())
    os.chmod(out, stat.S_IRUSR | stat.S_IWUSR)
    print(f"Sesion exportada en {out} (no la muestres ni la compartas).")


def get_client():
    _import_ci_token()
    Garmin = _import_garmin()
    if not TOKEN_DIR.exists() or not any(TOKEN_DIR.iterdir()):
        sys.exit("No hay sesion guardada. Ejecuta primero: python sync_garmin.py --login")
    client = Garmin()
    try:
        client.login(str(TOKEN_DIR))
    except Exception as e:
        sys.exit(f"La sesion guardada no funciono ({type(e).__name__}). Ejecuta de nuevo --login.")
    _lock_down_tokens()
    return client


# ---------------------------------------------------------------- fetch
ERRORS: list[str] = []


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        ERRORS.append(f"{getattr(fn, '__name__', fn)}{args}: {type(e).__name__}: {str(e)[:150]}")
        return None


def _bb_from_series(c, day: str):
    """Plan B para Body Battery: la serie del dia."""
    raw = safe(c.get_body_battery, day)
    if not isinstance(raw, list) or not raw:
        return None, None
    vals = []
    for row in (raw[0] or {}).get("bodyBatteryValuesArray") or []:
        if isinstance(row, list) and len(row) >= 2:
            v = row[1] if len(row) == 2 else row[2]
            if isinstance(v, (int, float)):
                vals.append(v)
    return (min(vals), max(vals)) if vals else (None, None)


def _get(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def fetch_wellness(c, day: str) -> dict:
    stats = safe(c.get_stats, day) or {}
    sleep = safe(c.get_sleep_data, day) or {}
    hrv = safe(c.get_hrv_data, day) or {}
    tr = safe(c.get_training_readiness, day)
    if isinstance(tr, list):
        tr = next((x for x in tr if isinstance(x, dict) and x.get("score") is not None), {})
    tr = tr or {}

    bb_low, bb_high = stats.get("bodyBatteryLowestValue"), stats.get("bodyBatteryHighestValue")
    if bb_low is None and bb_high is None:
        bb_low, bb_high = _bb_from_series(c, day)
    sleep_dto = sleep.get("dailySleepDTO") or {}
    secs = sleep_dto.get("sleepTimeSeconds")
    return {
        "date": day,
        "resting_hr": stats.get("restingHeartRate"),
        "hrv_overnight_ms": _get(hrv, "hrvSummary", "lastNightAvg"),
        "hrv_status": _get(hrv, "hrvSummary", "status"),
        "sleep_hours": round(secs / 3600, 1) if secs else None,
        "sleep_score": _get(sleep_dto, "sleepScores", "overall", "value"),
        "body_battery_low": bb_low,
        "body_battery_high": bb_high,
        "stress_avg": stats.get("averageStressLevel"),
        "steps": stats.get("totalSteps"),
        "training_readiness": tr.get("score"),
    }


# Garmin nombra las actividades con la comuna donde se grabaron ("<comuna> Ciclismo").
# Este repositorio es publico, asi que se guarda el nombre sin el lugar.
_TIPOS = ("Ciclismo", "Carrera", "Caminata", "Senderismo", "Natación", "Natacion", "Entrenamiento",
          "Fuerza", "Yoga", "Cardio", "Remo", "Elíptica", "Eliptica", "Running", "Cycling", "Walking")


def sin_lugar(name):
    if not name:
        return name
    if " - " in name:
        return name.split(" - ", 1)[1].strip() or name
    for t in _TIPOS:
        i = name.find(t)
        if i >= 0:
            return name[i:]
    return name


def fetch_activities(c, start: str, end: str) -> list[dict]:
    raw = safe(c.get_activities_by_date, start, end) or []
    out = []
    for a in raw:
        dist = a.get("distance")
        dur = a.get("duration")
        out.append({
            "id": a.get("activityId"),
            "name": sin_lugar(a.get("activityName")),
            "type": _get(a, "activityType", "typeKey"),
            "start_local": a.get("startTimeLocal"),
            "distance_km": round(dist / 1000, 2) if dist else None,
            "duration_min": round(dur / 60, 1) if dur else None,
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "calories": a.get("calories"),
            "elevation_gain_m": a.get("elevationGain"),
            "aerobic_te": a.get("aerobicTrainingEffect"),
            "anaerobic_te": a.get("anaerobicTrainingEffect"),
        })
    return out


# ---------------------------------------------------------------- write
def fmt(v, unit=""):
    return "sin dato" if v is None else f"{v}{unit}"


def wellness_md(w: dict) -> str:
    bb = ("sin dato" if w["body_battery_low"] is None and w["body_battery_high"] is None
          else f"{fmt(w['body_battery_low'])} -> {fmt(w['body_battery_high'])}")
    hrv = fmt(w["hrv_overnight_ms"], " ms")
    if w.get("hrv_status"):
        hrv += f" ({w['hrv_status']})"
    sleep = fmt(w["sleep_hours"], " h")
    if w.get("sleep_score") is not None:
        sleep += f" (puntaje {w['sleep_score']})"
    return "\n".join([
        f"# Garmin bienestar {w['date']}",
        f"- Pulso en reposo: {fmt(w['resting_hr'], ' lpm')}",
        f"- HRV (noche): {hrv}",
        f"- Sueno: {sleep}",
        f"- Body Battery: {bb}",
        f"- Estres (promedio): {fmt(w['stress_avg'])}",
        f"- Pasos: {fmt(w['steps'])}",
        f"- Training readiness: {fmt(w['training_readiness'])}",
        "",
    ])


def activity_md(a: dict) -> str:
    return "\n".join([
        f"# {a['name'] or 'Actividad'} ({a['type'] or '?'})",
        f"- Inicio: {fmt(a['start_local'])}",
        f"- Distancia: {fmt(a['distance_km'], ' km')}",
        f"- Duracion: {fmt(a['duration_min'], ' min')}",
        f"- Pulso promedio / max: {fmt(a['avg_hr'])} / {fmt(a['max_hr'])}",
        f"- Calorias: {fmt(a['calories'])}",
        f"- Desnivel positivo: {fmt(a['elevation_gain_m'], ' m')}",
        f"- Training effect aerobico / anaerobico: {fmt(a['aerobic_te'])} / {fmt(a['anaerobic_te'])}",
        "",
    ])


def slug(s: str) -> str:
    keep = "".join(ch.lower() if ch.isalnum() else "-" for ch in (s or "actividad"))
    return "-".join(p for p in keep.split("-") if p)[:40] or "actividad"


def write_files(out: Path, wellness: list[dict], acts: list[dict], devices=None) -> None:
    (out / "daily").mkdir(parents=True, exist_ok=True)
    (out / "activities").mkdir(parents=True, exist_ok=True)
    store_path = out / "data.json"
    store = {"wellness": {}, "activities": {}}
    if store_path.exists():
        try:
            store = json.loads(store_path.read_text())
        except Exception:
            pass
    for w in wellness:
        store["wellness"][w["date"]] = w
        (out / "daily" / f"{w['date']}.md").write_text(wellness_md(w))
    for a in acts:
        store["activities"][str(a["id"])] = a
    for a in store.get("activities", {}).values():
        a["name"] = sin_lugar(a.get("name"))
    for a in acts:
        day = (a["start_local"] or "")[:10] or "sin-fecha"
        (out / "activities" / f"{day}-{slug(a['type'])}-{a['id']}.md").write_text(activity_md(a))
    store["updated"] = dt.datetime.now().isoformat(timespec="seconds")
    if devices is not None:
        store["devices"] = devices
    store["last_errors"] = ERRORS[-30:]
    store_path.write_text(json.dumps(store, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Sincroniza Garmin -> notas markdown (solo lectura).")
    ap.add_argument("--login", action="store_true", help="inicio de sesion unico")
    ap.add_argument("--days", type=int, default=3, help="dias hacia atras (default 3)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="carpeta de salida")
    ap.add_argument("--dry-run", action="store_true", help="mostrar sin escribir")
    ap.add_argument("--export-ci-token", action="store_true", help="exportar sesion para GitHub Actions")
    args = ap.parse_args()

    if args.login:
        do_login()
        return
    if args.export_ci_token:
        export_ci_token()
        return

    c = get_client()
    today = dt.date.today()
    # Relleno automatico: si hay poca historia guardada, baja 30 dias
    try:
        stored = json.loads((args.out / "data.json").read_text()).get("wellness", {})
    except Exception:
        stored = {}
    if not args.dry_run and len(stored) < 25:
        args.days = max(args.days, 30)
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(args.days - 1, -1, -1)]
    wellness = [fetch_wellness(c, d) for d in days]
    acts = fetch_activities(c, days[0], days[-1])
    devs = safe(c.get_devices) or []
    devices = [d.get("productDisplayName") or d.get("displayName") for d in devs if isinstance(d, dict)]

    if args.dry_run:
        for w in wellness:
            print(wellness_md(w))
        for a in acts:
            print(activity_md(a))
        return
    write_files(args.out, wellness, acts, devices)
    print(f"OK: {len(wellness)} dias y {len(acts)} actividades guardados en {args.out}")
    print("Relojes:", ", ".join(x for x in devices if x) or "no informado")
    if ERRORS:
        print(f"Avisos ({len(ERRORS)}):", *ERRORS[:10], sep="\n  ")
    for w in wellness[-1:]:
        print("\n" + wellness_md(w))


if __name__ == "__main__":
    main()
