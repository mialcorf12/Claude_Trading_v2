"""MCP server that serves the candles exported by NinjaTrader 8.

`ninjatrader_addon/LocalBarExporter.cs` writes the last 200 closed bars of the
1m and 5m series to `candles/<SYMBOL>_<tf>.json` (UTF-8 with BOM, bar times in
ET stamped at bar CLOSE). This server exposes them as the `get_local_candles`
tool, with a `freshness` block so the caller can refuse to trade on stale data.

Run as a script by `.mcp.json` (so `tradovate_bot/` is on sys.path) and imported
as `tradovate_bot.local_market_data_mcp` by the tests. The candles directory
defaults to `<repo>/candles` and can be overridden with the CANDLES_DIR env var.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

try:
    from tradovate_bot.time_utils import EASTERN, now_et   # imported as a package
except ImportError:
    from time_utils import EASTERN, now_et                 # run as a script


# Inicializar MCPServer (API v2.x)
mcp = MCPServer("LocalMarketDataMCP")


def _default_data_dir(env=None):
    """`CANDLES_DIR` if set, else `<repo>/candles` (never a hard-coded path)."""
    env = os.environ if env is None else env
    override = env.get("CANDLES_DIR")
    return Path(override) if override else Path(__file__).resolve().parents[1] / "candles"


DATA_DIR = _default_data_dir()

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,8}$")
_VALID_TIMEFRAMES = ("1m", "5m")


def _normalize_timeframe(tf: str) -> str:
    """Convierte variantes ('5Minute', '5min', '5M') al formato del archivo ('5m')."""
    tf_clean = tf.strip().lower()
    if tf_clean in ("5m", "5min", "5minute", "5minutes", "5"):
        return "5m"
    if tf_clean in ("1m", "1min", "1minute", "1minutes", "1"):
        return "1m"
    return tf.strip()


# NT8 bars are stamped at bar CLOSE in the VPS local time, which is ET.
# A closed bar should land within a few seconds, so age > timeframe + grace
# means the exporter (or NT8) stopped. Also expected when the market is closed.
STALE_GRACE_SECONDS = 60
_TF_SECONDS = {"1m": 60, "5m": 300}
# A bar or export stamped ahead of the machine clock means the clocks disagree (e.g. a
# suspended VPS resumed with its clock behind while NT8 kept the real time). A few seconds
# of skew is normal; more than this is reported as stale instead of a negative "age".
FUTURE_TOLERANCE_SECONDS = 5

_UTC_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$")


def _now_utc():
    return datetime.now(timezone.utc)


def _parse_utc(stamp: str) -> datetime:
    """Parses the exporter's `updated_utc` (e.g. 2026-11-19T05:45:00.1293119Z):
    any number of fractional digits (NT8 writes 7), `Z`, an explicit offset, or
    none (treated as UTC). Raises ValueError for anything else."""
    match = _UTC_STAMP.match(str(stamp).strip())
    if not match:
        raise ValueError("unrecognized updated_utc %r" % (stamp,))
    base, fraction, zone = match.groups()
    text = base + ("." + fraction[:6].ljust(6, "0") if fraction else "")
    moment = datetime.fromisoformat(text)
    if zone in (None, "Z"):
        tzinfo = timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        hours, minutes = int(zone[1:3]), int(zone[4:6])
        tzinfo = timezone(sign * timedelta(hours=hours, minutes=minutes))
    return moment.replace(tzinfo=tzinfo).astimezone(timezone.utc)


def _freshness(payload: dict, tf: str) -> dict:
    """Age of the newest bar and of the export file, in ET, plus a stale flag.

    Fail-closed: unparseable timestamps report stale=True.
    """
    now = now_et()
    info = {"now_et": now.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        last = datetime.strptime(payload["bars"][-1]["time"], "%Y-%m-%d %H:%M:%S")
        last_age = (now - last.replace(tzinfo=EASTERN)).total_seconds()
        file_age = (_now_utc() - _parse_utc(payload["updated_utc"])).total_seconds()
        limit = _TF_SECONDS.get(tf, 300) + STALE_GRACE_SECONDS
        in_future = last_age < -FUTURE_TOLERANCE_SECONDS or file_age < -FUTURE_TOLERANCE_SECONDS
        info.update({
            "last_bar_time_et": last.strftime("%Y-%m-%d %H:%M:%S"),
            "last_bar_age_seconds": round(last_age),
            "file_age_seconds": round(file_age),
            "stale": in_future or last_age > limit or file_age > limit,
        })
        if in_future:
            info["freshness_error"] = (
                "timestamps in the future of the machine clock (bar %+ds, file %+ds): "
                "clock skew or mixed data, do not trade" % (round(last_age), round(file_age)))
    except Exception as e:
        info.update({"stale": True, "freshness_error": str(e)})
    return info


def _error(message: str, **extra) -> str:
    return json.dumps({"error": message, **extra}, ensure_ascii=False)


@mcp.tool()
def get_local_candles(symbol: str = "MNQ", timeframe: str = "5m", limit: int = 10) -> str:
    """
    Obtiene las últimas velas OHLCV cerradas de MNQ o NQ desde el almacenamiento local de NinjaTrader 8.
    Por defecto devuelve las últimas 10 velas en temporalidad de 5 minutos (5m).
    Horas en ET (hora local del VPS). `freshness.stale=true` => no confiar en los datos.
    """
    sym = symbol.strip().upper()
    if not _SYMBOL_RE.match(sym):
        return _error("Símbolo inválido %r (solo letras y números, 1-8 caracteres)." % (symbol,))
    tf = _normalize_timeframe(timeframe)
    if tf not in _VALID_TIMEFRAMES:
        return _error("Temporalidad %r no disponible; usa una de %s." % (timeframe, ", ".join(_VALID_TIMEFRAMES)))
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        return _error("limit debe ser un entero, recibido %r." % (limit,))

    file_path = DATA_DIR / f"{sym}_{tf}.json"

    # Fallback automático entre MNQ y NQ si solo uno está abierto en NT8
    if not file_path.exists() and sym in ("MNQ", "NQ"):
        alt_sym = "NQ" if sym == "MNQ" else "MNQ"
        alt_path = DATA_DIR / f"{alt_sym}_{tf}.json"
        if alt_path.exists():
            file_path = alt_path

    if not file_path.exists():
        available = [f.name for f in DATA_DIR.glob("*.json")] if DATA_DIR.exists() else []
        return _error(
            f"No hay datos locales para {sym} en {tf}.",
            archivos_disponibles=available,
            sugerencia="Verifica que el indicador LocalBarExporter esté activo en NT8.",
        )

    try:
        # utf-8-sig: NT8's File.WriteAllText(Encoding.UTF8) prepends a BOM.
        with open(file_path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)

        payload["freshness"] = _freshness(payload, tf)
        payload["bars"] = payload["bars"][-limit:]
        payload["count_returned"] = len(payload["bars"])
        return json.dumps(payload, indent=2)
    except Exception as e:
        return _error(f"Error leyendo el archivo local: {str(e)}")


if __name__ == "__main__":
    mcp.run()
