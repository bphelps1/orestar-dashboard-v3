/**
 * Supabase client configuration.
 *
 * SUPABASE_URL and SUPABASE_ANON_KEY are public (anon) credentials —
 * safe to include in client-side code. Row-Level Security on the
 * database ensures only authenticated users can read/write protected tables.
 *
 * To configure: replace the placeholders below with your Supabase project values,
 * or set them via Vercel environment variables and a build step.
 */

// These will be replaced by actual values when you create the Supabase project
const SUPABASE_URL  = window.__SUPABASE_URL  || "https://zkyvnprygbfywniisugi.supabase.co";
const SUPABASE_ANON = window.__SUPABASE_ANON || "sb_publishable_8tWa4xOZ6i0NZzkq4hr9xw_Z9mADjQv";

// Load Supabase client from CDN
let _supabaseClient = null;

async function getSupabase() {
  if (_supabaseClient) return _supabaseClient;

  // supabase-js is loaded via script tag in the HTML
  if (typeof supabase !== "undefined" && supabase.createClient) {
    _supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON);
    return _supabaseClient;
  }

  throw new Error("Supabase client not loaded. Ensure the supabase-js script tag is included.");
}

async function getSession() {
  const sb = await getSupabase();
  const { data: { session } } = await sb.auth.getSession();
  return session;
}

/** Signed-in pages call this first. Without a session it sends the visitor to
 *  /login, which brings them back here once they have signed in. */
async function requireAuth() {
  const session = await getSession();
  if (!session) {
    sendToLogin();
    return null;
  }
  document.getElementById("app-content").hidden = false;
  return session;
}

/** Admin pages call this instead of requireAuth. Signed out, it sends the
 *  visitor to /login; signed in without the admin or reviewer role, it shows
 *  a notice in place of the page. Returns the session only for admins. */
async function requireAdmin() {
  const session = await getSession();
  if (!session) {
    sendToLogin();
    return null;
  }
  if (!(await isAdminOrReviewer())) {
    const notice = document.createElement("main");
    notice.className = "admin-only";
    notice.innerHTML = `<h2>Admins only</h2>
      <p>This page is limited to admins and reviewers. You're signed in as <strong></strong>.</p>
      <a class="btn-primary" href="/login">Back to your tools</a>`;
    notice.querySelector("strong").textContent = session.user.email;
    document.getElementById("app-content").before(notice);
    return null;
  }
  document.getElementById("app-content").hidden = false;
  return session;
}

// ── Signed-in-only files ──────────────────────────────────────────────────
// The signed-in tools' code, markup, styles and guide are not part of this
// site or its repository. They are published to the private Storage bucket
// below, whose row-level security lets staff read them (files under admin/
// need the admin or reviewer role too).
const PRIVATE_BUCKET = "recommend";

function privateHeaders(session) {
  return session
    ? { apikey: SUPABASE_ANON, Authorization: `Bearer ${session.access_token}` }
    : { apikey: SUPABASE_ANON };
}

/** A file from the private bucket, requested with the current session.
 *  `init` adds fetch options such as an abort signal. */
async function fetchPrivate(name, init = {}) {
  const session = await getSession();
  const path = name.split("/").map(encodeURIComponent).join("/");
  return fetch(`${SUPABASE_URL}/storage/v1/object/authenticated/${PRIVATE_BUCKET}/${path}`, {
    ...init, headers: privateHeaders(session), cache: "no-store",
  });
}

/** Short-lived links to private files, for <img> tags and downloads that
 *  cannot send the session header: { path: url }. Files the user may not
 *  read are left out. */
async function signPrivate(paths, seconds = 3600) {
  const session = await getSession();
  if (!session || !paths.length) return {};
  const res = await fetch(`${SUPABASE_URL}/storage/v1/object/sign/${PRIVATE_BUCKET}`, {
    method: "POST",
    headers: { ...privateHeaders(session), "Content-Type": "application/json" },
    body: JSON.stringify({ expiresIn: seconds, paths }),
  });
  if (!res.ok) throw new Error(`Could not sign files (HTTP ${res.status})`);
  const links = {};
  for (const item of await res.json()) {
    if (item.signedURL && !item.error) links[item.path] = encodeURI(`${SUPABASE_URL}/storage/v1${item.signedURL}`);
  }
  return links;
}

/** Fill #app-content with a private page's markup and styles, then run its
 *  scripts in order, as the <script> tags they replace would have. Returns
 *  false, with the reason shown on the page, if any piece could not load. */
async function loadPrivatePage({ markup, styles = [], scripts = [] }) {
  const main = document.getElementById("app-content");
  try {
    const names = [...(markup ? [markup] : []), ...styles, ...scripts];
    const texts = await Promise.all(names.map(async name => {
      const res = await fetchPrivate(name);
      if (!res.ok) throw new Error(`${name}: HTTP ${res.status}`);
      return res.text();
    }));
    const html = markup ? texts.shift() : null;
    styles.forEach(() => {
      const el = document.createElement("style");
      el.textContent = texts.shift();
      document.head.appendChild(el);
    });
    if (markup) main.innerHTML = html;
    texts.forEach((code, i) => {
      const el = document.createElement("script");
      el.textContent = `${code}\n//# sourceURL=${PRIVATE_BUCKET}/${scripts[i]}`;
      document.body.appendChild(el);
    });
    return true;
  } catch (e) {
    main.innerHTML = `<p class="error-msg">This page could not be loaded (<span></span>). Reload to try again.</p>`;
    main.querySelector(".error-msg span").textContent = e.message;
    return false;
  }
}

function sendToLogin() {
  const here = window.location.pathname + window.location.search + window.location.hash;
  window.location.replace("/login?next=" + encodeURIComponent(here));
}

/** The ?next= target /login returns to, kept to paths on this site so the
 *  link cannot send someone elsewhere after they sign in. */
function safeNextPath(raw) {
  if (!raw) return null;
  try {
    const url = new URL(raw, window.location.origin);
    if (url.origin !== window.location.origin) return null;
    if (/^\/login(\.html)?$/.test(url.pathname)) return null;
    return url.pathname + url.search + url.hash;
  } catch (e) {
    return null;
  }
}

async function signIn(email, password) {
  const sb = await getSupabase();
  const { data, error } = await sb.auth.signInWithPassword({ email, password });
  if (error) throw error;
  return data;
}

async function signOut() {
  const sb = await getSupabase();
  await sb.auth.signOut();
  window.location.reload();
}

/** Get the current user's role from user_roles table. Returns null if not logged in or no role. */
async function getUserRole() {
  const session = await getSession();
  if (!session) { console.debug("[getUserRole] no session"); return null; }
  try {
    const sb = await getSupabase();
    const { data, error } = await sb.from("user_roles")
      .select("role")
      .eq("user_id", session.user.id)
      .single();
    if (error) console.warn("[getUserRole] query error:", error.message);
    console.debug("[getUserRole] user_id:", session.user.id, "role:", data?.role);
    return data?.role || null;
  } catch (e) {
    console.warn("[getUserRole] exception:", e);
    return null; // table may not exist yet
  }
}

/** Check if the current user has a specific role. */
async function hasRole(role) {
  return (await getUserRole()) === role;
}

/** Check if the current user has admin or reviewer access. */
async function isAdminOrReviewer() {
  const role = await getUserRole();
  return role === "admin" || role === "reviewer";
}

// ── Navigation ─────────────────────────────────────────────────────────────
// Public pages carry a "Login" tab (#login-tab). Signed-in pages and the
// /login hub carry the account (#user-info) and Sign Out (#sign-out-btn).
// Both are filled in here so every page behaves the same way.
async function initAccountNav() {
  let session = null;
  try { session = await getSession(); } catch (e) { return; }
  if (!session) return;

  const loginTab = document.getElementById("login-tab");
  if (loginTab) {
    loginTab.textContent = "Tools";
    loginTab.title = `Signed in as ${session.user.email}`;
    loginTab.classList.add("is-signed-in");
  }
  const who = document.getElementById("user-info");
  if (who) who.textContent = session.user.email;
  const out = document.getElementById("sign-out-btn");
  if (out) out.addEventListener("click", signOut);
  document.querySelectorAll(".tools-account").forEach(el => { el.hidden = false; });

  // Admin-only links (lobbyist attribution, donor admin) stay hidden for other users.
  const gated = document.querySelectorAll("[data-needs-admin]");
  if (gated.length && await isAdminOrReviewer()) {
    gated.forEach(el => { el.hidden = false; });
  }
}

document.addEventListener("DOMContentLoaded", initAccountNav);
