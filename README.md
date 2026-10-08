# obsidian-mcp

MCP server that wraps the [Obsidian CLI](https://obsidian.md/cli). Gives AI assistants (Claude Code, etc.) structured access to your vault without opening full shell access.

Semantic search lives in a separate `vault-search` server; this one does vault reads and writes only.

Requires Obsidian 1.8+ with the CLI enabled and running for the CLI-backed tools.

## What's exposed

36 tools covering the safe, useful parts of the Obsidian CLI:

- **Read** — read notes, outlines, file info, search
- **Write** — create, append, prepend, properties, templates
- **Daily notes** — read, append, prepend, path
- **Organization** — files, folders, move, rename, delete (trash by default)
- **Graph** — tags, backlinks, orphans, unresolved links
- **Tasks** — list, toggle
- **Vault** — bookmarks, templates, commands, plugins, sync status

## What's deliberately excluded

- `eval` — arbitrary JS execution
- `dev:*` — Chrome DevTools Protocol, DOM, console, screenshots
- `plugin:install/uninstall` — use `run_command` if needed

## Requirements

- Python 3.11+
- Obsidian running with CLI enabled
- Works from Windows, macOS, and Linux/WSL2

## Install

```bash
git clone <this-repo> obsidian-mcp
cd obsidian-mcp
python3 -m venv .venv
.venv/bin/pip install "mcp[cli]>=1.0.0"
```

On Windows, replace `.venv/bin/pip` with `.venv\Scripts\pip`.

## Configure for Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "obsidian": {
      "command": "/path/to/obsidian-mcp/.venv/bin/python",
      "args": ["/path/to/obsidian-mcp/server.py"],
      "env": {
        "OBSIDIAN_VAULT": "My Vault"
      }
    }
  }
}
```

Restart Claude Code to pick up the change.

## Tests

```
python -m unittest discover tests
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OBSIDIAN_VAULT` | No | Default vault. If omitted, tools target whichever vault Obsidian has active. |
| `OBSIDIAN_CLI` | No | Path to the CLI binary. Auto-detected if omitted. |

### Choosing a vault per call

Every tool except `list_vaults` and `version` takes an optional `vault` argument. A call with `vault="Notebooks"` runs against that vault; a call without it uses `OBSIDIAN_VAULT`. One server can therefore reach every vault Obsidian knows about, and setting `OBSIDIAN_VAULT` keeps the default from drifting when someone switches vaults in the Obsidian UI.

### Auto-detection paths

- **Windows** — `C:\Program Files\Obsidian\Obsidian.com`
- **macOS** — `/Applications/Obsidian.app/Contents/MacOS/obsidian-cli`
- **WSL2** — `/mnt/c/Program Files/Obsidian/Obsidian.com`
- **Linux** — `obsidian` on PATH (snap, AppImage, etc.)

Set `OBSIDIAN_CLI` if your install is in a non-standard location.
