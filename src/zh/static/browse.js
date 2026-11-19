/** Local history browser. Renders journal text as text and never runs commands. */

const searchInput = document.getElementById("search");
const statusSelect = document.getElementById("status");
const fromInput = document.getElementById("from");
const toInput = document.getElementById("to");
const results = document.getElementById("results");
const detail = document.getElementById("detail");
const notice = document.getElementById("notice");
const statsEl = document.getElementById("stats");
const moreButton = document.getElementById("more");
const form = document.getElementById("search-form");
const themeToggle = document.getElementById("theme-toggle");
const graph = document.getElementById("graph");
const rangeLabel = document.getElementById("range-label");
const listView = document.getElementById("list-view");
const graphView = document.getElementById("graph-view");
const listTab = document.getElementById("view-list");
const graphTab = document.getElementById("view-graph");
const timeButton = document.getElementById("time-button");
const timePopover = document.getElementById("time-popover");
const timeButtonLabel = document.getElementById("time-button-label");
const applyRangeButton = document.getElementById("apply-range");
const svgNs = "http://www.w3.org/2000/svg";

let offset = 0;
let selectedId = "";
let generation = 0;
let activePreset = "all";
let sinceDate = null;
let untilDate = null;
const pageSize = 50;

form.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});

searchInput.addEventListener("input", () => {
  window.clearTimeout(searchInput.timer);
  searchInput.timer = window.setTimeout(runSearch, 200);
});

statusSelect.addEventListener("change", runSearch);
timeButton.addEventListener("click", () => {
  const open = timePopover.hidden;
  if (open) {
    timePopover.hidden = false;
    timeButton.setAttribute("aria-expanded", "true");
  } else {
    closeTimePicker();
  }
});
applyRangeButton.addEventListener("click", applyAbsolute);
document.addEventListener("click", (event) => {
  if (timePopover.hidden) return;
  if (event.target.closest(".time-picker")) return;
  closeTimePicker();
});
for (const button of document.querySelectorAll(".preset")) {
  button.addEventListener("click", () => applyPreset(button.dataset.preset));
}
listTab.addEventListener("click", () => selectView("list"));
graphTab.addEventListener("click", () => selectView("graph"));
moreButton.addEventListener("click", () => {
  const ticket = generation;
  loadCommands(false, ticket, currentBounds()).catch(() => {
    if (ticket === generation) showError();
  });
});
themeToggle.addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme !== "dark";
  storeTheme(dark ? "dark" : "light");
  applyTheme(dark);
});

document.addEventListener("keydown", (event) => {
  if (event.altKey || event.metaKey || event.ctrlKey) return;
  if (
    event.target.classList.contains("tab") &&
    (event.key === "ArrowRight" || event.key === "ArrowLeft")
  ) {
    event.preventDefault();
    selectView(event.target === listTab ? "graph" : "list");
    (event.target === listTab ? graphTab : listTab).focus();
    return;
  }
  if (event.key === "/" && document.activeElement !== searchInput) {
    event.preventDefault();
    searchInput.focus();
    return;
  }
  if (event.key === "Escape") {
    if (!timePopover.hidden) {
      closeTimePicker();
      timeButton.focus();
      return;
    }
    searchInput.focus();
    return;
  }
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const options = [...results.querySelectorAll("[role='option']")];
  if (!options.length) return;
  const inSearch = document.activeElement === searchInput;
  const inList = results.contains(document.activeElement);
  if (!inSearch && !inList) return;
  event.preventDefault();
  const current = options.findIndex(
    (option) => option.getAttribute("aria-selected") === "true",
  );
  let next = event.key === "ArrowDown" ? current + 1 : current - 1;
  if (current === -1) next = event.key === "ArrowDown" ? 0 : options.length - 1;
  next = Math.max(0, Math.min(options.length - 1, next));
  options[next].focus();
  options[next].click();
});

initTheme();
updateRangeLabel();
runSearch();

function runSearch() {
  const ticket = ++generation;
  const range = currentBounds();
  const problem = rangeProblem(range);
  updateRangeLabel();
  if (problem) {
    notice.textContent = problem;
    results.replaceChildren();
    const empty = document.createElement("li");
    empty.className = "placeholder";
    empty.textContent = problem;
    results.append(empty);
    renderGraph(null, problem);
    return;
  }
  loadCommands(true, ticket, range).catch(() => {
    if (ticket === generation) showError();
  });
  loadStats(ticket, range);
  loadActivity(ticket, range);
}

/** Read the applied from/to instants. */
function currentBounds() {
  return { since: sinceDate, until: untilDate };
}

/** Reject a range whose start is later than its end. */
function rangeProblem(range) {
  if (range.since && range.until && range.since > range.until)
    return "The start is after the end.";
  return "";
}

/** Apply the calendar fields and close the time menu. */
function applyAbsolute() {
  const since = readInputDate(fromInput, false);
  const until = readInputDate(toInput, true);
  if (since && until && since > until) {
    notice.textContent = "The start is after the end.";
    return;
  }
  sinceDate = since;
  untilDate = until;
  activePreset = since || until ? "custom" : "all";
  markPresets();
  closeTimePicker();
  runSearch();
}

function closeTimePicker() {
  timePopover.hidden = true;
  timeButton.setAttribute("aria-expanded", "false");
  fromInput.value = sinceDate ? toLocalInput(sinceDate) : "";
  toInput.value = untilDate ? toLocalInput(untilDate) : "";
}

/** Apply a named range and show it in the calendar fields. */
function applyPreset(name) {
  const now = new Date();
  activePreset = name;
  if (name === "hour") {
    sinceDate = new Date(now.getTime() - 60 * 60 * 1000);
    untilDate = now;
  } else if (name === "hours24") {
    sinceDate = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    untilDate = now;
  } else if (name === "day") {
    const today = startOfLocalDay(now);
    sinceDate = new Date(today.getTime() - 24 * 60 * 60 * 1000);
    untilDate = new Date(today.getTime() - 1000);
  } else if (name === "week") {
    sinceDate = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000);
    untilDate = now;
  } else {
    activePreset = "all";
    sinceDate = null;
    untilDate = null;
  }
  fromInput.value = sinceDate ? toLocalInput(sinceDate) : "";
  toInput.value = untilDate ? toLocalInput(untilDate) : "";
  markPresets();
  closeTimePicker();
  runSearch();
}

function markPresets() {
  for (const button of document.querySelectorAll(".preset")) {
    button.setAttribute(
      "aria-pressed",
      button.dataset.preset === activePreset ? "true" : "false",
    );
  }
}

function selectView(name) {
  const showGraph = name === "graph";
  listView.hidden = showGraph;
  graphView.hidden = !showGraph;
  listTab.setAttribute("aria-selected", showGraph ? "false" : "true");
  graphTab.setAttribute("aria-selected", showGraph ? "true" : "false");
  listTab.tabIndex = showGraph ? -1 : 0;
  graphTab.tabIndex = showGraph ? 0 : -1;
}

function updateRangeLabel() {
  const pressed = document.querySelector(".preset[aria-pressed='true']");
  let label = "All time";
  if (pressed && activePreset !== "custom") label = pressed.textContent;
  else {
    const range = currentBounds();
    if (range.since && range.until) {
      label = `From ${formatTime(range.since.toISOString())} to ${formatTime(range.until.toISOString())}`;
    } else if (range.since) {
      label = `From ${formatTime(range.since.toISOString())}`;
    } else if (range.until) {
      label = `Until ${formatTime(range.until.toISOString())}`;
    }
  }
  rangeLabel.textContent = label;
  timeButtonLabel.textContent = label;
}

async function loadCommands(reset, ticket, range) {
  const requestOffset = reset ? 0 : offset;
  results.setAttribute("aria-busy", "true");
  notice.textContent = "Loading commands…";
  const params = filterParams(range);
  params.set("limit", String(pageSize));
  params.set("offset", String(requestOffset));
  const response = await fetch(`/api/commands?${params.toString()}`);
  if (ticket !== generation) return;
  if (!response.ok) throw new Error("commands");
  const body = await response.json();
  if (ticket !== generation) return;
  if (reset) results.replaceChildren();
  appendItems(body.items);
  offset = body.offset + body.items.length;
  moreButton.hidden = offset >= body.total;
  if (body.total === 0) {
    const empty = document.createElement("li");
    empty.className = "placeholder";
    empty.textContent = filtered()
      ? "No commands match this search."
      : "The journal is empty.";
    results.append(empty);
    notice.textContent = empty.textContent;
  } else {
    notice.textContent = `${body.total} matching commands.`;
  }
  results.setAttribute("aria-busy", "false");
}

async function loadStats(ticket, range) {
  try {
    const response = await fetch(
      `/api/stats?${filterParams(range).toString()}`,
    );
    if (ticket !== generation) return;
    if (!response.ok) throw new Error("stats");
    const stats = await response.json();
    if (ticket !== generation) return;
    statsEl.replaceChildren(
      statChip(stats.total, "commands", "all"),
      statChip(stats.failed, "failed", "failed"),
      statChip(stats.succeeded, "succeeded", "succeeded"),
      statChip(stats.running, "running", "running"),
      statChip(stats.unknown || 0, "no exit code", "unknown"),
    );
  } catch {
    if (ticket === generation) statsEl.textContent = "Stats unavailable.";
  }
}

async function loadActivity(ticket, range) {
  try {
    const response = await fetch(
      `/api/activity?${filterParams(range).toString()}`,
    );
    if (ticket !== generation) return;
    if (!response.ok) throw new Error("activity");
    const activity = await response.json();
    if (ticket !== generation) return;
    renderGraph(activity, "");
  } catch {
    if (ticket === generation) renderGraph(null, "Could not load the graph.");
  }
}

function filterParams(range) {
  const params = new URLSearchParams({
    q: searchInput.value,
    status: statusSelect.value,
  });
  if (range.since) params.set("since", range.since.toISOString());
  if (range.until) params.set("until", range.until.toISOString());
  return params;
}

function filtered() {
  const range = currentBounds();
  return Boolean(
    searchInput.value.trim() ||
    statusSelect.value !== "all" ||
    range.since ||
    range.until,
  );
}

function statChip(value, label, tone) {
  const item = document.createElement("span");
  item.className = "stat";
  item.dataset.tone = tone;
  const number = document.createElement("strong");
  number.textContent = String(value);
  const name = document.createElement("span");
  name.textContent = label;
  item.append(number, name);
  return item;
}

function appendItems(items) {
  for (const item of items) {
    const row = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "row";
    button.role = "option";
    button.dataset.id = item.id;
    button.setAttribute(
      "aria-selected",
      item.id === selectedId ? "true" : "false",
    );

    const status = badgeClass(item);
    button.dataset.status = status;

    const command = document.createElement("span");
    command.className = "command";
    const prompt = document.createElement("span");
    prompt.className = "prompt";
    prompt.setAttribute("aria-hidden", "true");
    prompt.textContent = "$";
    const commandText = document.createElement("span");
    commandText.className = "command-text";
    commandText.textContent = item.command;
    command.append(prompt, commandText);

    const meta = document.createElement("span");
    meta.className = "meta";
    const badge = document.createElement("span");
    badge.className = `badge ${status}`;
    badge.textContent = badgeLabel(item);
    const origin = document.createElement("span");
    origin.className = "origin";
    origin.textContent = formatOrigin(item);
    const where = document.createElement("span");
    where.className = "cwd";
    where.textContent = item.cwd || "Unknown directory";
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = formatTime(item.started_at);
    meta.append(badge, origin, where, when);
    if (item.duration_ms !== null && item.duration_ms !== undefined) {
      const duration = document.createElement("span");
      duration.className = "duration";
      duration.textContent = formatDuration(item);
      meta.append(duration);
    }
    button.append(command, meta);
    button.addEventListener("click", () => selectCommand(item.id, button));
    row.append(button);
    results.append(row);
  }
}

async function selectCommand(id, button) {
  selectedId = id;
  for (const option of results.querySelectorAll("[role='option']")) {
    option.setAttribute("aria-selected", option === button ? "true" : "false");
  }
  const response = await fetch(`/api/commands/${encodeURIComponent(id)}`);
  if (response.status === 404) {
    renderMessage("That command is not in the journal.");
    return;
  }
  if (!response.ok) {
    showError();
    return;
  }
  renderDetail(await response.json());
}

function renderDetail(item) {
  detail.replaceChildren();
  const head = document.createElement("div");
  head.className = "detail-head";
  const heading = document.createElement("h2");
  heading.textContent = "Details";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.id = "copy-command";
  copy.className = "ghost";
  copy.textContent = "Copy command";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(item.command);
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Copy unavailable";
    }
  });
  head.append(heading, copy);
  const command = document.createElement("pre");
  command.id = "command-text";
  command.textContent = item.command;
  const terminal = document.createElement("div");
  terminal.className = "terminal";
  const bar = document.createElement("p");
  bar.className = "terminal-bar";
  bar.textContent = "command";
  terminal.append(bar, command);
  const list = document.createElement("dl");
  addPair(list, "Exit code", exitText(item));
  addPair(list, "Started", formatTime(item.started_at));
  addPair(
    list,
    "Ended",
    item.ended_at ? formatTime(item.ended_at) : "Still running",
  );
  addPair(list, "Duration", formatDuration(item));
  addPair(list, "Directory", item.cwd || "Unknown");
  addPair(list, "Host", item.hostname || "Unknown");
  addPair(list, "User", item.username || "Unknown");
  addPair(list, "Session", item.session_id || "None");
  addPair(list, "Id", item.id);
  detail.append(head, terminal, list);
}

function renderMessage(message) {
  detail.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = "Details";
  const paragraph = document.createElement("p");
  paragraph.className = "detail-empty";
  paragraph.textContent = message;
  detail.append(heading, paragraph);
}

function formatOrigin(item) {
  const user = item.username || "";
  const host = item.hostname || "";
  if (user && host) return `${user}@${host}`;
  if (user || host) return user || host;
  return "Unknown machine";
}

function addPair(list, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  list.append(term, description);
}

function showError() {
  results.replaceChildren();
  const empty = document.createElement("li");
  empty.className = "placeholder";
  empty.textContent = "Could not load the journal.";
  results.append(empty);
  results.setAttribute("aria-busy", "false");
  notice.textContent = "Could not load the journal.";
}

/** Draw command counts as stacked bars and a text table. */
function renderGraph(activity, message) {
  graph.replaceChildren();
  if (!activity || !activity.points || activity.points.length === 0) {
    const empty = document.createElement("p");
    empty.className = "placeholder";
    empty.textContent = message || "No commands in this range.";
    graph.append(empty);
    return;
  }
  const points = activity.points;
  const totals = points.map((point) => pointTotal(point));
  const peak = Math.max(1, ...totals);
  const grand = totals.reduce((sum, value) => sum + value, 0);
  const summary = document.createElement("p");
  summary.className = "graph-summary";
  summary.textContent = `${grand} commands from ${formatTime(activity.since)} to ${formatTime(activity.until)}.`;
  graph.append(summary, legend(), chart(points, peak, activity.step_seconds));
  const table = graphTable(points);
  if (table) {
    const details = document.createElement("details");
    details.className = "intervals";
    const summaryRow = document.createElement("summary");
    summaryRow.textContent = "Intervals";
    details.append(summaryRow, table);
    graph.append(details);
  }
}

function legend() {
  const list = document.createElement("p");
  list.className = "legend";
  list.append(
    swatch("Succeeded", "ok"),
    swatch("Failed", "failed"),
    swatch("Running", "running"),
    swatch("No exit code", "unknown"),
  );
  return list;
}

function swatch(label, tone) {
  const item = document.createElement("span");
  const mark = document.createElement("span");
  mark.className = `swatch ${tone}`;
  mark.setAttribute("aria-hidden", "true");
  item.append(mark, document.createTextNode(label));
  return item;
}

/** Draw a fixed-height time series. A single bucket stays a narrow bar. */
function chart(points, peak, stepSeconds) {
  const axis = 40;
  const plotTop = 12;
  const plotHeight = 168;
  const height = plotTop + plotHeight + 28;
  const host = Math.max(640, graph.getBoundingClientRect().width - 32);
  const minSlot = 28;
  const plotWidth = Math.max(host - axis - 12, points.length * minSlot);
  const slot = plotWidth / points.length;
  const width = axis + plotWidth + 12;
  const scroll = document.createElement("div");
  scroll.className = "graph-scroll";
  const svg = document.createElementNS(svgNs, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(height));
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
  if (width > host) svg.style.minWidth = `${width}px`;
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Commands started over time");
  const ticks = yTicks(peak);
  const base = plotTop + plotHeight - 8;
  for (const value of ticks) {
    const y = base - (value / peak) * (plotHeight - 16);
    svg.append(
      svgEl("line", {
        x1: String(axis),
        y1: String(y),
        x2: String(width - 8),
        y2: String(y),
        stroke: "currentColor",
        "stroke-opacity": "0.16",
      }),
    );
    const tick = document.createElementNS(svgNs, "text");
    tick.setAttribute("x", String(axis - 8));
    tick.setAttribute("y", String(y + 4));
    tick.setAttribute("text-anchor", "end");
    tick.setAttribute("fill", "currentColor");
    tick.setAttribute("fill-opacity", "0.7");
    tick.setAttribute("font-size", "11");
    tick.setAttribute("font-family", "inherit");
    tick.textContent = String(value);
    svg.append(tick);
  }
  points.forEach((point, index) => {
    const total = pointTotal(point);
    const barWidth = Math.min(32, Math.max(8, slot - 14));
    const x = axis + index * slot + (slot - barWidth) / 2;
    let y = base;
    const group = document.createElementNS(svgNs, "g");
    const title = document.createElementNS(svgNs, "title");
    title.textContent = `${formatBucket(point.start, stepSeconds)}: ${point.succeeded} succeeded, ${point.failed} failed, ${point.running} running, ${point.unknown || 0} with no exit code`;
    group.append(title);
    for (const [key, tone] of [
      ["succeeded", "var(--ok)"],
      ["failed", "var(--failed)"],
      ["running", "var(--running)"],
      ["unknown", "var(--muted)"],
    ]) {
      const value = point[key];
      if (!value) continue;
      const barHeight = Math.max(
        2,
        Math.round((value / peak) * (plotHeight - 16)),
      );
      y -= barHeight;
      group.append(
        svgEl("rect", {
          x: String(x),
          y: String(y),
          width: String(barWidth),
          height: String(barHeight),
          rx: "2",
          fill: tone,
        }),
      );
    }
    if (total === 0) {
      group.append(
        svgEl("line", {
          x1: String(x),
          y1: String(base),
          x2: String(x + barWidth),
          y2: String(base),
          stroke: "currentColor",
          "stroke-opacity": "0.25",
        }),
      );
    }
    if (shouldLabel(index, points.length)) {
      const label = document.createElementNS(svgNs, "text");
      label.setAttribute("x", String(x + barWidth / 2));
      label.setAttribute("y", String(height - 8));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("fill", "currentColor");
      label.setAttribute("fill-opacity", "0.75");
      label.setAttribute("font-size", "11");
      label.textContent = formatBucket(point.start, stepSeconds);
      group.append(label);
    }
    svg.append(group);
  });
  scroll.append(svg);
  return scroll;
}

/** Integer axis labels from zero up to the tallest bar. */
function yTicks(peak) {
  const steps = peak <= 4 ? peak : 4;
  const ticks = [];
  for (let step = 0; step <= steps; step += 1) {
    ticks.push(Math.round((peak * step) / steps));
  }
  return [...new Set(ticks)].reverse();
}

function shouldLabel(index, count) {
  if (count <= 8) return true;
  return index === 0 || index === count - 1 || index === Math.floor(count / 2);
}

function graphTable(points) {
  const filled = points.filter(
    (point) =>
      point.succeeded || point.failed || point.running || point.unknown,
  );
  if (!filled.length) return null;
  const wrap = document.createElement("div");
  wrap.className = "graph-table-wrap";
  const table = document.createElement("table");
  table.className = "graph-table";
  const caption = document.createElement("caption");
  caption.textContent = "Commands in each interval";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const label of [
    "Start",
    "Succeeded",
    "Failed",
    "Running",
    "No exit code",
  ]) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = label;
    headRow.append(cell);
  }
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const point of filled) {
    const row = document.createElement("tr");
    for (const value of [
      formatTime(point.start),
      point.succeeded,
      point.failed,
      point.running,
      point.unknown || 0,
    ]) {
      const cell = document.createElement("td");
      cell.textContent = String(value);
      row.append(cell);
    }
    body.append(row);
  }
  table.append(caption, head, body);
  wrap.append(table);
  return wrap;
}

function svgEl(name, attributes) {
  const element = document.createElementNS(svgNs, name);
  for (const [key, value] of Object.entries(attributes))
    element.setAttribute(key, value);
  return element;
}

function exitText(item) {
  if (item.exit_code === null || item.exit_code === undefined) {
    return item.ended_at ? "No exit code" : "Running";
  }
  return String(item.exit_code);
}

function badgeClass(item) {
  const exitCode = item.exit_code;
  if (exitCode === null || exitCode === undefined) {
    return item.ended_at ? "unknown" : "running";
  }
  if (exitCode === 0) return "ok";
  return "failed";
}

function badgeLabel(item) {
  const exitCode = item.exit_code;
  if (exitCode === null || exitCode === undefined) {
    return item.ended_at ? "No exit code" : "Running";
  }
  if (exitCode === 0) return "Exit 0";
  return `Exit ${exitCode}`;
}

function pointTotal(point) {
  return point.succeeded + point.failed + point.running + (point.unknown || 0);
}

function formatTime(value) {
  if (!value) return "Unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

function formatBucket(value, stepSeconds) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  if (stepSeconds < 86400) {
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    }).format(date);
  }
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
  }).format(date);
}

function formatDuration(item) {
  const durationMs = item.duration_ms;
  const exitCode = item.exit_code;
  if (durationMs === null || durationMs === undefined) {
    if (item.ended_at) return "Unknown";
    return exitCode === null || exitCode === undefined
      ? "In progress"
      : "Unknown";
  }
  if (durationMs < 1000) return `${durationMs} ms`;
  if (durationMs < 60000) return `${(durationMs / 1000).toFixed(1)} s`;
  const minutes = Math.floor(durationMs / 60000);
  const seconds = Math.floor((durationMs % 60000) / 1000);
  return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
}

function readInputDate(input, endOfMinute) {
  if (!input.value) return null;
  const date = new Date(input.value);
  if (Number.isNaN(date.getTime())) return null;
  if (endOfMinute && input.value.length === 16) date.setSeconds(59, 0);
  return date;
}

function toLocalInput(date) {
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function startOfLocalDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function initTheme() {
  const stored = readTheme();
  const dark = stored
    ? stored === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(dark);
}

/** Apply the saved theme and show the icon for that theme. */
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  themeToggle.setAttribute("aria-pressed", dark ? "true" : "false");
  themeToggle.setAttribute("aria-label", dark ? "Dark theme" : "Light theme");
}

function readTheme() {
  try {
    return window.localStorage.getItem("zh-theme");
  } catch {
    return null;
  }
}

function storeTheme(value) {
  try {
    window.localStorage.setItem("zh-theme", value);
  } catch {
    return;
  }
}
