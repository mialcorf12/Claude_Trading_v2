#region Using declarations
using System;
using System.ComponentModel.DataAnnotations;
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

namespace NinjaTrader.NinjaScript.AddOns
{
    public class AccountTelemetryAddOn : NinjaTrader.NinjaScript.AddOnBase
    {
        private Account monitoredAccount;
        private System.Timers.Timer exportTimer;
        private readonly object stateLock = new object();

        private double realizedPnlAtDayStart;
        private double cashValueAtDayStart;
        private DateTime currentTradingDay = DateTime.MinValue;

        [NinjaScriptProperty]
        [Display(Name = "Account Name", Order = 1, GroupName = "Parameters")]
        public string AccountName { get; set; } = "Sim101";

        [NinjaScriptProperty]
        [Display(Name = "Output JSON Path", Order = 2, GroupName = "Parameters")]
        public string OutputJsonPath { get; set; } =
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "NinjaTrader 8", "AccountTelemetry", "account_telemetry.json");

        [NinjaScriptProperty]
        [Display(Name = "Export Interval (seconds)", Order = 3, GroupName = "Parameters")]
        public int ExportIntervalSeconds { get; set; } = 5;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "AccountTelemetryAddOn";
            }
            else if (State == State.Active)
            {
                StartExporting();
            }
            else if (State == State.Terminated)
            {
                StopExporting();
            }
        }

        private void StartExporting()
        {
            monitoredAccount = Account.All.FirstOrDefault(a => a.Name == AccountName);
            if (monitoredAccount == null)
            {
                NinjaTrader.Code.Output.Process(
                    string.Format("AccountTelemetryAddOn: account '{0}' not found, telemetry export disabled.", AccountName),
                    PrintTo.OutputTab1);
                return;
            }

            ResetDayBoundaryIfNeeded(force: true);

            exportTimer = new System.Timers.Timer(Math.Max(1, ExportIntervalSeconds) * 1000.0);
            exportTimer.Elapsed += (sender, args) => SafeExportOnce();
            exportTimer.AutoReset = true;
            exportTimer.Start();

            // Export once immediately so a fresh start doesn't leave a stale
            // or missing file for up to ExportIntervalSeconds.
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

                var payload = new
                {
                    updated_at = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture),
                    daily_realized_pnl = dailyProfitToday,
                    eod_balance_last_close = cashValueAtDayStart,
                    current_balance = currentBalance,
                    daily_profit_today = dailyProfitToday,
                    total_realized_profit = totalRealizedProfit,
                };

                WriteJsonAtomically(payload);
            }
        }

        // The account's trading day boundary is tracked locally (not NT8's
        // exchange session calendar) using a simple local-midnight rollover.
        // This is intentionally conservative/simple; if the Lucid/Tradovate
        // "day" boundary for the EOD drawdown floor differs from local
        // midnight, this must be revisited before going live -- see
        // tradovate_bot/README.md's verification checklist.
        private void ResetDayBoundaryIfNeeded(bool force)
        {
            DateTime today = DateTime.Now.Date;
            if (!force && today == currentTradingDay)
                return;

            currentTradingDay = today;
            realizedPnlAtDayStart = monitoredAccount.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
            cashValueAtDayStart = monitoredAccount.Get(AccountItem.CashValue, Currency.UsDollar);
        }

        private void WriteJsonAtomically(object payload)
        {
            string directory = Path.GetDirectoryName(OutputJsonPath);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
                Directory.CreateDirectory(directory);

            string json = SerializeFlatObject(payload);
            string tempPath = OutputJsonPath + ".tmp";
            File.WriteAllText(tempPath, json);

            // File.Replace/Move with overwrite is the closest .NET equivalent
            // of the temp-write-then-rename pattern used in oif_writer.py --
            // the Python reader never observes a partially written file.
            if (File.Exists(OutputJsonPath))
                File.Replace(tempPath, OutputJsonPath, null);
            else
                File.Move(tempPath, OutputJsonPath);
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
