/**
 * /login — the one place to sign in.
 *
 * Signed out, it shows the form. Signed-in pages send visitors here with
 * ?next=<their path> (see requireAuth), and they go straight back after
 * signing in. Signed in with nowhere to return to, it lists the tools.
 */

const TOOL_NAMES = {
  "/recommend": "Recommend",
  "/recommend/how-it-works": "the Recommend guide",
  "/admin/lobbyists": "Lobbyist attribution",
  "/admin/donors": "Donor admin",
};

document.addEventListener("DOMContentLoaded", async () => {
  const next = safeNextPath(new URLSearchParams(window.location.search).get("next"));
  const session = await getSession();

  if (session) {
    if (next) { window.location.replace(next); return; }
    document.getElementById("tools-hub").hidden = false;
    return;
  }

  const tool = next && TOOL_NAMES[next.split(/[?#]/)[0].replace(/\.html$/, "")];
  if (tool) {
    document.getElementById("login-desc").textContent = `Sign in to continue to ${tool}.`;
  }
  document.getElementById("login-form").hidden = false;
  document.getElementById("login-email").focus();

  document.getElementById("login-form-el").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("login-btn");
    const errEl = document.getElementById("login-error");
    errEl.hidden = true;
    btn.disabled = true;
    try {
      await signIn(
        document.getElementById("login-email").value,
        document.getElementById("login-password").value,
      );
      if (next) window.location.replace(next);
      else window.location.reload();
    } catch (err) {
      errEl.textContent = err.message || "Sign-in failed";
      errEl.hidden = false;
      btn.disabled = false;
    }
  });
});
