"""Categorise JSONL events + auxiliary startup files into named sources.

Output unit: characters. The tool never converts to tokens; percentages over the
sum of all visible content are exact and proportionally accurate.

Categorisation
--------------
Three groups are collapsible (per spec): MCP tool schemas / Files read / Tool
results retained. Other categories are flat lines.

Where each line of attribution comes from:

  Startup (read once at attach):
    - User CLAUDE.md             (~/.claude/CLAUDE.md)
    - Project CLAUDE.md          (<cwd>/CLAUDE.md)
    - Memory files               (~/.claude/projects/<encoded>/memory/*.md)

  JSONL (streaming):
    - Hook outputs               (attachment.type == hook_success / hook_additional_context)
    - Skill listing              (attachment.type == skill_listing)
    - Deferred tools list        (attachment.type == deferred_tools_delta)
    - MCP instructions           (attachment.type == mcp_instructions_delta)
    - Other system injections    (todo_reminder, edited_text_file, etc.)
    - Files read                 (Read tool_use → tool_result, per file_path)
    - Tool results retained      (every tool_result, grouped by tool name)
    - Skills invoked             (Skill tool_use)
    - Sub-agents                 (Agent tool_use, aggregate)
    - User messages              (user.message text)
    - Assistant messages         (assistant.message text + thinking)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# --- Data model ------------------------------------------------------------

@dataclass
class Item:
    """One attributed item — appears as a single visible line."""
    label: str
    chars: int
    extra: str = ""  # optional secondary text (e.g. command preview)


@dataclass
class Group:
    """Collapsible group with named children."""
    key: str  # 'mcp' | 'files' | 'results'
    title: str
    items: list[Item] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return sum(i.chars for i in self.items)

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class Snapshot:
    """Full attribution snapshot for one render."""
    project_name: str
    short_id: str
    turn: int
    cc_version: str | None
    compaction_at: int | None  # turn number, if detected
    git_branch: str | None = None
    model: str | None = None
    tokens_in_context: int | None = None  # cache_read + cache_creation + input
    ai_title: str | None = None
    unknown_event_types: list[str] = field(default_factory=list)
    unknown_attachment_types: list[str] = field(default_factory=list)

    startup_items: list[Item] = field(default_factory=list)        # CLAUDE.md, memory
    skill_listing: Item | None = None
    system_injections: list[Item] = field(default_factory=list)    # hooks, todo reminders, mcp instr, deferred tool list
    mcp_group: Group = field(default_factory=lambda: Group("mcp", "MCP tool schemas"))
    files_group: Group = field(default_factory=lambda: Group("files", "Files read"))
    results_group: Group = field(default_factory=lambda: Group("results", "Tool results retained"))
    skills_invoked: list[Item] = field(default_factory=list)
    subagents_total: Item = field(default_factory=lambda: Item("sub-agent total", 0))
    user_total: Item = field(default_factory=lambda: Item("user messages", 0))
    assistant_total: Item = field(default_factory=lambda: Item("assistant messages", 0))
    user_count: int = 0
    assistant_count: int = 0

    def total_chars(self) -> int:
        return (
            sum(i.chars for i in self.startup_items)
            + (self.skill_listing.chars if self.skill_listing else 0)
            + sum(i.chars for i in self.system_injections)
            + self.mcp_group.chars
            + self.files_group.chars
            + self.results_group.chars
            + sum(i.chars for i in self.skills_invoked)
            + self.subagents_total.chars
            + self.user_total.chars
            + self.assistant_total.chars
        )


# --- Helpers ---------------------------------------------------------------

def _content_chars(content) -> int:
    """Total char length of a `message.content` value (str or list of blocks)."""
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    n = 0
    for b in content:
        if not isinstance(b, dict):
            continue
        for k in ("text", "thinking", "content"):
            v = b.get(k)
            if isinstance(v, str):
                n += len(v)
            elif isinstance(v, list):
                n += _content_chars(v)
    return n


def _attachment_chars(att: dict) -> int:
    """Char size of an attachment block."""
    n = 0
    for k in ("content", "stdout", "stderr", "additionalContext"):
        v = att.get(k)
        if isinstance(v, str):
            n += len(v)
        elif isinstance(v, list):
            for x in v:
                if isinstance(x, str):
                    n += len(x)
    # deferred_tools_delta: addedLines is a list of one-liners
    for k in ("addedNames", "addedLines", "removedNames"):
        v = att.get(k)
        if isinstance(v, list):
            n += sum(len(x) for x in v if isinstance(x, str))
    return n


def _short(text: str, n: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _server_from_tool_name(name: str) -> str | None:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) >= 2:
            return parts[1]
    return None


def _tool_result_block_chars(block: dict) -> int:
    c = block.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        return _content_chars(c)
    return 0


# --- Startup files ---------------------------------------------------------

def read_startup_items(cwd: Path | None, project_dir: Path) -> list[Item]:
    items: list[Item] = []
    candidates: list[tuple[str, Path]] = [
        ("CLAUDE.md (user)", Path.home() / ".claude" / "CLAUDE.md"),
    ]
    if cwd:
        candidates.append(("CLAUDE.md (project)", cwd / "CLAUDE.md"))
    memory_dir = project_dir / "memory"
    if memory_dir.is_dir():
        for md in sorted(memory_dir.glob("*.md")):
            candidates.append((f"memory/{md.name}", md))

    for label, path in candidates:
        try:
            chars = len(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if chars:
            items.append(Item(label, chars))
    return items


KNOWN_EVENT_TYPES = {
    "queue-operation",
    "attachment",
    "user",
    "assistant",
    "last-prompt",
    "summary",
    "system",
    # CC 2.1.126+ — pure session metadata, no model-context impact
    "ai-title",
    "file-history-snapshot",
    "permission-mode",
}

KNOWN_ATTACHMENT_TYPES = {
    "hook_success",
    "hook_additional_context",
    "skill_listing",
    "deferred_tools_delta",
    "mcp_instructions_delta",
    "todo_reminder",
    "edited_text_file",
    "queued_command",
}


# --- Main pass -------------------------------------------------------------

def build_snapshot(
    events: list[dict],
    project_name: str,
    short_id: str,
    project_dir: Path,
    cwd: Path | None,
    cc_version: str | None,
    git_branch: str | None = None,
    ai_title: str | None = None,
) -> Snapshot:
    snap = Snapshot(
        project_name=project_name,
        short_id=short_id,
        turn=0,
        cc_version=cc_version,
        compaction_at=None,
        git_branch=git_branch,
        ai_title=ai_title,
    )
    snap.startup_items = read_startup_items(cwd, project_dir)

    # Track tool calls so we can attribute their results when they arrive.
    pending_tool_use: dict[str, dict] = {}  # tool_use_id -> {"name", "input"}

    latest_model: str | None = None
    latest_tokens: int | None = None

    unknown_events: set[str] = set()
    unknown_attachments: set[str] = set()

    files_chars: dict[str, int] = {}        # file_path -> chars (sum of result content)
    results_by_tool: dict[str, dict] = {}   # tool_use_id -> {"tool", "preview", "chars"}
    mcp_by_server: dict[str, int] = {}      # server_name -> chars (calls + results)
    skills_invoked: dict[str, int] = {}     # skill_name -> chars (input + result)
    subagent_chars = 0
    user_chars = 0
    assistant_chars = 0
    user_count = 0
    assistant_count = 0
    turn_count = 0

    for e in events:
        et = e.get("type")
        if et and et not in KNOWN_EVENT_TYPES:
            unknown_events.add(et)
        if et == "ai-title":
            t = e.get("aiTitle")
            if isinstance(t, str) and t.strip():
                snap.ai_title = t.strip()
        if et == "attachment":
            a = e.get("attachment", {}) or {}
            atype = a.get("type")
            if atype and atype not in KNOWN_ATTACHMENT_TYPES:
                unknown_attachments.add(atype)
            chars = _attachment_chars(a)
            if not chars:
                continue
            if atype == "skill_listing":
                if snap.skill_listing is None:
                    snap.skill_listing = Item("skill listing (catalogue)", chars)
                else:
                    snap.skill_listing.chars = chars  # later listings overwrite
            elif atype in ("hook_success", "hook_additional_context"):
                hook = a.get("hookName") or a.get("hookEvent") or "hook"
                label = f"hook: {hook}"
                snap.system_injections.append(Item(label, chars))
            elif atype == "deferred_tools_delta":
                snap.system_injections.append(Item("deferred tools list (delta)", chars))
            elif atype == "mcp_instructions_delta":
                snap.system_injections.append(Item("MCP instructions (delta)", chars))
            elif atype == "todo_reminder":
                snap.system_injections.append(Item("todo reminder", chars))
            elif atype == "edited_text_file":
                snap.system_injections.append(Item("edited file delta", chars))
            elif atype == "queued_command":
                snap.system_injections.append(Item("queued command", chars))
            else:
                snap.system_injections.append(Item(f"attachment: {atype}", chars))

        elif et == "assistant":
            assistant_count += 1
            turn_count += 1
            m = e.get("message", {}) or {}
            content = m.get("content")
            assistant_chars += _content_chars(content)
            mdl = m.get("model")
            if mdl:
                latest_model = mdl
            usage = m.get("usage") or {}
            ck = usage.get("cache_read_input_tokens", 0) or 0
            cc = usage.get("cache_creation_input_tokens", 0) or 0
            it = usage.get("input_tokens", 0) or 0
            tokens = ck + cc + it
            if tokens:
                latest_tokens = tokens
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        tu_id = b.get("id")
                        name = b.get("name", "?")
                        inp = b.get("input", {}) or {}
                        if tu_id:
                            pending_tool_use[tu_id] = {"name": name, "input": inp}

                        # Skill: count input chars right now (result will follow as tool_result).
                        if name == "Skill":
                            skill = inp.get("skill", "?")
                            skills_invoked.setdefault(skill, 0)
                            # input itself is small — we'll fold result chars in below.
                        elif name == "Agent":
                            # agent invocation cost (prompt) — result added on tool_result
                            pass
                        elif name.startswith("mcp__"):
                            srv = _server_from_tool_name(name) or "mcp"
                            # call payload itself (input) has cost
                            mcp_by_server[srv] = mcp_by_server.get(srv, 0) + len(str(inp))

        elif et == "user":
            m = e.get("message", {}) or {}
            content = m.get("content")
            if isinstance(content, list) and content and content[0].get("type") == "tool_result":
                # Tool result, not a real user message.
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    tu_id = b.get("tool_use_id")
                    chars = _tool_result_block_chars(b)
                    info = pending_tool_use.get(tu_id, {})
                    name = info.get("name", "unknown")
                    inp = info.get("input", {})

                    if name == "Read":
                        fp = inp.get("file_path", "(unknown)")
                        files_chars[fp] = files_chars.get(fp, 0) + chars
                    elif name == "Skill":
                        skill = inp.get("skill", "?")
                        skills_invoked[skill] = skills_invoked.get(skill, 0) + chars
                    elif name == "Agent":
                        subagent_chars += chars
                    elif name.startswith("mcp__"):
                        srv = _server_from_tool_name(name) or "mcp"
                        mcp_by_server[srv] = mcp_by_server.get(srv, 0) + chars
                        # Also include in generic tool_results group, named by server.
                        preview = _short(name)
                        results_by_tool[tu_id or f"r{len(results_by_tool)}"] = {
                            "tool": name,
                            "preview": preview,
                            "chars": chars,
                        }
                    else:
                        # Generic tool result.
                        preview = _short(_describe_tool_input(name, inp))
                        results_by_tool[tu_id or f"r{len(results_by_tool)}"] = {
                            "tool": name,
                            "preview": preview,
                            "chars": chars,
                        }
            else:
                user_count += 1
                turn_count += 1
                user_chars += _content_chars(content) if not isinstance(content, str) else len(content)

    # Materialise groups.
    for fp, c in sorted(files_chars.items(), key=lambda kv: -kv[1]):
        snap.files_group.items.append(Item(fp, c))
    for srv, c in sorted(mcp_by_server.items(), key=lambda kv: -kv[1]):
        snap.mcp_group.items.append(Item(srv, c))
    for r in sorted(results_by_tool.values(), key=lambda r: -r["chars"]):
        label = r["tool"]
        snap.results_group.items.append(Item(label, r["chars"], extra=r["preview"]))
    for skill, c in sorted(skills_invoked.items(), key=lambda kv: -kv[1]):
        snap.skills_invoked.append(Item(skill, c))

    snap.subagents_total.chars = subagent_chars
    snap.user_total.chars = user_chars
    snap.user_total.label = f"user messages ({user_count})"
    snap.assistant_total.chars = assistant_chars
    snap.assistant_total.label = f"assistant messages ({assistant_count})"
    snap.user_count = user_count
    snap.assistant_count = assistant_count
    snap.turn = turn_count
    snap.model = latest_model
    snap.tokens_in_context = latest_tokens
    snap.unknown_event_types = sorted(unknown_events)
    snap.unknown_attachment_types = sorted(unknown_attachments)

    return snap


def _describe_tool_input(name: str, inp: dict) -> str:
    if name == "Bash":
        return inp.get("command", "")[:80]
    if name in ("Grep", "Glob"):
        return inp.get("pattern", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name == "Edit":
        return inp.get("file_path", "")
    if name == "Write":
        return inp.get("file_path", "")
    return ""
