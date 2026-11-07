/** Talk to the local review board. */

export async function loadBoard() {
  const response = await fetch("/api/board");
  if (!response.ok) {
    throw new Error(`Could not load scans (${response.status}).`);
  }
  return response.json();
}

export async function saveVerdict(key, verdict, note) {
  const response = await fetch("/api/verdicts", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, verdict, note }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : `Save failed (${response.status}).`;
    throw new Error(detail);
  }
  return response.json();
}
