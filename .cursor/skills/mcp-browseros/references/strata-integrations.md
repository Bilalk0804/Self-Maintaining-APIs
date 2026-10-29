# Klavis Strata integrations (BrowserOS MCP)

BrowserOS exposes 40+ external services via Klavis Strata: Gmail, Slack, GitHub, Notion, Google Calendar, Jira, Linear, Figma, Salesforce, and more.

**Rule:** Never guess action names. Discover the schema before every `execute_action`.

---

## Progressive discovery flow

```
connector_mcp_servers(server_name)
        ↓ connected?
discover_server_categories_or_actions
        ↓
get_category_actions
        ↓
get_action_details
        ↓
execute_action (with include_output_fields)
```

Fallback when stuck: `search_documentation` with keywords.

---

## Step 1: Check connection

```
connector_mcp_servers({ server_name: "<service>" })
```

- **Connected** → proceed to discovery.
- **Not connected** → show user the returned `authUrl`, ask them to authenticate, wait for confirmation, then call `connector_mcp_servers` again.

---

## Step 2–4: Discover actions and parameters

1. `discover_server_categories_or_actions` — list top-level categories or actions.
2. `get_category_actions` — expand a category from step 1.
3. `get_action_details` — read required/optional params before calling.

Do not call `execute_action` until `get_action_details` confirms the parameter schema.

---

## Step 5: Execute

```
execute_action({
  server_name: "...",
  action_name: "...",
  params: { ... },
  include_output_fields: ["field1", "field2"]  // limit response size
})
```

Use `include_output_fields` to avoid oversized responses.

---

## Authentication failures

When `execute_action` returns an auth error:

1. `connector_mcp_servers(server_name)` — get fresh `authUrl`.
2. Prompt user to open `authUrl` and authenticate.
3. Wait for **explicit user confirmation**.
4. Retry `execute_action`.

Alternatively: `handle_auth_failure` when the tool schema applies to the error context.

---

## Parallelism

Independent read/discovery calls (`connector_mcp_servers`, `discover_*`, `get_action_details` for unrelated services) may run in parallel. `execute_action` writes should be sequential unless the user confirms parallel side effects are safe.
