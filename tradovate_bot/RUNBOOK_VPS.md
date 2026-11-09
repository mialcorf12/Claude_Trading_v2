# Runbook: deploy the NT8 ATI execution bot on the Windows VPS

Every command below is run **by the user**, on the VPS, via RDP. Claude has
no remote-execution tool into this machine, and per the 2026-11-09 decision
recorded in `odd/tasks/tradovate-execution-bot.md`, none will be set up —
this runbook is the deliberate substitute. Paste back any error output here
(minus credentials) and it'll get fixed.

Prerequisites already true per the task doc: the VPS (TradingVPS Basic, 2
cores/8GB) is provisioned, and NT8 is already installed and connected to the
Lucid Tradovate account.

## 1. Clone/pull the repo

Check git is available:
```powershell
git --version
```
If missing, install "Git for Windows" first.

First time:
```powershell
cd C:\
git clone https://github.com/mialcorf12/Claude_Trading_v2.git
cd Claude_Trading_v2
```

The OIF/AddOn work currently lives on `feature/tradovate-oif-execution`, not
yet merged to `main`:
```powershell
git fetch origin
git checkout feature/tradovate-oif-execution
```
Later updates: `git pull`. Once this branch is reviewed and merged, switch
back with `git checkout main && git pull`.

## 2. Install Python + dependencies

Check `python --version` (3.9+; developed against 3.14). If missing, install
from python.org (check "Add python.exe to PATH" during install).

```powershell
cd C:\Claude_Trading_v2
python -m pip install --user pytest requests
```
(`requests`/`pytest` are only needed for `client.py`'s historical tests —
the live ATI path itself has zero third-party dependencies.)

Sanity check before touching NT8 at all:
```powershell
python -m pytest tests\tradovate_bot\ -q
```
Expect `69 passed`.

## 3. Enable NT8's Automated Trading Interface (ATI)

1. In NT8: Tools → Options → the ATI settings tab.
2. Enable ATI and note the `incoming`/`outgoing` folder paths it shows
   (default under `Documents\NinjaTrader 8\`) — these feed
   `NT8_INCOMING_DIR`/`NT8_OUTGOING_DIR` in step 5.
3. Confirm the Lucid Tradovate account is the one connected (already true
   per the task doc).
4. Restart NT8 if prompted.

Not independently verified: the exact ATI menu wording/location for this
specific NT8 build. If what's on screen doesn't match, check NT8's own Help
for "Automated Trading Interface", or describe what you see and this gets
corrected.

## 4. Install the read-only telemetry AddOn

1. Copy `ninjatrader_addon\AccountTelemetryAddOn.cs` from this repo into `Documents\NinjaTrader 8\bin\Custom\AddOns\`.
   copy ninjatrader_addon\AccountTelemetryAddOn.cs "C:\Users\Administrator\Documents\NinjaTrader 8\bin\Custom\AddOns\"
2. In NT8: Tools → Edit NinjaScript → AddOn → Compile (F5). Confirm no compile errors.
3. Set the environment variables in step 5 below **before** restarting NT8
   — the AddOn has no window, so NT8 has no properties screen for it; it
   reads its config from the same `NT8_ACCOUNT_ID` /
   `ACCOUNT_TELEMETRY_JSON_PATH` env vars the Python executor uses, plus
   the optional `ACCOUNT_TELEMETRY_EXPORT_INTERVAL_SECONDS` (default `5`).
4. Restart NT8 so the AddOn loads with that config.
5. Verify it's running: open NT8's NinjaScript Output window (Control
   Center → New → NinjaScript Output) and look for a line starting with
   `AccountTelemetryAddOn: started.` — there is no menu entry to "find" for
   a headless AddOn like this one; the Output window is the real signal. A
   line like `account '...' not found` instead means `NT8_ACCOUNT_ID`
   doesn't match any connected account's exact name (check NT8's Accounts
   tab).
6. Once it's logging as started, open the configured JSON path in Notepad
   after a few seconds and confirm the numbers match NT8's own Cash Value /
   Account Performance display. This is the sanity check already flagged
   as unverified in `tradovate_bot/README.md` — do it now, including
   watching it survive one day rollover, before trusting it for the funded
   account.

## 5. Configure environment variables

Same contract as `tradovate_bot/README.md`. Set at machine scope so they
survive reboots (PowerShell as Administrator):
```powershell
[Environment]::SetEnvironmentVariable("NT8_INCOMING_DIR", "C:\Users\Administrator\Documents\NinjaTrader 8\incoming", "Machine")
[Environment]::SetEnvironmentVariable("NT8_OUTGOING_DIR", "C:\Users\Administrator\Documents\NinjaTrader 8\outgoing", "Machine")
[Environment]::SetEnvironmentVariable("NT8_ACCOUNT_ID", "Sim101", "Machine")
[Environment]::SetEnvironmentVariable("ACCOUNT_TELEMETRY_JSON_PATH", "C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\account_telemetry.json", "Machine")
```
Open a new terminal afterward so it picks up the machine-level values.

## 6. Run it once in the foreground first

```powershell
cd C:\TradingPlatform_v2\Claude_Trading_v2
python -m tradovate_bot.executor
```
With `trader/orders.json` empty or absent, a clean startup line on stderr
and no crash across a few poll cycles is a successful smoke test. Ctrl-C to
stop. Do this before ever pointing it at a real signal, and before wiring
up unattended execution.

## 7. Run it unattended (NSSM)

NSSM (https://nssm.cc) over Task Scheduler for a long-lived process — it
gives direct stdout/stderr log redirection and simpler restart-on-failure
behavior.

```powershell
C:\nssm\nssm.exe install TradovateExecutor
```
In the GUI NSSM opens:
- **Path**: full path to `python.exe` (`where python` to find it)
- **Startup directory**: `C:\Claude_Trading_v2`
- **Arguments**: `-m tradovate_bot.executor`
- **I/O tab**: redirect stdout/stderr to, e.g.,
  `C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\executor.stdout.log` /
  `executor.stderr.log`
- **Environment tab** (only if step 5's vars weren't set at Machine scope):
  add them here, one `NAME=value` per line.

Start and verify:
```powershell
nssm start TradovateExecutor
Get-Content C:\TradingPlatform_v2\Claude_Trading_v2\tradovate_bot\state\decisions.log.jsonl -Tail 5
```

Stop/restart:
```powershell
nssm stop TradovateExecutor
nssm restart TradovateExecutor
```

Task Scheduler alternative, if NSSM is undesirable: a task triggered "At
startup", action = `python.exe` with the same arguments and working
directory, "Run whether user is logged on or not", with Task Scheduler's own
restart-on-failure settings — functionally similar, without NSSM's log
redirection convenience.

## What this runbook does not cover

- Claude cannot run any step above directly — there is no remote-access
  tool into this VPS, and that is now a closed decision, not an open
  question (see the task doc).
- The exact NT8 ATI menu wording for your installed build — verify on
  screen.
- Whether the NinjaScript AddOn's day-rollover logic and NT8 ATI's outgoing
  confirmation-file format are correct — both still need manual
  verification against this live NT8 instance (tracked in
  `tradovate_bot/README.md`'s "What is NOT verified yet").
