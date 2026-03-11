/**
 * NAS Photo Album Selector — Frontend Application
 */

const API = "";  // Same origin

// ── Auth ──────────────────────────────────────────────────────────────────────
const auth = {
  token: sessionStorage.getItem("nas_token") || null,

  save(token) {
    this.token = token;
    sessionStorage.setItem("nas_token", token);
  },

  clear() {
    this.token = null;
    sessionStorage.removeItem("nas_token");
  },
};

// ── State ────────────────────────────────────────────────────────────────────
const state = {
  connected: false,
  connectionConfig: {},
  currentBrowserPath: "/",
  modalBrowserPath: "/",
  selectedFolders: new Set(),
  albumType: "general",
  albumTypes: {},
  currentJobId: null,
  pollInterval: null,
};

// ── DOM Helpers ───────────────────────────────────────────────────────────────
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

function showEl(el) { if (el) el.style.display = ""; }
function hideEl(el) { if (el) el.style.display = "none"; }

function setConnStatus(status, text) {
  const badge = $("#conn-status");
  badge.className = "badge badge-" + (status === "ok" ? "success" : status === "error" ? "error" : "idle");
  badge.textContent = text;
}

function setMessage(el, text, type = "") {
  if (!el) return;
  el.textContent = text;
  el.className = "status-message " + type;
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", async () => {
  await initAuth();
});

async function initAuth() {
  // Check if server requires auth
  let authEnabled = false;
  try {
    const status = await apiFetch("/api/auth/status");
    authEnabled = status.auth_enabled;
  } catch (e) {
    // If this call fails (401), auth is definitely enabled
    authEnabled = true;
  }

  if (!authEnabled) {
    // Auth disabled: go straight to app
    showApp();
    return;
  }

  // Auth enabled: check existing token
  if (auth.token) {
    try {
      // Validate token by calling a protected endpoint
      await apiFetch("/api/album-types");
      showApp();
      return;
    } catch (e) {
      auth.clear();
    }
  }

  // Show login overlay
  showLoginOverlay();
}

function showLoginOverlay() {
  const overlay = $("#login-overlay");
  showEl(overlay);
  hideEl($("header.app-header"));
  hideEl($("main.app-main"));

  const form = $("#login-form");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#btn-login");
    const errEl = $("#login-error");
    hideEl(errEl);
    btn.disabled = true;
    btn.textContent = "確認中...";

    try {
      const password = $("#login-password").value;
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "ログイン失敗");
      }
      const data = await res.json();
      auth.save(data.token);
      hideEl(overlay);
      showApp();
    } catch (err) {
      errEl.textContent = err.message;
      showEl(errEl);
      btn.disabled = false;
      btn.textContent = "ログイン";
    }
  });
}

async function showApp() {
  showEl($("header.app-header"));
  showEl($("main.app-main"));

  // Show logout button only if auth is enabled
  try {
    const status = await apiFetch("/api/auth/status");
    if (status.auth_enabled) showEl($("#btn-logout"));
  } catch (e) { /* ignore */ }

  initSetupGuide();
  await loadAlbumTypes();
  await loadSavedConfig();
  bindEvents();
  updateRunSummary();
}

// ── Setup Guide ───────────────────────────────────────────────────────────────
function initSetupGuide() {
  const body = $("#setup-guide-body");
  const chevron = $("#setup-guide-chevron");
  const toggle = $("#setup-guide-toggle");

  // Collapse/expand
  const collapsed = localStorage.getItem("guide_collapsed") === "1";
  if (collapsed) {
    body.style.display = "none";
    chevron.textContent = "▶";
  }
  toggle.addEventListener("click", () => {
    const isHidden = body.style.display === "none";
    body.style.display = isHidden ? "" : "none";
    chevron.textContent = isHidden ? "▼" : "▶";
    localStorage.setItem("guide_collapsed", isHidden ? "0" : "1");
  });

  // Mode tabs
  const savedMode = localStorage.getItem("guide_mode") || "local";
  setGuideMode(savedMode);

  $$(".mode-tab").forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const mode = btn.dataset.mode;
      setGuideMode(mode);
      localStorage.setItem("guide_mode", mode);
    });
  });
}

function setGuideMode(mode) {
  $$(".mode-tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
  $("#guide-local").style.display    = mode === "local"    ? "" : "none";
  $("#guide-internet").style.display = mode === "internet" ? "" : "none";
}

async function loadAlbumTypes() {
  try {
    const data = await apiFetch("/api/album-types");
    state.albumTypes = data;
    renderAlbumTypeGrid(data);
  } catch (e) {
    console.error("Album types load failed", e);
  }
}

function renderAlbumTypeGrid(types) {
  const grid = $("#album-type-grid");
  grid.innerHTML = "";
  for (const [key, info] of Object.entries(types)) {
    const card = document.createElement("div");
    card.className = "album-type-card" + (key === state.albumType ? " selected" : "");
    card.dataset.type = key;
    card.innerHTML = `<span class="type-icon">${info.icon}</span><span class="type-label">${info.label}</span>`;
    card.title = info.description;
    card.addEventListener("click", () => selectAlbumType(key));
    grid.appendChild(card);
  }
}

function selectAlbumType(key) {
  state.albumType = key;
  $$(".album-type-card").forEach(c => c.classList.toggle("selected", c.dataset.type === key));
  const customGroup = $("#custom-prompt-group");
  if (key === "custom") showEl(customGroup); else hideEl(customGroup);
  updateRunSummary();
}

async function loadSavedConfig() {
  try {
    const config = await apiFetch("/api/config/load");
    if (config.connection_type) {
      setFormFromConfig(config);
    }
  } catch (e) { /* ignore */ }
}

function setFormFromConfig(config) {
  if (config.connection_type) {
    const radio = $(`input[name="conn-type"][value="${config.connection_type}"]`);
    if (radio) { radio.checked = true; toggleConnectionFields(config.connection_type); }
  }
  if (config.host) $("#nas-host").value = config.host;
  if (config.share_name) $("#nas-share").value = config.share_name;
  if (config.username) $("#nas-user").value = config.username;
  if (config.base_path) $("#nas-local-path").value = config.base_path;
  if (config.output_folder) $("#output-folder").value = config.output_folder;
}

// ── Event Binding ─────────────────────────────────────────────────────────────
function bindEvents() {
  // Connection type toggle
  $$("input[name='conn-type']").forEach(r => {
    r.addEventListener("change", () => toggleConnectionFields(r.value));
  });

  // Connect button
  $("#btn-connect").addEventListener("click", testConnection);

  // Folder browser navigation
  $("#btn-folder-up").addEventListener("click", () => navigateBrowser(".."));
  $("#btn-refresh-folder").addEventListener("click", () => loadFolderBrowser(state.currentBrowserPath));

  // Output folder browse
  $("#btn-browse-output").addEventListener("click", openFolderModal);
  $("#btn-folder-up").addEventListener("click", () => navigateBrowser(".."));

  // Modal
  $("#modal-close").addEventListener("click", closeFolderModal);
  $("#modal-backdrop").addEventListener("click", closeFolderModal);
  $("#modal-folder-up").addEventListener("click", () => navigateModalBrowser(".."));
  $("#modal-confirm").addEventListener("click", confirmModalFolder);

  // Photo count slider
  $("#photo-count").addEventListener("input", e => {
    $("#photo-count-label").textContent = e.target.value;
    updateRunSummary();
  });

  // Min resolution slider
  $("#min-resolution").addEventListener("input", e => {
    const v = parseInt(e.target.value);
    $("#min-res-label").textContent = v > 0 ? v + " px" : "指定なし";
  });

  // Date clear
  $("#btn-clear-dates").addEventListener("click", () => {
    $("#date-from").value = "";
    $("#date-to").value = "";
    updateRunSummary();
  });

  // Date change
  ["date-from", "date-to"].forEach(id => {
    document.getElementById(id).addEventListener("change", updateRunSummary);
  });

  // Count photos
  $("#btn-count-photos").addEventListener("click", countPhotos);

  // Run
  $("#btn-run").addEventListener("click", runAlbumCreation);

  // New album
  $("#btn-new-album").addEventListener("click", resetForNewAlbum);

  // Retry
  $("#btn-retry").addEventListener("click", () => {
    hideEl($("#error-panel"));
    showEl($("#btn-run").parentElement);
  });

  // Album name change
  $("#album-name").addEventListener("input", updateRunSummary);

  // Logout
  $("#btn-logout").addEventListener("click", async () => {
    try { await apiFetch("/api/auth/logout", "POST"); } catch (e) { /* ignore */ }
    auth.clear();
    location.reload();
  });
}

function toggleConnectionFields(type) {
  const network = $("#fields-network");
  const local = $("#fields-local");
  const smbShare = $("#smb-share-group");
  if (type === "local") {
    hideEl(network);
    showEl(local);
  } else {
    showEl(network);
    hideEl(local);
    if (type === "sftp") hideEl(smbShare); else showEl(smbShare);
  }
}

// ── Connection ────────────────────────────────────────────────────────────────
async function testConnection() {
  const btn = $("#btn-connect");
  const msg = $("#conn-message");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 接続中...';
  setMessage(msg, "");
  setConnStatus("idle", "接続テスト中...");

  const config = buildConnectionConfig();
  try {
    const res = await apiFetch("/api/nas/test", "POST", config);
    state.connected = true;
    state.connectionConfig = config;
    setConnStatus("ok", "接続済み");
    setMessage(msg, "✓ " + res.message, "success");
    saveConfig(config);
    loadFolderBrowser("/");
    showEl($("#photo-estimate"));
  } catch (e) {
    setConnStatus("error", "接続エラー");
    setMessage(msg, "✗ " + e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "接続テスト";
  }
}

function buildConnectionConfig() {
  const type = $("input[name='conn-type']:checked").value;
  const config = { connection_type: type };
  if (type === "local") {
    config.base_path = $("#nas-local-path").value.trim();
  } else {
    config.host = $("#nas-host").value.trim();
    config.username = $("#nas-user").value.trim();
    config.password = $("#nas-pass").value;
    if (type === "smb") config.share_name = $("#nas-share").value.trim();
  }
  return config;
}

async function saveConfig(config) {
  try {
    await apiFetch("/api/config/save", "POST", config);
  } catch (e) { /* ignore */ }
}

// ── Folder Browser ────────────────────────────────────────────────────────────
async function loadFolderBrowser(path) {
  const list = $("#folder-list");
  list.innerHTML = '<div class="placeholder-msg">読み込み中...</div>';
  try {
    const req = { ...state.connectionConfig, folder_path: path };
    const data = await apiFetch("/api/nas/browse", "POST", req);
    state.currentBrowserPath = data.path;
    $("#browser-path").textContent = data.path;
    renderFolderList(list, data.items, path, false);
  } catch (e) {
    list.innerHTML = `<div class="placeholder-msg" style="color:var(--error)">エラー: ${e.message}</div>`;
  }
}

function renderFolderList(container, items, parentPath, isModal) {
  container.innerHTML = "";
  const folders = items.filter(i => i.is_dir);
  const files = items.filter(i => !i.is_dir);

  if (folders.length === 0 && files.length === 0) {
    container.innerHTML = '<div class="placeholder-msg">フォルダが空です</div>';
    return;
  }

  for (const item of folders) {
    const div = document.createElement("div");
    const isSelected = !isModal && state.selectedFolders.has(item.path);
    div.className = "folder-item" + (isSelected ? " selected" : "");
    div.innerHTML = `
      <span class="item-icon">📁</span>
      <span class="item-name">${escapeHtml(item.name)}</span>
      ${isSelected ? '<span class="item-badge">✓</span>' : ''}
    `;
    div.addEventListener("click", (e) => {
      if (isModal) {
        navigateModalBrowser(item.path);
      } else {
        handleFolderClick(item.path, div);
      }
    });
    div.addEventListener("dblclick", () => {
      if (isModal) navigateModalBrowser(item.path);
      else navigateBrowser(item.path);
    });
    container.appendChild(div);
  }

  // Show photo count for non-modal
  if (!isModal) {
    for (const item of files.slice(0, 5)) {
      const ext = item.name.split(".").pop().toLowerCase();
      const photoExts = ["jpg", "jpeg", "png", "heic", "heif", "webp", "tiff"];
      if (photoExts.includes(ext)) {
        // Just show a count indicator
        break;
      }
    }
  }
}

function handleFolderClick(path, element) {
  // Single click = select/deselect for album source
  if (state.selectedFolders.has(path)) {
    state.selectedFolders.delete(path);
    element.classList.remove("selected");
    element.querySelector(".item-badge")?.remove();
  } else {
    state.selectedFolders.add(path);
    element.classList.add("selected");
    if (!element.querySelector(".item-badge")) {
      const badge = document.createElement("span");
      badge.className = "item-badge";
      badge.textContent = "✓";
      element.appendChild(badge);
    }
  }
  updateSelectedFoldersList();
  updateFolderCountBadge();
  updateRunSummary();
  showEl($("#photo-estimate"));
}

function navigateBrowser(path) {
  let newPath;
  if (path === "..") {
    const parts = state.currentBrowserPath.split("/").filter(Boolean);
    parts.pop();
    newPath = "/" + parts.join("/");
  } else if (path.startsWith("/")) {
    newPath = path;
  } else {
    newPath = state.currentBrowserPath.replace(/\/$/, "") + "/" + path;
  }
  loadFolderBrowser(newPath || "/");
}

function updateSelectedFoldersList() {
  const panel = $("#selected-folders-panel");
  const list = $("#selected-folders-list");
  if (state.selectedFolders.size === 0) {
    hideEl(panel);
    return;
  }
  showEl(panel);
  list.innerHTML = "";
  for (const folder of state.selectedFolders) {
    const li = document.createElement("li");
    li.textContent = folder;
    const btn = document.createElement("button");
    btn.textContent = "×";
    btn.title = "削除";
    btn.addEventListener("click", () => {
      state.selectedFolders.delete(folder);
      updateSelectedFoldersList();
      updateFolderCountBadge();
      updateRunSummary();
      loadFolderBrowser(state.currentBrowserPath);
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

function updateFolderCountBadge() {
  const badge = $("#folder-count-badge");
  const count = state.selectedFolders.size;
  badge.textContent = count + " フォルダ選択中";
  badge.className = "badge " + (count > 0 ? "badge-info" : "badge-idle");
}

// ── Modal Folder Browser ──────────────────────────────────────────────────────
function openFolderModal() {
  if (!state.connected) {
    alert("先にNASに接続してください。");
    return;
  }
  const modal = $("#folder-modal");
  showEl(modal);
  state.modalBrowserPath = "/";
  loadModalBrowser("/");
}

function closeFolderModal() {
  hideEl($("#folder-modal"));
}

async function loadModalBrowser(path) {
  const list = $("#modal-folder-list");
  list.innerHTML = '<div class="placeholder-msg">読み込み中...</div>';
  try {
    const req = { ...state.connectionConfig, folder_path: path };
    const data = await apiFetch("/api/nas/browse", "POST", req);
    state.modalBrowserPath = data.path;
    $("#modal-browser-path").textContent = data.path;
    $("#modal-selected-path").textContent = data.path;
    renderFolderList(list, data.items, path, true);
  } catch (e) {
    list.innerHTML = `<div class="placeholder-msg" style="color:var(--error)">エラー: ${e.message}</div>`;
  }
}

function navigateModalBrowser(path) {
  let newPath;
  if (path === "..") {
    const parts = state.modalBrowserPath.split("/").filter(Boolean);
    parts.pop();
    newPath = "/" + parts.join("/");
  } else if (path.startsWith("/")) {
    newPath = path;
  } else {
    newPath = state.modalBrowserPath.replace(/\/$/, "") + "/" + path;
  }
  loadModalBrowser(newPath || "/");
}

function confirmModalFolder() {
  $("#output-folder").value = state.modalBrowserPath;
  closeFolderModal();
  updateRunSummary();
}

// ── Photo Count ───────────────────────────────────────────────────────────────
async function countPhotos() {
  if (!state.connected || state.selectedFolders.size === 0) return;
  const btn = $("#btn-count-photos");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 確認中...';
  try {
    const req = buildAlbumRequest();
    const data = await apiFetch("/api/photos/count", "POST", req);
    $("#estimate-text").textContent = `対象写真: 約 ${data.count} 枚`;
  } catch (e) {
    $("#estimate-text").textContent = "取得エラー: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "📊 写真数を確認";
  }
}

// ── Run Summary ───────────────────────────────────────────────────────────────
function updateRunSummary() {
  const summary = $("#run-summary");
  const albumType = state.albumTypes[state.albumType];
  const dateFrom = $("#date-from").value;
  const dateTo = $("#date-to").value;
  const period = dateFrom || dateTo
    ? `${dateFrom || "〜"} 〜 ${dateTo || "〜"}`
    : "すべての期間";

  summary.innerHTML = `
    <div class="run-summary-item">
      <div class="label">接続</div>
      <div class="value">${state.connected ? "✓ 接続済み" : "未接続"}</div>
    </div>
    <div class="run-summary-item">
      <div class="label">選択フォルダ</div>
      <div class="value">${state.selectedFolders.size} フォルダ</div>
    </div>
    <div class="run-summary-item">
      <div class="label">撮影期間</div>
      <div class="value">${period}</div>
    </div>
    <div class="run-summary-item">
      <div class="label">選別枚数</div>
      <div class="value">${$("#photo-count").value} 枚</div>
    </div>
    <div class="run-summary-item">
      <div class="label">アルバムタイプ</div>
      <div class="value">${albumType ? albumType.icon + " " + albumType.label : "-"}</div>
    </div>
    <div class="run-summary-item">
      <div class="label">アルバム名</div>
      <div class="value">${$("#album-name").value || "(自動生成)"}</div>
    </div>
  `;
}

// ── Album Creation ────────────────────────────────────────────────────────────
async function runAlbumCreation() {
  if (!state.connected) {
    alert("先にNASに接続してください。");
    return;
  }
  if (state.selectedFolders.size === 0) {
    alert("対象フォルダを1つ以上選択してください。");
    return;
  }

  const btn = $("#btn-run");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 処理中...';

  hideEl($("#result-panel"));
  hideEl($("#error-panel"));
  showEl($("#progress-panel"));
  setProgress(0, "アルバム作成を開始しています...");

  try {
    const req = buildAlbumRequest();
    const data = await apiFetch("/api/album/create", "POST", req);
    state.currentJobId = data.job_id;
    startPolling(data.job_id);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = "🚀 アルバム作成を開始";
    hideEl($("#progress-panel"));
    showEl($("#error-panel"));
    $("#error-message").textContent = e.message;
  }
}

function buildAlbumRequest() {
  const config = buildConnectionConfig();
  return {
    ...config,
    source_folders: [...state.selectedFolders],
    date_from: $("#date-from").value || null,
    date_to: $("#date-to").value || null,
    photo_count: parseInt($("#photo-count").value),
    album_type: state.albumType,
    album_name: $("#album-name").value.trim(),
    output_folder: $("#output-folder").value.trim() || "/Albums",
    custom_prompt: $("#custom-prompt").value.trim() || null,
    include_raw: $("#include-raw").checked,
    min_resolution: parseInt($("#min-resolution").value) || null,
  };
}

function startPolling(jobId) {
  if (state.pollInterval) clearInterval(state.pollInterval);
  state.pollInterval = setInterval(async () => {
    try {
      const status = await apiFetch(`/api/album/status/${jobId}`);
      handleJobUpdate(status);
      if (["completed", "error"].includes(status.status)) {
        clearInterval(state.pollInterval);
        state.pollInterval = null;
      }
    } catch (e) {
      console.error("Poll error", e);
    }
  }, 1200);
}

function handleJobUpdate(status) {
  setProgress(status.progress, status.message);

  if (status.status === "completed" && status.result) {
    showResult(status.result);
    hideEl($("#progress-panel"));
    const btn = $("#btn-run");
    btn.disabled = false;
    btn.textContent = "🚀 アルバム作成を開始";
  } else if (status.status === "error") {
    hideEl($("#progress-panel"));
    showEl($("#error-panel"));
    $("#error-message").textContent = status.error || "不明なエラーが発生しました。";
    const btn = $("#btn-run");
    btn.disabled = false;
    btn.textContent = "🚀 アルバム作成を開始";
  }
}

function setProgress(pct, msg) {
  $("#progress-bar").style.width = pct + "%";
  $("#progress-message").textContent = msg;
}

function showResult(result) {
  showEl($("#result-panel"));
  const summary = $("#result-summary");
  summary.innerHTML = `
    <p>✅ アルバム作成完了!</p>
    <p><strong>${result.selected_count}枚</strong> を選別し、
    <strong>${result.copied_count}枚</strong> をアルバムに保存しました。</p>
    <p>📁 保存先: <code>${result.output_path}</code></p>
    <p style="font-size:0.8rem;color:#166534;margin-top:0.5rem">
      スキャン: ${result.total_scanned}枚 → フィルタ後: ${result.after_filter}枚 → 選別: ${result.selected_count}枚
    </p>
  `;

  const grid = $("#result-grid");
  grid.innerHTML = "";
  const photos = result.selected_photos || [];
  photos.forEach((photo, idx) => {
    const card = document.createElement("div");
    card.className = "photo-card";
    const score = photo.score ? parseFloat(photo.score).toFixed(1) : "-";
    card.innerHTML = `
      <img src="/api/photo/thumbnail/${state.currentJobId}/${idx}"
           alt="${escapeHtml(photo.filename)}"
           loading="lazy"
           onerror="this.style.display='none'" />
      <div class="photo-info">
        <div><span class="photo-score">⭐ ${score}</span></div>
        <div class="photo-name" title="${escapeHtml(photo.filename)}">${escapeHtml(photo.filename)}</div>
        ${photo.date ? `<div style="color:var(--text-light);font-size:0.68rem">${photo.date}</div>` : ""}
        ${photo.reason ? `<div class="photo-reason">${escapeHtml(photo.reason)}</div>` : ""}
      </div>
    `;
    grid.appendChild(card);
  });
}

function resetForNewAlbum() {
  hideEl($("#result-panel"));
  hideEl($("#error-panel"));
  state.currentJobId = null;
  // Scroll to top
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ── API Utilities ─────────────────────────────────────────────────────────────
async function apiFetch(endpoint, method = "GET", body = null) {
  const headers = { "Content-Type": "application/json" };
  if (auth.token) headers["Authorization"] = `Bearer ${auth.token}`;
  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API + endpoint, opts);
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { const err = await res.json(); msg = err.detail || msg; } catch {}
    if (res.status === 401) {
      auth.clear();
      location.reload();
    }
    throw new Error(msg);
  }
  return res.json();
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
