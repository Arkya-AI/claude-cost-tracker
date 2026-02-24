#!/usr/bin/env bash
# claude-cost-tracker installer
# Usage: bash install.sh

set -e

INSTALL_DIR="$HOME/.claude/cost-tracker"
SETTINGS_FILE="$HOME/.claude/settings.json"

echo "Installing claude-cost-tracker to $INSTALL_DIR ..."

# 1. Copy files
mkdir -p "$INSTALL_DIR/sessions"
cp tracker.py    "$INSTALL_DIR/tracker.py"
cp mcp_server.py "$INSTALL_DIR/mcp_server.py"
cp pricing.json  "$INSTALL_DIR/pricing.json"
chmod +x "$INSTALL_DIR/tracker.py" "$INSTALL_DIR/mcp_server.py"

echo "Files installed."

# 2. Patch settings.json hooks
PYTHON=$(command -v python3.11 || command -v python3 || echo "python3")
echo "Using Python: $PYTHON"

if [ ! -f "$SETTINGS_FILE" ]; then
    echo "{}" > "$SETTINGS_FILE"
fi

"$PYTHON" - <<PYEOF
import json, pathlib, sys

settings_path = pathlib.Path("$SETTINGS_FILE")
install_dir   = "$INSTALL_DIR"
python        = "$PYTHON"

try:
    data = json.loads(settings_path.read_text())
except Exception:
    data = {}

hooks = data.setdefault("hooks", {})

def has_hook(hook_list, keyword):
    for group in hook_list:
        for h in group.get("hooks", []):
            if keyword in h.get("command", ""):
                return True
    return False

# PreToolUse
pre = hooks.setdefault("PreToolUse", [{"hooks": []}])
if not has_hook(pre, "tracker.py pre"):
    pre[0]["hooks"].append({
        "type": "command",
        "command": f"{python} -S {install_dir}/tracker.py pre",
        "timeout": 5
    })

# PostToolUse
post = hooks.setdefault("PostToolUse", [{"hooks": []}])
if not has_hook(post, "tracker.py post"):
    post[0]["hooks"].append({
        "type": "command",
        "command": f"{python} -S {install_dir}/tracker.py post",
        "timeout": 5
    })

# Stop
stop = hooks.setdefault("Stop", [{"hooks": []}])
if not has_hook(stop, "tracker.py stop"):
    stop[0]["hooks"].append({
        "type": "command",
        "command": f"{python} {install_dir}/tracker.py stop",
        "timeout": 15
    })


settings_path.write_text(json.dumps(data, indent=2))
print("Hooks written to", settings_path)
PYEOF

echo ""
echo "Done! Next steps:"
echo ""
echo "1. Add the MCP server to your ~/.claude.json (mcpServers section):"
echo ""
echo '   "cost-tracker": {'
echo '     "command": "'"$PYTHON"'",'
echo '     "args": ["'"$INSTALL_DIR"'/mcp_server.py"]'
echo '   }'
echo ""
echo "2. Restart Claude Code."
echo ""
echo "3. Run /cost inside any session to see a full cost report."
