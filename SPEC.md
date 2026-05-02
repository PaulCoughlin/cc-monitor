# Consumption Attribution Viewer

A sidecar diagnostic tool for Claude Code that shows, in real time, **what** a session's context is being consumed by — broken down per source.

## Purpose

Claude Code already provides `/context`, `/status`, and `/usage` for totals and remaining capacity. This tool answers a different question: **where is the consumption actually going?**

It is a *consumption attribution viewer*, not a usage meter, not a forecaster, not a budget enforcer. It exists to surface the specific contributors to a session's context — every MCP server, every file read, every tool result, every skill — so the user can see exactly what is filling their context and adapt how they use Claude Code.

## User Scenario

The user is working in a Claude Code session in one terminal. In a second terminal, they launch this tool. The tool auto-attaches to the active session and displays a live-updating attribution view. As the user works, the view updates after each turn, showing the proportional breakdown of context composition by named source. The user can expand or collapse groups to drill into detail.

The tool runs read-only against the session's local transcript file. It never writes to the session, never modifies state, and has zero impact on the session being observed.

## Architecture

### Sidecar process

- Runs as a separate process, in a separate terminal, attached to one specific Claude Code session.
- Reads the session's JSONL transcript file at `~/.claude/projects/{encoded-project-path}/{session-id}.jsonl`.
- Tails the file for changes using filesystem watch (stdlib polling preferred; `watchdog` only if stdlib is insufficient).
- Never writes. Never modifies. Read-only on the transcript.

### Per-session, per-instance

- One running instance of this tool attaches to one session-id.
- Multiple concurrent Claude Code sessions = multiple instances of this tool, one per session.

### Session selection at launch

Three modes:

1. **Auto-attach (default)** — invoked with no arguments, the tool scans `~/.claude/projects/`, finds the most recently modified JSONL, and attaches to that session.
2. **Picker** — if more than one JSONL has been written to within a recent window (e.g. last 5 minutes), present a list and let the user pick:
   ```
   Multiple active sessions detected:
     1. my-project    3a7f8e21   (last activity: 12s ago)
     2. side-project  b9c477ad   (last activity: 1m ago)
   Select: _
   ```
3. **Explicit** — `attribviewer --session 3a7f8e21` for unambiguous attachment by session-id.

The header in the live view always shows project name + short session ID so the user can confirm which session is being observed.

## Core Mechanic — Attribution by Content Size

This is the central design decision. Read it carefully.

The tool does **not** count tokens locally. Anthropic's exact production tokeniser is not public, so any local token count would be an approximation, and reconciling approximations against the JSONL's billed totals would introduce a class of correctness bugs.

Instead, the tool measures **content size in characters (or bytes) per category** and displays each category as a **percentage of the turn's total content**. Percentages are exact, free to compute, and proportionally accurate enough to answer "where is the consumption going."

The tool **does not display token counts at all**. It does not display dollar costs. It does not display billing information. The user's existing tools (`/context`, `/status`, `/usage`) already cover those questions. This tool answers a different question and stays in its lane.

### What gets categorised

For each turn appended to the JSONL, the tool parses the message content blocks and assigns each to a category by named source. The categories, with their identity-level granularity, are:

| Category | Granularity |
|---|---|
| MCP tool schemas | Per MCP server (each named individually) |
| Files read | Per file (full path shown) |
| Tool results retained | Per result (with the command/query that produced it shown) |
| Skills loaded | Per skill (each named individually) |
| Sub-agents | Aggregate total (per-sub-agent breakdown deferred to v2) |
| Conversation — user messages | Aggregate total with count |
| Conversation — assistant messages | Aggregate total with count |
| System prompt | Single line |

The only aggregations are: sub-agents (deferred granularity), and the two conversation totals (which are inherently aggregate at this view level). Everything else is named individually. No fuzzy groupings.

### What "still in context" means

The tool measures the **current context composition** — what's actually in the context window right now, not what was sent and discarded. Files that were read but later auto-compacted out are not shown. Tool results that have been pruned are not shown. The view reflects what's currently consuming context, because that's what the user can act on.

If auto-compaction events occur, they should be detected and indicated in the display (e.g. a marker noting "context was compacted at turn N"). The tool must not pretend the compaction didn't happen.

## Display Specification

### Layout

Single-pane terminal TUI. Live-updating after each turn. Header, body, footer.

- **Header** — fixed, two lines: tool name; session identifier line (project name, short session ID, current turn number, timestamp, "live" indicator).
- **Body** — the attribution view (see mockups below).
- **Footer** — keybinding hints, sort state.

### Default state — collapsed

```
══════════════════════════════════════════════════════════════════════
 Consumption Attribution Viewer
 Session: my-project · 3a7f8e21 · turn 14 · 18:42:03 (live)
══════════════════════════════════════════════════════════════════════

 CURRENT CONTEXT COMPOSITION  (each line = % of full context)

 ▸ MCP tool schemas (4)                    ██████████  38%
 ▸ Files read (7)                          ██████████████  30%
 ▸ Tool results retained (12)              █████        9%

 Skills loaded
   frontend-design                         ███          5%
   pdf-reading                             █            2%

 Sub-agents
   sub-agent total                         ████         7%

 Conversation
   user messages (14)                      ██           3%
   assistant messages (14)                 ██████      12%

 System prompt                             ██           4%
──────────────────────────────────────────────────────────────────────
 [m] MCPs  [f] files  [t] tool results
 [s] sort: size ▾  [r] refresh  [q] quit
══════════════════════════════════════════════════════════════════════
```

### Expanded — files revealed

```
 ▸ MCP tool schemas (4)                    ██████████  38%
 ▾ Files read (7)
     src/components/Dashboard.tsx          ██████      11%
     src/api/handlers.ts                   ████         7%
     package.json                          ██           4%
     README.md                             ██           3%
     src/utils/format.ts                   █            2%
     tests/dashboard.test.ts               █            2%
     docs/architecture.md                  ▌            1%
 ▸ Tool results retained (12)              █████        9%
```

### Expansion behaviour

- Three groups are collapsible: **MCP tool schemas**, **Files read**, **Tool results retained**.
- Skills and sub-agents stay always-expanded in v1 (typically short).
- Toggles are independent — expanding files does not collapse MCPs.
- `▸` indicates collapsed; `▾` indicates expanded.
- When collapsed, the group line shows the count in parentheses and the group's combined percentage with a single bar.
- When expanded, the group total bar is removed; the children carry the visual weight. No double-counting in the eye.

### Sorting

- Default: **size-descending, biggest consumer first**, in every group and at every level.
- `[s]` toggles between size-descending and alphabetical. Sort state shown in footer (`sort: size ▾` or `sort: name`).
- Sort applies to all groups simultaneously.

### Keybindings

| Key | Action |
|---|---|
| `m` | Toggle MCP tool schemas group |
| `f` | Toggle Files read group |
| `t` | Toggle Tool results retained group |
| `s` | Toggle sort: size ▾ / name |
| `r` | Manual refresh (live updates are automatic; this forces an immediate redraw) |
| `q` | Quit |

### Visual conventions

- Bars rendered with block characters scaled to the percentage. A reasonable bar width (e.g. 14 chars for 100%) keeps the display compact.
- Percentages rounded to whole numbers. Items below 1% may show `▌` or `<1%` rather than an empty bar.
- The tool's own resource footprint (its own contribution to context, if any — note: as a sidecar process running in a separate terminal, it should be zero) displayed in a small corner indicator if non-zero.

## Edge Cases — Must Handle

### JSONL schema changes between Claude Code versions

The JSONL format is not a contracted public interface. If the tool encounters fields it doesn't recognise or expected fields that are missing, it must **fail loudly with a clear version-mismatch message**. Never silently miscategorise. Tag the version of Claude Code the tool was built/tested against in the README and in the error message.

### Auto-compaction events

When Claude Code auto-compacts the conversation, the JSONL will reflect rewritten history. The tool must detect this and display an indicator (e.g. "context compacted at turn N") so the user knows the view doesn't reflect everything that has happened — only what's currently in context.

### Sub-agents

If sub-agents spawn, their context contributions appear under the **Sub-agents** group as an aggregate total. Per-sub-agent breakdown is explicitly deferred to v2.

### Anonymous content

Occasionally a content block may not have an obvious named identifier (e.g. an inline file paste). These appear on their own line with a generic label like `inline content (turn 9)` rather than being absorbed into a group.

### Long lists

The collapsible groups solve most of this. If a group's expanded list is still too long for the terminal, scroll within the group or truncate with a clear marker (`... 12 more`). Decidable at build time.

### No active session

If `~/.claude/projects/` contains no recently-modified JSONL files, the tool should print a friendly message and exit, rather than hang or crash.

## Out of Scope for v1

These are deliberate omissions. Do not add them to v1.

- **Past-session analysis.** Current session only. Historical analysis is a separate concern.
- **Recommendations or auto-optimisation.** The tool surfaces information; the user draws conclusions. Recommendations layer is v2.
- **Per-sub-agent breakdown.** Aggregate only in v1.
- **Token counts or dollar amounts.** Not the question this tool answers.
- **Web dashboard, IDE integration, status-line embedding.** Terminal TUI only.
- **MCP server integration (agent self-introspection).** v1 is a viewer for the human, not a tool callable by the agent.
- **Modification of the session being observed.** Read-only, always.
- **Multi-session aggregation.** One instance, one session.

## Tech Stack

- **Language**: Python 3.13. The global environment uses `py` / `python` as configured per the user's `~/.claude/CLAUDE.md`.
- **TUI library**: [Rich](https://github.com/Textualize/rich). Standard for Python TUIs, well-maintained, batteries-included for live-updating tables, bars, and renderables. No Textual app framework, no curses-from-scratch.
- **File watching**: stdlib first (`os.stat` polling at a sensible interval, e.g. 250–500ms). Only introduce `watchdog` if stdlib polling proves insufficient.
- **No other dependencies** unless explicitly justified.

## Build Approach

Per the user's global `~/.claude/CLAUDE.md`: (also dropped into this working folder for info)

1. **Think before coding.** State assumptions explicitly. If the JSONL schema is ambiguous, inspect a real session's file before writing parsing code. Surface tradeoffs.
2. **Simplicity first.** Minimum code that solves the problem. No speculative abstractions, no flexibility that wasn't requested. If it could be 50 lines instead of 200, rewrite it.
3. **Surgical changes.** Touch only what's needed. Match style. No "improvements" outside the requested change.
4. **Goal-driven execution.** Define success criteria, loop until verified.

## Success Criteria

The tool is working when:

1. Launching it against an active Claude Code session attaches automatically (or via picker / explicit ID).
2. The header shows correct session identification.
3. As the user performs work in the observed session — file reads, MCP tool calls, web searches, bash commands, skill loads, sub-agent spawns — the view updates after each turn.
4. The breakdown is accurate: every named MCP, file, tool result, and skill in the session's current context appears as its own line under its group, with its proportional size.
5. Group expand/collapse via `m`/`f`/`t` works independently per group.
6. Sort toggle via `s` switches between size-descending and alphabetical, in all groups simultaneously.
7. The user can look at the display and answer the question: **"what is this session specifically using its consumption on?"** — concretely, by named source, in proportions.

If item 7 is not satisfied, the tool is not done.

## Repo Layout (suggested, not prescriptive)

Repo: https://github.com/PaulCoughlin/cc-monitor

```
.
├── SPEC.md            (this document)
├── README.md          (install, usage, version compatibility)
├── pyproject.toml     (or requirements.txt)
├── attribviewer/
│   ├── __init__.py
│   ├── __main__.py    (entry point)
│   ├── session.py     (JSONL discovery, attachment, tailing)
│   ├── parser.py      (content-block categorisation)
│   ├── view.py        (Rich renderable, layout, keybindings)
│   └── state.py       (collapse/expand state, sort state)
└── tests/
    └── fixtures/      (sample JSONL files for parser tests)
```

The build agent should propose adjustments to this layout if simpler/cleaner.

## Versioning and Compatibility

- Pin the Claude Code version the tool is built and tested against.
- Document this prominently in the README.
- On schema mismatch, fail with a clear message naming the expected vs detected version (where detectable).

## Notes for the Build Agent

- The user's `~/.claude/CLAUDE.md` applies to this build. Read it. Honour it.
- Inspect a real session's JSONL before writing parsing code. Don't guess at the structure. The format includes per-turn `usage` blocks (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) and message content blocks of various types (text, tool_use, tool_result, etc.). The categorisation logic must walk these content blocks and identify the named source of each.
- The tool is a magnifying glass. Keep it focused. Resist the urge to add forecasting, recommendations, or "helpful" interpretations. Surface the information; the human interprets.
- When in doubt about scope, prefer "out of v1" over "let's add it."
