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
        private string dayStartSnapshotPath;
        private int exportIntervalSeconds;

        private double realizedPnlAtDayStart;
        private double cashValueAtDayStart;
        private DateTime currentTradingDayBoundary = DateTime.MinValue;

        // Format used for the persisted day-start snapshot's boundary field
        // (see WriteDayStartSnapshot/TryReadDayStartSnapshot). Plain
        // (no UTC offset) because it's only ever compared for equality
        // against another DateTime computed the same way (ComputeTradingDay
        // Boundary), never used for absolute-instant math.
        private const string BoundaryFormat = "yyyy-MM-ddTHH:mm:ss";

        // RealizedProfitLoss and CashValue are two independent NT8 account
        // counters; both daily baselines are captured at the same instant
        // (ResetDayBoundaryIfNeeded), so the "today" delta computed from
        // either one should match within commission-sized noise. If NT8
        // resets/adjusts one counter without the other underneath us (seen
        // live 2026-11-10: daily_realized_pnl read -549 while NT8's own
        // display, driven by CashValue, read -200.50), the two deltas
        // diverge sharply. Skipping the export in that case leaves the last
        // good file in place; it ages past max_staleness_seconds and
        // account_state.py's existing fail-closed path rejects it, instead
        // of risk_guard trusting a silently wrong number.
        private const double DailyPnlDivergenceToleranceDollars = 5.0;

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
            dayStartSnapshotPath = Path.Combine(
                Path.GetDirectoryName(outputJsonPath) ?? string.Empty, "account_telemetry_daystart.json");

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

            InitializeDayBoundary();

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
                ResetDayBoundaryIfNeeded();

                double currentBalance = monitoredAccount.Get(AccountItem.CashValue, Currency.UsDollar);
                double totalRealizedProfit = monitoredAccount.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
                double dailyProfitToday = totalRealizedProfit - realizedPnlAtDayStart;
                double dailyCashProfitToday = currentBalance - cashValueAtDayStart;

                if (Math.Abs(dailyProfitToday - dailyCashProfitToday) > DailyPnlDivergenceToleranceDollars)
                {
                    NinjaTrader.Code.Output.Process(
                        string.Format(
                            "AccountTelemetryAddOn: daily P&L sources diverged (realized-based={0:F2}, " +
                            "cash-based={1:F2}) -- withholding this export so the stale telemetry file " +
                            "ages out and risk_guard fails closed instead of trusting a wrong number.",
                            dailyProfitToday, dailyCashProfitToday),
                        PrintTo.OutputTab1);
                    return;
                }

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

                WriteJsonAtomically(outputJsonPath, payload);
            }
        }

        // The account's trading day boundary rolls over explicitly at 17:00
        // (5:00 PM) Eastern, not at local VPS machine midnight -- Lucid/
        // Tradovate's EOD drawdown floor recalculates once per day at the
        // close, so the boundary needs to track that close, not whatever
        // timezone the VPS happens to be set to. The boundary is computed by
        // converting a known UTC instant (DateTimeOffset.UtcNow) rather than
        // constructing local time from scratch, so DST transition days are
        // not ambiguous. This 17:00-ET boundary is an explicit user
        // decision (odd/tasks/eastern-time-migration.md, T4) -- do not
        // change it to 18:00 without the user re-confirming that decision;
        // a 2026-11-10 report of `daily_realized_pnl` disagreeing with NT8
        // ($-549 vs $-200.50) turned out to be a separate problem, caught by
        // the divergence guard in ExportOnce above, not this boundary hour.
        private DateTime ComputeTradingDayBoundary(DateTimeOffset easternNow)
        {
            DateTime rolloverToday = easternNow.Date.AddHours(17);
            return easternNow.DateTime >= rolloverToday ? rolloverToday : rolloverToday.AddDays(-1);
        }

        // Called once from StartExporting. A NinjaScript recompile or an
        // NT8 restart used to blow away the in-memory currentTradingDay
        // Boundary (back to DateTime.MinValue) and silently re-baseline to
        // whatever the account read at that exact restart moment -- seen
        // live 2026-11-10: a restart reset daily_realized_pnl to 0 and
        // eod_balance_last_close to the current balance, erasing an
        // already-realized -200.50 loss from both the daily-loss and EOD-
        // floor checks for the rest of that trading day. Recovering the
        // same-day baseline from the persisted snapshot (if one exists for
        // today's boundary) fixes that; only a genuinely new trading day or
        // a missing/corrupt snapshot falls back to the old capture-from-
        // live-account behavior.
        private void InitializeDayBoundary()
        {
            DateTime boundary = ComputeTradingDayBoundary(TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, EasternZone));

            double recoveredRealized, recoveredCash;
            if (TryReadDayStartSnapshot(boundary, out recoveredRealized, out recoveredCash))
            {
                currentTradingDayBoundary = boundary;
                realizedPnlAtDayStart = recoveredRealized;
                cashValueAtDayStart = recoveredCash;
                NinjaTrader.Code.Output.Process(
                    string.Format(
                        "AccountTelemetryAddOn: recovered day-start baseline for {0:yyyy-MM-dd HH:mm} ET " +
                        "(realized={1:F2}, cash={2:F2}) from {3} -- surviving this restart instead of " +
                        "re-baselining to now.",
                        boundary, recoveredRealized, recoveredCash, dayStartSnapshotPath),
                    PrintTo.OutputTab1);
                return;
            }

            CaptureAndPersistNewBoundary(boundary);
        }

        // Called from every ExportOnce tick. Only re-baselines (and
        // re-persists) when the computed boundary has actually moved past
        // currentTradingDayBoundary, i.e. a genuine 17:00-ET rollover
        // happened while this process kept running.
        private void ResetDayBoundaryIfNeeded()
        {
            DateTime boundary = ComputeTradingDayBoundary(TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, EasternZone));
            if (boundary == currentTradingDayBoundary)
                return;
            CaptureAndPersistNewBoundary(boundary);
        }

        private void CaptureAndPersistNewBoundary(DateTime boundary)
        {
            currentTradingDayBoundary = boundary;
            realizedPnlAtDayStart = monitoredAccount.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar);
            cashValueAtDayStart = monitoredAccount.Get(AccountItem.CashValue, Currency.UsDollar);
            WriteDayStartSnapshot(boundary, realizedPnlAtDayStart, cashValueAtDayStart);
        }

        private void WriteDayStartSnapshot(DateTime boundary, double realizedAtStart, double cashAtStart)
        {
            var payload = new
            {
                trading_day_boundary = boundary.ToString(BoundaryFormat, CultureInfo.InvariantCulture),
                realized_pnl_at_day_start = realizedAtStart,
                cash_value_at_day_start = cashAtStart,
            };
            WriteJsonAtomically(dayStartSnapshotPath, payload);
        }

        // Reads back ONLY this AddOn's own WriteDayStartSnapshot output --
        // not a general JSON parser, same reasoning as SerializeFlatObject
        // below (avoids a JSON dependency for a handful of known flat
        // fields we fully control on both the write and read side).
        // Returns false (never throws) for a missing file, a parse
        // failure, or a snapshot whose boundary doesn't match
        // expectedBoundary (an older day's leftover snapshot, or a bogus/
        // future one) -- any of those means "don't trust this, capture
        // fresh instead."
        private bool TryReadDayStartSnapshot(DateTime expectedBoundary, out double realizedAtStart, out double cashAtStart)
        {
            realizedAtStart = 0;
            cashAtStart = 0;

            string text;
            try
            {
                if (!File.Exists(dayStartSnapshotPath))
                    return false;
                text = File.ReadAllText(dayStartSnapshotPath);
            }
            catch (IOException)
            {
                return false;
            }

            string boundaryRaw = ExtractFlatJsonField(text, "trading_day_boundary");
            string realizedRaw = ExtractFlatJsonField(text, "realized_pnl_at_day_start");
            string cashRaw = ExtractFlatJsonField(text, "cash_value_at_day_start");

            DateTime parsedBoundary;
            double realized, cash;
            if (boundaryRaw == null || realizedRaw == null || cashRaw == null
                || !DateTime.TryParseExact(boundaryRaw.Trim('"'), BoundaryFormat, CultureInfo.InvariantCulture,
                    DateTimeStyles.None, out parsedBoundary)
                || !double.TryParse(realizedRaw, NumberStyles.Float, CultureInfo.InvariantCulture, out realized)
                || !double.TryParse(cashRaw, NumberStyles.Float, CultureInfo.InvariantCulture, out cash))
                return false;

            if (parsedBoundary != expectedBoundary)
                return false;

            realizedAtStart = realized;
            cashAtStart = cash;
            return true;
        }

        private static string ExtractFlatJsonField(string json, string key)
        {
            string marker = "\"" + key + "\":";
            int idx = json.IndexOf(marker, StringComparison.Ordinal);
            if (idx < 0)
                return null;
            int start = idx + marker.Length;
            int end = json.IndexOfAny(new[] { ',', '\n' }, start);
            if (end < 0)
                end = json.Length;
            return json.Substring(start, end - start).Trim();
        }

        private void WriteJsonAtomically(string path, object payload)
        {
            string directory = Path.GetDirectoryName(path);
            if (!string.IsNullOrEmpty(directory) && !Directory.Exists(directory))
                Directory.CreateDirectory(directory);

            string json = SerializeFlatObject(payload);
            string tempPath = path + ".tmp";
            File.WriteAllText(tempPath, json);

            // File.Replace/Move with overwrite is the closest .NET equivalent
            // of the temp-write-then-rename pattern used in oif_writer.py --
            // the Python reader never observes a partially written file.
            if (File.Exists(path))
                File.Replace(tempPath, path, null);
            else
                File.Move(tempPath, path);
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
