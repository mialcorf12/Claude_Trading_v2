#region Using declarations
using System;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class LocalBarExporter : Indicator
    {
        private string outputDir = @"C:\TradingPlatform_v2\Claude_Trading_v2\candles";
        private int barsToExport = 200;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "Exporta velas OHLCV cerradas de 1m y 5m a archivos JSON locales.";
                Name = "LocalBarExporter";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
            }
            else if (State == State.Configure)
            {
                // Serie 0 es la del gráfico principal (ej. 1 minuto)
                // Agregamos la Serie 1 explícitamente en 5 minutos
                AddDataSeries(BarsPeriodType.Minute, 5);
            }
            else if (State == State.DataLoaded)
            {
                if (!Directory.Exists(outputDir))
                    Directory.CreateDirectory(outputDir);
            }
        }

        protected override void OnBarUpdate()
        {
            // Validar que existan al menos 200 barras en la serie actual
            if (CurrentBars[BarsInProgress] < barsToExport)
                return;

            // Si está en historial, solo exportar en la ÚLTIMA vela histórica disponible (al cargar el gráfico)
            if (State == State.Historical && CurrentBars[BarsInProgress] < Count - 2)
                return;

            if (BarsInProgress == 0)
            {
                ExportSeriesToJson(0, "1m");
            }
            else if (BarsInProgress == 1)
            {
                ExportSeriesToJson(1, "5m");
            }
        }

        private void ExportSeriesToJson(int seriesIndex, string timeframeLabel)
        {
            try
            {
                string instrumentName = Instrument.MasterInstrument.Name; // Ej: NQ, MNQ
                string baseFileName = $"{instrumentName}_{timeframeLabel}";

                string tempPath = Path.Combine(outputDir, $"{baseFileName}.tmp");
                string finalPath = Path.Combine(outputDir, $"{baseFileName}.json");

                StringBuilder sb = new StringBuilder();
                sb.AppendLine("{");
                sb.AppendLine($"  \"symbol\": \"{instrumentName}\",");
                sb.AppendLine($"  \"timeframe\": \"{timeframeLabel}\",");
                sb.AppendLine($"  \"updated_utc\": \"{DateTime.UtcNow:O}\",");
                sb.AppendLine("  \"bars\": [");

                // Accedemos explícitamente a Times[seriesIndex], Opens[seriesIndex], etc.
                for (int barsAgo = barsToExport - 1; barsAgo >= 0; barsAgo--)
                {
                    sb.Append("    {");
                    sb.Append($"\"time\": \"{Times[seriesIndex][barsAgo]:yyyy-MM-dd HH:mm:ss}\", ");
                    sb.Append(string.Format(CultureInfo.InvariantCulture, "\"open\": {0}, ", Opens[seriesIndex][barsAgo]));
                    sb.Append(string.Format(CultureInfo.InvariantCulture, "\"high\": {0}, ", Highs[seriesIndex][barsAgo]));
                    sb.Append(string.Format(CultureInfo.InvariantCulture, "\"low\": {0}, ", Lows[seriesIndex][barsAgo]));
                    sb.Append(string.Format(CultureInfo.InvariantCulture, "\"close\": {0}, ", Closes[seriesIndex][barsAgo]));
                    sb.Append(string.Format(CultureInfo.InvariantCulture, "\"volume\": {0}", Volumes[seriesIndex][barsAgo]));
                    sb.Append(barsAgo > 0 ? "},\n" : "}\n");
                }

                sb.AppendLine("  ]");
                sb.AppendLine("}");

                // Escritura atómica
                File.WriteAllText(tempPath, sb.ToString(), Encoding.UTF8);
                if (File.Exists(finalPath))
                    File.Delete(finalPath);
                File.Move(tempPath, finalPath);
            }
            catch (Exception ex)
            {
                Print($"[LocalBarExporter - {timeframeLabel}] Error exportando velas: {ex.Message}");
            }
        }
    }
}