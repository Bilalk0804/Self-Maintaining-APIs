/** Draw the review desk from a board snapshot. */

const VERDICT_TEXT = { true: "True", false: "False", unsure: "Unsure" };

export function renderSummary(node, board) {
  const summary = board.summary;
  const precision =
    summary.precision === null
      ? "Label a true or false hit to measure precision."
      : `Precision ${(summary.precision * 100).toFixed(0)}% from ${summary.true_count} true and ${summary.false_count} false.`;
  node.textContent = `${board.root_name} · ${summary.findings} findings · ${summary.unlabeled_count} unlabeled. ${precision}`;
}

export function renderBanner(node, board) {
  const problems = board.files.filter((file) => file.error || file.skipped);
  if (!problems.length) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.textContent = problems
    .map((file) => {
      const skipped = file.skipped ? `${file.skipped} skipped` : "";
      return [file.name, file.error, skipped].filter(Boolean).join(": ");
    })
    .join(" · ");
}

export function renderList(node, findings, selectedKey, onSelect, emptyDirectory) {
  node.replaceChildren();
  if (!findings.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = emptyDirectory
      ? "This folder has no scan files yet. Copy one findings JSON per repository here, then reload."
      : "No findings match these filters.";
    node.append(empty);
    return;
  }
  let currentRepo = null;
  let list = null;
  for (const finding of findings) {
    if (finding.repo !== currentRepo) {
      currentRepo = finding.repo;
      const group = document.createElement("section");
      group.className = "group";
      const heading = document.createElement("h2");
      heading.textContent = finding.repo;
      list = document.createElement("ol");
      group.append(heading, list);
      node.append(group);
    }
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "finding";
    button.dataset.severity = finding.severity || "unknown";
    if (finding.key === selectedKey) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => onSelect(finding.key));

    const location = document.createElement("span");
    location.className = "location";
    location.textContent = `${finding.file_path}:${finding.start_line}`;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = `${finding.change_type} · ${finding.confidence.toFixed(3)} · ${finding.severity || "unspecified"}`;
    button.append(location, meta);
    if (finding.verdict) {
      const mark = document.createElement("span");
      mark.className = "mark-label";
      mark.dataset.verdict = finding.verdict;
      mark.textContent = VERDICT_TEXT[finding.verdict];
      button.append(mark);
    }
    item.append(button);
    list.append(item);
  }
}

export function renderDetail(node, finding, onVerdict) {
  node.replaceChildren();
  if (!finding) {
    const heading = document.createElement("h2");
    heading.textContent = "No finding selected";
    const copy = document.createElement("p");
    copy.className = "empty detail-body";
    copy.textContent = "Pick a row, then mark it true, false, or unsure. Keys 1, 2, 3, and 0 do the same when you are not typing.";
    node.append(heading, copy);
    return;
  }

  const heading = document.createElement("h2");
  heading.textContent = finding.change_type;
  const body = document.createElement("div");
  body.className = "detail-body";
  const location = document.createElement("p");
  location.className = "location";
  location.textContent = `${finding.file_path}:${finding.start_line}`;
  const symbol = document.createElement("p");
  symbol.className = "symbol";
  symbol.textContent = finding.symbol_ref || finding.surface_ref || "No symbol";
  const snippet = document.createElement("pre");
  snippet.className = "snippet";
  snippet.textContent = finding.snippet || "No snippet was stored for this finding.";

  const facts = document.createElement("dl");
  facts.className = "facts";
  for (const [term, value] of [
    ["Severity", finding.severity || "unspecified"],
    ["Confidence", finding.confidence.toFixed(3)],
    ["Match", finding.match_kind || "unknown"],
    ["Scope", finding.scope || "unknown"],
    ["Surface", finding.surface_ref || "unknown"],
    ["Detail", finding.detail || "none"],
    ["Renamed to", finding.renamed_to || "none"],
  ]) {
    const label = document.createElement("dt");
    label.textContent = term;
    const data = document.createElement("dd");
    data.textContent = value;
    facts.append(label, data);
  }

  const verdicts = document.createElement("div");
  verdicts.className = "verdicts";
  const note = document.createElement("textarea");
  note.className = "note";
  note.maxLength = 500;
  note.rows = 3;
  note.placeholder = "Why this label is right";
  note.value = finding.note || "";
  const noteLabel = document.createElement("label");
  noteLabel.textContent = "Note";
  noteLabel.append(note);

  for (const [value, text] of [
    ["true", "True hit"],
    ["false", "False hit"],
    ["unsure", "Unsure"],
    [null, "Clear"],
  ]) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    const selected = value !== null && finding.verdict === value;
    button.setAttribute("aria-pressed", String(selected));
    button.addEventListener("click", () => onVerdict(finding.key, value, note.value));
    verdicts.append(button);
  }

  const keys = document.createElement("p");
  keys.className = "keys";
  keys.textContent = "1 true · 2 false · 3 unsure · 0 clear";
  body.append(location, symbol, snippet, facts, verdicts, noteLabel, keys);
  node.append(heading, body);
  return note;
}
