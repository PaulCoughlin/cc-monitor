"""JSONL session discovery and tailing.

Reads ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl in append-only fashion.
Never writes. Polls via os.stat so we can drop watchdog as a dependency.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
RECENT_WINDOW_SECONDS = 5 * 60


@dataclass
class SessionRef:
    project_dir: Path
    jsonl_path: Path
    session_id: str
    project_name: str
    git_branch: str | None = None
    first_prompt: str | None = None
    ai_title: str | None = None

    @property
    def short_id(self) -> str:
        return self.session_id.split("-")[0]


def _peek_session_meta(jsonl: Path) -> tuple[str | None, str | None, str | None, str | None]:
    """Grab (cwd, gitBranch, first-user-prompt, ai-title) without reading the whole file.

    Reads up to ~64KB — enough to cover startup attachments, first user message,
    and the auto-generated session title in any normal session.
    """
    cwd = None
    branch = None
    first_prompt = None
    ai_title = None
    try:
        with jsonl.open("rb") as f:
            blob = f.read(65536).decode("utf-8", errors="replace")
    except OSError:
        return None, None, None, None
    for line in blob.split("\n"):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if cwd is None and o.get("cwd"):
            cwd = o["cwd"]
        if branch is None:
            b = o.get("gitBranch")
            if b and b != "HEAD":
                branch = b
        if first_prompt is None and o.get("type") == "user":
            m = o.get("message", {}) or {}
            c = m.get("content")
            if isinstance(c, str):
                first_prompt = c.strip()
            elif isinstance(c, list) and c and isinstance(c[0], dict):
                if c[0].get("type") == "text":
                    first_prompt = (c[0].get("text") or "").strip()
        if ai_title is None and o.get("type") == "ai-title":
            t = o.get("aiTitle")
            if isinstance(t, str) and t.strip():
                ai_title = t.strip()
        if cwd and branch and first_prompt and ai_title:
            break
    return cwd, branch, first_prompt, ai_title


def _project_label(cwd: str | None, encoded_dirname: str) -> str:
    """Human-friendly project label. Prefer cwd basename over encoded dirname."""
    if cwd:
        # Handle both win and posix paths.
        cleaned = cwd.replace("\\", "/").rstrip("/")
        base = cleaned.rsplit("/", 1)[-1]
        if base:
            return base
    # Fall back: strip drive prefix like 'D---' and return last path segment.
    parts = [p for p in encoded_dirname.split("-") if p]
    return parts[-1] if parts else encoded_dirname


def _scan_jsonls() -> list[SessionRef]:
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    out: list[SessionRef] = []
    for proj in CLAUDE_PROJECTS_DIR.iterdir():
        if not proj.is_dir():
            continue
        for jsonl in proj.glob("*.jsonl"):
            cwd, branch, prompt, ai_title = _peek_session_meta(jsonl)
            out.append(
                SessionRef(
                    project_dir=proj,
                    jsonl_path=jsonl,
                    session_id=jsonl.stem,
                    project_name=_project_label(cwd, proj.name),
                    git_branch=branch,
                    first_prompt=prompt,
                    ai_title=ai_title,
                )
            )
    return out


def _live_session_ids() -> set[str]:
    """SessionIds of currently-running CC processes, per ~/.claude/sessions/*.json."""
    out: set[str] = set()
    if not CLAUDE_SESSIONS_DIR.exists():
        return out
    for f in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sid = d.get("sessionId")
        if sid:
            out.add(sid)
    return out


def discover_session(session_arg: str | None) -> SessionRef:
    """Choose which session to attach to.

    Modes (in order of precedence):
      - explicit session id via --session
      - auto-attach to single live (or recently-modified) session
      - picker if more than one
      - fall back to most recent JSONL if nothing else
    """
    refs = _scan_jsonls()
    if not refs:
        print("No Claude Code sessions found under ~/.claude/projects/", file=sys.stderr)
        sys.exit(1)

    if session_arg:
        match = next(
            (r for r in refs if r.session_id == session_arg or r.short_id == session_arg),
            None,
        )
        if not match:
            print(f"No session matching '{session_arg}'", file=sys.stderr)
            sys.exit(1)
        return match

    now = time.time()
    live_ids = _live_session_ids()

    # Active = live process OR JSONL written within recent window.
    active = [
        r for r in refs
        if r.session_id in live_ids
        or (now - r.jsonl_path.stat().st_mtime <= RECENT_WINDOW_SECONDS)
    ]
    if len(active) == 0:
        refs.sort(key=lambda r: r.jsonl_path.stat().st_mtime, reverse=True)
        return refs[0]
    if len(active) == 1:
        return active[0]
    return _picker(active, now)


def _picker(refs: list[SessionRef], now: float) -> SessionRef:
    refs = sorted(refs, key=lambda r: r.jsonl_path.stat().st_mtime, reverse=True)
    live = _live_session_ids()
    print("Multiple active sessions detected:")
    print()
    for i, r in enumerate(refs, 1):
        age = _format_age(int(now - r.jsonl_path.stat().st_mtime))
        branch = f" [{r.git_branch}]" if r.git_branch else ""
        live_badge = " [live]" if r.session_id in live else ""
        title = f'  ·  "{r.ai_title}"' if r.ai_title else ""
        print(f"  {i}. {r.project_name}{branch}  ·  {r.short_id}  ·  {age}{live_badge}{title}")
        if not r.ai_title and r.first_prompt:
            snip = " ".join(r.first_prompt.split())
            if len(snip) > 60:
                snip = snip[:59] + "…"
            print(f'     "{snip}"')
    print()
    while True:
        try:
            sel = input("Select: ").strip()
            idx = int(sel) - 1
            if 0 <= idx < len(refs):
                return refs[idx]
            print("Invalid selection.")
        except ValueError:
            print("Enter a number.")
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def tail_jsonl(path: Path, poll_interval: float = 0.4):
    """Yield each parsed JSONL line; block & poll for new lines as the file grows.

    Reads in binary mode so byte offsets are reliable on Windows (text mode's
    `tell()` returns opaque integers that can't be compared to `st_size`).
    Tolerates partial writes via the `pending` buffer.
    """
    pos = 0
    pending = b""
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(poll_interval)
            continue
        if size < pos:
            # File truncated/rotated — start over.
            pos = 0
            pending = b""
        if size > pos:
            with path.open("rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            buf = pending + chunk
            lines = buf.split(b"\n")
            pending = lines.pop()
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        time.sleep(poll_interval)


def read_all(path: Path) -> list[dict]:
    """Read entire JSONL once (used at attach time before tailing)."""
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def session_cwd(events: list[dict]) -> Path | None:
    """First non-empty `cwd` we see — used to find the project's CLAUDE.md."""
    for e in events:
        cwd = e.get("cwd")
        if cwd:
            return Path(cwd)
    return None


def session_version(events: list[dict]) -> str | None:
    for e in events:
        v = e.get("version")
        if v:
            return v
    return None
