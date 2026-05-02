"""Entry point: discover session, build snapshots, drive the TUI."""

from __future__ import annotations

import argparse
import io
import sys
import threading
from pathlib import Path

# Ensure UTF-8 stdio on Windows so block characters render.
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, io.UnsupportedOperation):
        pass

from .parser import build_snapshot
from .session import (
    discover_session,
    read_all,
    session_cwd,
    session_version,
    tail_jsonl,
)
from .view import Runner


def _session_branch(events: list[dict]) -> str | None:
    for e in events:
        b = e.get("gitBranch")
        if b and b != "HEAD":
            return b
    return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="cc-monitor",
        description="Sidecar consumption attribution viewer for Claude Code sessions.",
    )
    p.add_argument(
        "--session",
        help="Attach to a specific session id (full or short prefix).",
        default=None,
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    ref = discover_session(args.session)

    # Initial load.
    events: list[dict] = read_all(ref.jsonl_path)
    cwd = session_cwd(events)
    cc_version = session_version(events)

    # Shared state — tail thread appends, snapshot builder reads.
    lock = threading.Lock()

    def append_event(e: dict) -> None:
        with lock:
            events.append(e)

    def snapshot_provider():
        with lock:
            evs = list(events)
        nonlocal cwd, cc_version
        if cwd is None:
            cwd = session_cwd(evs)
        if cc_version is None:
            cc_version = session_version(evs)
        branch = ref.git_branch or _session_branch(evs)
        return build_snapshot(
            evs,
            project_name=ref.project_name,
            short_id=ref.short_id,
            project_dir=ref.project_dir,
            cwd=cwd,
            cc_version=cc_version,
            git_branch=branch,
            ai_title=ref.ai_title,
        )

    # Launch the tail thread. We skip events already loaded by seeking by count.
    initial_count = len(events)

    def tail_loop():
        seen = 0
        for e in tail_jsonl(ref.jsonl_path):
            seen += 1
            if seen <= initial_count:
                continue
            append_event(e)

    threading.Thread(target=tail_loop, daemon=True).start()

    runner = Runner(snapshot_provider)
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
