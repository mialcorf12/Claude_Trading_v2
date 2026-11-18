from mcp.server.mcpserver import MCPServer
import json
from pathlib import Path

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

@mcp.tool()
def get_local_candles(symbol: str = "MNQ", timeframe: str = "5m", limit: int = 10) -> str:
    """
    Obtiene las últimas velas OHLCV cerradas de MNQ o NQ desde el almacenamiento local de NinjaTrader 8.
    Por defecto devuelve las últimas 10 velas en temporalidad de 5 minutos (5m).
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
        with open(file_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
            
        payload["bars"] = payload["bars"][-limit:]
        payload["count_returned"] = len(payload["bars"])
        return json.dumps(payload, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Error leyendo el archivo local: {str(e)}"})

if __name__ == "__main__":
    mcp.run()