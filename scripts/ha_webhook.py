"""Call the ha-mcp tools over its webhook, without the DSH MCP client.

Why this exists: the `ha` MCP client in DSH is disconnected, and its config sets
``failOnStartupError: false``, so when Home Assistant was offline for ~3.5 hours
on 2026-10-13 DSH started without it and never reconnected. The server itself is
healthy -- the webhook answers HTTP 200 and lists all 77 tools -- so the tools can
be driven over plain HTTP until DSH is restarted.

**The webhook URL is an admin credential.** The DSH profile comments say so:
"the URL below IS the credential: it is an HA webhook secret with admin access.
Treat it like a password."

So this script does not contain it. The URL is read at runtime, in this order:

1. ``$ROBOROCK_HA_WEBHOOK`` if set,
2. else the url of the ``mcp-ha`` entry in the DSH profile patch, if that file
   is readable.

Reading it from the profile mirrors what ``mcp-github.cmd`` does with the GitHub
token: fetch the secret at spawn time so no copy of it is stored anywhere new.

Usage:
    python scripts/ha_webhook.py <tool> [json-args]
    python scripts/ha_webhook.py ha_get_state '{"entity_id": "cover.x"}'
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ENV_VAR = "ROBOROCK_HA_WEBHOOK"

# The DSH profile that defines the `ha` MCP client. Only read, never written.
PROFILE_PATCHES = (
    Path.home() / ".dsh" / "profiles" / "web" / "cordis.patch.yml",
    Path.home() / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml",
)


def webhook_url() -> str:
    """Return the ha-mcp webhook URL, or explain how to provide it."""
    if value := os.environ.get(ENV_VAR):
        return value.strip()

    for path in PROFILE_PATCHES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Take the url on the `mcp-ha` entry specifically: other MCP entries in
        # the same file have their own urls, and picking the wrong one would
        # send an admin credential nowhere useful.
        block = re.search(
            r"-\s*id:\s*mcp-ha\b(.*?)(?=\n\s*-\s*id:|\Z)", text, re.DOTALL
        )
        if block and (match := re.search(r"url:\s*(\S+)", block.group(1))):
            return match.group(1).strip()

    raise SystemExit(
        f"no ha-mcp webhook found.\n"
        f"Set {ENV_VAR}, or make sure one of these is readable:\n  "
        + "\n  ".join(str(p) for p in PROFILE_PATCHES)
    )


def call(tool: str, arguments: dict, url: str | None = None) -> str:
    """Call one tool and return its text content."""
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ).encode()
    request = urllib.request.Request(
        url or webhook_url(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        body = response.read().decode("utf-8", "replace")

    # Server-sent events: the payload is on the `data:` line(s).
    data = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    if not data:
        return body
    parsed = json.loads(data[-1])
    if error := parsed.get("error"):
        raise SystemExit(f"{tool} failed: {json.dumps(error, ensure_ascii=False)}")
    content = parsed.get("result", {}).get("content") or []
    return content[0].get("text", "") if content else ""


def list_tools(url: str | None = None) -> list[str]:
    """Return the tool names the server exposes."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    ).encode()
    request = urllib.request.Request(
        url or webhook_url(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8", "replace")
    data = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    parsed = json.loads(data[-1])
    return [t["name"] for t in parsed["result"]["tools"]]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    if sys.argv[1] == "--list-tools":
        for name in list_tools():
            print(name)
        raise SystemExit(0)

    tool_name = sys.argv[1]
    tool_args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(call(tool_name, tool_args))
