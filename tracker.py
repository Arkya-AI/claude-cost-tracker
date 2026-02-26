#!/usr/bin/env python3
"""
Claude Code Cost Tracker
Modes: pre | post | stop | report
Called by native hooks in ~/.claude/settings.json
"""

import sys, json, time, pathlib

BASE          = pathlib.Path(__file__).parent
SESSIONS_DIR  = BASE / "sessions"
ORPHANED_DIR  = SESSIONS_DIR / "orphaned"
HISTORY_FILE  = SESSIONS_DIR / "history.jsonl"
PRICING_FILE  = BASE / "pricing.json"

TOOL_CATEGORIES = {
    "Read": "Reading files",       "Write": "Writing files",
    "Edit": "Editing files",       "MultiEdit": "Editing files",
    "Bash": "Running commands",    "Task": "Delegating to sub-task",
    "Grep": "Searching code",      "Glob": "Searching code",
    "WebFetch": "Browsing web",    "WebSearch": "Browsing web",
}

def category_for(tool_name):
    if str(tool_name).startswith("mcp__"):
        return "MCP tool call"
    return TOOL_CATEGORIES.get(tool_name, "Other")

FALLBACK_PRICING    = {"input": 3.00, "output": 15.00, "cache_creation": 3.75, "cache_read": 0.30}
CONTEXT_CAP         = 200_000
MAX_RESPONSE_TOKENS = 50_000
CLAUDE_PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"

def load_pricing():
    try:
        with open(PRICING_FILE) as f:
            data = json.load(f)
        return (
            data.get("models", {}),
            data.get("model_aliases", {}),
            data.get("context_window_cap_tokens", CONTEXT_CAP),
        )
    except Exception:
        return {}, {}, CONTEXT_CAP

def price_for(model, models_dict, aliases_dict=None):
    aliases_dict = aliases_dict or {}
    # Try direct match first
    if model in models_dict:
        return models_dict[model]
    # Try alias lookup
    resolved = aliases_dict.get(model)
    if resolved and resolved in models_dict:
        return models_dict[resolved]
    # Try stripping date suffix: "claude-sonnet-4-5-20250929" → try shorter prefixes
    parts = (model or "").split("-")
    for end in range(len(parts), 2, -1):
        candidate = "-".join(parts[:end])
        if candidate in models_dict:
            return models_dict[candidate]
    print(f"[cost-tracker] Warning: model '{model}' not in pricing.json, using sonnet fallback", file=sys.stderr)
    return FALLBACK_PRICING

def session_file(session_id):
    return SESSIONS_DIR / f"active-{session_id}.jsonl"

def ensure_dirs():
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ORPHANED_DIR.mkdir(parents=True, exist_ok=True)

def append_event(session_id, event):
    ensure_dirs()
    with open(session_file(session_id), "a") as f:
        f.write(json.dumps(event) + "\n")

def read_events(session_id):
    path = session_file(session_id)
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events

def cleanup_orphaned():
    import shutil
    cutoff = time.time() - 86400
    try:
        for p in SESSIONS_DIR.glob("active-*.jsonl"):
            if p.stat().st_mtime < cutoff:
                shutil.move(str(p), str(ORPHANED_DIR / p.name))
    except Exception:
        pass

def estimate_tokens(text):
    return min(len(str(text)) // 4, MAX_RESPONSE_TOKENS)

def find_claude_session_jsonl(session_id):
    """Search ~/.claude/projects/*/<session_id>.jsonl for real API usage data."""
    if not session_id or session_id in ("unknown", ""):
        return None
    if not CLAUDE_PROJECTS_DIR.exists():
        return None
    try:
        for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if candidate.exists():
                return candidate
    except Exception:
        pass
    return None

def parse_real_usage(jsonl_path):
    """
    Parse actual API token usage from a Claude Code JSONL transcript.

    Each API call (one requestId) produces 1-4 streaming events with identical
    usage figures. We keep only the first event per requestId to avoid 3-4x inflation.

    Returns dict with: input_tokens, output_tokens, cache_creation_tokens,
    cache_read_tokens, api_calls, model, context_by_turn — or None on failure.
    """
    seen_req = set()
    api_calls = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message", {})
                if "usage" not in msg:
                    continue
                req_id = d.get("requestId")
                if req_id in seen_req:
                    continue
                seen_req.add(req_id)
                usage = msg["usage"]
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cc  = usage.get("cache_creation_input_tokens", 0)
                cr  = usage.get("cache_read_input_tokens", 0)
                api_calls.append({
                    "ts":           d.get("timestamp", ""),
                    "model":        msg.get("model", ""),
                    "input":        inp,
                    "output":       out,
                    "cache_create": cc,
                    "cache_read":   cr,
                    "total_ctx":    inp + cc + cr,
                })
    except Exception:
        return None
    if not api_calls:
        return None
    model = next((e["model"] for e in reversed(api_calls) if e["model"]), "")
    return {
        "input_tokens":          sum(e["input"]        for e in api_calls),
        "output_tokens":         sum(e["output"]       for e in api_calls),
        "cache_creation_tokens": sum(e["cache_create"] for e in api_calls),
        "cache_read_tokens":     sum(e["cache_read"]   for e in api_calls),
        "api_calls":             len(api_calls),
        "model":                 model,
        "context_by_turn":       [{"ts": e["ts"], "total_ctx": e["total_ctx"]} for e in api_calls],
    }


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse(events, pricing_models, pricing_aliases, context_cap, jsonl_path=None):
    import collections
    if not events:
        return {}

    pre_events  = [e for e in events if e.get("type") == "pre"]
    post_events = [e for e in events if e.get("type") == "post"]

    all_ts     = [e["ts"] for e in events if "ts" in e]
    duration_s = max(all_ts) - min(all_ts) if len(all_ts) > 1 else 0

    # ── Timing pairs (always from hook events) ────────────────────────────────
    post_by_tool = collections.defaultdict(list)
    for p in post_events:
        post_by_tool[p.get("tool")].append(p)

    paired = []
    for pre in pre_events:
        tool       = pre.get("tool")
        candidates = post_by_tool.get(tool, [])
        matched    = next((c for c in candidates if c.get("ts", 0) > pre.get("ts", 0)), None)
        if matched:
            candidates.remove(matched)
            elapsed = matched["ts"] - pre["ts"]
        else:
            elapsed = 0
            matched = {}
        paired.append({
            "tool":            tool,
            "category":        category_for(tool),
            "elapsed_s":       elapsed,
            "input_tokens":    pre.get("input_tokens", 0),
            "response_tokens": matched.get("response_tokens", 0),
            "file_path":       pre.get("file_path") or matched.get("file_path"),
            "command":         pre.get("command"),
            "ts":              pre.get("ts", 0),
        })

    time_by_category = collections.Counter()
    for p in paired:
        time_by_category[p["category"]] += p["elapsed_s"]
    total_timed = sum(time_by_category.values()) or 1

    # File read counts (for report display)
    file_map = {}
    for p in paired:
        if p["tool"] == "Read" and p["file_path"]:
            fp = p["file_path"]
            file_map.setdefault(fp, {"reads": 0})["reads"] += 1

    # Bash command frequency
    bash_commands = collections.Counter()
    for p in paired:
        if p["tool"] == "Bash" and p["command"]:
            base = p["command"].strip().split()[0] if p["command"].strip() else p["command"]
            bash_commands[base] += 1

    # ── Real token data (from Claude Code JSONL) ──────────────────────────────
    real_usage = parse_real_usage(jsonl_path) if jsonl_path else None
    using_real_data = real_usage is not None

    if using_real_data:
        model   = real_usage["model"] or "claude-sonnet-4-6"
        pricing = price_for(model, pricing_models, pricing_aliases)

        inp_tok = real_usage["input_tokens"]
        out_tok = real_usage["output_tokens"]
        cc_tok  = real_usage["cache_creation_tokens"]
        cr_tok  = real_usage["cache_read_tokens"]

        total_cost = (
            inp_tok * pricing["input"]
            + out_tok * pricing["output"]
            + cc_tok * pricing.get("cache_creation", pricing["input"] * 1.25)
            + cr_tok * pricing.get("cache_read", pricing["input"] * 0.1)
        ) / 1_000_000

        context_timeline = [
            {"ts": e["ts"], "tokens": e["total_ctx"]}
            for e in real_usage["context_by_turn"]
        ]
        peak_tokens = max((c["tokens"] for c in context_timeline), default=0)
        api_calls_count = real_usage["api_calls"]

    else:
        # Fallback: estimation (unchanged logic)
        model   = next((e.get("model") for e in events if e.get("model")), "claude-sonnet-4-6")
        pricing = price_for(model, pricing_models, pricing_aliases)
        input_price  = pricing["input"]  / 1_000_000
        output_price = pricing["output"] / 1_000_000
        inp_tok = out_tok = cc_tok = cr_tok = 0

        sorted_events     = sorted(paired, key=lambda x: x["ts"])
        cumulative_tokens = 0
        context_timeline  = []
        total_cost        = 0.0
        turn_count        = len(sorted_events)

        for i, ev in enumerate(sorted_events):
            added             = ev["input_tokens"] + ev["response_tokens"]
            cumulative_tokens = min(cumulative_tokens + added, context_cap)
            turns_remaining   = turn_count - i
            turn_cost         = (
                ev["response_tokens"] * input_price * turns_remaining
                + ev["response_tokens"] * output_price
            )
            total_cost += turn_cost
            context_timeline.append({"ts": ev["ts"], "tokens": cumulative_tokens})

        peak_tokens     = max((c["tokens"] for c in context_timeline), default=0)
        api_calls_count = len(paired)

    # ── Suggestions ───────────────────────────────────────────────────────────
    suggestions = []
    for cmd, count in bash_commands.most_common(3):
        if count > 2:
            suggestions.append(
                f"You ran `{cmd}` {count} times mid-task — consider running it once at the end."
            )
    if peak_tokens > 50_000:
        suggestions.append(
            f"At {peak_tokens // 1000}K peak context, splitting into 2 sessions saves ~50% cost."
        )
    suggestions = suggestions[:3] or ["No major inefficiencies detected."]

    return {
        "duration_s":       duration_s,
        "total_cost":       total_cost,
        "peak_tokens":      peak_tokens,
        "tool_calls":       len(paired),
        "api_calls":        api_calls_count,
        "time_by_category": time_by_category,
        "total_timed":      total_timed,
        "file_map":         file_map,
        "context_timeline": context_timeline,
        "suggestions":      suggestions,
        "bash_commands":    bash_commands,
        "model":            model,
        "using_real_data":  using_real_data,
        "pricing":          pricing,
        "inp_tok":          inp_tok,
        "out_tok":          out_tok,
        "cc_tok":           cc_tok,
        "cr_tok":           cr_tok,
    }


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_duration(s):
    s = int(s)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s"

def fmt_tokens(n):
    return f"{n / 1000:.1f}K" if n >= 1000 else str(n)

def bar(frac, width=12):
    filled = int(round(frac * width))
    return "█" * filled + " " * (width - filled)

def format_short_summary(data):
    if not data:
        return ""
    d = data
    cost_prefix = "$" if d.get("using_real_data") else "~$"
    header = (
        f"  \u2713 Done \u00b7 {fmt_duration(d['duration_s'])} \u00b7 "
        f"{cost_prefix}{d['total_cost']:.4f} \u00b7 "
        f"Peak: {fmt_tokens(d['peak_tokens'])} tokens  "
    )
    w = len(header)
    top = sorted(d["time_by_category"].items(), key=lambda x: -x[1])[:2]
    cats = "   ".join(f"{cat} {fmt_duration(t)}" for cat, t in top)
    lines = [
        "\u2554" + "\u2550" * w + "\u2557",
        "\u2551" + header + "\u2551",
        "\u2560" + "\u2550" * w + "\u2563",
        "\u2551  " + cats.ljust(w - 2) + "\u2551",
        "\u2551  " + "Run /cost for full breakdown".ljust(w - 2) + "\u2551",
        "\u255a" + "\u2550" * w + "\u255d",
    ]
    return "\n".join(lines)

def format_full_report(data):
    if not data:
        return "No session data recorded. Run a task first."
    d           = data
    cost_prefix = "$" if d.get("using_real_data") else "~$"
    header = (
        f"  Task done · {fmt_duration(d['duration_s'])} · "
        f"Cost: {cost_prefix}{d['total_cost']:.4f} · Peak: {fmt_tokens(d['peak_tokens'])} tokens  "
    )
    w     = len(header)
    lines = ["╔" + "═" * w + "╗", "║" + header + "║", "╚" + "═" * w + "╝", ""]

    # Time breakdown
    lines += ["  Time breakdown", "  " + "─" * 57]
    for cat, t in sorted(d["time_by_category"].items(), key=lambda x: -x[1]):
        frac = t / d["total_timed"]
        lines.append(f"  {cat:<28}  {fmt_duration(t):>7}  {bar(frac):12}  {int(frac * 100)}%")
    lines.append("")

    # Cost breakdown — real token table or estimated fallback
    lines += ["  Cost breakdown", "  " + "─" * 57]
    if d.get("using_real_data"):
        pricing  = d["pricing"]
        inp_tok  = d["inp_tok"]
        out_tok  = d["out_tok"]
        cc_tok   = d["cc_tok"]
        cr_tok   = d["cr_tok"]
        inp_cost = inp_tok * pricing["input"]                          / 1_000_000
        out_cost = out_tok * pricing["output"]                         / 1_000_000
        cc_cost  = cc_tok * pricing.get("cache_creation", 0)          / 1_000_000
        cr_cost  = cr_tok * pricing.get("cache_read", 0)              / 1_000_000
        saved    = (cc_tok + cr_tok) * (pricing["input"] - pricing.get("cache_read", 0)) / 1_000_000

        lines += [
            f"  {'Token type':<28}  {'Tokens':>9}  {'Rate/M':>8}  {'Cost':>8}",
            "  " + "─" * 57,
            f"  {'Input (uncached)':<28}  {fmt_tokens(inp_tok):>9}  ${pricing['input']:>6.2f}  ${inp_cost:>7.4f}",
            f"  {'Output':<28}  {fmt_tokens(out_tok):>9}  ${pricing['output']:>6.2f}  ${out_cost:>7.4f}",
            f"  {'Cache write (creation)':<28}  {fmt_tokens(cc_tok):>9}  ${pricing.get('cache_creation', 0):>6.2f}  ${cc_cost:>7.4f}",
            f"  {'Cache read (10x cheaper)':<28}  {fmt_tokens(cr_tok):>9}  ${pricing.get('cache_read', 0):>6.2f}  ${cr_cost:>7.4f}",
            "  " + "─" * 57,
            f"  {'TOTAL':<28}  {'':>9}  {'':>8}  ${d['total_cost']:>7.4f}",
            "",
            f"  API calls: {d['api_calls']}   Cache saved you: ${saved:.4f} vs all-input pricing",
            "",
        ]
        # File read counts (no per-file cost since total is now real)
        file_map = d.get("file_map", {})
        if file_map:
            lines += [
                f"  {'Files read':<40}  {'Reads':>5}",
                "  " + "─" * 48,
            ]
            for fp, fm in sorted(file_map.items(), key=lambda x: -x[1]["reads"])[:8]:
                name = pathlib.Path(fp).name[:38]
                lines.append(f"  {name:<40}  {fm['reads']:>5}")
            lines.append("")
    else:
        lines += [
            "  (Estimated — Claude JSONL not found for this session)",
            f"  Estimated total cost: ~${d['total_cost']:.4f}",
            "",
        ]

    # Context growth
    tl = d["context_timeline"]
    if len(tl) >= 2:
        lines += [
            "  Context window growth", "  " + "─" * 57,
            f"  Start  →  {fmt_tokens(tl[0]['tokens']):>8} tokens",
            f"  Mid    →  {fmt_tokens(tl[len(tl) // 2]['tokens']):>8} tokens",
            f"  End    →  {fmt_tokens(tl[-1]['tokens']):>8} tokens  ← peak this session",
            "",
        ]

    # Suggestions
    lines += ["  3 things worth changing", "  " + "─" * 57]
    for i, s in enumerate(d["suggestions"], 1):
        lines.append(f"  {i}. {s}")
    lines.append("")
    return "\n".join(lines)


# ── Modes ─────────────────────────────────────────────────────────────────────

def mode_pre():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("{}", flush=True)
        return

    session_id = payload.get("session_id", "unknown")
    tool_name  = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    event = {
        "type":         "pre",
        "ts":           time.time(),
        "tool":         tool_name,
        "session_id":   session_id,
        "input_tokens": estimate_tokens(json.dumps(tool_input)),
    }
    if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
        event["file_path"] = (
            tool_input.get("file_path") or
            tool_input.get("path") or
            tool_input.get("filename")
        )
    if tool_name == "Bash":
        event["command"] = tool_input.get("command", "")[:200]

    import random
    if random.random() < 0.01:
        cleanup_orphaned()
    append_event(session_id, event)
    print("{}", flush=True)


def mode_post():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print("{}", flush=True)
        return

    session_id    = payload.get("session_id", "unknown")
    tool_name     = payload.get("tool_name", "")
    tool_response = payload.get("tool_response", "")

    event = {
        "type":            "post",
        "ts":              time.time(),
        "tool":            tool_name,
        "session_id":      session_id,
        "response_tokens": estimate_tokens(tool_response),
    }
    if isinstance(tool_response, dict) and "model" in tool_response:
        event["model"] = tool_response["model"]

    append_event(session_id, event)
    print("{}", flush=True)


def cost_box_already_shown(jsonl_path):
    """Check if Claude's last response already contains the cost box text."""
    if not jsonl_path or not jsonl_path.exists():
        return False
    try:
        with open(jsonl_path) as f:
            lines = f.readlines()
        for line in reversed(lines[-30:]):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("type") == "assistant":
                    content = d.get("message", {}).get("content", [])
                    text = " ".join(
                        item.get("text", "") if isinstance(item, dict) else ""
                        for item in (content if isinstance(content, list) else [])
                    )
                    # Only match if the response IS the cost box (short, <400 chars).
                    # A longer response that merely mentions "✓ Done ·" is not the box.
                    return len(text) < 400 and "\u2713 Done \u00b7" in text
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        pass
    return False


def _archive_session(session_id, events, data):
    """Archive tracker events and write to history. Called once after cost box is shown."""
    import shutil, datetime
    if not events:
        return
    ensure_dirs()
    ts_str       = datetime.datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    archive_path = SESSIONS_DIR / f"{ts_str}.jsonl"
    src          = session_file(session_id)
    if src.exists():
        shutil.copy2(str(src), str(archive_path))
        src.unlink()
    with open(HISTORY_FILE, "a") as f:
        f.write(json.dumps({
            "date":            datetime.datetime.now().isoformat(),
            "session_id":      session_id,
            "duration_ms":     int(data.get("duration_s", 0) * 1000),
            "total_cost":      round(data.get("total_cost", 0), 6),
            "peak_tokens":     data.get("peak_tokens", 0),
            "tool_calls":      data.get("tool_calls", 0),
            "api_calls":       data.get("api_calls", 0),
            "using_real_data": data.get("using_real_data", False),
            "archive":         str(archive_path),
        }) + "\n")


def mode_stop():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session_id   = payload.get("session_id", "unknown")
    events       = read_events(session_id)
    pricing_models, pricing_aliases, context_cap = load_pricing()
    claude_jsonl = find_claude_session_jsonl(session_id)
    data         = analyse(events, pricing_models, pricing_aliases, context_cap, jsonl_path=claude_jsonl)
    summary      = format_short_summary(data)

    # Always archive tracker events (idempotent — safe if already done)
    _archive_session(session_id, events, data)

    if summary and not cost_box_already_shown(claude_jsonl):
        print(json.dumps({
            "decision": "block",
            "reason": summary,
            "systemMessage": (
                "MANDATORY: Output ONLY the exact cost box from the stop hook reason "
                "as your complete response. No introduction, no explanation, no "
                "paraphrasing, no commentary. Just the box lines verbatim."
            ),
        }), flush=True)
    else:
        print("{}", flush=True)


def mode_prompt_inject():
    """Called by UserPromptSubmit hook. Injects last task's cost box as a start-of-response requirement."""
    last_cost_file = SESSIONS_DIR / "last-cost.txt"
    if last_cost_file.exists():
        cost_box = last_cost_file.read_text().strip()
        last_cost_file.unlink()  # Consume — show once only
        if cost_box:
            msg = (
                "MANDATORY: The FIRST thing you output in your response MUST be this exact "
                "cost box (verbatim, before any other text). Copy it exactly:\n\n"
                + cost_box + "\n\n"
                "After outputting the cost box, continue with your normal response."
            )
            print(json.dumps({"systemMessage": msg}), flush=True)
            return
    print("{}", flush=True)


def mode_inline():
    """Compact summary for inline inclusion at end of Claude responses."""
    active_files = list(SESSIONS_DIR.glob("active-*.jsonl"))
    if not active_files:
        print("*(no session data)*")
        return
    target = max(active_files, key=lambda p: p.stat().st_mtime)
    events = []
    with open(target) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    session_id = next((e.get("session_id") for e in events if "session_id" in e), None)
    pricing_models, pricing_aliases, context_cap = load_pricing()
    claude_jsonl = find_claude_session_jsonl(session_id)
    data = analyse(events, pricing_models, pricing_aliases, context_cap, jsonl_path=claude_jsonl)
    print(format_short_summary(data) or "*(no session data)*")


def mode_report(session_id_hint=None):
    target = None
    if session_id_hint:
        candidate = session_file(session_id_hint)
        if candidate.exists():
            target = candidate
    if target is None:
        active_files = list(SESSIONS_DIR.glob("active-*.jsonl"))
        if active_files:
            target = max(active_files, key=lambda p: p.stat().st_mtime)
    if target is None:
        print("No active session data found. Run /cost after starting a task.")
        return

    events = []
    with open(target) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    session_id                          = next((e.get("session_id") for e in events if "session_id" in e), None)
    pricing_models, pricing_aliases, context_cap = load_pricing()
    claude_jsonl                        = find_claude_session_jsonl(session_id)
    data = analyse(events, pricing_models, pricing_aliases, context_cap, jsonl_path=claude_jsonl)
    print(format_full_report(data))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "report"
    try:
        if mode == "pre":
            mode_pre()
        elif mode == "post":
            mode_post()
        elif mode == "stop":
            mode_stop()
        elif mode == "inline":
            mode_inline()
        elif mode == "prompt-inject":
            mode_prompt_inject()
        elif mode == "report":
            mode_report(sys.argv[2] if len(sys.argv) > 2 else None)
        else:
            print(f"Unknown mode: {mode}", file=sys.stderr)
    except Exception as e:
        print(f"[cost-tracker error] {e}", file=sys.stderr)
        if mode in ("pre", "post"):
            print("{}", flush=True)
    finally:
        sys.exit(0)
