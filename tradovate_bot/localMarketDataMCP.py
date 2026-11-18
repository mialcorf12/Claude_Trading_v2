from mcp.server.mcpserver import MCPServer
import json
from datetime import datetime, timezone
from pathlib import Path

from time_utils import EASTERN, now_et

# Inicializar MCPServer (API v2.x)
mcp = MCPServer("LocalMarketDataMCP")

DATA_DIR = Path(r"C:\TradingPlatform_v2\Claude_Trading_v2\candles")

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


def _freshness(payload: dict, tf: str) -> dict:
    """Age of the newest bar and of the export file, in ET, plus a stale flag.

    Fail-closed: unparseable timestamps report stale=True.
    """
    now = now_et()
    info = {"now_et": now.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        last = datetime.strptime(payload["bars"][-1]["time"], "%Y-%m-%d %H:%M:%S")
        last_age = (now - last.replace(tzinfo=EASTERN)).total_seconds()
        upd = datetime.fromisoformat(payload["updated_utc"].replace("Z", "+00:00")[:26] + "+00:00"
                                     if "+" not in payload["updated_utc"] else payload["updated_utc"])
        file_age = (datetime.now(timezone.utc) - upd).total_seconds()
        limit = _TF_SECONDS.get(tf, 300) + STALE_GRACE_SECONDS
        info.update({
            "last_bar_time_et": last.strftime("%Y-%m-%d %H:%M:%S"),
            "last_bar_age_seconds": round(last_age),
            "file_age_seconds": round(file_age),
            "stale": last_age > limit or file_age > limit,
        })
    except Exception as e:
        info.update({"stale": True, "freshness_error": str(e)})
    return info


@mcp.tool()
def get_local_candles(symbol: str = "MNQ", timeframe: str = "5m", limit: int = 10) -> str:
    """
    Obtiene las últimas velas OHLCV cerradas de MNQ o NQ desde el almacenamiento local de NinjaTrader 8.
    Por defecto devuelve las últimas 10 velas en temporalidad de 5 minutos (5m).
    Horas en ET (hora local del VPS). `freshness.stale=true` => no confiar en los datos.
    """
    sym = symbol.strip().upper()
    tf = _normalize_timeframe(timeframe)
    
    file_path = DATA_DIR / f"{sym}_{tf}.json"
    
    # Fallback automático entre MNQ y NQ si solo uno está abierto en NT8
    if not file_path.exists() and sym in ("MNQ", "NQ"):
        alt_sym = "NQ" if sym == "MNQ" else "MNQ"
        alt_path = DATA_DIR / f"{alt_sym}_{tf}.json"
        if alt_path.exists():
            file_path = alt_path

    if not file_path.exists():
        available = [f.name for f in DATA_DIR.glob("*.json")] if DATA_DIR.exists() else []
        return json.dumps({
            "error": f"No hay datos locales para {sym} en {tf}.",
            "archivos_disponibles": available,
            "sugerencia": "Verifica que el indicador LocalBarExporter esté activo en NT8."
        }, ensure_ascii=False)
    
    try:
        # utf-8-sig: NT8's File.WriteAllText(Encoding.UTF8) prepends a BOM.
        with open(file_path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
            
        payload["freshness"] = _freshness(payload, tf)
        payload["bars"] = payload["bars"][-limit:]
        payload["count_returned"] = len(payload["bars"])
        return json.dumps(payload, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Error leyendo el archivo local: {str(e)}"})

if __name__ == "__main__":
    mcp.run()