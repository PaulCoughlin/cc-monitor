"""Rich-based TUI rendering and live update loop."""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime

from rich.console import Console, Group as RGroup
from rich.live import Live
from rich.text import Text

from .parser import Group, Item, Snapshot
from .state import ViewState


BAR_WIDTH = 14  # chars representing 100%
BAR_FULL = "█"
BAR_HALF = "▌"

# Max items shown per expanded group; remainder collapsed into a "... N more" line.
MAX_GROUP_ITEMS = 20

# Claude Code versions this tool has been observed to parse cleanly. Any other
# version triggers the schema-mismatch banner.
TESTED_CC_VERSIONS: set[str] = {"2.1.121", "2.1.126"}

# NOTE: we deliberately do NOT map model id → context window. Both Opus and
# Sonnet have 200k-standard and 1M-variant offerings sharing the same model id
# in the JSONL, so any % we showed would be a guess. Use /context for the exact
# window. We surface raw tokens-in-context only.


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _schema_warnings(snap) -> list[str]:
    out: list[str] = []
    if snap.cc_version and snap.cc_version not in TESTED_CC_VERSIONS:
        tested = ", ".join(sorted(TESTED_CC_VERSIONS))
        out.append(
            f"Claude Code version {snap.cc_version} not in tested set ({tested}) — "
            "categories may be inaccurate."
        )
    if snap.unknown_event_types:
        kinds = ", ".join(snap.unknown_event_types)
        out.append(f"Unknown JSONL event types observed: {kinds} — not categorised.")
    if snap.unknown_attachment_types:
        kinds = ", ".join(snap.unknown_attachment_types)
        out.append(f"Unknown attachment types observed: {kinds} — folded into 'attachment: <type>'.")
    return out


def _render_model_line(snap) -> "Text":
    model = snap.model or "(unknown model)"
    parts = [f"Model: {model}"]
    tokens = snap.tokens_in_context or 0
    if tokens:
        parts.append(f"~{_format_tokens(tokens)} tokens in context")
    t = Text(" " + "  ·  ".join(parts), style="magenta")
    return t


def _bar(pct: float) -> str:
    if pct <= 0:
        return ""
    units = pct / 100 * BAR_WIDTH
    full = int(units)
    half = (units - full) >= 0.5
    s = BAR_FULL * full + (BAR_HALF if half else "")
    return s or BAR_HALF  # ensure something visible for tiny non-zero


def _pct(part: int, total: int) -> float:
    return (part / total * 100) if total else 0.0


def _fmt_pct(pct: float) -> str:
    if pct < 1:
        return "<1%"
    return f"{int(round(pct))}%"


def _line(label: str, chars: int, total: int, indent: int = 1, label_width: int = 38) -> Text:
    pct = _pct(chars, total)
    bar = _bar(pct)
    pad = " " * indent
    label_disp = label[: label_width - 1].ljust(label_width)
    bar_field = bar.ljust(BAR_WIDTH)
    t = Text()
    t.append(pad)
    t.append(label_disp)
    t.append(" ")
    t.append(bar_field, style="bright_blue")
    t.append(f" {_fmt_pct(pct):>4}", style="bold")
    return t


def _sorted_items(items: list[Item], state: ViewState) -> list[Item]:
    if state.sort_mode == "name":
        return sorted(items, key=lambda i: i.label.lower())
    return sorted(items, key=lambda i: -i.chars)


def render(snap: Snapshot, state: ViewState) -> RGroup:
    total = snap.total_chars()
    lines: list[Text] = []

    # Header
    header_rule = "═" * 70
    lines.append(Text(header_rule, style="bright_black"))
    lines.append(Text(" Consumption Attribution Viewer", style="bold"))
    ts = datetime.now().strftime("%H:%M:%S")
    branch = f" [{snap.git_branch}]" if snap.git_branch else ""
    title = f' · "{snap.ai_title}"' if snap.ai_title else ""
    sub = f" Session: {snap.project_name}{branch}{title} · {snap.short_id} · turn {snap.turn} · {ts} (live)"
    lines.append(Text(sub, style="cyan"))

    if snap.model or snap.tokens_in_context:
        lines.append(_render_model_line(snap))

    lines.append(Text(header_rule, style="bright_black"))

    # Schema-mismatch warnings.
    warnings = _schema_warnings(snap)
    if warnings:
        for w in warnings:
            lines.append(Text(f" ⚠ {w}", style="bold yellow"))
        lines.append(Text(""))
    lines.append(Text(""))
    lines.append(Text(" CURRENT CONTEXT COMPOSITION  (each line = % of full context)", style="bold"))
    lines.append(Text(""))

    # Startup files
    if snap.startup_items:
        lines.append(Text(" Startup files", style="bold"))
        for it in _sorted_items(snap.startup_items, state):
            lines.append(_line(it.label, it.chars, total, indent=3))
        lines.append(Text(""))

    # Collapsible groups
    lines.append(_render_group(snap.mcp_group, "mcp", state, total))
    lines.append(_render_group(snap.files_group, "files", state, total))
    lines.append(_render_group(snap.results_group, "results", state, total))
    lines.append(Text(""))

    # Skill listing (single line)
    if snap.skill_listing:
        lines.append(Text(" Skill catalogue", style="bold"))
        lines.append(_line(snap.skill_listing.label, snap.skill_listing.chars, total, indent=3))
        lines.append(Text(""))

    # Skills invoked
    if snap.skills_invoked:
        lines.append(Text(" Skills invoked", style="bold"))
        for it in _sorted_items(snap.skills_invoked, state):
            lines.append(_line(it.label, it.chars, total, indent=3))
        lines.append(Text(""))

    # Sub-agents
    if snap.subagents_total.chars:
        lines.append(Text(" Sub-agents", style="bold"))
        lines.append(_line(snap.subagents_total.label, snap.subagents_total.chars, total, indent=3))
        lines.append(Text(""))

    # Conversation
    lines.append(Text(" Conversation", style="bold"))
    lines.append(_line(snap.user_total.label, snap.user_total.chars, total, indent=3))
    lines.append(_line(snap.assistant_total.label, snap.assistant_total.chars, total, indent=3))
    lines.append(Text(""))

    # System injections
    if snap.system_injections:
        lines.append(Text(" System injections", style="bold"))
        # group same-label injections (e.g. multiple todo reminders)
        agg: dict[str, int] = {}
        for it in snap.system_injections:
            agg[it.label] = agg.get(it.label, 0) + it.chars
        items = [Item(k, v) for k, v in agg.items()]
        for it in _sorted_items(items, state):
            lines.append(_line(it.label, it.chars, total, indent=3))
        lines.append(Text(""))

    # Compaction marker
    if snap.compaction_at is not None:
        lines.append(Text(f" ⚠ context was compacted at turn {snap.compaction_at}", style="yellow"))
        lines.append(Text(""))

    # Footer
    lines.append(Text("─" * 70, style="bright_black"))
    foot1 = " [m] MCPs  [f] files  [t] tool results"
    sort_indicator = "size ▾" if state.sort_mode == "size" else "name"
    foot2 = f" [s] sort: {sort_indicator}  [r] refresh  [q] quit"
    lines.append(Text(foot1, style="dim"))
    lines.append(Text(foot2, style="dim"))
    lines.append(Text(header_rule, style="bright_black"))

    return RGroup(*lines)


def _render_group(group: Group, key: str, state: ViewState, total: int) -> Text | RGroup:
    expanded = state.is_expanded(key)
    marker = "▾" if expanded else "▸"
    title = f" {marker} {group.title} ({group.count})"

    if not group.items:
        # Show the group header anyway for predictability.
        t = Text()
        t.append(" ")
        t.append(marker, style="bright_black")
        t.append(f" {group.title} (0)", style="dim")
        return t

    if not expanded:
        pct = _pct(group.chars, total)
        bar = _bar(pct)
        t = Text()
        t.append(" ")
        t.append(marker)
        title_field = f" {group.title} ({group.count})"
        t.append(title_field.ljust(38))
        t.append(" ")
        t.append(bar.ljust(BAR_WIDTH), style="bright_blue")
        t.append(f" {_fmt_pct(pct):>4}", style="bold")
        return t

    out = [Text(title, style="bold")]
    items = _sorted_items(group.items, state)
    shown = items[:MAX_GROUP_ITEMS]
    hidden = items[MAX_GROUP_ITEMS:]
    for it in shown:
        label = it.label
        if it.extra:
            label = f"{label}  ⟶ {it.extra}"
        out.append(_line(label, it.chars, total, indent=5))
    if hidden:
        hidden_chars = sum(i.chars for i in hidden)
        more_label = f"... {len(hidden)} more"
        out.append(_line(more_label, hidden_chars, total, indent=5))
    return RGroup(*out)


# --- Live loop -------------------------------------------------------------

class Runner:
    """Owns the Live display, the snapshot, and the keyboard thread."""

    def __init__(self, snapshot_provider, refresh_per_sec: float = 4.0):
        self.snapshot_provider = snapshot_provider
        self.state = ViewState()
        self.console = Console()
        self.refresh_per_sec = refresh_per_sec
        self._stop = threading.Event()

    def _renderable(self):
        snap = self.snapshot_provider()
        return render(snap, self.state)

    def run(self) -> None:
        kb_thread = threading.Thread(target=self._keyboard_loop, daemon=True)
        kb_thread.start()
        try:
            with Live(
                self._renderable(),
                console=self.console,
                refresh_per_second=self.refresh_per_sec,
                screen=False,
            ) as live:
                while not self._stop.is_set():
                    live.update(self._renderable())
                    time.sleep(1.0 / self.refresh_per_sec)
        except KeyboardInterrupt:
            pass

    def _keyboard_loop(self) -> None:
        for ch in _read_keys():
            if self._stop.is_set():
                return
            if ch in ("q", "\x03", "\x04"):
                self._stop.set()
                return
            elif ch == "m":
                self.state.toggle("mcp")
            elif ch == "f":
                self.state.toggle("files")
            elif ch == "t":
                self.state.toggle("results")
            elif ch == "s":
                self.state.toggle_sort()
            elif ch == "r":
                pass  # next tick will re-render anyway


def _read_keys():
    """Yield single keypresses. Cross-platform best effort."""
    if sys.platform == "win32":
        import msvcrt
        while True:
            ch = msvcrt.getwch()
            yield ch
    else:
        import termios, tty, select
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                if select.select([sys.stdin], [], [], 0.2)[0]:
                    yield sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
