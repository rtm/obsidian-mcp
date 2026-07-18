"""Bring the vault index in sync with disk. Entry point for the systemd timer.

Usage: reindex.py [vault-name] [-q]
Omitting the name reindexes every discovered vault.
"""

import sys
import time

import vault_index as vi


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    verbose = "-q" not in sys.argv
    target = args[0] if args else None
    started = time.monotonic()

    def progress(vault, rel, changed, chunks):
        if changed % 25 == 0:
            print(f"  [{vault}] {changed} files, {chunks} chunks… ({rel})", flush=True)

    try:
        stats = vi.reindex(vault=target, progress=progress if verbose else None)
    except Exception as e:  # noqa: BLE001 — timer context: report, don't traceback
        print(f"reindex failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started
    print(
        f"reindexed {', '.join(stats['vaults'])} in {elapsed:.1f}s — "
        f"{stats['files_changed']} changed, {stats['files_removed']} removed, "
        f"{stats['chunks_written']} chunks written, "
        f"{stats['chunks_total']} chunks total",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
