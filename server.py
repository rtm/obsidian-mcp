"""
Minimal MCP server wrapping the Obsidian CLI.

Works from Windows, macOS, and Linux/WSL2.
Exposes a curated set of tools — no eval, no dev commands, no shell injection surface.

Configure via environment variables:
  OBSIDIAN_CLI  — path to the CLI binary (auto-detected if omitted)
  OBSIDIAN_VAULT — vault name for multi-vault setups (optional)
"""

import asyncio
import os
import platform
import shutil
from mcp.server.fastmcp import FastMCP

import vault_index

def _transport_security():
    """Keep FastMCP's DNS-rebinding protection ON, but allow the hostnames we
    actually serve (the public tunnel host + loopback). Env-driven so the
    hostname isn't baked into source. Returns None when unset → FastMCP default
    (loopback only), which is correct for plain local/stdio use."""
    hosts = os.environ.get("OBSIDIAN_MCP_ALLOWED_HOSTS")
    if not hosts:
        return None
    from mcp.server.transport_security import TransportSecuritySettings
    origins = os.environ.get("OBSIDIAN_MCP_ALLOWED_ORIGINS", "")
    return TransportSecuritySettings(
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        allowed_origins=[o.strip() for o in origins.split(",") if o.strip()],
    )


mcp = FastMCP(
    "obsidian",
    # Server-level guidance sent to the client in the MCP `initialize` handshake.
    # One line on purpose: the vault stays the single source of truth. The named
    # note gathers the conventions and links to the rest.
    instructions=(
        'Before working with this Obsidian vault in any way—searching, reading, '
        'creating, or editing—first read the note "Instructions to the Chef" '
        '(read_note path="meta/Instructions to the Chef.md") and follow it. '
        'To find notes, prefer `hybrid_search`: it fuses keyword and semantic '
        'ranking and beats the plain `search` tool for anything but an exact '
        'string match.'
    ),
    # Only used when served over HTTP (OBSIDIAN_MCP_TRANSPORT=streamable-http).
    # Default binds to loopback — the public path is a tunnel in front, never a
    # direct listen on a routable interface.
    host=os.environ.get("OBSIDIAN_MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("OBSIDIAN_MCP_PORT", "8788")),
    transport_security=_transport_security(),
)


def _find_cli() -> str:
    """Locate the Obsidian CLI binary."""
    # Explicit override
    env = os.environ.get("OBSIDIAN_CLI")
    if env:
        return env

    system = platform.system()

    if system == "Windows":
        candidate = r"C:\Program Files\Obsidian\Obsidian.com"
        if os.path.isfile(candidate):
            return candidate

    elif system == "Darwin":
        # macOS: CLI ships inside the .app bundle
        candidate = "/Applications/Obsidian.app/Contents/MacOS/obsidian-cli"
        if os.path.isfile(candidate):
            return candidate

    else:
        # Linux — check for WSL2 first (Windows Obsidian via /mnt/c/)
        wsl_candidate = "/mnt/c/Program Files/Obsidian/Obsidian.com"
        if os.path.isfile(wsl_candidate):
            return wsl_candidate
        # Native Linux (snap, AppImage, etc.)
        found = shutil.which("obsidian")
        if found:
            return found

    # Last resort: hope it's on PATH
    found = shutil.which("obsidian") or shutil.which("obsidian-cli")
    if found:
        return found

    raise FileNotFoundError(
        "Could not find the Obsidian CLI. Set OBSIDIAN_CLI to the path of the binary."
    )


OBSIDIAN = _find_cli()
VAULT = os.environ.get("OBSIDIAN_VAULT")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _run(*args: str) -> str:
    """Run the Obsidian CLI with the given arguments and return stdout."""
    cmd = [OBSIDIAN]
    if VAULT:
        cmd.append(f"vault={VAULT}")
    cmd.extend(args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    text = stdout.decode()
    if proc.returncode != 0:
        err = stderr.decode().strip() or text.strip()
        raise RuntimeError(f"obsidian CLI error (exit {proc.returncode}): {err}")
    return text


def _file_args(file: str | None, path: str | None) -> list[str]:
    """Build file=/path= arguments, raising if neither is given."""
    args: list[str] = []
    if file:
        args.append(f"file={file}")
    if path:
        args.append(f"path={path}")
    return args


def _optional(name: str, value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bool):
        return [name] if value else []
    return [f"{name}={value}"]


# ---------------------------------------------------------------------------
# Vault info
# ---------------------------------------------------------------------------

@mcp.tool()
async def vault_info() -> str:
    """Show current vault name, path, file/folder counts, and size."""
    return await _run("vault")


@mcp.tool()
async def list_vaults() -> str:
    """List all known vaults with paths."""
    return await _run("vaults", "verbose")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

@mcp.tool()
async def read_note(file: str | None = None, path: str | None = None) -> str:
    """Read the contents of a note. Specify file (wikilink name) or path (exact)."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    return await _run("read", *fa)


@mcp.tool()
async def file_info(file: str | None = None, path: str | None = None) -> str:
    """Show metadata for a file (size, dates, links, tags)."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    return await _run("file", *fa)


@mcp.tool()
async def outline(
    file: str | None = None,
    path: str | None = None,
    format: str = "tree",
) -> str:
    """Show headings/outline for a note."""
    fa = _file_args(file, path)
    return await _run("outline", *fa, f"format={format}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool()
async def search(
    query: str,
    path: str | None = None,
    limit: int | None = None,
    case_sensitive: bool = False,
) -> str:
    """Full-text search across the vault. Returns matching files with context."""
    args = ["search:context", f"query={query}", "format=json"]
    args += _optional("path", path)
    args += _optional("limit", limit)
    if case_sensitive:
        args.append("case")
    return await _run(*args)


# ---------------------------------------------------------------------------
# Writing / modifying
# ---------------------------------------------------------------------------

@mcp.tool()
async def create_note(
    name: str | None = None,
    path: str | None = None,
    content: str | None = None,
    template: str | None = None,
    overwrite: bool = False,
) -> str:
    """Create a new note. Provide name or path. Optionally set content or use a template."""
    args = ["create"]
    args += _optional("name", name)
    args += _optional("path", path)
    args += _optional("content", content)
    args += _optional("template", template)
    if overwrite:
        args.append("overwrite")
    return await _run(*args)


@mcp.tool()
async def append_to_note(
    content: str,
    file: str | None = None,
    path: str | None = None,
    inline: bool = False,
) -> str:
    """Append content to an existing note."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    args = ["append", *fa, f"content={content}"]
    if inline:
        args.append("inline")
    return await _run(*args)


@mcp.tool()
async def prepend_to_note(
    content: str,
    file: str | None = None,
    path: str | None = None,
    inline: bool = False,
) -> str:
    """Prepend content to an existing note."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    args = ["prepend", *fa, f"content={content}"]
    if inline:
        args.append("inline")
    return await _run(*args)


@mcp.tool()
async def set_property(
    name: str,
    value: str,
    file: str | None = None,
    path: str | None = None,
    type: str | None = None,
) -> str:
    """Set a frontmatter property on a note."""
    fa = _file_args(file, path)
    args = ["property:set", f"name={name}", f"value={value}", *fa]
    args += _optional("type", type)
    return await _run(*args)


@mcp.tool()
async def read_property(
    name: str,
    file: str | None = None,
    path: str | None = None,
) -> str:
    """Read a frontmatter property value from a note."""
    fa = _file_args(file, path)
    return await _run("property:read", f"name={name}", *fa)


@mcp.tool()
async def remove_property(
    name: str,
    file: str | None = None,
    path: str | None = None,
) -> str:
    """Remove a frontmatter property from a note."""
    fa = _file_args(file, path)
    return await _run("property:remove", f"name={name}", *fa)


# ---------------------------------------------------------------------------
# Daily notes
# ---------------------------------------------------------------------------

@mcp.tool()
async def daily_read() -> str:
    """Read today's daily note."""
    return await _run("daily:read")


@mcp.tool()
async def daily_append(content: str, inline: bool = False) -> str:
    """Append content to today's daily note."""
    args = ["daily:append", f"content={content}"]
    if inline:
        args.append("inline")
    return await _run(*args)


@mcp.tool()
async def daily_prepend(content: str, inline: bool = False) -> str:
    """Prepend content to today's daily note."""
    args = ["daily:prepend", f"content={content}"]
    if inline:
        args.append("inline")
    return await _run(*args)


@mcp.tool()
async def daily_path() -> str:
    """Get the file path of today's daily note."""
    return await _run("daily:path")


# ---------------------------------------------------------------------------
# File management
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_files(
    folder: str | None = None,
    ext: str | None = None,
) -> str:
    """List files in the vault, optionally filtered by folder or extension."""
    args = ["files"]
    args += _optional("folder", folder)
    args += _optional("ext", ext)
    return await _run(*args)


@mcp.tool()
async def list_folders(folder: str | None = None) -> str:
    """List folders in the vault."""
    args = ["folders"]
    args += _optional("folder", folder)
    return await _run(*args)


@mcp.tool()
async def move_file(
    to: str,
    file: str | None = None,
    path: str | None = None,
) -> str:
    """Move or rename a file. 'to' is the destination folder or full path."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    return await _run("move", *fa, f"to={to}")


@mcp.tool()
async def rename_file(
    name: str,
    file: str | None = None,
    path: str | None = None,
) -> str:
    """Rename a file (updates all links)."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    return await _run("rename", *fa, f"name={name}")


@mcp.tool()
async def delete_file(
    file: str | None = None,
    path: str | None = None,
    permanent: bool = False,
) -> str:
    """Delete a file (moves to trash by default)."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    args = ["delete", *fa]
    if permanent:
        args.append("permanent")
    return await _run(*args)


# ---------------------------------------------------------------------------
# Tags & links
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_tags(
    file: str | None = None,
    path: str | None = None,
    counts: bool = False,
    sort: str | None = None,
) -> str:
    """List tags in the vault or for a specific file."""
    args = ["tags", "format=json"]
    args += _file_args(file, path)
    if counts:
        args.append("counts")
    args += _optional("sort", sort)
    return await _run(*args)


@mcp.tool()
async def backlinks(
    file: str | None = None,
    path: str | None = None,
) -> str:
    """List files that link to a given note."""
    fa = _file_args(file, path)
    if not fa:
        raise ValueError("Provide file= or path=")
    return await _run("backlinks", *fa, "counts", "format=json")


@mcp.tool()
async def orphans() -> str:
    """List notes with no incoming links."""
    return await _run("orphans")


@mcp.tool()
async def unresolved_links() -> str:
    """List wikilinks that don't resolve to any file."""
    return await _run("unresolved", "verbose", "format=json")


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_tasks(
    file: str | None = None,
    path: str | None = None,
    done: bool | None = None,
    daily: bool = False,
) -> str:
    """List tasks in the vault. Filter by file, completion status, or daily note."""
    args = ["tasks", "format=json"]
    args += _file_args(file, path)
    if done is True:
        args.append("done")
    elif done is False:
        args.append("todo")
    if daily:
        args.append("daily")
    return await _run(*args)


@mcp.tool()
async def toggle_task(
    file: str | None = None,
    path: str | None = None,
    line: int | None = None,
) -> str:
    """Toggle a task's completion status."""
    fa = _file_args(file, path)
    args = ["task", *fa, "toggle"]
    args += _optional("line", line)
    return await _run(*args)


# ---------------------------------------------------------------------------
# Templates & bookmarks
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_templates() -> str:
    """List available templates."""
    return await _run("templates")


@mcp.tool()
async def read_template(name: str, resolve: bool = False, title: str | None = None) -> str:
    """Read a template's content, optionally resolving variables."""
    args = ["template:read", f"name={name}"]
    if resolve:
        args.append("resolve")
    args += _optional("title", title)
    return await _run(*args)


@mcp.tool()
async def list_bookmarks() -> str:
    """List bookmarks."""
    return await _run("bookmarks", "verbose", "format=json")


# ---------------------------------------------------------------------------
# Properties (vault-wide)
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_properties(
    file: str | None = None,
    path: str | None = None,
    counts: bool = False,
) -> str:
    """List all frontmatter properties used across the vault (or for a file)."""
    args = ["properties", "format=json"]
    args += _file_args(file, path)
    if counts:
        args.append("counts")
    return await _run(*args)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_commands(filter: str | None = None) -> str:
    """List available Obsidian commands (for use with run_command)."""
    args = ["commands"]
    args += _optional("filter", filter)
    return await _run(*args)


@mcp.tool()
async def run_command(id: str) -> str:
    """Execute an Obsidian command by its ID (e.g. 'editor:toggle-bold')."""
    return await _run("command", f"id={id}")


@mcp.tool()
async def list_plugins(filter: str | None = None) -> str:
    """List installed plugins."""
    args = ["plugins", "versions", "format=json"]
    args += _optional("filter", filter)
    return await _run(*args)


@mcp.tool()
async def sync_status() -> str:
    """Show Obsidian Sync status."""
    return await _run("sync:status")


@mcp.tool()
async def version() -> str:
    """Show Obsidian version."""
    return await _run("version")


# ---------------------------------------------------------------------------
# Tool annotations — hint which tools only read vs. modify the vault, so clients
# can auto-approve reads and prompt only on writes. Hints, not enforcement; the
# client decides whether to honor them.
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS = {
    "vault_info", "list_vaults", "read_note", "file_info", "outline", "search",
    "read_property", "daily_read", "daily_path", "list_files", "list_folders",
    "list_tags", "backlinks", "orphans", "unresolved_links", "list_tasks",
    "list_templates", "read_template", "list_bookmarks", "list_properties",
    "list_commands", "list_plugins", "sync_status", "version",
}
_DESTRUCTIVE_TOOLS = {"delete_file", "remove_property"}


def _annotate_tools():
    """Attach read-only / destructive hints to every registered tool. Uses the
    tool-manager registry; guarded so a FastMCP internals change degrades to
    'no hints' rather than breaking startup."""
    from mcp.types import ToolAnnotations
    try:
        registry = mcp._tool_manager._tools
    except AttributeError:
        return
    for name, tool in registry.items():
        if name in _READ_ONLY_TOOLS:
            tool.annotations = ToolAnnotations(readOnlyHint=True)
        else:
            tool.annotations = ToolAnnotations(
                readOnlyHint=False,
                destructiveHint=name in _DESTRUCTIVE_TOOLS,
            )


_annotate_tools()


# ---------------------------------------------------------------------------
# Auth (HTTP transport only)
# ---------------------------------------------------------------------------

class _AccessVerifier:
    """Verify a Cloudflare Access 'Cf-Access-Jwt-Assertion' JWT — signature
    (RS256, keys from the team's JWKS), issuer, audience (the app's AUD tag),
    and expiry. This is how requests arriving via Cloudflare Access (claude.ai
    web / Cowork, which authenticate through Access Managed OAuth) are trusted."""

    def __init__(self, team_domain: str, aud: str):
        self.issuer = f"https://{team_domain}"
        self.aud = aud
        self._jwks = None  # lazily-built jwt.PyJWKClient (does network I/O)

    def _client(self):
        if self._jwks is None:
            import jwt
            self._jwks = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs")
        return self._jwks

    def verify(self, token: str) -> bool:
        import jwt
        try:
            key = self._client().get_signing_key_from_jwt(token).key
            jwt.decode(token, key, algorithms=["RS256"], audience=self.aud,
                       issuer=self.issuer)
            return True
        except Exception:
            return False


class _AuthASGI:
    """Pure-ASGI gate (not BaseHTTPMiddleware, so it never buffers the SSE
    streaming responses the MCP transport relies on). A request is allowed if
    EITHER a valid bearer token (local/LAN path) OR a valid Cloudflare Access
    assertion (the claude.ai path) is present; otherwise 401."""

    def __init__(self, app, token=None, access: "_AccessVerifier | None" = None):
        self.app = app
        self._expected = f"Bearer {token}".encode() if token else None
        self.access = access

    async def _ok(self, headers: dict) -> bool:
        if self._expected is not None:
            import hmac
            if hmac.compare_digest(headers.get(b"authorization", b""),
                                   self._expected):
                return True
        if self.access is not None:
            tok = headers.get(b"cf-access-jwt-assertion", b"").decode()
            if tok:
                import asyncio
                if await asyncio.to_thread(self.access.verify, tok):
                    return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            if not await self._ok(headers):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error":"unauthorized"}',
                })
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Semantic / hybrid search
#
# Backed by a local sqlite-vec index built from Ollama embeddings, kept current
# by the vault-search-reindex.timer user unit. These tools read that database
# directly and never shell out to the Obsidian CLI, so they keep answering when
# headless Obsidian is down—which is why this unit only Wants= obsidian.service
# rather than Requires= it.
# ---------------------------------------------------------------------------

RRF_K = 60  # reciprocal-rank-fusion damping; 60 is the standard default


def _search_key(hit: dict) -> tuple:
    return (hit["vault"], hit["path"], hit["heading"])


def _fmt_hits(hits: list[dict], limit_chars: int = 600) -> str:
    if not hits:
        return "No matches."
    multi = len({h["vault"] for h in hits}) > 1
    out = []
    for i, h in enumerate(hits, 1):
        text = h["text"]
        if len(text) > limit_chars:
            text = text[:limit_chars] + "…"
        loc = f"{h['vault']}:{h['path']}" if multi else h["path"]
        out.append(f"[{i}] {loc}  (score {h['score']})\n{h['heading']}\n{text}")
    return "\n\n---\n\n".join(out)


def _search_scope(vault: str | None, all_vaults: bool) -> str | None:
    """None means 'every vault'; otherwise a validated vault name."""
    return None if all_vaults else vault_index.resolve(vault)[0]


@mcp.tool()
async def hybrid_search(query: str, k: int = 8, vault: str | None = None,
                        folder: str | None = None, all_vaults: bool = False) -> str:
    """Search the vault by both keyword and meaning, fused into one ranking.

    The recommended way to find notes. Combines BM25 keyword precision with
    embedding recall, so it works whether or not you know the exact wording.

    Args:
        query: What you are looking for.
        k: Number of results (default 8).
        folder: Optional vault-relative folder, e.g. "writing/nihongoism".
        vault: Vault name; omit for the default vault.
        all_vaults: Search every vault instead of one.
    """
    scope = _search_scope(vault, all_vaults)
    sem, kw = await asyncio.gather(
        asyncio.to_thread(vault_index.semantic, query, k * 3, scope, folder),
        asyncio.to_thread(vault_index.keyword, query, k * 3, scope, folder),
    )
    scores: dict[tuple, float] = {}
    best: dict[tuple, dict] = {}
    for ranking in (sem, kw):
        for rank, hit in enumerate(ranking):
            key = _search_key(hit)
            scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank)
            best.setdefault(key, hit)

    fused, seen_notes = [], set()
    for key in sorted(scores, key=lambda k_: scores[k_], reverse=True):
        note = (key[0], key[1])
        # One chunk per note: several chunks of one long note crowding out
        # everything else is the classic failure mode of chunked retrieval.
        if note in seen_notes:
            continue
        seen_notes.add(note)
        fused.append({**best[key], "score": round(scores[key], 5)})
        if len(fused) == k:
            break
    return _fmt_hits(fused)


@mcp.tool()
async def semantic_search(query: str, k: int = 8, vault: str | None = None,
                          folder: str | None = None, all_vaults: bool = False) -> str:
    """Find notes by meaning rather than exact wording.

    Use when you know the concept but not the vocabulary the note uses. For
    exact strings—sigil filenames, dates, identifiers—prefer `search`.

    Args:
        query: What you are looking for, in natural language.
        k: Number of results (default 8).
        folder: Optional vault-relative folder to restrict to.
        vault: Vault name; omit for the default vault.
        all_vaults: Search every vault instead of one.
    """
    scope = _search_scope(vault, all_vaults)
    hits = await asyncio.to_thread(vault_index.semantic, query, k, scope, folder)
    return _fmt_hits(hits)


@mcp.tool()
async def reindex_vault(vault: str | None = None, all_vaults: bool = False) -> str:
    """Bring the search index up to date now rather than waiting for the timer.

    Only changed notes are re-embedded, so this is fast when little has changed.
    Call it when a note was just written and a search for it comes up empty.

    Args:
        vault: Vault to refresh; omit for the default vault.
        all_vaults: Refresh every vault instead of one.
    """
    scope = None if all_vaults else vault_index.resolve(vault)[0]
    try:
        stats = await asyncio.to_thread(vault_index.reindex, scope)
    except RuntimeError as e:
        return str(e)
    return (
        f"Reindexed {', '.join(stats['vaults'])}: {stats['files_changed']} changed, "
        f"{stats['files_removed']} removed, {stats['chunks_written']} chunks written, "
        f"{stats['chunks_total']} chunks total."
    )


@mcp.tool()
async def index_status() -> str:
    """Report search-index freshness: notes and chunks indexed per vault."""
    def _stat():
        known = vault_index.vaults()
        db = vault_index.connect()
        try:
            files = dict(db.execute(
                "SELECT vault, COUNT(*) FROM files GROUP BY vault").fetchall())
            chunks = dict(db.execute(
                "SELECT vault, COUNT(*) FROM chunks GROUP BY vault").fetchall())
        finally:
            db.close()
        disk = {n: sum(1 for _ in vault_index.iter_notes(p)) for n, p in known.items()}
        return files, chunks, disk

    files, chunks, disk = await asyncio.to_thread(_stat)
    db_mb = (round(vault_index.DB_PATH.stat().st_size / 1e6, 1)
             if vault_index.DB_PATH.exists() else 0)
    lines = [
        f"  {name}: {files.get(name, 0)} indexed / {n} on disk, "
        f"{chunks.get(name, 0)} chunks"
        for name, n in sorted(disk.items())
    ]
    return ("Vaults:\n" + "\n".join(lines) +
            f"\n\nIndex size: {db_mb} MB\nModel: {vault_index.MODEL}\n"
            f"Database: {vault_index.DB_PATH}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Default stdio keeps the local Claude Code integration working unchanged.
    # Set OBSIDIAN_MCP_TRANSPORT=streamable-http to run as a persistent networked
    # service (behind a tunnel) for claude.ai web / Cowork.
    transport = os.environ.get("OBSIDIAN_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run()
    elif transport == "streamable-http":
        token = os.environ.get("OBSIDIAN_MCP_TOKEN")
        team = os.environ.get("OBSIDIAN_MCP_ACCESS_TEAM_DOMAIN")
        aud = os.environ.get("OBSIDIAN_MCP_ACCESS_AUD")
        access = _AccessVerifier(team, aud) if team and aud else None
        if not token and access is None:
            # No auth configured — bare HTTP, fine only for loopback-local testing.
            mcp.run(transport="streamable-http")
        else:
            import uvicorn
            app = _AuthASGI(mcp.streamable_http_app(), token=token, access=access)
            uvicorn.run(
                app,
                host=os.environ.get("OBSIDIAN_MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("OBSIDIAN_MCP_PORT", "8788")),
                log_level="warning",
            )
    else:
        mcp.run(transport=transport)
