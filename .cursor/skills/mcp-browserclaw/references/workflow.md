# BrowserClaw workflow reference

## Tool map (MCP server `BrowserClaw`)

| Goal | Tools |
|------|--------|
| Session label | `name_session` |
| Tabs | `tabs` (list/new/…), `tab_groups` |
| Navigate | `navigate` (invalidates refs) |
| Observe | `snapshot`, `diff` |
| Interact | `act` (refs from last snapshot) |
| Wait | `wait` (`for=text` / `selector`) |
| Extract | `read`, `grep` |
| Visual / archive | `screenshot`, `pdf` |
| Files | `download`, `upload` |
| Bulk / JS | `run`, `evaluate` |
| Isolation | `windows` |

## Tab ownership

- `tabs` list shows: yours / other agents / user.
- Only operate on tabs you own.
- User points at a foreign tab → `tabs` new with that URL; leave original untouched.
- Parallel work: one tab (or window) per independent subtask; ≤5 tabs unless asked.

## Form fill pattern

```text
snapshot → act kind=fill fields=[{ref, value}, ...] → trust returned diff
```

Never fill field-by-field in separate calls when `fields[]` can do one batch.

## Stale refs

Refs expire after navigate, submit, major re-render, or another agent's change. Symptom: act fails with missing/stale ref → `snapshot` again.

## Connect / recovery

1. BrowserClaw app running.
2. Cursor `mcp.json` entry `BrowserClaw` with Streamable HTTP URL from the in-app MCP board (often `http://127.0.0.1:9200/mcp`; local installs may differ — e.g. `:9010`).
3. Restart Cursor after first connect if tools missing.
4. If disconnected mid-task: stop; ask user to reopen BrowserClaw / check cockpit; resume with `tabs` list + fresh `snapshot`.

## Cockpit (user-facing)

- New-tab dashboard: Running now + Recent activity.
- User can Watch or Stop a live agent session.
- Replay: scrubbable DOM recording per agent tab; data under `~/.browserclaw/`.

## vs BrowserOS MCP

| | BrowserClaw | BrowserOS |
|--|-------------|-----------|
| Role | Agent-dedicated browser | Human browser (+ Strata) |
| Typical port (docs) | `:9200/mcp` | `:9000/mcp` |
| Prefer for | Logged-in agent web work | CAPTCHA/2FA in human profile, Klavis apps |
