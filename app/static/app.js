"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TERMINAL = ["completed", "partial", "failed", "cancelled"];

const state = { me: null, nav: 0, poll: null, items: {}, itemSeq: 0, dlItem: null };

async function api(path, opts = {}) {
  const o = { ...opts, headers: { "Content-Type": "application/json", ...(opts.headers || {}) } };
  if (o.body && typeof o.body !== "string") o.body = JSON.stringify(o.body);
  const r = await fetch(path, o);
  if (r.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("Not logged in");
  }
  const isJson = (r.headers.get("content-type") || "").includes("json");
  const data = isJson ? await r.json() : null;
  if (!r.ok) {
    const d = data && data.detail;
    throw new Error(typeof d === "string" ? d : d ? JSON.stringify(d) : r.statusText);
  }
  return data;
}

let toastTimer;
function toast(msg, err = false) {
  const t = $("#toast");
  t.innerHTML = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), err ? 6000 : 3500);
}

/* ---------- formatting ---------- */
const big = (u, n = 600) => (u ? u.replace(/\/\d+x\d+(bb)?\./, `/${n}x${n}bb.`) : u);
const img = (u, cls = "") => (u ? `<img loading="lazy" class="${cls}" src="${esc(u)}" alt="">` : `<div class="noart ${cls}"></div>`);
const dur = (ms) => (ms ? `${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, "0")}` : "");
const qBadges = (q) => (q || []).map((x) => `<span class="badge q-${x.toLowerCase().replace(/[^a-z]/g, "")}">${esc(x)}</span>`).join("");
const relPath = (p) => (p || "").replace(/^\/music\//, "");
const ago = (t) => {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(t * 1000).toLocaleDateString();
};
const statusLabel = (s) => ({ in_library: "in library", exists: "already downloaded" }[s] || s);

function remember(obj) {
  const key = "i" + ++state.itemSeq;
  state.items[key] = obj;
  return key;
}

function dlItem(kind, x, sf) {
  return remember({ kind, url: x.url, title: x.name, artist: x.artist, artwork: x.artwork, quality: x.quality, storefront: sf });
}

/* ---------- auth ---------- */
function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
}

async function boot() {
  try {
    state.me = await api("/api/me");
  } catch {
    return showLogin();
  }
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#nd-link").href = state.me.navidromeUrl;
  refreshStatus();
  refreshActive();
  setInterval(refreshStatus, 30000);
  setInterval(refreshActive, 5000);
  route();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { method: "POST", body: { username: f.get("username"), password: f.get("password") } });
    e.target.reset();
    boot();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" }).catch(() => {});
  showLogin();
});

async function refreshStatus() {
  try {
    const { wrapper } = await api("/api/status");
    state.wrapper = wrapper;
    const el = $("#wm-status");
    el.className = "pill " + (wrapper.ok ? "ok" : "err");
    el.textContent = wrapper.ok ? `wrapper · ${wrapper.regions.join(", ").toUpperCase()}` : "wrapper offline";
    el.title = wrapper.ok ? `wrapper-manager ${wrapper.url} · ${wrapper.clients} client(s) · searching the ${wrapper.regions[0]} catalog` : wrapper.error;
    const banner = $("#wm-banner");
    if (banner) banner.classList.toggle("hidden", wrapper.ok);
    renderWmInfo();
  } catch {}
}

function renderWmInfo() {
  const w = state.wrapper;
  $("#wm-info").innerHTML = w
    ? w.ok
      ? `✅ Ready · regions <b>${esc(w.regions.join(", ").toUpperCase())}</b> · ${w.clients} client(s)`
      : `⚠️ ${esc(w.error || "Unavailable")}`
    : "";
}

/* ---------- wrapper-manager account login ---------- */
const wm = { flow: null };

function wmStep(step) {
  $("#wm-step-login").classList.toggle("hidden", step !== "login");
  $("#wm-step-2fa").classList.toggle("hidden", step !== "2fa");
  $("#wm-submit").textContent = step === "2fa" ? "Verify" : "Sign in";
  $("#wm-submit").classList.toggle("hidden", step === "done");
}

function openWrapperDialog() {
  renderWmInfo();
  refreshStatus();
  $("#wm-msg").textContent = "";
  $("#wm-form").reset();
  wm.flow = null;
  wmStep("login");
  $("#wm-dialog").showModal();
}

$("#wm-status").addEventListener("click", openWrapperDialog);
$("#wm-close").addEventListener("click", () => $("#wm-dialog").close());

$("#wm-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const msg = $("#wm-msg");
  const btn = $("#wm-submit");
  btn.disabled = true;
  try {
    let r;
    if (!wm.flow) {
      msg.textContent = "Starting wrapper and signing in… this can take a minute.";
      r = await api("/api/wrapper/login", { method: "POST", body: { username: f.username.value, password: f.password.value } });
      f.password.value = "";
    } else {
      msg.textContent = "Verifying code…";
      r = await api(`/api/wrapper/login/${wm.flow}/2fa`, { method: "POST", body: { code: f.code.value } });
    }
    wm.flow = r.flow;
    while (r.state === "pending") r = await api(`/api/wrapper/login/${wm.flow}`);
    msg.textContent = r.message;
    if (r.state === "2fa") {
      wmStep("2fa");
      f.code.value = "";
      f.code.focus();
    } else if (r.state === "ok") {
      wmStep("done");
      toast("Apple Music account logged in");
      await refreshStatus();
      setTimeout(() => $("#wm-dialog").close(), 1500);
    } else {
      wm.flow = null;
      wmStep("login");
    }
  } catch (err) {
    msg.textContent = err.message;
    wm.flow = null;
    wmStep("login");
  } finally {
    btn.disabled = false;
  }
});

async function refreshActive() {
  try {
    const jobs = await api("/api/jobs?limit=50");
    const n = jobs.filter((j) => !TERMINAL.includes(j.status)).length;
    const c = $("#active-count");
    c.textContent = n;
    c.classList.toggle("hidden", !n);
  } catch {}
}

/* ---------- routing ---------- */
window.addEventListener("hashchange", route);

function route() {
  clearInterval(state.poll);
  state.poll = null;
  const token = ++state.nav;
  const [path, qs] = (location.hash.slice(1) || "/").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  const params = new URLSearchParams(qs || "");
  const view = $("#view");
  const ctx = { view, token, alive: () => token === state.nav };
  window.scrollTo(0, 0);
  switch (parts[0]) {
    case "search":
      $("#search-input").value = params.get("q") || "";
      return renderSearch(ctx, params.get("q"));
    case "album":
    case "playlist":
    case "libplaylist":
    case "artist":
    case "song":
      return renderItem(ctx, parts[0], parts[1], parts[2]);
    case "jobs":
      return parts[1] ? renderJob(ctx, parts[1]) : renderJobs(ctx);
    default:
      return renderHome(ctx);
  }
}

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#search-input").value.trim();
  if (!q) return;
  if (/^https?:\/\//i.test(q)) {
    try {
      const ref = await api("/api/resolve?url=" + encodeURIComponent(q));
      location.hash = `#/${ref.type}/${ref.storefront}/${encodeURIComponent(ref.id)}`;
    } catch (err) {
      toast(esc(err.message), true);
    }
    return;
  }
  location.hash = `#/search?q=${encodeURIComponent(q)}`;
});

// navigator.clipboard only exists in secure contexts, so plain-HTTP LAN access needs the legacy path
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {}
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {}
  ta.remove();
  return ok;
}

// Delegated clicks: download buttons, copy buttons and tile navigation
document.addEventListener("click", (e) => {
  const dl = e.target.closest("[data-dl]");
  if (dl) {
    e.preventDefault();
    e.stopPropagation();
    return openDownload(state.items[dl.dataset.dl]);
  }
  const cp = e.target.closest("[data-copy]");
  if (cp) {
    e.preventDefault();
    e.stopPropagation();
    const value = cp.dataset.copy;
    return copyText(value).then((ok) =>
      ok ? toast(`Copied playlist ID <code>${esc(value)}</code>`) : toast(`Couldn't copy. Playlist ID: <code>${esc(value)}</code>`, true));
  }
  const nav = e.target.closest("[data-href]");
  if (nav && !e.target.closest("a")) location.hash = nav.dataset.href;
});

const spinner = (ctx) => (ctx.view.innerHTML = `<div class="spinner"></div>`);
const fail = (ctx, err) => ctx.alive() && (ctx.view.innerHTML = `<div class="empty">⚠️ ${esc(err.message)}</div>`);

/* ---------- home ---------- */
async function renderHome(ctx) {
  const offline = state.wrapper && !state.wrapper.ok;
  ctx.view.innerHTML = `
    <div id="wm-banner" class="banner ${offline ? "" : "hidden"}">
      <span>⚠️ wrapper-manager has no working Apple Music account, so downloads will fail until you log in.</span>
      <button class="btn primary small" onclick="openWrapperDialog()">Log in</button>
    </div>
    <div class="home-hint">
      <h1>Search Apple Music</h1>
      <p>Find an album, song, artist or playlist, or paste a <code>music.apple.com</code> link.
      Downloads land in your Navidrome library as FLAC (or MP3 when lossless isn't available).
      Playlists become Navidrome playlists.</p>
    </div>
    <h2 class="section">Recent jobs</h2><div id="recent" class="jobs"></div>`;
  try {
    const jobs = await api("/api/jobs?limit=6");
    if (!ctx.alive()) return;
    $("#recent").innerHTML = jobs.length ? jobs.map(jobCard).join("") : `<div class="empty">No downloads yet</div>`;
  } catch {}
}

/* ---------- search ---------- */
function albumTile(a, sf) {
  return `<div class="tile" data-href="#/album/${sf}/${a.id}" data-album="${esc(a.id)}">
    ${img(a.artwork)}
    <div class="t-badges">${qBadges(a.quality)}<span class="lib-slot"></span></div>
    <button class="btn primary small t-dl" data-dl="${dlItem("album", a, sf)}" title="Download">⬇</button>
    <div class="t-name" title="${esc(a.name)}">${esc(a.name)}</div>
    <div class="t-sub">${esc(a.artist)}${a.year ? " · " + a.year : ""}${a.trackCount ? " · " + a.trackCount + " tracks" : ""}</div>
  </div>`;
}

function simpleTile(kind, x, sf, sub) {
  return `<div class="tile ${kind === "artist" ? "round" : ""}" data-href="#/${kind}/${sf}/${encodeURIComponent(x.id)}">
    ${img(x.artwork)}
    <button class="btn primary small t-dl" data-dl="${dlItem(kind, x, sf)}" title="Download">⬇</button>
    <div class="t-name" title="${esc(x.name)}">${esc(x.name)}</div>
    <div class="t-sub">${esc(sub || "")}</div>
  </div>`;
}

function songRows(songs, sf, { showAlbum = true, numbered = false } = {}) {
  return songs
    .map(
      (s, i) => `<tr data-song="${esc(s.id)}">
      <td class="num">${numbered ? (s.track ?? i + 1) : img(s.artwork, "mini")}</td>
      <td class="ellip"><div>${esc(s.name)}</div><div class="sub">${esc(s.artist)}</div></td>
      ${showAlbum ? `<td class="ellip small hide-sm">${esc(s.album || "")}</td>` : ""}
      <td class="small">${dur(s.duration)}</td>
      <td>${qBadges(s.quality)}</td>
      <td class="lib-cell small"></td>
      <td><button class="btn ghost small" data-dl="${dlItem("song", s, sf)}" title="Download this track">⬇</button></td>
    </tr>`
    )
    .join("");
}

function trackTable(songs, sf, opts) {
  const showAlbum = opts?.showAlbum !== false;
  return `<table class="tracks"><thead><tr><th>#</th><th>Title</th>${showAlbum ? '<th class="hide-sm">Album</th>' : ""}
    <th>Time</th><th>Quality</th><th>Navidrome</th><th></th></tr></thead><tbody>${songRows(songs, sf, opts)}</tbody></table>`;
}

async function libraryCheck(ctx, albums, songs) {
  if (!albums.length && !songs.length) return null;
  try {
    const res = await api("/api/library/check", {
      method: "POST",
      body: {
        albums: albums.map((a) => ({ id: a.id, name: a.name, artist: a.artist })),
        songs: songs.map((s) => ({ id: s.id, isrc: s.isrc, title: s.name, artist: s.artist, album: s.album })),
      },
    });
    if (!ctx.alive()) return null;
    for (const a of albums) {
      const hit = res.albums[a.id];
      if (!hit) continue;
      const partial = a.trackCount && hit.songCount < a.trackCount;
      $$(`[data-album="${CSS.escape(a.id)}"] .lib-slot`).forEach(
        (el) => (el.outerHTML = `<span class="badge ${partial ? "lib-partial" : "lib"}" title="In Navidrome">${partial ? `${hit.songCount}/${a.trackCount}` : "✓ In library"}</span>`)
      );
    }
    for (const s of songs) {
      const hit = res.songs[s.id];
      $$(`tr[data-song="${CSS.escape(s.id)}"] .lib-cell`).forEach(
        (el) => (el.innerHTML = hit ? `<a href="${esc(hit.link)}" target="_blank" rel="noopener" class="chip ok" title="${esc(hit.path)}">✓ ${esc(hit.suffix || "")}</a>` : `<span class="muted">—</span>`)
      );
    }
    return res;
  } catch {
    return null;
  }
}

async function renderSearch(ctx, q) {
  if (!q) return renderHome(ctx);
  spinner(ctx);
  let r;
  try {
    r = await api(`/api/search?q=${encodeURIComponent(q)}`);
  } catch (err) {
    return fail(ctx, err);
  }
  if (!ctx.alive()) return;
  const sf = r.storefront;
  const sec = (title, html) => (html ? `<h2 class="section">${title}</h2>${html}` : "");
  ctx.view.innerHTML =
    sec("Albums", r.albums.length && `<div class="grid">${r.albums.map((a) => albumTile(a, sf)).join("")}</div>`) +
    sec("Songs", r.songs.length && trackTable(r.songs, sf)) +
    sec("Artists", r.artists.length && `<div class="grid">${r.artists.map((a) => simpleTile("artist", a, sf, (a.genres || []).join(", "))).join("")}</div>`) +
    sec("Playlists", r.playlists.length && `<div class="grid">${r.playlists.map((p) => simpleTile("playlist", p, sf, p.artist)).join("")}</div>`) ||
    `<div class="empty">No results for “${esc(q)}”</div>`;
  libraryCheck(ctx, r.albums, r.songs);
}

/* ---------- item pages ---------- */
function hero(kind, x, sf, extra = "") {
  return `<div class="hero ${kind === "artist" ? "round" : ""}">
    ${img(big(x.artwork))}
    <div>
      <div class="h-kind">${kind}</div>
      <h1>${esc(x.name)}</h1>
      <div class="h-meta">${extra}</div>
      <div class="h-actions">
        <button class="btn primary" data-dl="${dlItem(kind, x, sf)}">⬇ Download${kind === "playlist" ? " & create playlist" : ""}</button>
        <a class="btn ghost" href="${esc(x.url)}" target="_blank" rel="noopener">Apple Music ↗</a>
        <span id="hero-lib"></span>
      </div>
    </div>
  </div>`;
}

async function renderItem(ctx, kind, sf, id) {
  spinner(ctx);
  let x;
  try {
    x = await api(`/api/${kind}/${encodeURIComponent(sf)}/${encodeURIComponent(id)}`);
  } catch (err) {
    return fail(ctx, err);
  }
  if (!ctx.alive()) return;
  sf = x.storefront || sf;

  if (kind === "album") {
    const meta = [esc(x.artist), x.year, `${x.tracks.length} tracks`, qBadges(x.quality), x.label && esc(x.label)].filter(Boolean).join(" · ");
    ctx.view.innerHTML = hero("album", x, sf, meta) + trackTable(x.tracks, sf, { showAlbum: false, numbered: true }) +
      (x.copyright ? `<p class="sub">${esc(x.copyright)}</p>` : "");
    const res = await libraryCheck(ctx, [], x.tracks);
    if (res) {
      const have = x.tracks.filter((t) => res.songs[t.id]);
      const first = have[0] && res.songs[have[0].id];
      if (first) {
        $("#hero-lib").innerHTML = `<a class="chip ${have.length === x.tracks.length ? "ok" : "warning"}" href="${esc(first.link)}" target="_blank" rel="noopener">
          ${have.length === x.tracks.length ? "✓ In Navidrome" : `${have.length}/${x.tracks.length} in Navidrome`} ↗</a>`;
      }
    }
  } else if (kind === "playlist" || kind === "libplaylist") {
    const meta = [esc(x.artist || ""), `${x.tracks.length} tracks`,
      x.unavailable ? `<span title="Uploaded or otherwise non-catalog tracks can't be downloaded">${x.unavailable} not on Apple Music</span>` : ""].filter(Boolean).join(" · ");
    ctx.view.innerHTML = hero("playlist", x, sf, `${meta}${x.description ? `<br><span class="sub">${esc(x.description)}</span>` : ""}`) +
      trackTable(x.tracks, sf);
    const res = await libraryCheck(ctx, [], x.tracks);
    if (res) {
      const n = x.tracks.filter((t) => res.songs[t.id]).length;
      $("#hero-lib").innerHTML = `<span class="chip ${n === x.tracks.length ? "ok" : ""}">${n}/${x.tracks.length} already in Navidrome</span>`;
    }
  } else if (kind === "artist") {
    ctx.view.innerHTML = hero("artist", x, sf, `${esc((x.genres || []).join(", "))} · ${x.albums.length} releases`) +
      `<div class="grid">${x.albums.map((a) => albumTile(a, sf)).join("")}</div>`;
    libraryCheck(ctx, x.albums, []);
  } else if (kind === "song") {
    const albumId = (x.url || "").match(/\/album\/[^/]+\/(\d+)/);
    ctx.view.innerHTML = hero("song", x, sf, `${esc(x.artist)} · ${albumId ? `<a href="#/album/${sf}/${albumId[1]}">${esc(x.album)}</a>` : esc(x.album)} · ${x.year} · ${dur(x.duration)} · ${qBadges(x.quality)}`) +
      `<table class="tracks hidden"><tbody>${songRows([x], sf)}</tbody></table>`;
    const res = await libraryCheck(ctx, [], [x]);
    const hit = res && res.songs[x.id];
    if (hit) $("#hero-lib").innerHTML = `<a class="chip ok" href="${esc(hit.link)}" target="_blank" rel="noopener">✓ In Navidrome ↗</a>`;
  }
}

/* ---------- download dialog ---------- */
function openDownload(item) {
  if (!item) return;
  state.dlItem = item;
  const d = $("#dl-dialog");
  const f = $("#dl-form");
  $("#dl-target").innerHTML = `${img(item.artwork)}<div><b>${esc(item.title)}</b><div class="sub">${esc(item.kind)} · ${esc(item.artist || "")}</div></div>`;
  $("#dl-playlist-opts").classList.toggle("hidden", item.kind !== "playlist");
  $("#dl-artist-opts").classList.toggle("hidden", item.kind !== "artist");
  f.playlist_name.value = item.kind === "playlist" ? item.title || "" : "";
  // The Navidrome ISRC pre-check only runs for playlist and album jobs
  const canCheckLibrary = item.kind === "playlist" || item.kind === "album";
  const libraryOpt = f.existing.querySelector('option[value="library"]');
  libraryOpt.hidden = libraryOpt.disabled = !canCheckLibrary;
  f.existing.value = item.kind === "playlist" ? "library" : "file";
  const lossless = !item.quality || item.quality.some((q) => q === "Lossless" || q === "Hi-Res");
  f.codec.value = lossless ? "alac" : "aac-legacy";
  d.showModal();
}

$("#dl-dialog").addEventListener("close", async () => {
  const d = $("#dl-dialog");
  if (d.returnValue !== "ok" || !state.dlItem) return;
  const f = $("#dl-form");
  const it = state.dlItem;
  const body = {
    url: it.url,
    codec: f.codec.value,
    force: f.existing.value === "overwrite",
    skip_in_library: f.existing.value === "library",
    fallback_aac: f.fallback_aac.checked,
    kind: it.kind,
    title: it.title,
    artist: it.artist,
    artwork: it.artwork,
  };
  if (it.kind === "playlist") {
    body.create_playlist = f.create_playlist.checked;
    body.playlist_name = f.playlist_name.value.trim() || null;
  } else if (it.kind === "artist") {
    body.include_participate = f.include_participate.checked;
  }
  try {
    const { id } = await api("/api/jobs", { method: "POST", body });
    toast(`Queued <b>${esc(it.title)}</b> · <a href="#/jobs/${id}">view job</a>`);
    refreshActive();
  } catch (err) {
    toast(esc(err.message), true);
  }
});

/* ---------- jobs ---------- */
function jobProgress(j) {
  const c = j.counts || {};
  const total = Object.values(c).reduce((a, b) => a + b, 0);
  const done = total - (c.queued || 0) - (c.downloading || 0) - (c.decrypting || 0);
  return { total, done, pct: total ? Math.round((done / total) * 100) : 0, c };
}

function ndLinks(j) {
  const links = [];
  if (j.nd_playlist && j.nd_playlist.link) links.push(`<a href="${esc(j.nd_playlist.link)}" target="_blank" rel="noopener">▶ Playlist “${esc(j.nd_playlist.name)}” (${j.nd_playlist.count} tracks${j.nd_playlist.missing ? `, ${j.nd_playlist.missing} missing` : ""})</a>`);
  if (j.nd_playlist && j.nd_playlist.id) links.push(`<button type="button" class="copy-id" data-copy="${esc(j.nd_playlist.id)}" title="Copy Navidrome playlist ID ${esc(j.nd_playlist.id)}">⧉ Copy ID</button>`);
  for (const a of (j.nd_albums || []).slice(0, 6)) links.push(`<a href="${esc(a.link)}" target="_blank" rel="noopener">💿 ${esc(a.name || "Album")}</a>`);
  if ((j.nd_albums || []).length > 6) links.push(`<span class="sub">+${j.nd_albums.length - 6} more albums</span>`);
  return links.length ? `<div class="links">${links.join("")}</div>` : "";
}

function jobCard(j) {
  const p = jobProgress(j);
  const c = p.c;
  const summary = [
    c.saved && `${c.saved} new`,
    c.exists && `${c.exists} existing`,
    c.in_library && `${c.in_library} in library`,
    c.warning && `${c.warning} warnings`,
    c.failed && `${c.failed} failed`,
  ].filter(Boolean).join(" · ");
  const active = !TERMINAL.includes(j.status);
  return `<div class="job" data-href="#/jobs/${j.id}">
    ${img(j.artwork)}
    <div>
      <div class="j-title">${esc(j.title || j.url)}</div>
      <div class="sub">${esc(j.kind || "")}${j.artist ? " · " + esc(j.artist) : ""} · ${ago(j.created)}${j.username ? " · " + esc(j.username) : ""}</div>
      ${active && p.total ? `<div class="progress"><div style="width:${p.pct}%"></div></div>` : ""}
      <div class="sub">${p.total ? `${p.done}/${p.total} tracks` : ""}${summary ? " · " + summary : ""}${j.message ? ` · <span title="${esc(j.message)}">${esc(j.message.slice(0, 120))}</span>` : ""}</div>
      ${ndLinks(j)}
    </div>
    <div class="j-right">
      <span class="chip ${esc(j.status)}">${esc(j.status)}${j.phase ? " · " + esc(j.phase) : ""}</span>
    </div>
  </div>`;
}

async function renderJobs(ctx) {
  ctx.view.innerHTML = `<h2 class="section">Jobs</h2><div id="joblist" class="jobs"><div class="spinner"></div></div>`;
  const load = async () => {
    try {
      const jobs = await api("/api/jobs?limit=100");
      if (!ctx.alive()) return;
      $("#joblist").innerHTML = jobs.length ? jobs.map(jobCard).join("") : `<div class="empty">No downloads yet. Search for something to get started.</div>`;
    } catch {}
  };
  await load();
  if (ctx.alive()) state.poll = setInterval(load, 2500);
}

async function renderJob(ctx, id) {
  spinner(ctx);
  let logOpen = false;
  const load = async () => {
    let j;
    try {
      j = await api(`/api/jobs/${encodeURIComponent(id)}`);
    } catch (err) {
      clearInterval(state.poll);
      return fail(ctx, err);
    }
    if (!ctx.alive()) return;
    const det = $("#job-log");
    if (det) logOpen = det.open;
    const pre = $("#job-log pre");
    const atBottom = pre ? pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20 : true;
    const active = !TERMINAL.includes(j.status);
    const p = jobProgress(j);
    const hasBad = j.tracks.some((t) => !["saved", "exists", "in_library"].includes(t.status));
    ctx.view.innerHTML = `
      <div class="hero">
        ${img(big(j.artwork))}
        <div>
          <div class="h-kind">${esc(j.kind || "job")} · job #${j.id}</div>
          <h1>${esc(j.title || j.url)}</h1>
          <div class="h-meta">${esc(j.artist || "")} · <a href="${esc(j.url)}" target="_blank" rel="noopener">Apple Music ↗</a>
            · codec ${esc((j.options || {}).codec || "alac")} · ${ago(j.created)}</div>
          <div class="h-actions">
            <span class="chip ${esc(j.status)}">${esc(j.status)}${j.phase ? " · " + esc(j.phase) : ""}</span>
            ${p.total ? `<span class="sub">${p.done}/${p.total} tracks</span>` : ""}
            ${active ? `<button class="btn ghost small" id="job-cancel">Cancel</button>` : ""}
            ${!active && hasBad ? `<button class="btn primary small" id="job-retry-failed">Retry failed tracks</button>` : ""}
            ${!active ? `<button class="btn ghost small" id="job-retry-all">Run again</button>` : ""}
            ${!active ? `<button class="btn ghost small" id="job-delete">Delete</button>` : ""}
          </div>
          ${active && p.total ? `<div class="progress"><div style="width:${p.pct}%"></div></div>` : ""}
          ${j.message ? `<p class="sub">${esc(j.message)}</p>` : ""}
          ${ndLinks(j)}
        </div>
      </div>
      <table class="tracks"><thead><tr><th>#</th><th>Track</th><th>Status</th><th class="hide-sm">File</th><th>Navidrome</th></tr></thead><tbody>
      ${j.tracks.map((t, i) => `<tr>
          <td class="num">${i + 1}</td>
          <td class="ellip"><div>${esc(t.title || t.id)}</div><div class="sub">${esc(t.artist || "")}${t.album ? " · " + esc(t.album) : ""}</div></td>
          <td><span class="chip ${esc(t.status)}">${esc(statusLabel(t.status))}</span>${t.message ? `<div class="sub" title="${esc(t.message)}">${esc(t.message.slice(0, 90))}</div>` : ""}</td>
          <td class="ellip small hide-sm" title="${esc(t.path || "")}">${esc(t.format || "")}${t.path ? `<div class="sub">${esc(relPath(t.path))}</div>` : ""}</td>
          <td>${t.nd_link ? `<a class="chip ok" href="${esc(t.nd_link)}" target="_blank" rel="noopener">open ↗</a>` : `<span class="muted">—</span>`}</td>
        </tr>`).join("")}
      </tbody></table>
      <details id="job-log" ${logOpen ? "open" : ""}><summary>Log</summary><pre class="log">${esc(j.log || "(empty)")}</pre></details>`;
    const newPre = $("#job-log pre");
    if (newPre && atBottom) newPre.scrollTop = newPre.scrollHeight;

    const act = async (path, opts, after) => {
      try {
        const r = await api(path, opts);
        after(r);
      } catch (err) {
        toast(esc(err.message), true);
      }
    };
    $("#job-cancel")?.addEventListener("click", () => act(`/api/jobs/${j.id}/cancel`, { method: "POST" }, () => toast("Cancelling…")));
    $("#job-retry-failed")?.addEventListener("click", () => act(`/api/jobs/${j.id}/retry`, { method: "POST", body: { mode: "failed" } }, (r) => (location.hash = `#/jobs/${r.id}`)));
    $("#job-retry-all")?.addEventListener("click", () => act(`/api/jobs/${j.id}/retry`, { method: "POST", body: { mode: "all" } }, (r) => (location.hash = `#/jobs/${r.id}`)));
    $("#job-delete")?.addEventListener("click", () => {
      if (confirm("Delete this job from history? (Downloaded files are kept.)"))
        act(`/api/jobs/${j.id}`, { method: "DELETE" }, () => (location.hash = "#/jobs"));
    });
    if (!active && state.poll) {
      clearInterval(state.poll);
      state.poll = null;
    }
    return active;
  };
  const active = await load();
  if (active && ctx.alive() && !state.poll) state.poll = setInterval(load, 2000);
}

boot();
