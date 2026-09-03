"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const ACTIVE = new Set(["queued", "running", "waiting"]);
  const LABELS = { lichess: "Lichess", "chess.com": "Chess.com" };
  const MODE_DEFAULTS = {
    lichess: ["blitz", "rapid", "classical", "correspondence"],
    "chess.com": ["blitz", "rapid", "correspondence"],
  };
  const MODE_FALLBACKS = {
    lichess: ["ultraBullet", "bullet", "blitz", "rapid", "classical", "correspondence", "chess960", "crazyhouse", "antichess", "atomic", "horde", "kingOfTheHill", "racingKings", "threeCheck"],
    "chess.com": ["correspondence", "rapid", "blitz", "bullet"],
  };
  const STATUS_LABELS = {
    queued: "Queued", running: "Downloading", completed: "Completed",
    failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted",
  };
  const VIEW_COPY = {
    download: ["YOUR GAMES, ONE PLACE", "Download games", "Bring your online games to your local chess library."],
    history: ["YOUR LOCAL COLLECTION", "Download history", "Every download, and the games that came with it."],
    settings: ["MAKE YOURSELF AT HOME", "Settings", "A few preferences for your local workspace."],
  };
  const state = { bootstrap: null, jobs: [], server: "lichess", modes: {}, selectedJobId: null, native: false, polling: null, retryTicker: null, sending: false, initialized: false, theme: "light", themeSaving: false, clearingHistory: false, jobsRevision: 0 };
  let toastTimer;
  let jobsSignature = "";
  let token = "";

  try {
    const params = new URLSearchParams(location.hash.slice(1));
    token = params.get("token") || sessionStorage.getItem("pgn-app-token") || "";
    if (token) sessionStorage.setItem("pgn-app-token", token);
    if (params.has("token")) history.replaceState(null, "", location.pathname + location.search);
  } catch (_) {
    token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
    if (token) history.replaceState(null, "", location.pathname + location.search);
  }

  function icon(name) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    element.classList.add("icon");
    element.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    element.append(use);
    return element;
  }

  function element(tag, className, text) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  }

  function actionButton(label, iconName, onClick, className = "secondary small", nativeOnly = false) {
    const button = element("button", `button ${className}`);
    button.type = "button";
    if (iconName) button.append(icon(iconName));
    button.append(document.createTextNode(label));
    button.addEventListener("click", onClick);
    if (nativeOnly) {
      button.dataset.native = "true";
      button.disabled = !state.native;
      if (!state.native) button.title = "Open files and folders in the Windows app.";
    }
    return button;
  }

  function errorMessage(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map((entry) => {
      if (typeof entry === "string") return entry;
      const field = entry.loc ? entry.loc.filter((part) => part !== "body").join(" ") : "";
      return [field, entry.msg || "Invalid value."].filter(Boolean).join(": ");
    }).join(" ");
    return value?.message || "Something went wrong. Please try again.";
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    let response;
    try {
      response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", "X-App-Token": token, ...options.headers },
        signal: controller.signal,
      });
    } catch (error) {
      if (error.name === "AbortError") throw new Error("The local app took too long to respond. Please retry.");
      throw new Error("The local app is unavailable. Keep the app running and try again.");
    } finally {
      clearTimeout(timeout);
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new Error("This window is no longer connected to the app. Close it and launch PGN Downloader again.");
      throw new Error(errorMessage(data.detail || data.error || data.message || `Request failed (${response.status}).`));
    }
    return data;
  }

  function showError(id, message) {
    $(id).textContent = message || "";
    $(id).hidden = !message;
  }

  function connectionError(message) {
    $("connection-error-text").textContent = message || "";
    $("connection-error").hidden = !message;
  }

  function toast(message, isError = false) {
    clearTimeout(toastTimer);
    $("toast").textContent = message;
    $("toast").className = `notice ${isError ? "error-notice" : "success-notice"}`;
    $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 5500);
  }

  function applyTheme(theme) {
    state.theme = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = state.theme;
    const dark = state.theme === "dark";
    $("theme-toggle").setAttribute("aria-pressed", String(dark));
    $("theme-toggle").setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} mode`);
    $("theme-label").textContent = dark ? "Light mode" : "Dark mode";
    $("theme-icon").setAttribute("href", dark ? "#i-sun" : "#i-moon");
  }

  async function toggleTheme() {
    if (!state.initialized || state.themeSaving) return;
    const previous = state.theme;
    state.themeSaving = true;
    $("theme-toggle").disabled = true;
    applyTheme(previous === "dark" ? "light" : "dark");
    try {
      const settings = await api("/api/appearance", { method: "PUT", body: JSON.stringify({ theme: state.theme }) });
      state.bootstrap.settings.theme = settings.theme;
      applyTheme(settings.theme);
    } catch (error) {
      applyTheme(previous);
      toast(`Could not save the appearance. ${error.message}`, true);
    } finally {
      state.themeSaving = false;
      $("theme-toggle").disabled = false;
    }
  }

  function clearForm() {
    if (!state.initialized || state.sending) return;
    $("username").value = "";
    Object.keys(LABELS).forEach((server) => { state.modes[server] = []; });
    renderModes(state.server);
    showError("form-error", "");
    $("username").focus();
    toast("Username and all game types cleared.");
  }

  function updateHistoryControls() {
    const busy = hasActiveJob() || state.sending;
    $("clear-history").disabled = !state.initialized || !state.jobs.length || busy || state.clearingHistory;
    if (busy) $("clear-history").title = "Finish or cancel the current download before clearing history.";
    else $("clear-history").removeAttribute("title");
    $("confirm-clear-history").disabled = busy || state.clearingHistory;
    $("cancel-clear-history").disabled = state.clearingHistory;
  }

  function askClearHistory() {
    if (!state.initialized || !state.jobs.length || hasActiveJob() || state.sending || state.clearingHistory) return;
    showError("clear-history-error", "");
    updateHistoryControls();
    $("clear-history-dialog").showModal();
  }

  async function clearHistory() {
    if (!$("clear-history-dialog").open || !state.initialized || hasActiveJob() || state.sending || state.clearingHistory) return;
    state.clearingHistory = true;
    state.jobsRevision += 1;
    clearTimeout(state.polling);
    updateDownloadButton();
    showError("clear-history-error", "");
    try {
      const result = await api("/api/jobs", { method: "DELETE" });
      state.jobsRevision += 1;
      state.selectedJobId = null;
      $("history-search").value = "";
      setJobs(result.jobs || []);
      $("clear-history-dialog").close();
      toast(`Cleared ${count(result.cleared)} download ${result.cleared === 1 ? "entry" : "entries"}. Your PGN files are unchanged.`);
    } catch (error) { showError("clear-history-error", error.message); }
    finally {
      state.clearingHistory = false;
      updateDownloadButton();
      schedulePoll();
    }
  }

  function setView(view) {
    if (!VIEW_COPY[view]) return;
    document.querySelectorAll(".view").forEach((panel) => { panel.hidden = panel.id !== `view-${view}`; });
    document.querySelectorAll("[data-view]").forEach((button) => {
      const active = button.dataset.view === view;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    [$("page-eyebrow").textContent, $("page-title").textContent, $("page-subtitle").textContent] = VIEW_COPY[view];
    document.title = `${VIEW_COPY[view][1]} · PGN Downloader`;
  }

  function niceMode(mode) {
    const labels = { ultraBullet: "UltraBullet", ultrabullet: "UltraBullet", kingOfTheHill: "King of the Hill", threeCheck: "Three-check", racingKings: "Racing Kings", chess960: "Chess960" };
    return labels[mode] || mode.charAt(0).toUpperCase() + mode.slice(1);
  }

  function rememberModes() {
    state.modes[state.server] = Array.from(document.querySelectorAll("#mode-options input:checked"), (input) => input.value);
  }

  function renderModes(server) {
    const available = state.bootstrap?.modes?.[server] || MODE_FALLBACKS[server];
    const selected = state.modes[server] || MODE_DEFAULTS[server];
    const items = available.map((mode) => {
      const label = element("label", "mode-option");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "mode";
      input.value = mode;
      input.checked = selected.includes(mode);
      input.addEventListener("change", rememberModes);
      label.append(input, element("span", "", niceMode(mode)));
      return label;
    });
    $("mode-options").replaceChildren(...items);
  }

  function setServer(server, remember = true) {
    if (!LABELS[server]) server = "lichess";
    if (remember) rememberModes();
    state.server = server;
    document.querySelectorAll("input[name=server]").forEach((input) => { input.checked = input.value === server; });
    $("username").placeholder = `Enter a ${LABELS[server]} username`;
    renderModes(server);
  }

  function displaySize(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function dateTime(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return "";
    return date.toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function count(value) { return (Number(value) || 0).toLocaleString(); }

  function statusLabel(job) {
    if (job.status === "waiting") return `Waiting for ${LABELS[job.server] || job.server}`;
    return STATUS_LABELS[job.status] || job.status;
  }

  function statusBadge(job) {
    return element("span", `status-badge status-${job.status}`, statusLabel(job));
  }

  function hasActiveJob() {
    return state.jobs.some((job) => ACTIVE.has(job.status));
  }

  function updateDownloadButton() {
    const busy = hasActiveJob();
    $("download-button").disabled = !state.initialized || state.sending || busy || state.clearingHistory;
    $("clear-form").disabled = !state.initialized || state.sending;
    $("download-button").querySelector("span").textContent = state.sending ? "Starting…" : "Download games";
    if (busy) $("download-button").title = "Wait for the current download to finish, or cancel it, before starting another.";
    else $("download-button").removeAttribute("title");
    updateHistoryControls();
  }

  function folderOf(path) {
    const index = Math.max(path.lastIndexOf("\\"), path.lastIndexOf("/"));
    return index >= 0 ? path.slice(0, index + 1) : "";
  }

  async function openPath(path) {
    if (!path) return;
    if (!state.native) { toast("Open files and folders from the Windows app."); return; }
    try {
      const result = await window.pywebview.api.open_path(path);
      if (result && result.ok === false) throw new Error(result.error || "This file or folder could not be opened.");
    } catch (error) { toast(error.message || "This file or folder could not be opened.", true); }
  }

  async function browse(inputId) {
    if (!state.native) return;
    const button = document.querySelector(`[data-browse="${inputId}"]`);
    button.disabled = true;
    try {
      const result = await window.pywebview.api.choose_folder($(inputId).value);
      if (typeof result === "string" && result) $(inputId).value = result;
    } catch (error) { toast(error.message || "The folder picker could not be opened.", true); }
    finally { button.disabled = !state.native; }
  }

  function enableNative() {
    state.native = Boolean(window.pywebview?.api?.choose_folder && window.pywebview?.api?.open_path);
    document.querySelectorAll("[data-browse], [data-native]").forEach((button) => {
      button.disabled = !state.native;
      if (state.native) button.removeAttribute("title");
    });
    $("open-data-dir").disabled = !state.native || !state.bootstrap?.paths?.data_dir;
  }

  function useJob(job) {
    if (!state.initialized) return;
    rememberModes();
    state.modes[job.server] = [...(job.modes || MODE_DEFAULTS[job.server] || [])];
    setServer(job.server, false);
    $("username").value = job.username || "";
    $("color").value = job.color || "";
    $("since").value = job.since || "";
    $("until").value = job.until || "";
    $("output-dir").value = job.output_dir || state.bootstrap.settings.output_dir || "";
    $("filename").value = job.filename || "";
    showError("form-error", "");
    setView("download");
    $("username").focus();
    toast("Download options filled in. Adjust them or start a new download.");
  }

  async function cancelJob(job, button) {
    if (button) button.disabled = true;
    try {
      const updated = await api(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
      upsertJob(updated);
    } catch (error) {
      connectionError(error.message);
      if (button) button.disabled = false;
    }
  }

  function jobActions(job, small = true) {
    const actions = [];
    const secondary = small ? "secondary small" : "secondary";
    if (job.status === "completed" && job.output_path) {
      actions.push(actionButton("Open PGN", "file", () => openPath(job.output_path), small ? "primary small" : "primary", true));
      actions.push(actionButton("Folder", "folder", () => openPath(job.output_dir || folderOf(job.output_path)), secondary, true));
    }
    if (["failed", "cancelled", "interrupted"].includes(job.status) && job.partial_path) {
      actions.push(actionButton("Folder", "folder", () => openPath(folderOf(job.partial_path)), secondary, true));
    }
    if (ACTIVE.has(job.status)) {
      const button = actionButton("Cancel", "close", () => cancelJob(job, button), small ? "danger small" : "danger");
      actions.push(button);
    } else {
      actions.push(actionButton("Reuse", "reuse", () => useJob(job), secondary));
    }
    return actions;
  }

  function activityJob() {
    return state.jobs.find((item) => item.id === state.selectedJobId) || state.jobs.find((item) => ACTIVE.has(item.status)) || state.jobs[0];
  }

  function renderRetryCountdown() {
    clearTimeout(state.retryTicker);
    state.retryTicker = null;
    const job = activityJob();
    const visible = job?.status === "waiting" && Number.isFinite(job.retry_at) && job.retry_at > 0;
    $("activity-retry").hidden = !visible;
    if (!visible) {
      $("activity-retry").textContent = "";
      return;
    }
    const remaining = Math.max(0, Math.ceil(job.retry_at - Date.now() / 1000));
    $("activity-retry").textContent = `Retrying in ${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;
    state.retryTicker = setTimeout(renderRetryCountdown, 1000);
  }

  function renderActivity() {
    const job = activityJob();
    $("activity-empty").hidden = Boolean(job);
    $("activity-detail").hidden = !job;
    $("activity-indicator").hidden = !job || !ACTIVE.has(job.status);
    $("activity-indicator").classList.toggle("waiting-indicator", job?.status === "waiting");
    renderRetryCountdown();
    if (!job) return;
    $("activity-avatar").textContent = (job.username || "?").slice(0, 1).toUpperCase();
    $("activity-username").textContent = job.username;
    $("activity-source").textContent = `${LABELS[job.server] || job.server} · ${job.color ? `${niceMode(job.color)} pieces` : "Either color"}`;
    $("activity-status").className = `status-badge status-${job.status}`;
    $("activity-status").textContent = statusLabel(job);
    $("activity-time").textContent = job.created_at ? new Date(job.created_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : "";
    $("activity-progress").hidden = !["queued", "running"].includes(job.status);
    $("activity-games").textContent = count(job.games);
    $("activity-size").textContent = displaySize(job.bytes);
    const message = job.error || job.message || (job.status === "completed" ? "Your PGN collection is ready." : "Waiting for an update…");
    if ($("activity-message").textContent !== message) $("activity-message").textContent = message;
    $("activity-message").classList.toggle("error-text", Boolean(job.error));
    const path = job.status === "completed" ? job.output_path || "" : job.partial_path || "";
    $("activity-path").textContent = path && job.status !== "completed" ? `Partial file: ${path}` : path;
    $("activity-path").hidden = !path;
    const actionSignature = `${job.id}:${job.status}:${job.output_path || ""}:${job.partial_path || ""}:${state.native}`;
    if ($("activity-actions").dataset.signature !== actionSignature) {
      $("activity-actions").replaceChildren(...jobActions(job, true));
      $("activity-actions").dataset.signature = actionSignature;
    }
  }

  function renderHistory() {
    const query = $("history-search").value.trim().toLowerCase();
    const filtered = state.jobs.filter((job) => [job.username, job.server, job.filename, job.output_path, job.status].some((value) => String(value || "").toLowerCase().includes(query)));
    $("history-count").textContent = count(state.jobs.length);
    $("history-count").hidden = state.jobs.length === 0;
    $("stat-downloads").textContent = count(state.jobs.length);
    const completed = state.jobs.filter((job) => job.status === "completed");
    $("stat-completed").textContent = count(completed.length);
    $("stat-games").textContent = count(completed.reduce((sum, job) => sum + (Number(job.games) || 0), 0));
    $("history-empty").hidden = state.jobs.length > 0;
    $("history-no-results").hidden = !state.jobs.length || filtered.length > 0;
    const rows = filtered.map((job) => {
      const row = element("article", "history-row");
      const content = element("div", "history-content");
      const title = element("div", "history-item-title");
      const username = element("button", "history-username", job.username);
      username.type = "button";
      username.title = "Show download details";
      username.addEventListener("click", () => { state.selectedJobId = job.id; renderActivity(); setView("download"); });
      title.append(username, statusBadge(job));
      const meta = element("div", "history-meta");
      [LABELS[job.server] || job.server, `${count(job.games)} games`, displaySize(job.bytes), dateTime(job.created_at)].filter(Boolean).forEach((text, index) => {
        if (index) meta.append(element("span", "meta-separator", "·"));
        meta.append(element("span", "", text));
      });
      content.append(title, meta);
      const path = job.status === "completed" ? job.output_path || "" : job.partial_path || "";
      if (path) content.append(element("div", "history-path", job.status === "completed" ? path : `Partial file: ${path}`));
      if (job.error) content.append(element("div", "history-error", job.error));
      const actions = element("div", "history-actions");
      actions.append(...jobActions(job));
      row.append(content, actions);
      return row;
    });
    // A running download can change its count often; retain focused controls when possible.
    const focus = document.activeElement;
    const previousRows = [...$("history-list").children];
    const focusedRow = focus?.closest(".history-row");
    const focusRowIndex = previousRows.indexOf(focusedRow);
    const focusActionIndex = focusedRow ? [...focusedRow.querySelectorAll("button")].indexOf(focus) : -1;
    $("history-list").replaceChildren(...rows);
    if (focusRowIndex >= 0 && focusActionIndex >= 0 && rows[focusRowIndex]) {
      rows[focusRowIndex].querySelectorAll("button")[focusActionIndex]?.focus({ preventScroll: true });
    }
  }

  function setJobs(jobs) {
    state.jobs = [...jobs].sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
    const signature = JSON.stringify(state.jobs);
    updateDownloadButton();
    renderActivity();
    if (signature !== jobsSignature) { jobsSignature = signature; renderHistory(); }
  }

  function upsertJob(job) {
    if (!job?.id) return;
    const rest = state.jobs.filter((item) => item.id !== job.id);
    setJobs([job, ...rest]);
    schedulePoll(800);
  }

  function schedulePoll(delay) {
    clearTimeout(state.polling);
    state.polling = setTimeout(pollJobs, delay ?? (hasActiveJob() ? 1000 : 4000));
  }

  async function pollJobs() {
    if (!state.initialized || state.clearingHistory) return;
    const revision = state.jobsRevision;
    try {
      const jobs = await api("/api/jobs");
      // A response started before Clear history must not restore removed entries.
      if (revision !== state.jobsRevision || state.clearingHistory) return;
      setJobs(Array.isArray(jobs) ? jobs : jobs.jobs || []);
      connectionError("");
    } catch (error) { connectionError(error.message); }
    finally { if (!state.clearingHistory) schedulePoll(); }
  }

  async function initialize() {
    $("retry-connection").disabled = true;
    clearTimeout(state.polling);
    try {
      if (!token) throw new Error("Launch PGN Downloader with its Windows launcher to connect this window to the app.");
      const data = await api("/api/bootstrap");
      state.bootstrap = data;
      applyTheme(data.settings?.theme);
      if (!state.initialized) {
        const last = data.settings?.last_request || {};
        const defaults = data.defaults || {};
        Object.keys(LABELS).forEach((server) => {
          const configured = defaults[server]?.modes || defaults.modes?.[server];
          state.modes[server] = Array.isArray(configured) ? [...configured] : [...MODE_DEFAULTS[server]];
        });
        const server = last.server || defaults.server || "lichess";
        if (Array.isArray(last.modes)) state.modes[server] = [...last.modes];
        setServer(server, false);
        $("username").value = last.username || "";
        $("color").value = last.color || "";
        $("since").value = last.since || "";
        $("until").value = last.until || "";
        $("filename").value = "";
        const output = data.settings?.output_dir || data.paths?.default_output_dir || "";
        $("output-dir").value = output;
        $("settings-output-dir").value = output;
      }
      $("data-dir").textContent = data.paths?.data_dir || "Unavailable";
      $("backend-name").textContent = data.backend?.name || "pgn-downloader";
      $("backend-version").textContent = data.backend?.commit ? String(data.backend.commit).slice(0, 12) : "Bundled";
      state.initialized = true;
      setJobs(data.jobs || []);
      $("save-settings").disabled = false;
      $("theme-toggle").disabled = state.themeSaving;
      enableNative();
      connectionError("");
      schedulePoll();
    } catch (error) { connectionError(error.message); }
    finally { $("retry-connection").disabled = false; }
  }

  $("download-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.sending || !state.initialized || hasActiveJob() || state.clearingHistory) return;
    showError("form-error", "");
    rememberModes();
    const request = {
      server: state.server,
      username: $("username").value.trim(),
      color: $("color").value || null,
      since: $("since").value.trim() || null,
      until: $("until").value.trim() || null,
      modes: [...state.modes[state.server]],
      output_dir: $("output-dir").value.trim(),
      filename: $("filename").value.trim() || null,
    };
    if (!request.username) { showError("form-error", "Enter the player's username."); $("username").focus(); return; }
    if (!request.output_dir) { showError("form-error", "Choose an output folder."); $("output-dir").focus(); return; }
    if (!request.modes.length) { showError("form-error", "Select at least one game type."); return; }
    if (request.filename && (!/\.pgn$/i.test(request.filename) || /[\\/]/.test(request.filename))) {
      showError("form-error", "Enter a file name ending in .pgn, without a folder path."); $("filename").focus(); return;
    }
    state.sending = true;
    updateDownloadButton();
    try {
      const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(request) });
      state.selectedJobId = job.id;
      upsertJob(job);
      connectionError("");
      toast(`Download added for ${request.username}. Follow its progress in Download activity.`);
    } catch (error) { showError("form-error", error.message); }
    finally {
      state.sending = false;
      updateDownloadButton();
    }
  });

  $("settings-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.initialized) return;
    showError("settings-error", "");
    const output = $("settings-output-dir").value.trim();
    if (!output) { showError("settings-error", "Choose a default output folder."); return; }
    $("save-settings").disabled = true;
    try {
      const previous = state.bootstrap.settings?.output_dir;
      const settings = await api("/api/settings", { method: "PUT", body: JSON.stringify({ output_dir: output }) });
      state.bootstrap.settings = settings;
      if (!$("output-dir").value || $("output-dir").value === previous) $("output-dir").value = settings.output_dir;
      $("settings-output-dir").value = settings.output_dir;
      toast("Your download preferences are saved on this computer.");
    } catch (error) { showError("settings-error", error.message); }
    finally { $("save-settings").disabled = false; }
  });

  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
  document.querySelectorAll("[data-go-download]").forEach((button) => button.addEventListener("click", () => setView("download")));
  document.querySelector(".brand").addEventListener("click", (event) => { event.preventDefault(); setView("download"); });
  document.querySelectorAll("input[name=server]").forEach((input) => input.addEventListener("change", () => setServer(input.value)));
  document.querySelectorAll("[data-browse]").forEach((button) => button.addEventListener("click", () => browse(button.dataset.browse)));
  $("history-search").addEventListener("input", renderHistory);
  $("theme-toggle").addEventListener("click", toggleTheme);
  $("clear-form").addEventListener("click", clearForm);
  $("clear-history").addEventListener("click", askClearHistory);
  $("confirm-clear-history").addEventListener("click", clearHistory);
  $("cancel-clear-history").addEventListener("click", () => { if (!state.clearingHistory) $("clear-history-dialog").close(); });
  $("clear-history-dialog").addEventListener("cancel", (event) => { if (state.clearingHistory) event.preventDefault(); });
  $("retry-connection").addEventListener("click", initialize);
  $("open-data-dir").addEventListener("click", () => openPath(state.bootstrap?.paths?.data_dir));
  window.addEventListener("pywebviewready", () => { enableNative(); renderActivity(); renderHistory(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.initialized) schedulePoll(100); });
  window.addEventListener("beforeunload", () => { clearTimeout(state.polling); clearTimeout(state.retryTicker); });
  applyTheme(document.documentElement.dataset.theme);
  renderModes("lichess");
  enableNative();
  initialize();
})();
