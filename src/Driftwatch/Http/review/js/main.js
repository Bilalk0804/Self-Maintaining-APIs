/** Wire the review desk page. */

import { loadBoard, saveVerdict } from "./api.js";
import { createFilters } from "./filters.js";
import { renderBanner, renderDetail, renderList, renderSummary } from "./render.js";
import { startWatch } from "./watch.js";

const filters = createFilters();
const summary = document.querySelector("#summary");
const banner = document.querySelector("#banner");
const list = document.querySelector("#list");
const detail = document.querySelector("#detail");
const form = document.querySelector("#filters");

const page = {
  board: null,
  selectedKey: null,
};

function selectedFinding(findings) {
  return findings.find((finding) => finding.key === page.selectedKey) ?? null;
}

function paint() {
  if (!page.board) return;
  const findings = filters.apply(page.board.findings);
  if (page.selectedKey && !page.board.findings.some((finding) => finding.key === page.selectedKey)) {
    page.selectedKey = null;
  }
  const emptyDirectory = page.board.summary.findings === 0 && !page.board.files.some((file) => file.error);
  renderSummary(summary, page.board);
  renderBanner(banner, page.board);
  renderList(list, findings, page.selectedKey, (key) => {
    page.selectedKey = key;
    paint();
  }, emptyDirectory);
  renderDetail(detail, selectedFinding(page.board.findings), (key, verdict, note) => {
    void label(key, verdict, note);
  });
}

async function refresh() {
  page.board = await loadBoard();
  paint();
}

async function label(key, verdict, note) {
  page.board = await saveVerdict(key, verdict, note);
  page.selectedKey = key;
  paint();
}

function shortcutVerdict(keyName) {
  const map = { Digit1: "true", Digit2: "false", Digit3: "unsure", Digit0: null };
  if (!(keyName in map) || !page.selectedKey) return;
  const note = detail.querySelector(".note");
  void label(page.selectedKey, map[keyName], note ? note.value : "");
}

filters.bind(form, () => {
  if (page.board) paint();
});

document.querySelector("#reload").addEventListener("click", () => {
  refresh().catch(showFailure);
});

document.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) {
    return;
  }
  if (["Digit0", "Digit1", "Digit2", "Digit3"].includes(event.code)) {
    event.preventDefault();
    shortcutVerdict(event.code);
  }
});

function showFailure(error) {
  banner.hidden = false;
  banner.textContent = error instanceof Error ? error.message : "The review desk failed to load.";
}

startWatch(document.querySelector("#watch-status"), document.querySelector("#watch-list"));
refresh().catch(showFailure);
