# Visible browser workflows (BrowserOS MCP)

## Tool map

| Goal | Tools |
|------|-------|
| New visible window | `create_window`, `activate_window` |
| Tab on top immediately | `new_page({ background: false })` |
| Hidden tab / window | `new_hidden_page`, `create_hidden_window` |
| Show tab to user | `show_page`, `set_window_visibility` |
| List / focus windows | `list_windows`, `activate_window` |

---

## Scenario A: Visible from start (anti-bot, user observation)

Use when the user must see the browser immediately — CAPTCHA, Cloudflare, manual oversight.

1. `browseros_info({ topic: "overview" })` — confirm MCP is up.
2. `create_window({ hidden: false })` — note returned `windowId`.
3. `new_page({ url, background: false, windowId })` — tab opens in foreground.
4. `activate_window({ windowId })` — ensure window has focus.
5. `take_snapshot({ page })` — baseline before automation.
6. Automate or **STOP at CAPTCHA/2FA** and ask user to intervene.
7. After user confirms → fresh snapshot → continue.
8. `take_screenshot({ page })` — evidence.

---

## Scenario B: Background prep → show user (intervention at end)

Use when automation can run headlessly first, but user must finish (login, CAPTCHA, review).

1. `create_hidden_window()` or `create_window({ hidden: true })` — note `windowId`.
2. `new_hidden_page({ url, windowId })` — note `page` id from response / `list_pages`.
3. `take_snapshot({ page })` → automate safe steps (navigation, form fill, data extract).
4. When manual step needed:
   - `show_page({ page, activate: true })`
   - `set_window_visibility({ windowId, visible: true, activate: true })`
5. Tell user what to do; **wait for explicit confirmation**.
6. Fresh snapshot → continue or capture evidence.

---

## Scenario C: Focus existing window

Use when BrowserOS windows already exist and you only need visibility/focus.

1. `list_windows()` — pick target `windowId`.
2. `set_window_visibility({ windowId, visible: true, activate: true })`.
3. `get_active_page()` or `list_pages` — pick `page`.
4. `take_snapshot({ page })` → proceed.

**Note:** `set_window_visibility` may return a new `windowId` because BrowserOS can replace the window during show/hide transitions. Always use the ID from the latest response.

---

## Defaults worth remembering

- `new_page` defaults to `background: true` — set `background: false` when the user must see the tab immediately.
- `show_page` errors if the page is already visible — use `activate_window` / focus instead.
- After `navigate_page`, all snapshot element IDs are invalid — re-snapshot before next action.
