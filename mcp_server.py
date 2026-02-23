#!/usr/bin/env python3
"""
Cost Tracker MCP Server — stdio transport
Implements minimal JSON-RPC 2.0 subset required by MCP spec.
Exposes session cost data to Claude as callable tools.
"""

import sys
import json
import io
import pathlib
import datetime

BASE         = pathlib.Path(__file__).parent
SESSIONS_DIR = BASE / "sessions"
HISTORY_FILE = SESSIONS_DIR / "history.jsonl"

TOOLS = [
    {
        "name": "get_session_report",
        "description": "Full timing and cost breakdown for the current (or specified) Claude session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID (optional — defaults to most recent active session)"
                }
            }
        }
    },
    {
        "name": "get_history",
        "description": "Cost summary across recent sessions — total spend, average per session, most expensive day",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to look back (default 7)"
                }
            }
        }
    },
    {
        "name": "get_suggestions",
        "description": "Pattern analysis across recent sessions — recurring high-cost operations and what to change",
        "inputSchema": {
            "type": "object",
            "properties": {}
        }
    }
]


def import_tracker():
    """Import tracker.py as a module (reuse analysis engine)."""
    sys.path.insert(0, str(BASE))
    import tracker
    return tracker


def capture_report(tracker, session_id=None):
    """Run tracker.mode_report() capturing stdout instead of printing."""
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        tracker.mode_report(session_id)
    finally:
        sys.stdout = old_stdout
    return buf.getvalue().strip() or "No session data found."


def get_history(days=7):
    """Read history.jsonl and summarise recent sessions."""
    if not HISTORY_FILE.exists():
        return "No session history found. Run a task first."

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    sessions = []

    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                date_str = entry.get("date", "")
                if date_str:
                    dt = datetime.datetime.fromisoformat(date_str)
                    if dt >= cutoff:
                        sessions.append(entry)
            except (json.JSONDecodeError, ValueError):
                pass

    if not sessions:
        return f"No sessions found in the last {days} days."

    total_cost   = sum(s.get("total_cost", 0) for s in sessions)
    total_calls  = sum(s.get("tool_calls", 0) for s in sessions)
    peak_tokens  = max((s.get("peak_tokens", 0) for s in sessions), default=0)

    by_day = {}
    for s in sessions:
        day = s.get("date", "")[:10]
        by_day.setdefault(day, []).append(s)

    lines = [
        f"  Sessions in last {days} days: {len(sessions)}",
        f"  Total cost: ~${total_cost:.4f}",
        f"  Avg cost per session: ~${total_cost / len(sessions):.4f}",
        f"  Total tool calls: {total_calls}",
        f"  Peak context (any session): {peak_tokens // 1000}K tokens",
        "",
        "  Daily breakdown:",
    ]
    for day in sorted(by_day.keys(), reverse=True):
        day_sessions = by_day[day]
        day_cost = sum(s.get("total_cost", 0) for s in day_sessions)
        lines.append(f"    {day}  {len(day_sessions)} session(s)  ~${day_cost:.4f}")

    return "\n".join(lines)


def get_suggestions():
    """Analyse history for recurring cost patterns."""
    if not HISTORY_FILE.exists():
        return "No session history to analyse yet."

    sessions = []
    with open(HISTORY_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sessions.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    if not sessions:
        return "No historical sessions found."

    recent = sessions[-20:]
    avg_cost = sum(s.get("total_cost", 0) for s in recent) / len(recent)
    avg_tokens = sum(s.get("peak_tokens", 0) for s in recent) / len(recent)
    expensive = [s for s in recent if s.get("total_cost", 0) > avg_cost * 2]

    suggestions = []
    if avg_tokens > 80_000:
        suggestions.append(
            f"Average peak context is {avg_tokens // 1000:.0f}K tokens. "
            "Split long tasks into 2 sessions to roughly halve cost."
        )
    if len(expensive) > len(recent) * 0.3:
        suggestions.append(
            f"{len(expensive)} of {len(recent)} recent sessions cost 2x above average. "
            "Check /cost after expensive sessions to see which files drove the cost."
        )
    if avg_cost > 0.10:
        suggestions.append(
            f"Average session cost is ${avg_cost:.3f}. "
            "Tell Claude 'read only files you absolutely need' to reduce file reads."
        )

    if not suggestions:
        suggestions.append(
            f"Sessions look efficient. Avg cost: ${avg_cost:.4f}, "
            f"avg peak context: {avg_tokens // 1000:.0f}K tokens."
        )

    return "\n".join(f"  {i+1}. {s}" for i, s in enumerate(suggestions))


def call_tool(name, args):
    """Dispatch tool call and return result string."""
    try:
        if name == "get_session_report":
            tracker = import_tracker()
            return capture_report(tracker, args.get("session_id"))
        elif name == "get_history":
            return get_history(args.get("days", 7))
        elif name == "get_suggestions":
            return get_suggestions()
        else:
            return f"Unknown tool: {name}"
    except Exception as e:
        return f"Error running {name}: {e}"


def handle_request(req):
    """Handle a single JSON-RPC 2.0 request."""
    method = req.get("method", "")
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cost-tracker", "version": "1.0.0"}
            }
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS}
        }

    if method == "tools/call":
        name = req.get("params", {}).get("name", "")
        args = req.get("params", {}).get("arguments", {})
        text = call_tool(name, args)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}]
            }
        }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req  = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                print(json.dumps(resp), flush=True)
        except json.JSONDecodeError as e:
            print(json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {e}"}
            }), flush=True)
        except Exception as e:
            print(json.dumps({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": str(e)}
            }), flush=True)


if __name__ == "__main__":
    main()
