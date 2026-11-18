# Claude_Trading_v2

## tradovate_bot

Standalone, deterministic Tradovate execution bot (MNQ, Lucid Trading
25k Flex Eval risk rules). See `tradovate_bot/README.md` for env vars,
demo/live switching, and how to run it. No LLM call exists in its
execution path; it sits below Claude's `day-trader-skill` pack, which
remains the signal/discretionary-approval layer.

Market data comes from NinjaTrader 8 via a local MCP
(`tradovate_bot/localMarketDataMCP.py`, `.mcp.json`,
`ninjatrader_addon/LocalBarExporter.cs`, `candles/`); see "Local market
data" in `tradovate_bot/README.md`.

Run tests:

```sh
python3 -m pytest tests/tradovate_bot/ -v
```
