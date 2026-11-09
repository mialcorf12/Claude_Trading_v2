# Claude_Trading_v2

## tradovate_bot

Standalone, deterministic Tradovate execution bot (MNQ, Lucid Trading
25k Flex Eval risk rules). See `tradovate_bot/README.md` for env vars,
demo/live switching, and how to run it. No LLM call exists in its
execution path; it sits below Claude's `day-trader-skill` pack, which
remains the signal/discretionary-approval layer.

Run tests:

```sh
python3 -m pytest tests/tradovate_bot/ -v
```
