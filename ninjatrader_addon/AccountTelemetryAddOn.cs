#region Using declarations
using System;
using System.Globalization;
using System.IO;
using System.Linq;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.NinjaScript;
#endregion

// ---------------------------------------------------------------------------
// READ-ONLY TELEMETRY. This AddOn must never place, modify, or cancel an
// order, and must never contain any trading/decision logic. Its only job is
// exporting account balance / realized P&L figures to a JSON file so the
// Python executor's risk_guard can read them (see tradovate_bot/account_state.py
// for the consuming side and odd/tasks/tradovate-execution-bot.md for why
// this exists: NT8 ATI's `outgoing` folder has no account-balance file).
// Any change that adds an order method (SubmitOrderUnmanaged, Account.Enter*,
// Account.CreateOrder, etc.) to this file is out of scope and must be
// rejected -- see the Constraints section of that task doc.
//
// NOT independently verified against a live/demo NT8 instance (no NT8
// runtime available in the environment this was written in). Before relying
// on this for the funded account, install it on the VPS, enable it, and
// confirm the emitted JSON file's values against NT8's own Account Performance
// / Cash Value display for at least one full session including a day
// rollover, per tradovate_bot/README.md.
// ---------------------------------------------------------------------------
//
// Configuration note (2026-11-09): this AddOn has no window, and NinjaTrader
// does not expose any UI to edit [NinjaScriptProperty] values on a windowless
// AddOn -- NinjaTrader support has confirmed those only become settable
// inside an AddOn's own custom window, which this deliberately doesn't have
// (see https://ninjatrader.com/support/forum/forum/ninjatrader-8/add-on-development/1162313-where-are-addon-settings-exposed).
// Configuration therefore comes from the same environment variables the
// Python executor already uses (see tradovate_bot/README.md): NT8_ACCOUNT_ID,
// ACCOUNT_TELEMETRY_JSON_PATH, and ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS.
// A headless AddOn does not need a menu entry to run -- NinjaTrader
// instantiates and drives it through OnStateChange automatically at startup
// and on every NinjaScript compile (confirmed: an AddOn "does not require
// its own window to function", https://docs.ninjatrader.com/ninjascript/addon_development_overview).
// Check NT8's NinjaScript Output window (Control Center -> New -> NinjaScript
// Output) for this AddOn's startup/export log lines -- that is the real
// "is it running" signal, not any menu.
// ---------------------------------------------------------------------------

namespace NinjaTrader.NinjaScript.AddOns
{
    public class AccountTelemetryAddOn : NinjaTrader.NinjaScript.AddOnBase
    {
        private const string DefaultAccountName = "Sim101";
        private static readonly string DefaultOutputJsonPath =
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "NinjaTrader 8", "AccountTelemetry", "account_telemetry.json");
        private const int DefaultExportIntervalSeconds = 5;

        private Account monitoredAccount;
        private System.Timers.Timer exportTimer;
        private readonly object stateLock = new object();

        private string accountName;
        private string outputJsonPath;
        private int exportIntervalSeconds;

        private double realizedPnlAtDayStart;
        private double cashValueAtDayStart;
        private DateTime currentTradingDayBoundary = DateTime.MinValue;

        // America/New_York via the Windows tz id "Eastern Standard Time" (it
        // observes DST despite the name). Never compare this Eastern
        // wall-clock DateTime against DateTime.Now/UtcNow directly -- Kind is
        // ignored by comparison operators.
        private static readonly TimeZoneInfo EasternZone =
            TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "AccountTelemetryAddOn";
            }
            else if (State == State.Active)
            {
                LoadConfigFromEnvironment();
                StartExporting();
            }
            else if (State == State.Terminated)
            {
                StopExporting();
            }
        }

        private void LoadConfigFromEnvironment()
        {
            accountName = FirstNonEmpty(Environment.GetEnvironmentVariable("NT8_ACCOUNT_ID"), DefaultAccountName);
            outputJsonPath = FirstNonEmpty(Environment.GetEnvironmentVariable("ACCOUNT_TELEMETRY_JSON_PATH"), DefaultOutputJsonPath);

            int parsedInterval;
            string rawInterval = Environment.GetEnvironmentVariable("ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS");
            exportIntervalSeconds = !string.IsNullOrWhiteSpace(rawInterval) && int.TryParse(rawInterval, out parsedInterval)
                ? parsedInterval
                : DefaultExportIntervalSeconds;
        }

        private static string FirstNonEmpty(string value, string fallback)
        {
            return string.IsNullOrWhiteSpace(value) ? fallback : value;
        }

        private void StartExporting()
        {
            monitoredAccount = Account.All.FirstOrDefault(
                a => string.Equals(a.Name, accountName, StringComparison.OrdinalIgnoreCase));
            if (monitoredAccount == null)
            {
                NinjaTrader.Code.Output.Process(
                    string.Format("AccountTelemetryAddOn: account '{0}' not found, telemetry export disabled.", accountName),
                    PrintTo.OutputTab1);
                return;
            }

            ResetDayBoundaryIfNeeded(force: true);

            exportTimer = new System.Timers.Timer(Math.Max(1, exportIntervalSeconds) * 1000.0);
            exportTimer.Elapsed += (sender, args) => SafeExportOnce();
            exportTimer.AutoReset = true;
            exportTimer.Start();

            NinjaTrader.Code.Output.Process(
                string.Format(
                    "AccountTelemetryAddOn: started. account='{0}' outputJsonPath='{1}' intervalSeconds={2}",
                    accountName, outputJsonPath, exportIntervalSeconds),
                PrintTo.OutputTab1);

            // Export once immediately so a fresh start doesn't leave a stale
            // or missing file for up to exportIntervalSeconds.
            SafeExportOnce();
        }

        private void StopExporting()
        {
            if (exportTimer != null)
            {
                exportTimer.Stop();
                exportTimer.Dispose();
                exportTimer = null;
            }
        }

        private void SafeExportOnce()
        {
            // A timer callback must never throw back into NT8's timer
            // infrastructure or it can take the whole AddOn down silently.
            try
            {
                ExportOnce();
            }
            catch (Exception ex)
            {
                NinjaTrader.Code.Output.Process(
                    "AccountTelemetryAddOn export failed: " + ex.Message, PrintTo.OutputTab1);
            }
        }

        private void ExportOnce()
        {
            if (monitoredAccount == null)
                return;

            lock (stateLock)
            {
                ResetDayBoundaryIfNeeded(force: false);

                double currentBalance = monitoredAccount.Get(AccountItem.CashValue, Currency.UsDollar);
                double totalRealizedProfit = monitoredAccount.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
                double dailyProfitToday = totalRealizedProfit - realizedPnlAtDayStart;

                DateTimeOffset easternNow = TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, EasternZone);

                var payload = new
                {
                    updated_at = easternNow.ToString("yyyy-MM-ddTHH:mm:sszzz", CultureInfo.InvariantCulture),
                    daily_realized_pnl = dailyProfitToday,
                    eod_balance_last_close = cashValueAtDayStart,
                    current_balance = currentBalance,
                    daily_profit_today = dailyProfitToday,
                    total_realized_profit = totalRealizedProfit,
                };

                WriteJsonAtomically(payload);
            }
        }

        // The account's trading day boundary rolls over explicitly at 17:00
        // (5:00 PM) Eastern, not at local VPS machine midnight -- Lucid/
        // Tradovate's EOD drawdown floor recalculates once per day at the
        // close, so the boundary needs to track that close, not whatever
        // timezone the VPS happens to be set to. The boundary is computed by
        // converting a known UTC instant (DateTimeOffset.UtcNow) rather than
        // constructing local time from scratch, so DST transition days are
        // not ambiguous. This specific 17:00-ET boundary is still not
        // verified against a live NT8/Lucid session -- see
        // tradovate_bot/README.md's verification checklist.
        private void ResetDayBoundaryIfNeeded(bool force)
        {
            DateTimeOffset easternNow = TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, EasternZone);
            DateTime rolloverToday = easternNow.Date.AddHours(17);
            DateTime currentBoundary = easternNow.DateTime >= rolloverToday ? rolloverToday : rolloverToday.AddDays(-1);
            if (!force && currentBoundary == currentTradingDayBoundary)
                return;

            currentTradingDayBoundary = currentBoundary;
            realizedPnlAtDayStart = monitoredAccount.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
            cashValueAtDayStart = monitoredAccount.Get(AccountItem.CashValue, Currency.UsDollar);
        }

        private void WriteJsonAtomically(object payload)
        {
            string directory = Path.GetDirectoryName(outputJsonPath);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
                Directory.CreateDirectory(directory);

            string json = SerializeFlatObject(payload);
            string tempPath = outputJsonPath + ".tmp";
            File.WriteAllText(tempPath, json);

            // File.Replace/Move with overwrite is the closest .NET equivalent
            // of the temp-write-then-rename pattern used in oif_writer.py --
            // the Python reader never observes a partially written file.
            if (File.Exists(outputJsonPath))
                File.Replace(tempPath, outputJsonPath, null);
            else
                File.Move(tempPath, outputJsonPath);
        }

        // Minimal hand-rolled serializer for our known flat payload shape --
        // avoids pulling in an extra JSON dependency for ~6 numeric/string
        // fields. If the payload shape above changes, update this too.
        private static string SerializeFlatObject(object payload)
        {
            var props = payload.GetType().GetProperties();
            var parts = props.Select(p =>
            {
                var value = p.GetValue(payload);
                string jsonValue = value is string s
                    ? "\"" + s.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\""
                    : Convert.ToDouble(value).ToString("G17", CultureInfo.InvariantCulture);
                return "\"" + p.Name + "\": " + jsonValue;
            });
            return "{\n  " + string.Join(",\n  ", parts) + "\n}\n";
        }
    }
}
