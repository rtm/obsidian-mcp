# obsidian-mcp

MCP server that wraps the [Obsidian CLI](https://obsidian.md/cli). Gives AI assistants (Claude Code, etc.) structured access to your vault without opening full shell access.

It also carries a **local semantic search index** over the vault (see below), which is independent of the CLI and keeps working when Obsidian is not running.

Requires Obsidian 1.8+ with the CLI enabled and running for the CLI-backed tools.

## What's exposed

40 tools. The CLI-backed set covers the safe, useful parts of the Obsidian CLI:

- **Read** — read notes, outlines, file info, search
- **Write** — create, append, prepend, properties, templates
- **Daily notes** — read, append, prepend, path
- **Organization** — files, folders, move, rename, delete (trash by default)
- **Graph** — tags, backlinks, orphans, unresolved links
- **Tasks** — list, toggle
- **Vault** — bookmarks, templates, commands, plugins, sync status

Plus four search tools backed by the local index:

- **`hybrid_search`** — keyword + semantic, fused. The recommended default
- **`semantic_search`** — meaning only, for when you do not know the wording
- **`reindex_vault`** — refresh now rather than waiting for the timer
- **`index_status`** — notes and chunks indexed per vault

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

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OBSIDIAN_VAULT` | No | Vault name for multi-vault setups. If omitted, uses the active vault. |
| `OBSIDIAN_CLI` | No | Path to the CLI binary. Auto-detected if omitted. |

## Semantic search

`vault_index.py` builds a hybrid search index over the vault's markdown, read
straight from disk. Keyword ranking is BM25 via SQLite FTS5; semantic ranking is
vector similarity via [sqlite-vec](https://github.com/asg017/sqlite-vec) over
embeddings from a local [Ollama](https://ollama.com) model. The two rankings
merge by reciprocal rank fusion.

Hybrid is the default because neither half suffices alone: BM25 carries exact
tokens (dated filenames, identifiers, proper nouns) that embeddings blur, while
the vectors carry paraphrases BM25 cannot see.

### Setup

```bash
sudo dnf install ollama && sudo systemctl enable --now ollama   # or your platform's install
ollama pull bge-m3
uv pip install --python .venv/bin/python sqlite-vec httpx pyyaml
./reindex.py            # first full index
```

Keep it current with the systemd user units in `systemd/` (a 15-minute timer):

```bash
cp systemd/vault-search-reindex.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vault-search-reindex.timer
```

Reindexing is incremental — files are hashed and only changed ones are
re-embedded, so a no-op run costs about a tenth of a second.

### Shell usage

`vsearch` searches from the command line; symlink it onto your `PATH`.

```bash
vsearch what did I decide about the water delivery order   # hybrid (default)
vsearch --semantic novel about Oxford translators
vsearch --keyword IRMAA
vsearch -k 20 --folder writing/nihongoism kata
vsearch --status        # index health
vsearch --reindex       # refresh now
```

### Search environment variables

| Variable | Default | Description |
|---|---|---|
| `VAULT_ROOT` | `~/wrk/obsidian` | Where vaults live. Any subdirectory containing `.obsidian/` is indexed |
| `VAULT_PATHS` | — | Explicit override: `name=/path,name=/path` |
| `VAULT_DEFAULT` | `Vault` | Vault used when a caller does not name one |
| `VAULT_INDEX_DB` | `$XDG_DATA_HOME/vault-search/index.db` | Index location. Keep it **outside** the vault, or Obsidian Sync will replicate a large binary to every machine |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama base URL |
| `VAULT_EMBED_MODEL` | `bge-m3` | Embedding model. Changing this needs a full reindex, and a schema rebuild if its dimensions differ from 1024 |

### Auto-detection paths

- **Windows** — `C:\Program Files\Obsidian\Obsidian.com`
- **macOS** — `/Applications/Obsidian.app/Contents/MacOS/obsidian-cli`
- **WSL2** — `/mnt/c/Program Files/Obsidian/Obsidian.com`
- **Linux** — `obsidian` on PATH (snap, AppImage, etc.)

Set `OBSIDIAN_CLI` if your install is in a non-standard location.
