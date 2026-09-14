const state = {
  jobs: [],
  selectedId: null,
  filter: "all",
  listSignature: "",
  detailSignature: "",
  detailRequest: 0,
  refreshing: false,
  hasLoaded: false,
  online: null,
};

const labels = {
  pending: "Pending",
  running: "Running",
  retry_pending: "Retry pending",
  succeeded: "Succeeded",
  dead: "Dead",
  cancelled: "Cancelled",
  failed: "Failed",
  lease_expired: "Lease expired",
};
const cancellable = new Set(["pending", "running", "retry_pending"]);
const descriptions = {
  echo: "Returns your message after a worker claims the job.",
  flaky: "Fails on attempt one, then succeeds after the retry delay.",
  sleep: "Runs for 25 seconds so you can request cancellation while it is active.",
  record_once: "Stores one uniquely keyed database effect, even if the handler is retried.",
};

const byId = (id) => document.getElementById(id);

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = String(content);
  return node;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

function chip(status) {
  const known = Object.hasOwn(labels, status);
  return element("span", `status-chip ${known ? `status-${status}` : "status-pending"}`, labels[status] || status);
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...options.headers } });
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* HTTP errors can have no JSON body. */ }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return response.json();
}

function announce(message, error = false) {
  const box = byId("announcement");
  box.textContent = message;
  box.classList.toggle("error", error);
  box.classList.add("visible");
  clearTimeout(announce.timer);
  announce.timer = setTimeout(() => box.classList.remove("visible"), error ? 12000 : 7000);
}

function setConnection(online) {
  if (state.online === online) return;
  state.online = online;
  byId("connection").classList.toggle("offline", !online);
  byId("connection-label").textContent = online ? "API connected" : "API unavailable";
}

function renderSummary() {
  const count = (statuses) => state.jobs.filter((job) => statuses.includes(job.status)).length;
  byId("count-pending").textContent = count(["pending"]);
  byId("count-running").textContent = count(["running"]);
  byId("count-succeeded").textContent = count(["succeeded"]);
  byId("count-attention").textContent = count(["retry_pending", "dead"]);
  byId("count-cancelled").textContent = count(["cancelled"]);
}

function renderList(force = false) {
  const visible = state.filter === "all" ? state.jobs : state.jobs.filter((job) => job.status === state.filter);
  const signature = JSON.stringify({
    filter: state.filter,
    selectedId: state.selectedId,
    rows: visible.map((job) => [job.id, job.status, job.attempt_count, job.updated_at]),
  });
  byId("list-count").textContent = `(${visible.length})`;
  if (!force && signature === state.listSignature) return;
  state.listSignature = signature;

  const list = byId("job-list");
  const focusedId = document.activeElement?.dataset?.jobId;
  list.replaceChildren();
  if (visible.length === 0) {
    const empty = element("p", "list-message");
    const title = element("strong", "", state.jobs.length ? "No jobs match this filter" : "No jobs yet");
    empty.append(title, document.createTextNode(state.jobs.length ? "Choose another status to see recent work." : "Create a demo job above to watch the queue come to life."));
    list.append(empty);
    return;
  }

  for (const job of visible) {
    const row = element("button", `job-row${job.id === state.selectedId ? " selected" : ""}`);
    row.type = "button";
    row.dataset.jobId = job.id;
    if (job.id === state.selectedId) row.setAttribute("aria-current", "true");
    row.setAttribute("aria-label", `${job.kind} job ${job.id}, ${labels[job.status] || job.status}`);
    const main = element("span", "job-main");
    main.append(element("span", "job-kind", job.kind), element("span", "job-id", job.id.slice(0, 12)));
    row.append(main, chip(job.status), element("span", "job-time", formatTime(job.created_at)));
    row.addEventListener("click", () => selectJob(job.id));
    list.append(row);
  }
  if (focusedId) {
    const replacement = [...list.querySelectorAll(".job-row")].find((row) => row.dataset.jobId === focusedId);
    replacement?.focus({ preventScroll: true });
  }
}

function selectJob(id) {
  state.selectedId = id;
  state.detailSignature = "";
  renderList();
  updateDetail(true);
  if (window.matchMedia("(max-width: 1050px)").matches) {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    byId("job-detail").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  }
}

function metaItem(list, name, value) {
  list.append(element("dt", "", name), element("dd", "", value == null || value === "" ? "—" : value));
}

function section(title, content) {
  const wrapper = element("section", "detail-section");
  wrapper.append(element("h4", "", title), content);
  return wrapper;
}

function jsonBlock(value) {
  return element("pre", "json-block", value == null ? "—" : JSON.stringify(value, null, 2));
}

function renderDetail(job, attempts) {
  const target = byId("job-detail");
  target.replaceChildren();
  const top = element("div", "detail-top");
  const title = element("div");
  title.append(element("h3", "", job.kind), element("span", "detail-id", job.id));
  top.append(title, chip(job.status));
  target.append(top);

  const actions = element("div", "detail-actions");
  if (cancellable.has(job.status)) {
    const cancel = element("button", "button button-danger", job.cancellation_requested_at ? "Cancellation requested" : "Cancel job");
    cancel.type = "button";
    cancel.disabled = Boolean(job.cancellation_requested_at);
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      cancel.textContent = "Requesting cancellation…";
      try {
        const result = await api(`/v1/jobs/${job.id}/cancel`, { method: "POST" });
        announce(result.status === "cancelled" ? "Job cancelled." : "Cancellation requested. The worker will stop at a safe checkpoint.");
        await refresh();
        byId("detail-title").focus();
      } catch (error) {
        cancel.disabled = false;
        cancel.textContent = "Cancel job";
        announce(`Could not cancel job: ${error.message}`, true);
      }
    });
    actions.append(cancel);
  }
  if (actions.childElementCount) target.append(actions);

  const meta = element("dl", "detail-meta");
  metaItem(meta, "Queue", job.queue);
  metaItem(meta, "Attempts", `${job.attempt_count} / ${job.max_attempts}`);
  metaItem(meta, "Created", formatTime(job.created_at));
  metaItem(meta, "Updated", formatTime(job.updated_at));
  metaItem(meta, "Lease owner", job.lease_owner);
  metaItem(meta, "Lease expires", formatTime(job.lease_expires_at));
  target.append(meta);
  target.append(section("Payload", jsonBlock(job.payload)));
  if (job.result != null) target.append(section("Result", jsonBlock(job.result)));
  if (job.last_error) target.append(section("Last error", element("p", "attempt-error", job.last_error)));

  const history = element("ol", "attempts");
  for (const attempt of attempts) {
    const item = element("li", "attempt");
    const body = element("div", "attempt-body");
    const line = element("div", "attempt-topline");
    line.append(chip(attempt.status), element("span", "job-time", formatTime(attempt.started_at)));
    body.append(line, element("p", "attempt-worker", `Worker: ${attempt.worker_id}`));
    if (attempt.error) body.append(element("p", "attempt-error", attempt.error));
    item.append(element("span", "attempt-number", attempt.attempt_number), body);
    history.append(item);
  }
  target.append(section("Attempt history", attempts.length ? history : element("p", "empty-attempts", "No worker has claimed this job yet.")));
}

async function updateDetail(showLoading = false) {
  if (!state.selectedId) return;
  const id = state.selectedId;
  const requestId = ++state.detailRequest;
  if (showLoading) byId("job-detail").replaceChildren(element("p", "list-message", "Loading job details…"));
  try {
    const [job, attemptResponse] = await Promise.all([
      api(`/v1/jobs/${id}`), api(`/v1/jobs/${id}/attempts`),
    ]);
    if (requestId !== state.detailRequest || id !== state.selectedId) return;
    const signature = JSON.stringify([job, attemptResponse.items]);
    if (signature !== state.detailSignature) {
      state.detailSignature = signature;
      renderDetail(job, attemptResponse.items);
    }
  } catch (error) {
    if (requestId !== state.detailRequest) return;
    byId("job-detail").replaceChildren(element("p", "list-message", `Could not load job details: ${error.message}. Select the job again to retry.`));
  }
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  byId("refresh-button").disabled = true;
  try {
    const ready = await api("/health/ready");
    if (ready.status !== "ready") throw new Error("Database is not ready");
    const response = await api("/v1/jobs?limit=200");
    state.jobs = response.items;
    state.hasLoaded = true;
    setConnection(true);
    renderSummary();
    renderList();
    byId("last-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    if (state.selectedId) await updateDetail();
  } catch (error) {
    setConnection(false);
    byId("last-updated").textContent = state.hasLoaded ? "Showing last known data" : "No data available";
    if (!state.hasLoaded) {
      byId("job-list").replaceChildren(element("p", "list-message", `Could not connect to the API: ${error.message}. Check that Docker Compose is running, then press Refresh.`));
    }
  } finally {
    state.refreshing = false;
    byId("refresh-button").disabled = false;
  }
}

function payloadFor(preset) {
  if (preset === "flaky") return { kind: "flaky", payload: { fail_attempts: 1 }, max_attempts: 3 };
  if (preset === "sleep") return { kind: "sleep", payload: { seconds: 25 } };
  if (preset === "record_once") return { kind: "record_once", payload: { business_key: `dashboard-${crypto.randomUUID()}`, value: { source: "dashboard" } } };
  return { kind: "echo", payload: { message: "Hello from the FaultLab console" } };
}

byId("preset").addEventListener("change", (event) => {
  byId("preset-description").textContent = descriptions[event.target.value];
});
byId("status-filter").addEventListener("change", (event) => {
  state.filter = event.target.value;
  renderList(true);
});
byId("refresh-button").addEventListener("click", refresh);
byId("create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("create-button");
  button.disabled = true;
  button.textContent = "Creating…";
  try {
    const created = await api("/v1/jobs", { method: "POST", body: JSON.stringify(payloadFor(byId("preset").value)) });
    announce(`${created.job.kind} job created. Watch its state and attempts below.`);
    state.jobs = [created.job, ...state.jobs.filter((job) => job.id !== created.job.id)].slice(0, 200);
    state.hasLoaded = true;
    state.filter = "all";
    byId("status-filter").value = "all";
    state.selectedId = created.job.id;
    state.detailSignature = "";
    renderSummary();
    renderList();
    await refresh();
    await updateDetail(true);
  } catch (error) {
    announce(`Could not create job: ${error.message}`, true);
  } finally {
    button.disabled = false;
    button.textContent = "Create job →";
  }
});

let refreshTimer;
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (document.hidden) return;
  refreshTimer = setTimeout(async () => {
    await refresh();
    scheduleRefresh();
  }, state.online ? 5000 : 15000);
}

refresh().finally(scheduleRefresh);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(refreshTimer);
  else refresh().finally(scheduleRefresh);
});
