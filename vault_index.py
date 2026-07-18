"""
Semantic + keyword index over one or more Obsidian vaults.

Reads markdown straight from disk—deliberately independent of the running
Obsidian instance, so search keeps working when Obsidian doesn't. Chunks by
heading section, embeds via a local Ollama model, and stores vectors in
sqlite-vec alongside an FTS5 keyword index over the same chunks.

Multi-vault by construction: every row carries a `vault` name, and searches
scope to one vault or span all of them. Vaults are auto-discovered as any
directory under VAULT_ROOT containing a `.obsidian/` folder.

The index lives OUTSIDE the vaults (under XDG data dir). Putting it inside
would hand Obsidian Sync an ~86 MB binary to replicate to every machine.

Configure via environment variables:
  VAULT_ROOT        — where vaults live (default ~/wrk/obsidian)
  VAULT_PATHS       — explicit override, "name=/path,name=/path"
  VAULT_DEFAULT     — vault name used when a caller does not specify one
  VAULT_INDEX_DB    — index location (default ~/.local/share/vault-search/index.db)
  OLLAMA_HOST       — Ollama base URL (default http://127.0.0.1:11434)
  VAULT_EMBED_MODEL — embedding model (default bge-m3)
"""

import hashlib
import os
import re
import sqlite3
import struct
from pathlib import Path

import httpx
import sqlite_vec
import yaml

VAULT_ROOT = Path(os.environ.get("VAULT_ROOT", Path.home() / "wrk/obsidian"))
OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MODEL = os.environ.get("VAULT_EMBED_MODEL", "bge-m3")
DIMS = 1024  # bge-m3

_xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
DB_PATH = Path(os.environ.get("VAULT_INDEX_DB", _xdg / "vault-search/index.db"))

# Folders that are noise for retrieval: plugin internals, deleted notes, and
# saved Copilot chats (which would otherwise return our own past answers).
EXCLUDE_DIRS = {".obsidian", ".trash", ".vault-search", "copilot-conversations"}

MAX_CHARS = 1400   # chunk target; ~350 tokens, comfortably inside bge-m3's window
OVERLAP = 200      # carried between splits of an oversized section
MIN_CHARS = 40     # below this a chunk is boilerplate, not content

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
FTS_TOKEN = re.compile(r"[\w'぀-ヿ一-鿿]+", re.UNICODE)
URLISH = re.compile(r"\A(https?://|/|~/)")

# Frontmatter keys whose values are machine plumbing, not prose. Everything else
# gets indexed: for the book notes under Zettelkasten/Reference Notes the entire
# note IS frontmatter (title, author, full synopsis), so discarding it wholesale
# made ~400 notes invisible to search.
SKIP_KEYS = {
    "coverurl", "coversmallurl", "link", "previewlink", "localcoverimage",
    "cssclass", "cssclasses", "banner", "id", "uid", "totalpage", "publishdate",
}


# ---------------------------------------------------------------- vaults

def vaults() -> dict[str, Path]:
    """Discover indexable vaults, newest convention first.

    Explicit VAULT_PATHS wins; otherwise any directory under VAULT_ROOT with a
    `.obsidian/` folder counts. Adding a second vault therefore needs no code
    change—drop it beside the first and reindex.
    """
    explicit = os.environ.get("VAULT_PATHS", "").strip()
    if explicit:
        found = {}
        for entry in explicit.split(","):
            name, _, path = entry.partition("=")
            if name.strip() and path.strip():
                found[name.strip()] = Path(path.strip()).expanduser()
        return found
    if not VAULT_ROOT.is_dir():
        return {}
    return {
        d.name: d
        for d in sorted(VAULT_ROOT.iterdir())
        if d.is_dir() and (d / ".obsidian").is_dir()
    }


def default_vault() -> str | None:
    known = vaults()
    if not known:
        return None
    preferred = os.environ.get("VAULT_DEFAULT")
    if preferred and preferred in known:
        return preferred
    return "Vault" if "Vault" in known else next(iter(known))


def resolve(name: str | None) -> tuple[str, Path]:
    known = vaults()
    if not known:
        raise ValueError(f"No vaults found under {VAULT_ROOT}")
    name = name or default_vault()
    if name not in known:
        raise ValueError(f"Unknown vault {name!r}. Known: {', '.join(sorted(known))}")
    return name, known[name]


# ---------------------------------------------------------------- storage

def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(f"""
        CREATE TABLE IF NOT EXISTS files (
            vault TEXT NOT NULL,
            path  TEXT NOT NULL,
            hash  TEXT NOT NULL,
            PRIMARY KEY (vault, path)
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id      INTEGER PRIMARY KEY,
            vault   TEXT NOT NULL,
            path    TEXT NOT NULL,
            heading TEXT NOT NULL,
            text    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS chunks_loc ON chunks(vault, path);
        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
            chunk_id INTEGER PRIMARY KEY,
            embedding FLOAT[{DIMS}]
        );
        -- Keyword half of hybrid search. Contentless: `chunks` stays the single
        -- source of truth and FTS5 just points back into it by rowid.
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
            text, content='chunks', content_rowid='id'
        );
    """)
    return db


# ---------------------------------------------------------------- chunking

def iter_notes(vault_path: Path):
    for p in vault_path.rglob("*.md"):
        if EXCLUDE_DIRS & set(p.relative_to(vault_path).parts):
            continue
        yield p


def _flatten(value) -> str:
    """Render a YAML scalar/list/dict as searchable prose, dropping URLs."""
    if isinstance(value, str):
        return "" if URLISH.match(value.strip()) else value.strip()
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(p for p in (_flatten(v) for v in value) if p)
    if isinstance(value, dict):
        return ", ".join(p for p in (_flatten(v) for v in value.values()) if p)
    return ""


def frontmatter_text(raw: str) -> str:
    """Extract the human-meaningful part of a YAML frontmatter block."""
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        data = None
    if not isinstance(data, dict):
        # Malformed or exotic frontmatter: fall back to the raw lines rather
        # than losing the content entirely.
        return " ".join(
            ln.strip() for ln in raw.splitlines()
            if ln.strip() and not URLISH.match(ln.split(":", 1)[-1].strip())
        )
    parts = []
    for key, value in data.items():
        if str(key).lower() in SKIP_KEYS:
            continue
        rendered = _flatten(value)
        if rendered:
            parts.append(f"{key}: {rendered}")
    return "\n".join(parts)


def chunk_note(rel_path: str, body: str) -> list[tuple[str, str]]:
    """Split a note into (heading_path, text) pairs.

    Each chunk is prefixed with the note title and its heading breadcrumb. On a
    wiki-shaped vault this matters more than it looks: it gives an otherwise
    context-free fragment ("Restart=always, linger enabled") enough anchoring to
    match a query about, say, headless Obsidian on ginjo.
    """
    title = rel_path.rsplit("/", 1)[-1][:-3]
    meta = ""
    m = FRONTMATTER.match(body)
    if m:
        meta = frontmatter_text(m.group(1))
        body = body[m.end():]

    sections: list[tuple[list[str], list[str]]] = []
    stack: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        m = HEADING.match(line)
        if m:
            if current:
                sections.append((stack.copy(), current))
            depth = len(m.group(1))
            stack = stack[: depth - 1] + [m.group(2).strip()]
            current = []
        else:
            current.append(line)
    if current:
        sections.append((stack.copy(), current))

    out: list[tuple[str, str]] = []
    for crumbs, lines in sections:
        text = "\n".join(lines).strip()
        if len(text) < MIN_CHARS:
            continue
        heading = " > ".join([title] + crumbs)
        # Oversized sections get windowed with overlap so a fact spanning the
        # split boundary still lands whole in at least one chunk.
        start = 0
        while start < len(text):
            piece = text[start : start + MAX_CHARS]
            out.append((heading, f"{heading}\n\n{piece}"))
            if start + MAX_CHARS >= len(text):
                break
            start += MAX_CHARS - OVERLAP

    if meta:
        # Metadata leads: for book and reference notes it carries the title,
        # author, and synopsis that the body never repeats.
        for i in range(0, len(meta), MAX_CHARS):
            out.insert(i // MAX_CHARS,
                       (f"{title} (metadata)", f"{title}\n\n{meta[i : i + MAX_CHARS]}"))

    if not out:
        # Floor: a note with any content at all must be findable. Short stubs,
        # link-only notes, and MOCs would otherwise vanish from the index.
        remainder = body.strip()
        if remainder or meta:
            blob = "\n\n".join(p for p in (meta, remainder) if p)[:MAX_CHARS]
            out.append((title, f"{title}\n\n{blob}"))
    return out


# ---------------------------------------------------------------- embedding

def embed(texts: list[str], client: httpx.Client | None = None) -> list[list[float]]:
    own = client is None
    client = client or httpx.Client(timeout=300.0)
    try:
        r = client.post(f"{OLLAMA}/api/embed", json={"model": MODEL, "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA}. Is the service running "
            f"(`systemctl status ollama`)?"
        ) from e
    finally:
        if own:
            client.close()


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------- indexing

def reindex(vault: str | None = None, db: sqlite3.Connection | None = None,
            batch: int = 32, progress=None) -> dict:
    """Sync one vault (or all, when vault is None) with disk.

    Only files whose content hash changed are re-embedded, so routine runs are
    cheap and the systemd timer can fire often.
    """
    own = db is None
    db = db or connect()
    try:
        targets = [resolve(vault)] if vault else list(vaults().items())
        if not targets:
            raise ValueError(f"No vaults found under {VAULT_ROOT}")
        totals = {"files_changed": 0, "files_removed": 0, "chunks_written": 0}
        for name, path in targets:
            stats = _reindex_one(db, name, path, batch, progress)
            for k in totals:
                totals[k] += stats[k]
        totals["chunks_total"] = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        totals["vaults"] = [n for n, _ in targets]
        return totals
    finally:
        if own:
            db.close()


def _reindex_one(db, name: str, path: Path, batch: int, progress) -> dict:
    client = httpx.Client(timeout=300.0)
    try:
        known = dict(
            db.execute("SELECT path, hash FROM files WHERE vault=?", (name,)).fetchall()
        )
        seen, changed, added = set(), 0, 0

        for p in iter_notes(path):
            rel = str(p.relative_to(path))
            seen.add(rel)
            try:
                body = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            digest = hashlib.sha256(body.encode()).hexdigest()
            if known.get(rel) == digest:
                continue

            pieces = chunk_note(rel, body)
            _drop(db, name, rel)
            for i in range(0, len(pieces), batch):
                window = pieces[i : i + batch]
                vectors = embed([t for _, t in window], client)
                for (heading, text), vec in zip(window, vectors):
                    cur = db.execute(
                        "INSERT INTO chunks(vault, path, heading, text) VALUES (?,?,?,?)",
                        (name, rel, heading, text),
                    )
                    rowid = cur.lastrowid
                    db.execute(
                        "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?,?)",
                        (rowid, _pack(vec)),
                    )
                    db.execute(
                        "INSERT INTO fts_chunks(rowid, text) VALUES (?,?)",
                        (rowid, text),
                    )
            db.execute(
                "INSERT INTO files(vault, path, hash) VALUES (?,?,?) "
                "ON CONFLICT(vault, path) DO UPDATE SET hash=excluded.hash",
                (name, rel, digest),
            )
            db.commit()
            changed += 1
            added += len(pieces)
            if progress:
                progress(name, rel, changed, added)

        removed = [r for r in known if r not in seen]
        for rel in removed:
            _drop(db, name, rel)
            db.execute("DELETE FROM files WHERE vault=? AND path=?", (name, rel))
        db.commit()
        return {"files_changed": changed, "files_removed": len(removed),
                "chunks_written": added}
    finally:
        client.close()


def _drop(db: sqlite3.Connection, vault: str, rel: str) -> None:
    ids = [
        r[0] for r in
        db.execute("SELECT id FROM chunks WHERE vault=? AND path=?", (vault, rel))
    ]
    for cid in ids:
        db.execute("DELETE FROM vec_chunks WHERE chunk_id=?", (cid,))
        # Contentless FTS5 needs the old text echoed back to retract the row.
        db.execute(
            "INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES ('delete', ?, "
            "(SELECT text FROM chunks WHERE id=?))",
            (cid, cid),
        )
    db.execute("DELETE FROM chunks WHERE vault=? AND path=?", (vault, rel))


# ---------------------------------------------------------------- search

def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query.

    Quoting each token defuses FTS5's operator syntax—an apostrophe or a bare
    'AND' in a natural-language query would otherwise be a syntax error.
    """
    tokens = FTS_TOKEN.findall(query)
    return " OR ".join(f'"{t}"' for t in tokens)


def _row_filter(vault: str | None, folder: str | None):
    clauses, params = [], []
    if vault:
        clauses.append("c.vault = ?")
        params.append(vault)
    if folder:
        clauses.append("c.path LIKE ?")
        params.append(folder.rstrip("/") + "/%")
    return (" AND ".join(clauses), params)


def semantic(query: str, k: int = 8, vault: str | None = None,
             folder: str | None = None, db: sqlite3.Connection | None = None) -> list[dict]:
    own = db is None
    db = db or connect()
    try:
        vec = embed([query])[0]
        # Over-fetch: vec0 runs KNN before our predicates, so a narrow scope
        # would otherwise come back near-empty.
        limit = k * 20 if (vault or folder) else k
        where, params = _row_filter(vault, folder)
        sql = ("SELECT c.vault, c.path, c.heading, c.text, v.distance "
               "FROM vec_chunks v JOIN chunks c ON c.id = v.chunk_id "
               "WHERE v.embedding MATCH ? AND k = ?")
        if where:
            sql += f" AND {where}"
        sql += " ORDER BY v.distance"
        rows = db.execute(sql, (_pack(vec), limit, *params)).fetchall()
        return [
            {"vault": vt, "path": p, "heading": h, "text": t,
             "score": round(1 / (1 + d), 4)}
            for vt, p, h, t, d in rows
        ][:k]
    finally:
        if own:
            db.close()


RRF_K = 60  # reciprocal-rank-fusion damping; 60 is the standard default


def hybrid(query: str, k: int = 8, vault: str | None = None,
           folder: str | None = None, db: sqlite3.Connection | None = None) -> list[dict]:
    """Fuse semantic and keyword rankings via reciprocal rank fusion.

    The recommended entry point: BM25 carries exact tokens (sigil filenames,
    dates, proper nouns) that embeddings blur, while the vectors carry the
    paraphrases BM25 cannot see.
    """
    own = db is None
    db = db or connect()
    try:
        sem = semantic(query, k * 3, vault, folder, db)
        kw = keyword(query, k * 3, vault, folder, db)
        scores: dict[tuple, float] = {}
        best: dict[tuple, dict] = {}
        for ranking in (sem, kw):
            for rank, hit in enumerate(ranking):
                key = (hit["vault"], hit["path"], hit["heading"])
                scores[key] = scores.get(key, 0) + 1 / (RRF_K + rank)
                best.setdefault(key, hit)

        fused, seen = [], set()
        for key in sorted(scores, key=lambda k_: scores[k_], reverse=True):
            note = (key[0], key[1])
            # One chunk per note: several chunks of one long note crowding out
            # everything else is the classic failure mode of chunked retrieval.
            if note in seen:
                continue
            seen.add(note)
            fused.append({**best[key], "score": round(scores[key], 5)})
            if len(fused) == k:
                break
        return fused
    finally:
        if own:
            db.close()


def keyword(query: str, k: int = 8, vault: str | None = None,
            folder: str | None = None, db: sqlite3.Connection | None = None) -> list[dict]:
    """BM25-ranked keyword search over the same chunks the vectors cover."""
    own = db is None
    db = db or connect()
    try:
        match = _fts_query(query)
        if not match:
            return []
        where, params = _row_filter(vault, folder)
        sql = ("SELECT c.vault, c.path, c.heading, c.text, bm25(fts_chunks) AS rank "
               "FROM fts_chunks JOIN chunks c ON c.id = fts_chunks.rowid "
               "WHERE fts_chunks MATCH ?")
        if where:
            sql += f" AND {where}"
        sql += " ORDER BY rank LIMIT ?"
        rows = db.execute(sql, (match, *params, k)).fetchall()
        return [
            {"vault": vt, "path": p, "heading": h, "text": t, "score": round(-r, 4)}
            for vt, p, h, t, r in rows
        ]
    finally:
        if own:
            db.close()
