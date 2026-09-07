"use strict";

const elements = {
  badge: document.getElementById("operation-badge"),
  statusMeta: document.getElementById("status-meta"),
  message: document.getElementById("message"),
  progress: document.getElementById("progress-list"),
  result: document.getElementById("operation-result"),
  typeFilter: document.getElementById("type-filter"),
  pathScope: document.getElementById("path-scope"),
  scoreOrder: document.getElementById("score-order"),
  alertCount: document.getElementById("alert-count"),
  alertsBody: document.getElementById("alerts-body"),
  refresh: document.getElementById("refresh-alerts"),
  selectVisible: document.getElementById("select-visible"),
  clearSelection: document.getElementById("clear-selection"),
  triageFromSelection: document.getElementById("triage-from-selection"),
};

let operationWasRunning = false;
const selectedAlertIds = new Set();

async function requestJSON(url, options = {}) {
  const response = await fetch(url, options);
  let value;
  try {
    value = await response.json();
  } catch (_error) {
    value = {error: `HTTP ${response.status}`};
  }
  if (!response.ok) {
    throw new Error(value.error || `HTTP ${response.status}`);
  }
  return value;
}

function showMessage(text) {
  elements.message.textContent = text || "";
  elements.message.hidden = !text;
}

function setBusy(busy) {
  document.querySelectorAll("button[data-operation]").forEach((button) => {
    button.disabled = busy;
  });
  elements.refresh.disabled = busy;
  elements.selectVisible.disabled = busy;
  elements.clearSelection.disabled = busy;
  elements.triageFromSelection.disabled = busy;
}

function setBadge(status) {
  const labels = {idle: "空闲", running: "运行中", succeeded: "成功", failed: "失败"};
  elements.badge.textContent = labels[status] || status;
  elements.badge.className = `badge badge-${status}`;
}

function renderOperation(state) {
  setBadge(state.status);
  setBusy(state.status === "running");
  if (state.status === "idle") {
    elements.statusMeta.textContent = "尚未执行操作";
  } else {
    const completed = state.completed_at ? `；完成于 ${state.completed_at}` : "";
    elements.statusMeta.textContent = `${state.kind}；开始于 ${state.started_at}${completed}`;
  }

  elements.progress.replaceChildren();
  (state.progress || []).forEach((event) => {
    const item = document.createElement("li");
    item.textContent = `[${event.phase || "progress"}] ${event.message || ""}`;
    elements.progress.appendChild(item);
  });
  if (elements.progress.lastElementChild) {
    elements.progress.scrollTop = elements.progress.scrollHeight;
  }

  showMessage(state.error || "");
  if (state.result !== null && state.result !== undefined) {
    elements.result.textContent = JSON.stringify(state.result, null, 2);
    elements.result.hidden = false;
  } else {
    elements.result.textContent = "";
    elements.result.hidden = true;
  }
}

async function loadProject() {
  const project = await requestJSON("/api/project");
  document.getElementById("project-config").textContent = project.config_path;
  document.getElementById("project-runtime").textContent = project.env_file || "—";
  document.getElementById("project-bitcode").textContent = project.bitcode_path;
  document.getElementById("project-source").textContent = project.source_dir;
  document.getElementById("project-artifact").textContent = project.artifact_dir;
}

function appendCell(row, value, className = "") {
  const cell = document.createElement("td");
  cell.className = className;
  if (value === null || value === undefined) {
    cell.textContent = "—";
  } else if (typeof value === "object") {
    cell.textContent = JSON.stringify(value);
  } else {
    cell.textContent = String(value);
  }
  row.appendChild(cell);
}

function renderAlerts(data) {
  elements.alertsBody.replaceChildren();
  const pathNote = data.path_scope === "project"
    ? `路径匹配 ${data.path_filtered}`
    : "未按路径过滤";
  elements.alertCount.textContent =
    `显示 ${data.filtered} / ${pathNote} / 共 ${data.total} 条；已勾选 ${selectedAlertIds.size}`;
  if (!data.alerts.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 10;
    cell.className = "empty-row";
    cell.textContent = "当前条件下没有告警";
    row.appendChild(cell);
    elements.alertsBody.appendChild(row);
    return;
  }
  data.alerts.forEach((alert) => {
    const row = document.createElement("tr");
    if (alert.suppressed) row.classList.add("suppressed");
    const selectCell = document.createElement("td");
    selectCell.className = "select-col";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.alertId = alert.alert_id;
    checkbox.checked = selectedAlertIds.has(alert.alert_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedAlertIds.add(alert.alert_id);
      else selectedAlertIds.delete(alert.alert_id);
      elements.alertCount.textContent =
        `显示 ${data.filtered} / ${pathNote} / 共 ${data.total} 条；已勾选 ${selectedAlertIds.size}`;
    });
    selectCell.appendChild(checkbox);
    row.appendChild(selectCell);
    appendCell(row, alert.alert_id);
    appendCell(row, alert.producer);
    appendCell(row, alert.type);
    appendCell(row, alert.content);
    appendCell(row, alert.graph_ids);
    appendCell(row, alert.suppressed);
    appendCell(row, alert.classifications);
    appendCell(row, alert.active_learning);
    appendCell(row, Number(alert.score).toFixed(6));
    elements.alertsBody.appendChild(row);
  });
}

async function loadAlerts() {
  const params = new URLSearchParams({
    type: elements.typeFilter.value,
    order: elements.scoreOrder.value,
    path_scope: elements.pathScope.value,
  });
  try {
    const data = await requestJSON(`/api/alerts?${params}`);
    renderAlerts(data);
  } catch (error) {
    elements.alertCount.textContent = "读取失败";
    showMessage(error.message);
  }
}

async function pollOperation() {
  try {
    const state = await requestJSON("/api/operations/current");
    const isRunning = state.status === "running";
    renderOperation(state);
    if (operationWasRunning && !isRunning) {
      await loadAlerts();
    }
    operationWasRunning = isRunning;
  } catch (error) {
    showMessage(error.message);
  }
}

async function submitOperation(kind, payload) {
  setBusy(true);
  showMessage("");
  operationWasRunning = true;
  try {
    const state = await requestJSON(`/api/operations/${kind}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    renderOperation(state);
  } catch (error) {
    operationWasRunning = false;
    setBusy(false);
    showMessage(error.message);
  }
}

async function resolveImportPath() {
  const typed = document.getElementById("import-xlsx-path").value.trim();
  const fileInput = document.getElementById("import-file");
  const file = fileInput.files && fileInput.files[0];
  if (file) {
    const body = new FormData();
    body.append("xlsx", file, file.name);
    const uploaded = await requestJSON("/api/uploads/xlsx", {method: "POST", body});
    return uploaded.xlsx_path;
  }
  if (typed) return typed;
  throw new Error("请选择 XLSX 文件或填写本机路径");
}

document.getElementById("import-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const weight = Number(document.getElementById("import-weight").value);
  if (!Number.isFinite(weight) || weight < 0 || weight > 1) {
    showMessage("initial weight 必须在 0 到 1 之间");
    return;
  }
  try {
    setBusy(true);
    const xlsxPath = await resolveImportPath();
    await submitOperation("import-xlsx", {
      xlsx_path: xlsxPath,
      producer: document.getElementById("import-producer").value.trim() || "tabular-sast",
      initial_weight: weight,
      mode: document.getElementById("import-mode").value,
      path_filter: document.getElementById("import-path-filter").checked,
    });
  } catch (error) {
    operationWasRunning = false;
    setBusy(false);
    showMessage(error.message);
  }
});

document.getElementById("analyze-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const checkers = [...document.querySelectorAll('input[name="checker"]:checked')]
    .map((input) => input.value);
  if (!checkers.length) {
    showMessage("至少选择一个 checker");
    return;
  }
  const newBaseline = document.getElementById("new-baseline").checked;
  if (newBaseline && !window.confirm("新基线会替换当前告警和图，确认继续？")) return;
  submitOperation("analyze", {checkers, new_baseline: newBaseline});
});

document.getElementById("triage-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const alertIds = document.getElementById("triage-alert-ids").value;
  const source = document.getElementById("triage-source").value.trim();
  if (!alertIds.trim()) {
    showMessage("至少输入一个 alert_id");
    return;
  }
  if (!source) {
    showMessage("classification source 不能为空");
    return;
  }
  submitOperation("triage", {
    alert_ids: alertIds,
    mode: document.getElementById("triage-mode").value,
    round_id: document.getElementById("triage-round-id").value,
    classification_source: source,
  });
});

document.getElementById("active-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const rounds = Number(document.getElementById("active-rounds").value);
  const feedback = document.getElementById("active-feedback").value;
  const model = document.getElementById("active-model").value.trim();
  if (!Number.isInteger(rounds) || rounds < 1) {
    showMessage("rounds 必须是正整数");
    return;
  }
  if (feedback === "none" && rounds !== 1) {
    showMessage("feedback=none 时 rounds 必须为 1");
    return;
  }
  if (!model) {
    showMessage("initial model 不能为空");
    return;
  }
  submitOperation("active-learning", {rounds, feedback, initial_model: model});
});

elements.triageFromSelection.addEventListener("click", () => {
  if (!selectedAlertIds.size) {
    showMessage("请先勾选告警");
    return;
  }
  document.getElementById("triage-alert-ids").value = [...selectedAlertIds].join("\n");
  document.getElementById("triage-mode").value = "classify";
  showMessage(`已填入 ${selectedAlertIds.size} 条告警 ID，可执行 Triage`);
});

elements.selectVisible.addEventListener("click", () => {
  elements.alertsBody.querySelectorAll('input[type="checkbox"][data-alert-id]').forEach((box) => {
    box.checked = true;
    selectedAlertIds.add(box.dataset.alertId);
  });
  loadAlerts();
});

elements.clearSelection.addEventListener("click", () => {
  selectedAlertIds.clear();
  loadAlerts();
});

elements.typeFilter.addEventListener("change", loadAlerts);
elements.pathScope.addEventListener("change", loadAlerts);
elements.scoreOrder.addEventListener("change", loadAlerts);
elements.refresh.addEventListener("click", loadAlerts);

Promise.all([loadProject(), loadAlerts(), pollOperation()]).catch((error) => {
  showMessage(error.message);
});
window.setInterval(pollOperation, 1000);
