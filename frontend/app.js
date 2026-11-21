/* Steam Free-to-Keep Tracker — PWA Client Engine
 * Features:
 * - IndexedDB durable client cache (cycles, games, notifications, live events, meta)
 * - Cache-first startup + automatic online sync + offline detection + auto-reconnection sync
 * - Catch-up popup remembering acknowledged cycle number (never repeats after dismissal)
 * - Real-time SSE live worker monitor with polling fallback (zero fake animations)
 * - Individual & Historical PDF modal viewer + download + JSON/CSV backup export
 */
(function () {
  "use strict";

  const DB_NAME = "steam_ftk_tracker_db";
  const DB_VERSION = 1;
  const CLIENT_ID = localStorage.getItem("sftk_client_id") || (() => {
    const id = "pwa-" + Math.random().toString(36).slice(2, 10);
    localStorage.setItem("sftk_client_id", id);
    return id;
  })();

  const state = {
    apiBase: (localStorage.getItem("sftk_api_base") || "").replace(/\/+$/, ""),
    online: navigator.onLine,
    staticMode: false,
    backendReachable: false,
    lastSyncUtc: null,
    lastKnownCycle: 0,
    acknowledgedCycle: Number(localStorage.getItem("sftk_ack_cycle") || "0"),
    liveState: null,
    stats: {},
    settings: {},
    cycles: [],
    games: [],
    notifications: [],
    consoleEvents: [],
    lastEventId: 0,
    sse: null,
    pollTimer: null,
    deferredInstallPrompt: null,
  };

  // ---------------- IndexedDB Layer ----------------
  function openIDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = (e) => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains("cycles")) {
          db.createObjectStore("cycles", { keyPath: "cycle_number" });
        }
        if (!db.objectStoreNames.contains("games")) {
          db.createObjectStore("games", { keyPath: "game_key" });
        }
        if (!db.objectStoreNames.contains("notifications")) {
          db.createObjectStore("notifications", { keyPath: "delivery_id" });
        }
        if (!db.objectStoreNames.contains("meta")) {
          db.createObjectStore("meta", { keyPath: "key" });
        }
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbGetAll(storeName) {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, "readonly");
      const req = tx.objectStore(storeName).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
    });
  }

  async function idbPutMany(storeName, items) {
    if (!items || !items.length) return;
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(storeName, "readwrite");
      const store = tx.objectStore(storeName);
      for (const item of items) store.put(item);
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  async function idbSetMeta(key, value) {
    await idbPutMany("meta", [{ key, value }]);
  }

  async function idbClearAll() {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(["cycles", "games", "notifications", "meta"], "readwrite");
      tx.objectStore("cycles").clear();
      tx.objectStore("games").clear();
      tx.objectStore("notifications").clear();
      tx.objectStore("meta").clear();
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error);
    });
  }

  // ---------------- Formatting Helpers ----------------
  function esc(val) {
    return String(val == null ? "" : val)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function formatIST(isoStr) {
    if (!isoStr) return "Never";
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return isoStr;
    return d.toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: true,
    }) + " IST";
  }

  function formatAge(isoStr) {
    if (!isoStr) return "never synced";
    const diffSec = Math.max(0, Math.floor((Date.now() - new Date(isoStr).getTime()) / 1000));
    if (diffSec < 60) return `${diffSec}s ago`;
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ${diffSec % 60}s ago`;
    const hrs = Math.floor(diffSec / 3600);
    const mins = Math.floor((diffSec % 3600) / 60);
    return `${hrs}h ${mins}m ago`;
  }

  function formatCountdown(targetIso) {
    if (!targetIso) return "--:--";
    const diffSec = Math.floor((new Date(targetIso).getTime() - Date.now()) / 1000);
    if (diffSec <= 0) return "Due now";
    const m = Math.floor(diffSec / 60);
    const s = diffSec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }

  function statusPillClass(status) {
    if (!status) return "";
    if (["VERIFIED", "SUCCESS_WITH_RESULTS", "SUCCESS_NO_RESULTS", "DELIVERED", "IDLE"].includes(status)) return "green";
    if (["RUNNING", "PENDING"].includes(status)) return "blue";
    if (["QUARANTINED", "COMPLETED_WITH_ERRORS", "EXPIRED"].includes(status)) return "amber";
    if (["REJECTED", "FAILED", "PERMANENTLY_FAILED"].includes(status)) return "red";
    return "";
  }

  function apiUrl(path) {
    return `${state.apiBase}${path}`;
  }

  // ---------------- Cache Hydration & Online Sync ----------------
  async function loadFromCache() {
    try {
      const [cycles, games, notifs, metaRows] = await Promise.all([
        idbGetAll("cycles"),
        idbGetAll("games"),
        idbGetAll("notifications"),
        idbGetAll("meta"),
      ]);
      const meta = Object.fromEntries(metaRows.map((m) => [m.key, m.value]));
      state.cycles = cycles.sort((a, b) => b.cycle_number - a.cycle_number);
      state.games = games.sort((a, b) => String(b.last_verified_utc).localeCompare(String(a.last_verified_utc)));
      state.notifications = notifs.sort((a, b) => b.delivery_id - a.delivery_id);
      state.lastSyncUtc = meta.lastSyncUtc || null;
      state.lastKnownCycle = Number(meta.lastKnownCycle || 0);
      state.liveState = meta.liveState || null;
      state.stats = meta.stats || {};
      state.settings = meta.settings || {};
    } catch (err) {
      console.warn("IndexedDB load failed:", err);
    }
    renderAll();
  }

  async function syncWithBackend(isReconnect = false) {
    if (state.staticMode) {
      badge.textContent = "Mode: GitHub Actions Snapshot ($0/mo)";
      badge.className = "pill blue";
      return;
    }
    if (!navigator.onLine) {
      state.online = false;
      state.backendReachable = false;
      renderAll();
      return;
    }
    try {
      let resp = await fetch(apiUrl(`/api/v1/sync?since_cycle=${state.lastKnownCycle}&client_id=${encodeURIComponent(CLIENT_ID)}`), { cache: "no-store" }).catch(() => null);
      if (!resp || !resp.ok) {
        resp = await fetch(apiUrl("./api/v1/sync.json"), { cache: "no-store" });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        state.staticMode = true;
      }
      const data = await resp.json();

      state.online = true;
      state.backendReachable = true;
      state.lastSyncUtc = data.synced_at_utc;
      state.liveState = data.live_state;
      state.stats = data.stats || {};
      state.settings = data.settings || {};

      // Merge missed cycles safely by cycle_number (preserving all existing history)
      const cycleMap = new Map(state.cycles.map((c) => [c.cycle_number, c]));
      const newlyReceivedCycles = [];
      for (const c of (data.missed_cycles || [])) {
        if (c && c.cycle_number) {
          if (!cycleMap.has(c.cycle_number)) newlyReceivedCycles.push(c);
          cycleMap.set(c.cycle_number, c);
        }
      }
      state.cycles = Array.from(cycleMap.values()).sort((a, b) => b.cycle_number - a.cycle_number);

      // Merge games safely by game_key
      const gameMap = new Map(state.games.map((g) => [g.game_key, g]));
      for (const g of (data.historical_games || [])) {
        if (g && g.game_key) gameMap.set(g.game_key, g);
      }
      state.games = Array.from(gameMap.values()).sort((a, b) =>
        String(b.last_verified_utc).localeCompare(String(a.last_verified_utc))
      );

      // Merge notifications
      const notifMap = new Map(state.notifications.map((n) => [n.delivery_id, n]));
      for (const n of (data.notifications || [])) {
        if (n && n.delivery_id) notifMap.set(n.delivery_id, n);
      }
      state.notifications = Array.from(notifMap.values()).sort((a, b) => b.delivery_id - a.delivery_id);

      const prevKnown = state.lastKnownCycle;
      state.lastKnownCycle = Math.max(state.lastKnownCycle, Number(data.server_latest_cycle || 0));

      // Persist to IndexedDB
      await Promise.all([
        idbPutMany("cycles", data.missed_cycles || []),
        idbPutMany("games", data.historical_games || []),
        idbPutMany("notifications", data.notifications || []),
        idbSetMeta("lastSyncUtc", state.lastSyncUtc),
        idbSetMeta("lastKnownCycle", state.lastKnownCycle),
        idbSetMeta("liveState", state.liveState),
        idbSetMeta("stats", state.stats),
        idbSetMeta("settings", state.settings),
      ]);

      // Fetch recent live console events
      await fetchRecentConsoleEvents();
      renderAll();

      // Catch-up popup check (only when unacknowledged new cycles exist)
      maybeShowCatchupPopup(isReconnect ? newlyReceivedCycles : null, prevKnown);
    } catch (err) {
      state.backendReachable = false;
      renderAll();
    }
  }

  async function fetchRecentConsoleEvents() {
    try {
      const evUrl = state.staticMode ? apiUrl("./api/v1/live/events.json") : apiUrl(`/api/v1/live/events?after_event_id=${state.lastEventId}&limit=100`);
      const resp = await fetch(evUrl, { cache: "no-store" });
      if (!resp.ok) return;
      const data = await resp.json();
      appendConsoleEvents(data.events || []);
    } catch (_) {}
  }

  function appendConsoleEvents(events) {
    if (!events || !events.length) return;
    const seen = new Set(state.consoleEvents.map((e) => e.event_id));
    for (const ev of events) {
      if (!seen.has(ev.event_id)) {
        state.consoleEvents.push(ev);
        state.lastEventId = Math.max(state.lastEventId, ev.event_id);
      }
    }
    if (state.consoleEvents.length > 250) {
      state.consoleEvents = state.consoleEvents.slice(-250);
    }
    renderConsole();
  }

  // ---------------- Real-Time Live Stream (SSE + Polling Fallback) ----------------
  function connectLiveStream() {
    if (state.sse) {
      try { state.sse.close(); } catch (_) {}
      state.sse = null;
    }
    const badge = document.getElementById("liveStreamBadge");
    if (state.staticMode) {
      badge.textContent = "Mode: GitHub Actions Snapshot ($0/mo)";
      badge.className = "pill blue";
      return;
    }
    if (!navigator.onLine) {
      badge.textContent = "Stream: Offline";
      return;
    }
    try {
      const es = new EventSource(apiUrl(`/api/v1/live/stream?after_event_id=${state.lastEventId}`));
      state.sse = es;
      es.onopen = () => {
        badge.textContent = "Stream: SSE Connected";
        badge.className = "pill green";
      };
      es.addEventListener("status", (e) => {
        try {
          const payload = JSON.parse(e.data);
          state.backendReachable = true;
          state.liveState = payload.worker;
          state.stats = payload.stats || state.stats;
          renderLiveMonitor();
          renderTopbar();
        } catch (_) {}
      });
      es.addEventListener("console", (e) => {
        try {
          const ev = JSON.parse(e.data);
          appendConsoleEvents([ev]);
          if (ev.event_type === "SCAN_COMPLETED") {
            syncWithBackend(false);
          }
        } catch (_) {}
      });
      es.onerror = () => {
        badge.textContent = "Stream: Polling Fallback";
        badge.className = "pill amber";
      };
    } catch (_) {
      badge.textContent = "Stream: Polling";
    }
  }

  // ---------------- Automatic Catch-Up Popup ----------------
  function maybeShowCatchupPopup(reconnectedCycles, prevKnown) {
    const unackedCycles = state.cycles.filter((c) => c.cycle_number > state.acknowledgedCycle);
    if (unackedCycles.length === 0) return;

    const newVerifiedGames = [];
    const seenIds = new Set();
    for (const c of unackedCycles) {
      for (const ev of (c.evaluations || [])) {
        if (ev.final_status === "VERIFIED") {
          const key = ev.app_id || ev.title;
          if (!seenIds.has(key)) {
            seenIds.add(key);
            newVerifiedGames.push(ev);
          }
        }
      }
    }

    const latest = unackedCycles[0];
    const bodyEl = document.getElementById("catchupBody");
    let html = `
      <div class="kv-grid" style="margin-bottom:14px;">
        <div class="kv-item"><div class="kv-label">New Completed Cycles</div><div class="kv-val">${unackedCycles.length}</div></div>
        <div class="kv-item"><div class="kv-label">New Verified Games</div><div class="kv-val" style="color:var(--green);">${newVerifiedGames.length}</div></div>
        <div class="kv-item"><div class="kv-label">Latest Scan (IST)</div><div class="kv-val" style="font-size:12px;">${esc(formatIST(latest.completed_at_utc || latest.started_at_utc))}</div></div>
      </div>
    `;

    if (newVerifiedGames.length > 0) {
      html += `<h4 style="margin:10px 0 8px;">Newly Verified Free-to-Keep Games</h4>`;
      for (const g of newVerifiedGames) {
        html += `
          <div class="card" style="margin-bottom:8px;">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
              <div>
                <strong>${esc(g.title)}</strong> <span style="color:var(--muted);">(App ID: ${esc(g.app_id)})</span><br/>
                <span class="price-old">${esc(g.original_price_formatted)}</span>
                <span class="price-now" style="font-size:14px;">${esc(g.current_price_formatted)}</span>
                <span style="color:var(--muted);font-size:12px;"> | Expires: ${esc(g.expiration_at || "Not disclosed")}</span>
              </div>
              <a class="btn green sm" href="${esc(g.claim_url)}" target="_blank" rel="noopener">Open on Steam</a>
            </div>
          </div>
        `;
      }
    } else {
      html += `<p style="color:var(--muted);">No new Verified Direct Steam Free-to-Keep games were discovered in these ${unackedCycles.length} cycle(s).</p>`;
    }

    html += `<h4 style="margin:14px 0 8px;">Cycle PDF Reports</h4><div style="display:flex;gap:8px;flex-wrap:wrap;">`;
    for (const c of unackedCycles.slice(0, 10)) {
      html += `<button class="btn sm" data-open-pdf="${c.cycle_number}">Cycle #${c.cycle_number} PDF</button>`;
    }
    html += `</div>`;

    bodyEl.innerHTML = html;
    bodyEl.querySelectorAll("[data-open-pdf]").forEach((btn) => {
      btn.addEventListener("click", () => openCyclePdfModal(Number(btn.dataset.openPdf)));
    });
    document.getElementById("catchupModal").classList.add("open");
  }

  function acknowledgeCatchup() {
    state.acknowledgedCycle = state.lastKnownCycle;
    localStorage.setItem("sftk_ack_cycle", String(state.acknowledgedCycle));
    document.getElementById("catchupModal").classList.remove("open");
  }

  // ---------------- Rendering Functions ----------------
  function renderAll() {
    renderTopbar();
    renderLiveMonitor();
    renderStats();
    renderVerifiedViews();
    renderQuarantinedView();
    renderRejectedView();
    renderHistoricalView();
    renderLedgerView();
    renderPdfArchiveView();
    renderNotificationsView();
    renderSettingsView();
    renderConsole();
  }

  function renderTopbar() {
    const connPill = document.getElementById("pillConn");
    const offlineBanner = document.getElementById("offlineBanner");
    const isLive = navigator.onLine && state.backendReachable;

    if (isLive) {
      connPill.className = "pill green";
      connPill.innerHTML = `<span class="dot"></span> Online (Live)`;
      offlineBanner.classList.remove("visible");
    } else {
      connPill.className = "pill amber";
      connPill.innerHTML = `<span class="dot"></span> Offline / Cached`;
      offlineBanner.classList.add("visible");
      document.getElementById("cacheAgeText").textContent = `Last synced ${formatAge(state.lastSyncUtc)}.`;
    }

    const wStatus = (state.liveState && state.liveState.status) || "AWAITING_FIRST_SCAN";
    const wp = document.getElementById("pillWorker");
    wp.textContent = `Worker: ${wStatus}`;
    wp.className = `pill ${statusPillClass(wStatus)}`;

    const cycNum = Math.max(state.lastKnownCycle, (state.liveState && state.liveState.current_cycle_number) || 0);
    document.getElementById("pillCycle").textContent = `Cycle #${cycNum}`;
    document.getElementById("sidebarSyncInfo").textContent = state.lastSyncUtc
      ? `Synced: ${formatAge(state.lastSyncUtc)}`
      : "Not synced yet";

    // Nav badges
    const activeVerified = state.games.filter((g) => g.is_active_promo && g.current_status === "VERIFIED");
    const quar = state.games.filter((g) => g.current_status === "QUARANTINED");
    const rej = state.games.filter((g) => g.current_status === "REJECTED");
    document.getElementById("badgeVerified").textContent = activeVerified.length;
    document.getElementById("badgeQuarantined").textContent = quar.length;
    document.getElementById("badgeRejected").textContent = rej.length;
    document.getElementById("badgeHistorical").textContent = state.games.length;
    document.getElementById("badgeCycles").textContent = state.cycles.length;
  }

  function renderStats() {
    const s = state.stats || {};
    document.getElementById("statCycles").textContent = s.total_completed_cycles ?? state.cycles.length;
    document.getElementById("statPages").textContent = s.total_pages_retrieved ?? 0;
    document.getElementById("statCandidates").textContent = s.total_candidates_discovered ?? 0;
    document.getElementById("statChecked").textContent = s.total_games_checked ?? 0;
    document.getElementById("statVerified").textContent = s.active_verified_opportunities ?? 0;
    document.getElementById("statQuarantined").textContent = s.total_quarantined_opportunities ?? 0;
    document.getElementById("statRejected").textContent = s.total_rejected_candidates ?? 0;
    document.getElementById("statNotifs").textContent = s.total_notifications_delivered ?? 0;
    document.getElementById("statErrors").textContent = s.total_errors ?? 0;
  }

  function renderLiveMonitor() {
    const w = state.liveState || {};
    const isLive = navigator.onLine && state.backendReachable;
    document.getElementById("stOnline").textContent = isLive ? "ONLINE" : "OFFLINE (CACHED)";
    document.getElementById("stLastSync").textContent = `Last sync: ${formatIST(state.lastSyncUtc)}`;
    document.getElementById("stWorkerStatus").textContent = w.status || "AWAITING_FIRST_SCAN";
    document.getElementById("stCurrentSource").textContent = `Source: ${w.current_source || "None"}`;
    document.getElementById("stCycleNum").textContent = `#${w.current_cycle_number || state.lastKnownCycle || 0}`;
    document.getElementById("stScanId").textContent = `Scan ID: ${w.current_scan_id || "None"}`;
    document.getElementById("stCountdown").textContent = formatCountdown(w.next_scheduled_scan_utc);
    document.getElementById("stNextAt").textContent = `Next: ${formatIST(w.next_scheduled_scan_utc)}`;
    document.getElementById("stLastScan").textContent = formatIST(w.last_completed_scan_utc);
    document.getElementById("stLastSuccess").textContent = `Success: ${formatIST(w.last_successful_scan_utc)}`;
    document.getElementById("stRegion").textContent = `${state.settings.target_region || "IN"} (${state.settings.default_currency || "INR"})`;
    document.getElementById("stCurrentOp").textContent = w.current_operation || "Idle";

    document.getElementById("lmStart").textContent = w.scan_started_at_utc ? formatIST(w.scan_started_at_utc) : "--";
    if (w.status === "RUNNING" && w.scan_started_at_utc) {
      const sec = Math.max(0, Math.floor((Date.now() - new Date(w.scan_started_at_utc).getTime()) / 1000));
      document.getElementById("lmElapsed").textContent = `${sec}s`;
    } else {
      document.getElementById("lmElapsed").textContent = w.status === "IDLE" ? "Idle" : "--";
    }
    document.getElementById("lmSearchReqs").textContent = w.steam_search_requests ?? 0;
    const totalPagesText = w.total_pages != null ? w.total_pages : "UNKNOWN";
    document.getElementById("lmPages").textContent = `${w.pages_retrieved ?? 0} / ${totalPagesText}`;
    document.getElementById("lmCands").textContent = `${w.candidates_discovered ?? 0} / ${w.unique_app_ids ?? 0}`;
    document.getElementById("lmAppDetails").textContent = w.appdetails_requests ?? 0;
    document.getElementById("lmWaiting").textContent = w.candidates_waiting ?? 0;
    document.getElementById("lmSecondary").textContent = w.secondary_requests ?? 0;
    document.getElementById("lmReqHealth").textContent = `${w.completed_requests ?? 0} / ${w.failed_requests ?? 0} / ${w.retried_requests ?? 0}`;

    const cg = w.current_game;
    const cgEl = document.getElementById("lmCurrentGame");
    if (cg) {
      cgEl.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
          <strong>${esc(cg.title)} (App ID: ${esc(cg.app_id || "N/A")})</strong>
          <span class="pill ${statusPillClass(cg.outcome)}">${esc(cg.outcome)}</span>
        </div>
        <div style="font-size:12px;color:var(--muted);margin-top:6px;">
          Price: ${esc(cg.original_price)} &rarr; ${esc(cg.current_price)} (${cg.discount_percent != null ? cg.discount_percent + "% OFF" : "N/A"})<br/>
          Source: ${esc(cg.source)} | Gate: ${esc(cg.gate)}<br/>
          ${cg.reason ? `Reason: ${esc(cg.reason)}` : ""}
        </div>
      `;
    } else {
      cgEl.textContent = "No game currently being checked (Worker is idle).";
    }
  }

  function renderConsole() {
    const box = document.getElementById("liveConsole");
    if (!state.consoleEvents.length) {
      box.innerHTML = `<div class="console-line" style="color:var(--muted);">No live worker events recorded yet.</div>`;
      return;
    }
    box.innerHTML = state.consoleEvents
      .map((ev) => `
        <div class="console-line">
          <span class="console-ts">${esc(formatIST(ev.created_at_utc))}</span>
          <span class="console-tag ${esc(ev.event_type)}">[${esc(ev.event_type)}]</span>
          <span>${esc(ev.message)}</span>
        </div>
      `)
      .join("");
    box.scrollTop = box.scrollHeight;
  }

  function gameCardHtml(g) {
    const claimedBtnClass = g.manual_claimed ? "btn sm green" : "btn sm";
    const claimedLabel = g.manual_claimed ? "CLAIMED" : "NOT CLAIMED";
    return `
      <article class="game-card">
        <div class="game-card-head">
          <div>
            <h3 class="game-title">${esc(g.title)}</h3>
            <div style="font-size:12px;color:var(--muted);">App ID: ${esc(g.app_id || "N/A")} &middot; ${esc(g.region)} (${esc(g.currency)})</div>
          </div>
          <span class="pill ${statusPillClass(g.current_status)}">${esc(g.current_status)}</span>
        </div>
        <div class="price-row">
          <span class="price-old">${esc(g.original_price_formatted)}</span>
          <span class="price-now">${esc(g.promotional_price_formatted)}</span>
          <span class="pill green">-${esc(g.discount_percent ?? 100)}%</span>
        </div>
        <div style="font-size:12px;color:var(--muted);">
          <div>Discovered: ${esc(formatIST(g.first_discovered_utc))}</div>
          <div>Last Verified: ${esc(formatIST(g.last_verified_utc))}</div>
          <div>Expires: ${esc(g.expiration_at || "Not disclosed by Steam")}</div>
          <div>Sources: ${esc((g.sources || []).join(", ") || "steam_search")}</div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:auto;padding-top:6px;">
          <a class="btn green sm" href="${esc(g.claim_url)}" target="_blank" rel="noopener">Open on Steam</a>
          <button class="${claimedBtnClass}" data-toggle-claim="${esc(g.game_key)}" data-claimed="${g.manual_claimed ? "1" : "0"}">${claimedLabel}</button>
          <button class="btn sm" data-inspect-gates="${esc(g.game_key)}">10-Gate Proof</button>
        </div>
      </article>
    `;
  }

  function bindGameCardActions(container) {
    container.querySelectorAll("[data-toggle-claim]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const key = btn.dataset.toggleClaim;
        const nextClaimed = btn.dataset.claimed !== "1";
        await toggleClaimedStatus(key, nextClaimed);
      });
    });
    container.querySelectorAll("[data-inspect-gates]").forEach((btn) => {
      btn.addEventListener("click", () => openGatesModal(btn.dataset.inspectGates));
    });
  }

  function renderVerifiedViews() {
    const active = state.games.filter((g) => g.is_active_promo && g.current_status === "VERIFIED");
    const empty = `<div class="empty-state" style="grid-column:1/-1;">No active Verified Direct Steam Free-to-Keep games right now. The background engine checks automatically every 60 minutes.</div>`;
    const html = active.length ? active.map(gameCardHtml).join("") : empty;

    ["homeVerifiedGrid", "verifiedGrid", "currentInventoryGrid"].forEach((id) => {
      const el = document.getElementById(id);
      el.innerHTML = html;
      bindGameCardActions(el);
    });
  }

  function renderQuarantinedView() {
    const rows = state.games.filter((g) => g.current_status === "QUARANTINED");
    const tbody = document.getElementById("quarantinedBody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px;">No quarantined candidates recorded.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((g) => `
      <tr>
        <td><strong>${esc(g.title)}</strong></td>
        <td>${esc(g.app_id || "N/A")}</td>
        <td>${esc(g.original_price_formatted)} &rarr; ${esc(g.promotional_price_formatted)}</td>
        <td>${esc((g.sources || []).join(", "))}</td>
        <td>${esc(g.decision_reason)}</td>
        <td>${esc(formatIST(g.last_verified_utc))}</td>
        <td><button class="btn sm" data-inspect-gates="${esc(g.game_key)}">Gates</button></td>
      </tr>
    `).join("");
    bindGameCardActions(tbody);
  }

  function renderRejectedView() {
    const rows = state.games.filter((g) => g.current_status === "REJECTED");
    const tbody = document.getElementById("rejectedBody");
    if (!rows.length) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:24px;">No rejected candidates recorded yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = rows.map((g) => `
      <tr>
        <td><strong>${esc(g.title)}</strong></td>
        <td>${esc(g.app_id || "N/A")}</td>
        <td>${esc(g.product_type)}</td>
        <td>${esc(g.original_price_formatted)} / ${esc(g.promotional_price_formatted)}</td>
        <td>${esc(g.decision_reason)}</td>
        <td>${esc(formatIST(g.last_verified_utc))}</td>
        <td><button class="btn sm" data-inspect-gates="${esc(g.game_key)}">Gates</button></td>
      </tr>
    `).join("");
    bindGameCardActions(tbody);
  }

  function renderHistoricalView() {
    const q = (document.getElementById("histSearch").value || "").trim().toLowerCase();
    const st = document.getElementById("histStatusFilter").value;
    const filtered = state.games.filter((g) => {
      if (st && g.current_status !== st) return false;
      if (q && !g.title.toLowerCase().includes(q) && !String(g.app_id || "").includes(q)) return false;
      return true;
    });
    const tbody = document.getElementById("historicalBody");
    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:24px;">No matching historical games found.</td></tr>`;
      return;
    }
    tbody.innerHTML = filtered.map((g) => `
      <tr>
        <td><strong>${esc(g.title)}</strong></td>
        <td>${esc(g.app_id || "N/A")}</td>
        <td><span class="pill ${statusPillClass(g.current_status)}">${esc(g.current_status)}</span></td>
        <td>${esc(g.original_price_formatted)} &rarr; ${esc(g.promotional_price_formatted)}</td>
        <td>${esc(formatIST(g.first_discovered_utc))}</td>
        <td>${esc(formatIST(g.last_verified_utc))}</td>
        <td>
          <button class="btn sm ${g.manual_claimed ? "green" : ""}" data-toggle-claim="${esc(g.game_key)}" data-claimed="${g.manual_claimed ? "1" : "0"}">
            ${g.manual_claimed ? "CLAIMED" : "NOT CLAIMED"}
          </button>
        </td>
        <td style="white-space:nowrap;">
          <a class="btn sm" href="${esc(g.claim_url)}" target="_blank" rel="noopener">Steam</a>
          <button class="btn sm" data-inspect-gates="${esc(g.game_key)}">Gates</button>
        </td>
      </tr>
    `).join("");
    bindGameCardActions(tbody);
  }

  function renderLedgerView() {
    const q = (document.getElementById("ledgerSearch").value || "").trim().toLowerCase();
    const dt = document.getElementById("ledgerDate").value;
    const st = document.getElementById("ledgerStatus").value;
    const filtered = state.cycles.filter((c) => {
      if (st && c.status !== st) return false;
      if (dt && !(c.started_at_ist || "").startsWith(dt)) return false;
      if (q && !String(c.cycle_number).includes(q) && !(c.scan_id || "").toLowerCase().includes(q)) return false;
      return true;
    });

    const recentBody = document.getElementById("homeRecentCyclesBody");
    const homeSlice = state.cycles.slice(0, 8);
    recentBody.innerHTML = homeSlice.length ? homeSlice.map((c) => `
      <tr>
        <td><strong>#${c.cycle_number}</strong></td>
        <td>${esc(formatIST(c.started_at_utc))}</td>
        <td><span class="pill ${statusPillClass(c.status)}">${esc(c.status)}</span></td>
        <td>${c.pages_retrieved ?? 0}</td>
        <td>${c.games_checked ?? 0}</td>
        <td>${c.verified_count ?? 0} / ${c.quarantined_count ?? 0} / ${c.rejected_count ?? 0}</td>
        <td>${Number(c.duration_seconds || 0).toFixed(1)}s</td>
        <td><button class="btn sm" data-open-pdf="${c.cycle_number}">Open PDF</button></td>
      </tr>
    `).join("") : `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:20px;">No completed scan cycles yet.</td></tr>`;

    const ledgerBody = document.getElementById("ledgerBody");
    ledgerBody.innerHTML = filtered.length ? filtered.map((c) => `
      <tr>
        <td><strong>#${c.cycle_number}</strong></td>
        <td style="font-family:var(--mono);font-size:12px;">${esc(c.scan_id)}</td>
        <td>${esc(formatIST(c.started_at_utc))}</td>
        <td><span class="pill ${statusPillClass(c.status)}">${esc(c.status)}</span></td>
        <td>${c.pages_retrieved ?? 0} / ${c.total_pages_exposed != null ? c.total_pages_exposed : "UNK"}</td>
        <td>${c.candidates_discovered ?? 0} / ${c.games_checked ?? 0}</td>
        <td>${c.verified_count ?? 0} / ${c.quarantined_count ?? 0} / ${c.rejected_count ?? 0}</td>
        <td>${(c.errors || []).length} / ${c.retried_requests ?? 0}</td>
        <td>${esc(c.notification_status || "NONE")}</td>
        <td style="white-space:nowrap;">
          <button class="btn sm" data-open-pdf="${c.cycle_number}">PDF Pop-Up</button>
          <a class="btn sm" href="${esc(apiUrl(`/api/v1/reports/cycle/${c.cycle_number}.pdf?download=true`))}" download>Save PDF</a>
          <a class="btn sm" href="${esc(apiUrl(`/api/v1/export/cycle/${c.cycle_number}.json`))}" download>JSON</a>
        </td>
      </tr>
    `).join("") : `<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:24px;">No scan cycles match your filters.</td></tr>`;

    document.querySelectorAll("[data-open-pdf]").forEach((btn) => {
      btn.onclick = () => openCyclePdfModal(Number(btn.dataset.openPdf));
    });
  }

  function renderPdfArchiveView() {
    const q = (document.getElementById("pdfSearch").value || "").trim().toLowerCase();
    const rows = state.cycles.filter((c) => {
      if (!q) return true;
      return String(c.cycle_number).includes(q) || (c.scan_id || "").toLowerCase().includes(q) || (c.started_at_ist || "").includes(q);
    });
    const tbody = document.getElementById("pdfArchiveBody");
    tbody.innerHTML = rows.length ? rows.map((c) => `
      <tr>
        <td><strong>#${c.cycle_number}</strong></td>
        <td style="font-family:var(--mono);font-size:12px;">${esc(c.scan_id)}</td>
        <td>${esc(formatIST(c.started_at_utc))}</td>
        <td><span class="pill ${statusPillClass(c.status)}">${esc(c.status)}</span></td>
        <td>${c.verified_count ?? 0}</td>
        <td style="white-space:nowrap;">
          <button class="btn primary sm" data-open-pdf="${c.cycle_number}">Open PDF Pop-Up</button>
          <a class="btn sm" href="${esc(apiUrl(`/api/v1/reports/cycle/${c.cycle_number}.pdf?download=true`))}" download>Download PDF</a>
        </td>
      </tr>
    `).join("") : `<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:24px;">No cycle PDF reports available yet.</td></tr>`;

    tbody.querySelectorAll("[data-open-pdf]").forEach((btn) => {
      btn.onclick = () => openCyclePdfModal(Number(btn.dataset.openPdf));
    });
  }

  function renderNotificationsView() {
    const tbody = document.getElementById("notificationsBody");
    if (!state.notifications.length) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;color:var(--muted);padding:24px;">No notifications dispatched yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = state.notifications.map((n) => `
      <tr>
        <td>${esc(formatIST(n.updated_at_utc || n.created_at_utc))}</td>
        <td>#${esc(n.cycle_number)}</td>
        <td><strong>${esc(n.game_title)}</strong></td>
        <td><span class="pill ${statusPillClass(n.classification)}">${esc(n.classification)}</span></td>
        <td>${esc(n.provider)}</td>
        <td><span class="pill ${statusPillClass(n.status)}">${esc(n.status)}</span></td>
        <td>${n.attempt_count}/${n.max_attempts}</td>
        <td style="font-size:12px;color:var(--muted);">${n.last_http_status || ""} ${esc(n.last_error || "OK")}</td>
      </tr>
    `).join("");
  }

  function renderSettingsView() {
    document.getElementById("cfgApiBase").value = state.apiBase;
    const grid = document.getElementById("serverSettingsGrid");
    const entries = Object.entries(state.settings || {});
    grid.innerHTML = entries.length
      ? entries.map(([k, v]) => `<div class="kv-item"><div class="kv-label">${esc(k)}</div><div class="kv-val">${esc(v)}</div></div>`).join("")
      : `<div class="kv-item">Connect to backend to view server settings.</div>`;
  }

  // ---------------- Interactive Modals & Actions ----------------
  function openCyclePdfModal(cycleNumber) {
    document.getElementById("pdfModalTitle").textContent = `Cycle #${cycleNumber} Hourly Audit PDF`;
    const cycPdf = state.staticMode ? `./api/v1/reports/cycle/${cycleNumber}.pdf` : apiUrl(`/api/v1/reports/cycle/${cycleNumber}.pdf`);
    document.getElementById("pdfFrame").src = cycPdf;
    document.getElementById("pdfModalDownload").href = state.staticMode ? `./api/v1/reports/cycle/${cycleNumber}.pdf` : apiUrl(`/api/v1/reports/cycle/${cycleNumber}.pdf?download=true`);
    document.getElementById("pdfModal").classList.add("open");
  }

  function openHistoricalPdfModal() {
    document.getElementById("pdfModalTitle").textContent = "Complete Historical Audit PDF";
    document.getElementById("pdfFrame").src = state.staticMode ? "./api/v1/reports/historical.pdf" : apiUrl("/api/v1/reports/historical.pdf");
    document.getElementById("pdfModalDownload").href = state.staticMode ? "./api/v1/reports/historical.pdf" : apiUrl("/api/v1/reports/historical.pdf?download=true");
    document.getElementById("pdfModal").classList.add("open");
  }

  function openGatesModal(gameKey) {
    const game = state.games.find((g) => g.game_key === gameKey);
    if (!game) return;
    document.getElementById("gatesModalTitle").textContent = `10-Gate Proof: ${game.title}`;
    const gates = game.latest_validation || [];
    const body = document.getElementById("gatesModalBody");
    body.innerHTML = `
      <p style="margin-top:0;color:var(--muted);">
        Final Status: <span class="pill ${statusPillClass(game.current_status)}">${esc(game.current_status)}</span>
        &middot; Reason: ${esc(game.decision_reason)}
      </p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>#</th><th>Gate</th><th>Status</th><th>Evidence</th><th>Source</th><th>Reason</th></tr></thead>
          <tbody>
            ${gates.map((gt) => `
              <tr>
                <td>${gt.gate_number}</td>
                <td><strong>${esc(gt.gate_name)}</strong></td>
                <td><span class="pill ${gt.status === "PASS" ? "green" : (gt.status === "FAIL" ? "red" : "amber")}">${esc(gt.status)}</span></td>
                <td style="font-size:12px;">${esc(gt.evidence)}</td>
                <td style="font-size:12px;">${esc(gt.source)}</td>
                <td style="font-size:12px;">${esc(gt.reason)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      </div>
    `;
    document.getElementById("gatesModal").classList.add("open");
  }

  async function toggleClaimedStatus(gameKey, claimed) {
    // Optimistically update local IndexedDB & UI, then sync to backend
    const g = state.games.find((x) => x.game_key === gameKey);
    if (g) {
      g.manual_claimed = claimed;
      await idbPutMany("games", [g]);
      renderAll();
    }
    if (navigator.onLine) {
      try {
        await fetch(apiUrl(`/api/v1/games/${encodeURIComponent(gameKey)}/claimed`), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ claimed }),
        });
      } catch (_) {}
    }
  }

  // ---------------- Event Bindings & Bootstrap ----------------
  function initUI() {
    // Navigation
    document.querySelectorAll(".nav-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        document.querySelectorAll(".view-panel").forEach((p) => p.classList.remove("active"));
        document.getElementById(`view-${btn.dataset.view}`).classList.add("active");
        document.getElementById("sidebar").classList.remove("open");
      });
    });

    document.getElementById("mobileMenuBtn").addEventListener("click", () => {
      document.getElementById("sidebar").classList.toggle("open");
    });

    document.getElementById("btnSyncNow").addEventListener("click", () => syncWithBackend(false));
    document.getElementById("btnRetryConn").addEventListener("click", () => syncWithBackend(true));
    document.getElementById("btnHistoricalPdf").addEventListener("click", openHistoricalPdfModal);
    document.getElementById("btnViewFullHistoricalPdf").addEventListener("click", openHistoricalPdfModal);
    document.getElementById("btnDownloadFullHistoricalPdf").addEventListener("click", () => {
      window.location.href = state.staticMode ? "./api/v1/reports/historical.pdf" : apiUrl("/api/v1/reports/historical.pdf?download=true");
    });
    document.getElementById("btnExportGamesCsv").addEventListener("click", () => {
      window.location.href = state.staticMode ? "./api/v1/export/games.csv" : apiUrl("/api/v1/export/csv?dataset=games");
    });
    document.getElementById("btnExportLedgerCsv").addEventListener("click", () => {
      window.location.href = state.staticMode ? "./api/v1/export/cycles.csv" : apiUrl("/api/v1/export/csv?dataset=cycles");
    });
    document.getElementById("btnDownloadFullBackup").addEventListener("click", () => {
      window.location.href = state.staticMode ? "./api/v1/export/full-backup.json" : apiUrl("/api/v1/export/json");
    });

    document.getElementById("btnSaveApiBase").addEventListener("click", () => {
      state.apiBase = (document.getElementById("cfgApiBase").value || "").trim().replace(/\/+$/, "");
      localStorage.setItem("sftk_api_base", state.apiBase);
      syncWithBackend(false);
      connectLiveStream();
    });

    document.getElementById("btnClearLocalCache").addEventListener("click", async () => {
      await idbClearAll();
      state.cycles = [];
      state.games = [];
      state.notifications = [];
      state.lastKnownCycle = 0;
      renderAll();
    });

    document.getElementById("btnCloseCatchup").addEventListener("click", acknowledgeCatchup);
    document.getElementById("btnAckCatchup").addEventListener("click", acknowledgeCatchup);
    document.getElementById("btnClosePdf").addEventListener("click", () => {
      document.getElementById("pdfModal").classList.remove("open");
      document.getElementById("pdfFrame").src = "about:blank";
    });
    document.getElementById("btnCloseGates").addEventListener("click", () => {
      document.getElementById("gatesModal").classList.remove("open");
    });

    ["histSearch", "histStatusFilter"].forEach((id) =>
      document.getElementById(id).addEventListener("input", renderHistoricalView)
    );
    ["ledgerSearch", "ledgerDate", "ledgerStatus"].forEach((id) =>
      document.getElementById(id).addEventListener("input", renderLedgerView)
    );
    document.getElementById("pdfSearch").addEventListener("input", renderPdfArchiveView);

    // Online / Offline automatic reconnection
    window.addEventListener("online", () => {
      state.online = true;
      syncWithBackend(true);
      connectLiveStream();
    });
    window.addEventListener("offline", () => {
      state.online = false;
      state.backendReachable = false;
      renderAll();
    });

    // PWA Install Prompt
    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      state.deferredInstallPrompt = e;
      const btn = document.getElementById("btnInstallPwa");
      btn.style.display = "inline-flex";
      btn.onclick = async () => {
        if (!state.deferredInstallPrompt) return;
        state.deferredInstallPrompt.prompt();
        await state.deferredInstallPrompt.userChoice;
        state.deferredInstallPrompt = null;
        btn.style.display = "none";
      };
    });

    // Register Service Worker
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }

    // 1-second tick for countdown & elapsed time (pure clock math, never fake progress)
    setInterval(() => {
      renderLiveMonitor();
      if (!navigator.onLine || !state.backendReachable) {
        document.getElementById("cacheAgeText").textContent = `Last synced ${formatAge(state.lastSyncUtc)}.`;
      }
    }, 1000);
  }

  // Boot sequence: cache-first render -> online sync -> SSE live stream
  initUI();
  loadFromCache().then(() => {
    syncWithBackend(false);
    connectLiveStream();
  });
})();
