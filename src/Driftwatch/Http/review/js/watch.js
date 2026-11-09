/** Show the backend source search and refresh it without a click. */

const REFRESH_MS = 5000;

export function startWatch(statusNode, listNode) {
  async function tick() {
    try {
      const response = await fetch("/api/watch");
      if (!response.ok) {
        throw new Error(`Watcher failed (${response.status}).`);
      }
      renderWatch(statusNode, listNode, await response.json());
    } catch (error) {
      statusNode.textContent = error instanceof Error ? error.message : "Watcher failed.";
    }
  }

  void tick();
  window.setInterval(() => {
    void tick();
  }, REFRESH_MS);
}

function renderWatch(statusNode, listNode, board) {
  listNode.replaceChildren();
  if (!board.connected) {
    statusNode.textContent =
      "Not connected to Postgres. Restart with DRIFTWATCH_DATABASE_URL so source search runs on its own.";
    return;
  }
  const sources = board.sources ?? [];
  statusNode.textContent = sources.length
    ? `Searching ${sources.length} ground-truth sources on their own interval. This list updates every 5 seconds.`
    : "Connected. The catalog has no active ground-truth sources.";
  for (const source of sources) {
    const row = document.createElement("article");
    row.className = "watch-row";
    const name = document.createElement("span");
    name.textContent = source.provider;
    const kind = document.createElement("span");
    kind.textContent = source.kind;
    const url = document.createElement("span");
    url.className = "watch-url";
    url.textContent = source.url;
    const outcome = document.createElement("span");
    outcome.textContent = outcomeText(source);
    row.append(name, kind, url, outcome);
    listNode.append(row);
  }
}

function outcomeText(source) {
  const status = source.last_status || "waiting";
  const indexed =
    source.indexed_entries === null || source.indexed_entries === undefined
      ? "not indexed"
      : `${source.indexed_entries} indexed`;
  const next = source.next_poll_at ? `next ${formatWhen(source.next_poll_at)}` : "due now";
  const failure = source.consecutive_failures ? `${source.consecutive_failures} failures` : "";
  return [status, indexed, next, failure].filter(Boolean).join(" · ");
}

function formatWhen(value) {
  const when = new Date(value);
  if (Number.isNaN(when.getTime())) return value;
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
