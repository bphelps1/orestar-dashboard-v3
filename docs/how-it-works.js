/**
 * /recommend/how-it-works — the Recommend guide (RECOMMENDATIONS.md), rendered.
 *
 * The Markdown file stays the one source. It is not part of the public site:
 * fetchPrivate() reads it from the private bucket, which only answers a
 * signed-in user.
 */

document.addEventListener("DOMContentLoaded", async () => {
  const session = await requireAuth();
  if (!session) return;

  const doc = document.getElementById("hiw-doc");
  let markdown;
  try {
    const res = await fetchPrivate("RECOMMENDATIONS.md");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    markdown = await res.text();
  } catch (e) {
    doc.innerHTML = `<p class="error-msg">Could not load the guide (<span></span>). Reload to try again.</p>`;
    doc.querySelector(".error-msg span").textContent = e.message;
    return;
  }

  doc.innerHTML = DOMPurify.sanitize(marked.parse(markdown));
  const headings = addHeadingIds(doc);
  fixLinks(doc);
  wrapTables(doc);
  buildToc(headings);
  addHeadingAnchors(headings);
  followScroll(headings);

  // The browser tried the #fragment before the headings existed.
  if (window.location.hash) {
    const target = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
    if (target) target.scrollIntoView();
  }
});

/** GitHub's heading slugs, so links written for GitHub (#top-donors-by-chamber--party)
 *  work here too: lowercase, punctuation dropped, each space a hyphen. */
function githubSlug(text) {
  return text.trim().toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, "")
    .replace(/\s/g, "-");
}

function addHeadingIds(doc) {
  const seen = new Map();
  return [...doc.querySelectorAll("h1, h2, h3, h4")].map(h => {
    const text = h.textContent.trim();
    const base = githubSlug(text);
    const n = seen.get(base) || 0;
    seen.set(base, n + 1);
    h.id = n ? `${base}-${n}` : base;
    return { el: h, level: Number(h.tagName[1]), text };
  });
}

/** Section links stay in the page; everything else opens in a new tab. */
function fixLinks(doc) {
  doc.querySelectorAll("a[href]").forEach(a => {
    if (a.getAttribute("href").startsWith("#")) return;
    a.target = "_blank";
    a.rel = "noopener";
  });
}

/** Wide tables scroll inside their own box instead of widening the page. */
function wrapTables(doc) {
  doc.querySelectorAll("table").forEach(t => {
    const box = document.createElement("div");
    box.className = "hiw-table";
    t.replaceWith(box);
    box.appendChild(t);
  });
}

function buildToc(headings) {
  const toc = document.getElementById("hiw-toc");
  const details = document.getElementById("hiw-toc-details");
  const root = document.createElement("ul");
  let sub = null;
  for (const h of headings) {
    if (h.level !== 2 && h.level !== 3) continue;
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = `#${h.el.id}`;
    a.textContent = h.text;
    a.dataset.target = h.el.id;
    li.appendChild(a);
    if (h.level === 2) {
      root.appendChild(li);
      sub = null;
    } else {
      if (!sub) {
        sub = document.createElement("ul");
        (root.lastElementChild || root).appendChild(sub);
      }
      sub.appendChild(li);
    }
  }
  toc.appendChild(root);

  // Open beside the text on wide screens; a collapsed "Contents" on phones.
  const wide = window.matchMedia("(min-width: 901px)");
  details.open = wide.matches;
  toc.addEventListener("click", e => {
    if (e.target.closest("a") && !wide.matches) details.open = false;
  });
}

function addHeadingAnchors(headings) {
  for (const h of headings) {
    if (h.level === 1) continue;
    const a = document.createElement("a");
    a.className = "hiw-anchor";
    a.href = `#${h.el.id}`;
    a.setAttribute("aria-label", `Link to “${h.text}”`);
    a.textContent = "#";
    h.el.appendChild(a);
  }
}

/** Highlight the contents entry for the section being read. */
function followScroll(headings) {
  const links = new Map(
    [...document.querySelectorAll("#hiw-toc a")].map(a => [a.dataset.target, a]));
  const tracked = headings.filter(h => links.has(h.el.id));
  let current = null;
  let queued = false;
  const update = () => {
    queued = false;
    let active = tracked[0];
    for (const h of tracked) {
      if (h.el.getBoundingClientRect().top <= 96) active = h;
      else break;
    }
    const link = active && links.get(active.el.id);
    if (link === current) return;
    if (current) current.classList.remove("active");
    if (link) {
      link.classList.add("active");
      // Keep it visible when the contents are taller than the screen. Only
      // the contents box scrolls; the page stays where the reader is.
      const box = link.closest(".hiw-toc");
      const b = box.getBoundingClientRect(), r = link.getBoundingClientRect();
      if (r.top < b.top) box.scrollTop -= b.top - r.top + 8;
      else if (r.bottom > b.bottom) box.scrollTop += r.bottom - b.bottom + 8;
    }
    current = link;
  };
  window.addEventListener("scroll", () => {
    if (!queued) { queued = true; requestAnimationFrame(update); }
  }, { passive: true });
  update();
}
