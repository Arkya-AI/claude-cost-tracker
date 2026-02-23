# claude-cost-tracker

Real-time cost and time tracking for [Claude Code](https://claude.ai/claude-code) sessions.

See exactly what each task costs in tokens, dollars, and wall-clock time — displayed automatically at the end of every response.

```
╔══════════════════════════════════════════════════════════╗
║  ✓ Done · 4m 12s · $0.0842 · Peak: 87.3K tokens         ║
╠══════════════════════════════════════════════════════════╣
║  Editing files 1m 48s   Running commands 42s             ║
║  Run /cost for full breakdown                            ║
╚══════════════════════════════════════════════════════════╝
```

---

## How it works

Claude Code has a [hooks system](https://docs.anthropic.com/en/docs/claude-code/hooks) that fires shell commands around every tool call. This tracker hooks into those events to measure:

- **Wall-clock time** per tool call (pre → post timestamp delta)
- **Real token usage** by reading Claude Code's native session JSONL transcripts
- **Cost** calculated from Anthropic's published pricing per model

Three files:
- `tracker.py` — the core engine (hooks + analysis + formatting)
- `mcp_server.py` — exposes data as MCP tools Claude can query
- `pricing.json` — model pricing table (edit to update)

---

## Install

```bash
git clone https://github.com/Arkya-AI/claude-cost-tracker
cd claude-cost-tracker
bash install.sh
```

The installer:
1. Copies files to `~/.claude/cost-tracker/`
2. Adds `PreToolUse`, `PostToolUse`, `Stop`, and `UserPromptSubmit` hooks to `~/.claude/settings.json`

Then add the MCP server to `~/.claude.json`:

```json
{
  "mcpServers": {
    "cost-tracker": {
      "command": "python3",
      "args": ["/Users/YOU/.claude/cost-tracker/mcp_server.py"]
    }
  }
}
```

Restart Claude Code. Done.

---

## Usage

**Automatic inline summary** — appears at the end of every Claude response (no action needed).

**Full report** — type `/cost` in any session:

```
╔══════════════════════════════════════════════════════════════╗
║  Task done · 4m 12s · Cost: $0.0842 · Peak: 87.3K tokens    ║
╚══════════════════════════════════════════════════════════════╝

  Time breakdown
  ─────────────────────────────────────────────────────────
  Editing files                  1m 48s  ████████████   42%
  Running commands                  42s  ████            16%
  Reading files                     31s  ███             12%

  Cost breakdown
  ─────────────────────────────────────────────────────────
  Token type                      Tokens    Rate/M      Cost
  ─────────────────────────────────────────────────────────
  Input (uncached)                  2.1K     $3.00    $0.0006
  Output                            471    $15.00    $0.0071
  Cache write (creation)           12.4K     $3.75    $0.0465
  Cache read (10x cheaper)        234.5K     $0.30    $0.0703
  ─────────────────────────────────────────────────────────
  TOTAL                                               $0.1245

  API calls: 43   Cache saved you: $0.6382 vs all-input pricing
```

**MCP tools** (Claude can call these directly):
- `get_session_report` — full report for current or named session
- `get_history` — cost summary across last N days
- `get_suggestions` — pattern analysis with cost-reduction tips

---

## Updating pricing

Edit `~/.claude/cost-tracker/pricing.json`:

```json
{
  "models": {
    "claude-sonnet-4-6": {
      "input": 3.00,
      "output": 15.00,
      "cache_creation": 3.75,
      "cache_read": 0.30
    }
  }
}
```

Prices are per million tokens. See [Anthropic pricing](https://www.anthropic.com/pricing).

---

## Requirements

- Python 3.8+
- Claude Code with hooks support

---

## License

MIT — see [LICENSE](LICENSE).
