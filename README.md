# cc-monitor — Consumption Attribution Viewer for Claude Code

A sidecar diagnostic tool for [Claude Code](https://claude.com/claude-code) that shows, in real time, **what** a session's context is being consumed by — broken down per named source.

```
══════════════════════════════════════════════════════════════════════
 Consumption Attribution Viewer
 Session: cc-monitor · "Test script functionality" · f1caa9c0 · turn 14 · 10:30:15 (live)
 Model: claude-opus-4-7  ·  ~123.6k tokens in context
══════════════════════════════════════════════════════════════════════

 CURRENT CONTEXT COMPOSITION  (each line = % of full context)

 Startup files
   CLAUDE.md (user)                       ▌                3%
   CLAUDE.md (project)                    ▌                3%

 ▸ MCP tool schemas (0)
 ▸ Files read (1)                        ██              17%
 ▸ Tool results retained (36)            ████            29%

 Skill catalogue
   skill listing (catalogue)              ██              15%

 Conversation
   user messages (9)                      ▌                2%
   assistant messages (65)                █▌              14%

 System injections
   hook: SessionStart:startup             ▌                6%
   hook: SessionStart                     ▌                6%
   deferred tools list (delta)            ▌                5%

──────────────────────────────────────────────────────────────────────
 [m] MCPs  [f] files  [t] tool results
 [s] sort: size ▾  [r] refresh  [q] quit
══════════════════════════════════════════════════════════════════════
```

## Why this exists

Claude Code already provides `/context`, `/status`, and `/usage` for totals and remaining capacity. This tool answers a different question: **where is the consumption actually going?**

It surfaces every file you've read, every tool result that's been retained, every skill loaded, every MCP call, every hook injection — by name — so you can see exactly what is filling your context window and adapt how you use Claude Code.

It is *not* a usage meter, forecaster, or budget enforcer. It does not display token counts (with one exception — see below) and never displays dollar costs. Those live in `/usage`.

## How it works

A separate process you run in a second terminal. It reads the JSONL transcript file Claude Code writes for the active session (`~/.claude/projects/<encoded>/<session-id>.jsonl`) and tails it for changes. It also reads a handful of well-known local files at attach time to break down the startup overhead (CLAUDE.md, memory files).

**Read-only, always.** Never writes. Never modifies the observed session. Has zero impact on it.

### What gets attributed

| Category | Source |
|---|---|
| `CLAUDE.md (user)` / `CLAUDE.md (project)` | read at attach from `~/.claude/CLAUDE.md` and `<cwd>/CLAUDE.md` |
| Memory files (`memory/*.md`) | read at attach from `~/.claude/projects/<encoded>/memory/` |
| **MCP tool schemas** (per server) | tool calls and results in JSONL grouped by server |
| **Files read** (per file) | `Read` tool calls + their tool_result content |
| **Tool results retained** (per call) | every `tool_result` block in JSONL, grouped by source tool name |
| Skill catalogue | `skill_listing` attachment in JSONL |
| Skills invoked (per skill) | `Skill` tool calls + results |
| Sub-agents | `Agent` tool calls + results (aggregate in v1) |
| User / assistant messages | text and thinking blocks |
| System injections | hook outputs, MCP instructions, todo reminders, etc. (`attachment` blocks) |

The base Claude Code system prompt and built-in tool schemas are **not** in the JSONL transcript and are not reconstructed in v1 — that's what `/context` is for.

### Why characters, not tokens

Anthropic's exact production tokeniser is not public, so any local token count would be an approximation, and reconciling approximations against the JSONL's billed totals would introduce a class of correctness bugs.

Instead, the tool measures **content size in characters per category** and displays each category as a **percentage of the total visible content**. Percentages are exact, free to compute, and proportionally accurate enough to answer "where is the consumption going."

The header line *does* show one token figure — `~N tokens in context` — taken straight from the most recent assistant turn's `usage.cache_read + cache_creation + input` block. That's a pass-through of what the API reports, not a local approximation.

## Install

Requires Python ≥ 3.11. Single dependency: [`rich`](https://github.com/Textualize/rich).

```bash
git clone https://github.com/PaulCoughlin/cc-monitor.git
cd cc-monitor
pip install -e .
```

`-e` installs an editable build — you can `git pull` later and the new code is picked up automatically.

## Usage

In a separate terminal from your Claude Code session:

```bash
# auto-attach to the active session
cc-monitor

# or attach by id (full or short prefix)
cc-monitor --session 1393c4a5
```

If multiple sessions are running, you'll get a picker:

```
Multiple active sessions detected:

  1. cc-monitor  ·  1393c4a5  ·  2s ago [live]  ·  "Test script functionality"
  2. cc-monitor  ·  f1caa9c0  ·  4m ago [live]
  3. coolify-default  ·  1c6780e0  ·  2h ago [live]

Select: _
```

`[live]` badges indicate sessions whose CC process is actually running (per `~/.claude/sessions/<pid>.json`), not just JSONLs that were recently touched.

### Keys

| Key | Action |
|---|---|
| `m` | Toggle MCP tool schemas group |
| `f` | Toggle Files read group |
| `t` | Toggle Tool results retained group |
| `s` | Toggle sort: size ▾ / name |
| `r` | Manual refresh |
| `q` | Quit |

Toggles are independent — expanding files does not collapse MCPs.

## Compatibility

Tested against Claude Code **2.1.121** and **2.1.126**. The JSONL format is not a contracted public interface — when CC ships a new version with new event or attachment types we don't know about, the tool surfaces a yellow banner at the top of the display rather than miscategorising silently:

```
⚠ Claude Code version 2.1.999 not in tested set (2.1.121, 2.1.126) — categories may be inaccurate.
⚠ Unknown JSONL event types observed: foo-bar — not categorised.
```

If you see this, please file an issue with the version + the unknown type names so the tested-against set can be bumped.

## Design notes

- **One sidecar instance per session.** Running multiple Claude Code sessions = run multiple `cc-monitor` instances. There's no multi-session aggregation.
- **No external dependencies on the observed session.** We don't query the running CC, we don't poke MCP servers to size their schemas, we don't intercept anything. We just read the same files CC writes.
- **No estimation games.** If we can't see something cleanly in the JSONL or in a small set of well-known local files, we don't show it. The exception is the single token figure pulled straight from the API's own `usage` block.
- **Variant detection is intentionally absent.** Claude's 1M-context Opus and Sonnet variants record the same model id in the JSONL as their 200k counterparts, so showing "% of window" would require guessing. We show the raw token figure and let `/context` answer the rest.

## Out of scope (v1)

These are deliberate omissions:

- Past-session analysis. Current session only.
- Recommendations or auto-optimisation. The tool surfaces information; the user draws conclusions.
- Per-sub-agent breakdown. Aggregate only in v1.
- Token counts beyond the single header figure. Dollar amounts. Window % estimates.
- Web dashboard / IDE integration / status-line embedding. Terminal TUI only.
- Modification of the session being observed. Read-only, always.
- Compaction-event detection. The visual marker is wired but the detection logic is deferred until a real compaction event has been observed in the wild.
- MCP server integration (the tool itself being callable by Claude). v1 is a viewer for the human, not a tool for the agent.

## Repo layout

```
.
├── SPEC.md            (the original design spec — read this for the why)
├── README.md          (this file)
├── pyproject.toml
├── cc_monitor/
│   ├── __init__.py
│   ├── __main__.py    (entry point: discover session, drive the TUI)
│   ├── session.py     (JSONL discovery, picker, tail)
│   ├── parser.py      (content-block categorisation)
│   ├── view.py        (Rich layout + keypress handling)
│   └── state.py       (collapse/expand + sort state)
└── .gitignore
```

## License

MIT.
