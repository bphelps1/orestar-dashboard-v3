"""
resolve_donors.py — donor entity resolution for the ORESTAR dashboard.

Builds the master `donors` + `donor_aliases` tables and stamps
`transactions.donor_id`, replacing name-only fuzzy dedup with layered signals:

  Stage A  — committee backbone: rows carrying a contributor/payee committee id
             belong to entity 'c<id>' (ORESTAR's own tagging, authoritative for
             that row). Name variants learned here are scoped 'committee' so
             they cannot swallow id-less rows on name alone.
  Stage B0 — embedded ids: id-less names ending in "(1234)" whose number is a
             known committee id (603/670 distinct names match tracked filers).
  Stage B  — name→committee matching for id-less tuples, behind a guard:
             only book_type-committee-like rows or committee-patterned names
             may be committee-assigned. Individuals / Candidate & Immediate
             Family are NEVER committee-assigned — "GELSER, SARA" giving
             personally stays a person even though committee 4680 exists.
             Fuzzy near-misses (88–95) go to the review queue, not auto.
  Stage C  — person/org clustering under the Moderate policy:
               AUTO   name ≥90  ∧ same normalized address
               AUTO   name ≥90  ∧ same zip5 ∧ same normalized employer
               AUTO   exact same normalized name ∧ same zip5   (see note below)
               REVIEW name ≥90  ∧ same city (no address corroboration)
               REVIEW name 80–90 ∧ same normalized address
               REVIEW same address ∧ same first name ∧ different surname
                      (possible marriage/name change — never auto)
  Constraints — donor_review_decisions: merged → must-link, rejected →
             cannot-link; entity_map.json → must-link. This closes the loop
             that previously discarded admin review work.

Person↔committee relationship: clusters whose name matches a candidate_name
get related_filer_slug (a LINK to the committee page — never a merge).

Every transaction ends up with a donor_id: tuples untouched by the stages
become single-alias 'provisional' entities.

Usage:
  python resolve_donors.py --full          # full re-resolution (CI weekly)
  python resolve_donors.py --dry-run       # stats only, no writes
  resolve_donors.assign_incremental(conn)  # daily: new rows via alias lookup
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz

import supabase_sync as sb

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                    datefmt="%H:%M:%S")

ROOT = Path(__file__).resolve().parent.parent
FILER_INDEX = ROOT / "data" / "aggregated" / "filer_index.json"
ENTITY_MAP = Path(__file__).parent / "entity_map.json"
REVIEW_QUEUE = ROOT / "data" / "review_queue.json"

REVIEW_QUEUE_CAP = 2000          # keep the admin UI usable
INDIVIDUAL_TYPES = {"Individual", "Candidate & Immediate Family"}
COMMITTEE_TYPES = {"Political Committee", "Political Party Committee", "Unregistered Committee"}
COMMITTEE_NAME_PAT = re.compile(
    r"\bpac\b|\bp\.a\.c\b|committee|friends of|citizens for|neighbors for|"
    r"\bfor (senate|house|congress|governor|mayor|city council|state rep|"
    r"county commissioner|district attorney|sheriff|judge|treasurer|oregon)\b|"
    r"\(\d{2,6}\)\s*$",
    re.I,
)
EMBEDDED_ID_PAT = re.compile(r"\((\d{2,6})\)\s*$")

# ── Normalization ────────────────────────────────────────────────────────────

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")
_SUFFIXES = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "drive": "dr",
    "road": "rd", "lane": "ln", "court": "ct", "place": "pl", "highway": "hwy",
    "parkway": "pkwy", "circle": "cir", "terrace": "ter", "northeast": "ne",
    "northwest": "nw", "southeast": "se", "southwest": "sw", "north": "n",
    "south": "s", "east": "e", "west": "w", "apartment": "", "suite": "",
    "unit": "", "ste": "", "apt": "",
}
_UNIT_TAIL = re.compile(r"\b(apt|suite|ste|unit|#)\s*\S*\s*$", re.I)

# A comma usually means "LAST, FIRST" — but not when what follows is a corporate
# form or a personal suffix. Without this guard "Philip Morris USA, Inc." becomes
# "inc philip morris usa" while "Philip Morris USA Inc." becomes
# "philip morris usa inc", so the two never share a blocking key and can never
# merge — even at an identical address. Same for "Smith, Jr." → "jr smith".
_NO_FLIP_AFTER_COMMA = {
    # corporate forms
    "inc", "llc", "corp", "co", "ltd", "lp", "llp", "pc", "plc", "pllc",
    "company", "incorporated", "corporation", "limited", "sa", "nv", "ag",
    "gmbh", "pa", "psc", "trust", "foundation", "fund", "and sons", "sons",
    # personal / professional suffixes
    "jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "esq", "dds", "cpa",
    "rn", "do", "dc", "od", "dvm", "jd", "mba", "pe", "aia", "ret",
}


def norm_name(raw: str) -> str:
    """Lowercase, de-punctuate, flip 'LAST, FIRST [MI]' → 'first [mi] last',
    strip a trailing '(1234)' committee-id suffix."""
    s = (raw or "").strip()
    s = EMBEDDED_ID_PAT.sub("", s)
    if s.count(",") == 1:
        last, first = s.split(",")
        # Only invert for a real "LAST, FIRST" — not "Acme, Inc." or "Smith, Jr."
        tail = _WS.sub(" ", _PUNCT.sub(" ", first.strip().lower())).strip()
        if (last.strip() and first.strip() and not any(ch.isdigit() for ch in s)
                and tail not in _NO_FLIP_AFTER_COMMA):
            s = f"{first.strip()} {last.strip()}"
    s = _PUNCT.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()


def norm_addr(addr: str, zip_code: str) -> str:
    """USPS-ish squash: 'addr_key' used for exact-address corroboration."""
    s = (addr or "").strip().lower()
    if not s:
        return ""
    s = _UNIT_TAIL.sub("", s)
    s = _PUNCT.sub(" ", s)
    toks = [_SUFFIXES.get(t, t) for t in s.split()]
    s = " ".join(t for t in toks if t)
    z5 = (zip_code or "").strip()[:5]
    return f"{s}|{z5}" if s else ""


def norm_employer(emp: str) -> str:
    s = _PUNCT.sub(" ", (emp or "").lower())
    s = _WS.sub(" ", s).strip()
    return "" if s in ("", "none", "n a", "na", "not employed", "retired", "self",
                       "self employed", "unemployed") else s


def zip5(z: str) -> str:
    return (z or "").strip()[:5]


def alias_key(nname: str, akey: str) -> str:
    return f"{nname}|{akey}"


def soundex(word: str) -> str:
    """Tiny Soundex for surname blocking (avoids a new dependency)."""
    word = re.sub(r"[^a-z]", "", (word or "").lower())
    if not word:
        return ""
    codes = {"b": "1", "f": "1", "p": "1", "v": "1",
             "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2", "x": "2", "z": "2",
             "d": "3", "t": "3", "l": "4", "m": "5", "n": "5", "r": "6"}
    out = word[0]
    prev = codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != prev:
            out += code
        if ch not in "hw":
            prev = code
    return (out + "000")[:4]


# ── Union-find ───────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self):
        self.parent: dict = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


# ── Data loading ─────────────────────────────────────────────────────────────

TUPLE_SQL = """
select contributor_payee                             as raw_name,
       coalesce(addr_line1,'')                       as addr,
       coalesce(city,'')                             as city,
       coalesce(state,'')                            as state,
       coalesce(zip,'')                              as zip,
       coalesce(employer,'')                         as employer,
       coalesce(occupation,'')                       as occupation,
       coalesce(contributor_payee_committee_id,'')   as cid,
       coalesce(book_type,'')                        as book_type,
       count(*)                                      as n
from transactions
where contributor_payee is not null and contributor_payee <> ''
group by 1,2,3,4,5,6,7,8,9
"""


def load_tuples(conn) -> pd.DataFrame:
    log.info("Loading distinct contributor tuples…")
    df = pd.read_sql(TUPLE_SQL, conn)
    log.info("  %s tuples", f"{len(df):,}")
    df["nname"] = df["raw_name"].map(norm_name)
    # A blank/whitespace-only contributor name normalizes to "" — which is NULL
    # to COPY and would abort the whole load on the not-null alias columns.
    # Give it a stable sentinel so the row still resolves to an entity.
    df.loc[df["nname"].str.strip() == "", "nname"] = "(unnamed)"
    df["akey"] = [norm_addr(a, z) for a, z in zip(df["addr"], df["zip"])]
    df["z5"] = df["zip"].map(zip5)
    df["nemp"] = df["employer"].map(norm_employer)
    df["aliaskey"] = [alias_key(n, a) for n, a in zip(df["nname"], df["akey"])]
    return df


def load_committee_namespace(conn) -> dict:
    """filer_id → {slug, name}; plus norm-name → filer_id lookups."""
    ns = {"by_id": {}, "by_norm": {}, "candidate_by_norm": {}}
    if sb.sync_enabled():
        # CI uses the live current population even if a partial local aggregate
        # directory happens to exist from an interrupted diagnostic run.
        index = sb.require_dashboard_cache("filer_index")
    elif FILER_INDEX.exists():
        index = json.loads(FILER_INDEX.read_text())
    else:
        # Generated aggregates are no longer committed in the clean repository.
        # This is a destructive full rebuild of the donor tables, so an absent
        # namespace must fail closed rather than silently resolving with zero
        # known committees.
        index = sb.require_dashboard_cache("filer_index")
    if not isinstance(index, list) or not index:
        raise RuntimeError("Current filer_index is empty or malformed")
    for row in index:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("filer_id") or "")
        slug = str(row.get("slug") or "").strip()
        name = str(row.get("name") or "").strip()
        if fid and slug and name:
            ns["by_id"][fid] = {"slug": slug, "name": name}
            ns["by_norm"].setdefault(norm_name(name), fid)
        cand = str(row.get("candidate_name") or "").strip()
        if cand and fid in ns["by_id"]:
            ns["candidate_by_norm"].setdefault(norm_name(cand), slug)
    if not ns["by_id"]:
        raise RuntimeError("Current filer_index contains no usable filer rows")
    # raw filer-name variants seen in transactions (richer than index names)
    cur = conn.cursor()
    cur.execute("""select distinct filer_id, filer from transactions
                   where filer_id <> '' and filer <> ''""")
    for fid, fname in cur.fetchall():
        if fid in ns["by_id"]:
            ns["by_norm"].setdefault(norm_name(fname), fid)
    log.info("Committee namespace: %d ids, %d name keys, %d candidates",
             len(ns["by_id"]), len(ns["by_norm"]), len(ns["candidate_by_norm"]))
    return ns


def load_alias_constraints(conn) -> tuple[list, set]:
    """Manual entity-level merges → (must_alias: [(a,b)], cannot_alias: {frozenset}).

    Keyed on alias_key (norm_name|addr_key) rather than name, so a reviewer can
    merge two entities that share a name but sit at different addresses — which
    a name→name constraint cannot express."""
    must: list[tuple[str, str]] = []
    cannot: set[frozenset] = set()
    cur = conn.cursor()
    try:
        cur.execute("select alias_a, alias_b, decision from donor_merge_overrides")
    except Exception:
        conn.rollback()          # table not migrated yet — no overrides to apply
        return must, cannot
    decisions = cur.fetchall()
    cur.execute("select to_regclass('public.donor_identity_redirects') is not null")
    stored_identities = cur.fetchone()[0]
    for a, b, decision in decisions:
        if not a or not b or a == b:
            continue
        if decision == "merged" and not stored_identities:
            # Legacy databases physically combine clusters. Migration 027 owns
            # manual merges in canonical_entity_id, preserving source entities
            # across full rebuilds so admin decisions stay separate from imports.
            must.append((a, b))
        elif decision == "separate":
            cannot.add(frozenset((a, b)))
    log.info("Alias overrides: %d merge, %d separate", len(must), len(cannot))
    return must, cannot


def load_constraints(conn) -> tuple[dict, set]:
    """entity_map + review decisions → (must_link: norm→norm, cannot: {frozenset})"""
    must: dict[str, str] = {}
    cannot: set[frozenset] = set()
    if ENTITY_MAP.exists():
        for raw, canon in json.loads(ENTITY_MAP.read_text()).items():
            a, b = norm_name(raw), norm_name(canon)
            if a and b and a != b:
                must[a] = b
    cur = conn.cursor()
    cur.execute("select pair_key, decision from donor_review_decisions")
    for pair_key, decision in cur.fetchall():
        parts = pair_key.split("|||")
        if len(parts) != 2:
            continue
        a, b = norm_name(parts[0]), norm_name(parts[1])
        if not a or not b or a == b:
            continue
        if decision == "merged":
            must[a] = b
        elif decision == "rejected":
            cannot.add(frozenset((a, b)))
    log.info("Constraints: %d must-link, %d cannot-link", len(must), len(cannot))
    return must, cannot


# ── Guards ───────────────────────────────────────────────────────────────────

def committee_eligible(book_type: str, raw_name: str) -> bool:
    """May this tuple be assigned to a committee entity? (the Gelser guard)"""
    if book_type in INDIVIDUAL_TYPES:
        return False
    return book_type in COMMITTEE_TYPES or bool(COMMITTEE_NAME_PAT.search(raw_name or ""))


# ── Resolution ───────────────────────────────────────────────────────────────

def resolve(df: pd.DataFrame, ns: dict, must: dict, cannot: set,
            must_alias: list | None = None, cannot_alias: set | None = None):
    """Returns (assignments: tuple_idx→donor_id, donors: {id→meta},
    aliases: list, review: list)."""
    must_alias = must_alias or []
    cannot_alias = cannot_alias or set()
    donors: dict[str, dict] = {}
    assign: dict[int, str] = {}
    aliases: list[dict] = []
    review: list[dict] = []

    def committee_donor(cid: str) -> str:
        did = f"c{cid}"
        if did not in donors:
            meta = ns["by_id"].get(cid)
            donors[did] = {
                "donor_id": did, "committee_id": cid,
                "display_name": meta["name"] if meta else "",
                "filer_slug": meta["slug"] if meta else None,
                "book_type": "Political Committee", "names": Counter(),
            }
        return did

    # ── Stage A: explicit committee ids ────────────────────────────────────
    a_mask = df["cid"] != ""
    for idx in df.index[a_mask]:
        row = df.loc[idx]
        did = committee_donor(row["cid"])
        assign[idx] = did
        donors[did]["names"][row["raw_name"]] += int(row["n"])
        aliases.append({"alias_key": row["aliaskey"], "donor_id": did,
                        "raw_name": row["raw_name"], "norm_name": row["nname"],
                        "addr_key": row["akey"], "source": "committee_id",
                        "alias_scope": "committee"})
    log.info("Stage A: %d tuples → %d committee entities",
             int(a_mask.sum()), len(donors))

    # ── Stage B0/B: id-less name → committee (guarded) ─────────────────────
    rest = df.index[~a_mask]
    b_count = 0
    unresolved: list[int] = []
    known_ids = set(ns["by_id"]) | {d[1:] for d in donors}
    for idx in rest:
        row = df.loc[idx]
        raw, nname, bt = row["raw_name"], row["nname"], row["book_type"]
        eligible = committee_eligible(bt, raw)
        m = EMBEDDED_ID_PAT.search(raw or "")
        if m and eligible and m.group(1) in known_ids:              # Stage B0
            did = committee_donor(m.group(1))
        elif eligible and nname in ns["by_norm"]:                    # exact name
            did = committee_donor(ns["by_norm"][nname])
        else:
            if eligible and COMMITTEE_NAME_PAT.search(raw or ""):
                # committee-looking but unknown → cluster in org space (Stage C)
                pass
            elif not eligible and COMMITTEE_NAME_PAT.search(raw or "") and bt in INDIVIDUAL_TYPES:
                # guard conflict: committee-patterned name filed as Individual
                review.append({"a": raw, "b": "(individual book_type, committee-like name)",
                               "score": 90,
                               "evidence": {"type": "guard_conflict", "book_type": bt}})
            unresolved.append(idx)
            continue
        assign[idx] = did
        donors[did]["names"][raw] += int(row["n"])
        aliases.append({"alias_key": row["aliaskey"], "donor_id": did,
                        "raw_name": raw, "norm_name": nname,
                        "addr_key": row["akey"], "source": "filer_match",
                        "alias_scope": "any"})
        b_count += 1
    log.info("Stage B: %d tuples matched to committees; %d left for clustering",
             b_count, len(unresolved))

    # fuzzy name-vs-filer near misses → review only (sample the frequent ones)
    ns_names = list(ns["by_norm"].keys())
    ns_first = defaultdict(list)
    for n in ns_names:
        ns_first[n.split(" ")[0] if n else ""].append(n)
    seen_pairs = set()
    for idx in unresolved:
        row = df.loc[idx]
        if int(row["n"]) < 5 or not committee_eligible(row["book_type"], row["raw_name"]):
            continue
        nname = row["nname"]
        for cand in ns_first.get(nname.split(" ")[0] if nname else "", [])[:50]:
            sc = fuzz.token_sort_ratio(nname, cand)
            if 88 <= sc < 100:
                pk = (nname, cand)
                if pk not in seen_pairs:
                    seen_pairs.add(pk)
                    review.append({"a": row["raw_name"],
                                   "b": ns["by_id"][ns["by_norm"][cand]]["name"],
                                   "score": int(sc),
                                   "evidence": {"type": "name_vs_filer",
                                                "filer_id": ns["by_norm"][cand]}})
                break

    # ── Stage C: cluster remaining tuples ───────────────────────────────────
    uf = UnionFind()
    sub = df.loc[unresolved]
    # must-links from constraints (name-level)
    norm_groups: dict[str, list[int]] = defaultdict(list)
    for idx in sub.index:
        norm_groups[sub.at[idx, "nname"]].append(idx)
    for a, b in must.items():
        if a in norm_groups and b in norm_groups:
            uf.union(norm_groups[a][0], norm_groups[b][0])
            for lst in (norm_groups[a], norm_groups[b]):
                for i in lst[1:]:
                    uf.union(lst[0], i)

    # must-links from manual entity-level merges (alias-level: name+address).
    # These express "same entity at a different address", which the name-level
    # constraints above cannot.
    alias_groups: dict[str, list[int]] = defaultdict(list)
    for idx in sub.index:
        alias_groups[sub.at[idx, "aliaskey"]].append(idx)
    n_applied = 0
    for a, b in must_alias:
        if a in alias_groups and b in alias_groups:
            uf.union(alias_groups[a][0], alias_groups[b][0])
            for lst in (alias_groups[a], alias_groups[b]):
                for i in lst[1:]:
                    uf.union(lst[0], i)
            n_applied += 1
    if must_alias:
        log.info("Applied %d/%d manual entity merges", n_applied, len(must_alias))

    def blocked(i, j) -> bool:
        if frozenset((sub.at[i, "nname"], sub.at[j, "nname"])) in cannot:
            return True
        return frozenset((sub.at[i, "aliaskey"], sub.at[j, "aliaskey"])) in cannot_alias

    # Pass 1 — same normalized name: same zip5 → auto-union (exact-name rule)
    for nname, idxs in norm_groups.items():
        if len(idxs) < 2:
            continue
        by_zip = defaultdict(list)
        for i in idxs:
            by_zip[sub.at[i, "z5"]].append(i)
        for z, zi in by_zip.items():
            if z:
                for i in zi[1:]:
                    if not blocked(zi[0], i):
                        uf.union(zi[0], i)

    # Pass 2 — fuzzy pairs inside (surname soundex + zip5) blocks
    blocks = defaultdict(list)
    for i in sub.index:
        nn = sub.at[i, "nname"]
        surname = nn.split(" ")[-1] if nn else ""
        if sub.at[i, "z5"]:
            blocks[(soundex(surname), sub.at[i, "z5"])].append(i)
    pair_budget = 4_000_000
    checked = 0
    for key, idxs in blocks.items():
        if len(idxs) < 2 or len(idxs) > 200:
            continue
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                if checked >= pair_budget:
                    break
                i, j = idxs[x], idxs[y]
                ni, nj = sub.at[i, "nname"], sub.at[j, "nname"]
                if ni == nj or blocked(i, j):
                    continue
                checked += 1
                sc = max(fuzz.token_sort_ratio(ni, nj), fuzz.token_set_ratio(ni, nj))
                same_addr = sub.at[i, "akey"] and sub.at[i, "akey"] == sub.at[j, "akey"]
                same_emp = sub.at[i, "nemp"] and sub.at[i, "nemp"] == sub.at[j, "nemp"]
                if sc >= 90 and same_addr:
                    uf.union(i, j)
                elif sc >= 90 and same_emp:          # same zip via block key
                    uf.union(i, j)
                elif 80 <= sc < 90 and same_addr:
                    review.append({"a": sub.at[i, "raw_name"], "b": sub.at[j, "raw_name"],
                                   "score": int(sc),
                                   "evidence": {"type": "same_address",
                                                "addr": sub.at[i, "akey"]}})
                elif sc >= 90:
                    review.append({"a": sub.at[i, "raw_name"], "b": sub.at[j, "raw_name"],
                                   "score": int(sc), "evidence": {"type": "name_only"}})
                else:
                    # possible name change: same addr + same first name + diff surname
                    fi, fj = ni.split(" ")[0], nj.split(" ")[0]
                    si, sj = ni.split(" ")[-1], nj.split(" ")[-1]
                    if same_addr and fi and fi == fj and si != sj:
                        review.append({"a": sub.at[i, "raw_name"], "b": sub.at[j, "raw_name"],
                                       "score": int(sc),
                                       "evidence": {"type": "possible_name_change",
                                                    "addr": sub.at[i, "akey"],
                                                    "employer_match": bool(same_emp)}})
    log.info("Stage C: %s fuzzy pairs scored", f"{checked:,}")

    # materialize clusters
    clusters: dict[int, list[int]] = defaultdict(list)
    for i in sub.index:
        clusters[uf.find(i)].append(i)
    for root, members in clusters.items():
        keys = sorted(sub.at[i, "aliaskey"] for i in members)
        did = "d" + hashlib.sha1(keys[0].encode()).hexdigest()[:12]
        names = Counter()
        for i in members:
            names[sub.at[i, "raw_name"]] += int(sub.at[i, "n"])
        source = "cluster" if len(members) > 1 else "provisional"
        donors[did] = {"donor_id": did, "committee_id": None, "display_name": "",
                       "filer_slug": None, "book_type": "", "names": names}
        for i in members:
            assign[i] = did
            aliases.append({"alias_key": sub.at[i, "aliaskey"], "donor_id": did,
                            "raw_name": sub.at[i, "raw_name"],
                            "norm_name": sub.at[i, "nname"],
                            "addr_key": sub.at[i, "akey"],
                            "source": source, "alias_scope": "any"})
    log.info("Stage C: %s clusters (%s multi-member)",
             f"{len(clusters):,}",
             f"{sum(1 for m in clusters.values() if len(m) > 1):,}")

    # display names + book_type + related_filer_slug
    for did, meta in donors.items():
        if not meta["display_name"] and meta["names"]:
            meta["display_name"] = meta["names"].most_common(1)[0][0]
        if not (meta["display_name"] or "").strip():
            meta["display_name"] = "(unnamed)"    # display_name is not-null
        meta["related_filer_slug"] = None
        if did.startswith("d"):
            slug = ns["candidate_by_norm"].get(norm_name(meta["display_name"]))
            if slug:
                meta["related_filer_slug"] = slug
    return assign, donors, aliases, review


# ── Write-back ───────────────────────────────────────────────────────────────

def stamp_resolved_batch(cur, start, end):
    """Reassign existing cluster IDs too; committee IDs remain authoritative."""
    cur.execute("""update transactions t set donor_id = m.donor_id
                       from _dmap m
                       where t.tran_id >= %s and t.tran_id < %s
                         and coalesce(t.contributor_payee_committee_id,'') = ''
                         and t.donor_id is distinct from m.donor_id
                         and t.contributor_payee = m.raw_name
                         and coalesce(t.addr_line1,'') = m.addr
                         and coalesce(t.zip,'') = m.zip""", (start, end))
    return cur.rowcount


def write_back(conn, df, assign, donors, aliases):
    cur = conn.cursor()
    log.info("Write-back: donors + aliases…")
    # Preserve the identities used by reviewed contacts/attributions before
    # cluster hashes change. The read-through map follows these stable aliases.
    cur.execute("""insert into donor_identity_anchors(donor_id,alias_key)
                   select donor_id,min(alias_key) from donor_aliases
                   where donor_id in (select donor_id from donor_lobbyist_links
                     union select donor_id from donor_client_links
                     union select donor_id from donor_contacts)
                   group by donor_id on conflict(donor_id) do nothing""")
    cur.execute("truncate donor_aliases, donors")

    dbuf = io.StringIO()
    drows = pd.DataFrame([{
        "donor_id": m["donor_id"], "display_name": m["display_name"][:500],
        "book_type": m["book_type"], "committee_id": m["committee_id"],
        "filer_slug": m["filer_slug"], "related_filer_slug": m.get("related_filer_slug"),
        "alias_count": 0,
    } for m in donors.values()])
    drows.to_csv(dbuf, index=False)
    dbuf.seek(0)
    cur.copy_expert(
        "copy donors (donor_id, display_name, book_type, committee_id, filer_slug,"
        " related_filer_slug, alias_count) from stdin with (format csv, header true,"
        # not-null columns must never be NULLed by an empty field; the nullable
        # slug/committee columns keep '' -> NULL so joins behave.
        " null '', force_not_null (donor_id, display_name))",
        dbuf)

    arows = pd.DataFrame(aliases).drop_duplicates(subset=["alias_key"])
    abuf = io.StringIO()
    arows.to_csv(abuf, index=False, columns=["alias_key", "donor_id", "raw_name",
                                             "norm_name", "addr_key", "source", "alias_scope"])
    abuf.seek(0)
    cur.copy_expert(
        "copy donor_aliases (alias_key, donor_id, raw_name, norm_name, addr_key,"
        " source, alias_scope) from stdin with (format csv, header true,"
        # every text column: empty means empty string, never NULL
        " force_not_null (alias_key, raw_name, norm_name, addr_key, source, alias_scope))",
        abuf)
    conn.commit()
    log.info("  %s donors, %s aliases", f"{len(drows):,}", f"{len(arows):,}")

    log.info("Write-back: stamping transactions.donor_id…")
    # Updating ~2.8M rows while donor_id is indexed forces an index write per
    # row and blew past the CI timeout. Drop the index first so Postgres can do
    # HOT updates (no index maintenance at all), batch by tran_id so each
    # transaction stays small, then rebuild the index once. Same drop/rebuild
    # pattern the bulk transaction load uses.
    cur.execute("drop index if exists idx_txn_donor_id")
    cur.execute("drop table if exists _dmap")
    # UNLOGGED + real (not temp) so it survives the per-batch commits below
    # and skips WAL for the staging write.
    cur.execute("""create unlogged table _dmap (raw_name text, addr text, zip text,
                   donor_id text)""")
    conn.commit()

    mbuf = io.StringIO()
    mdf = pd.DataFrame({
        "raw_name": df["raw_name"], "addr": df["addr"], "zip": df["zip"],
        "donor_id": [assign[i] for i in df.index],
    })
    mdf.to_csv(mbuf, index=False)
    mbuf.seek(0)
    # force_not_null: empty addr/zip must stay '' so the coalesce() join below
    # matches no-address rows — as NULLs they would silently never join.
    cur.copy_expert(
        "copy _dmap from stdin with (format csv, header true,"
        " force_not_null (raw_name, addr, zip))", mbuf)
    cur.execute("create index _dmap_key on _dmap (raw_name, addr, zip)")
    cur.execute("analyze _dmap")
    conn.commit()

    # committee-id rows are authoritative regardless of name/address
    cur.execute("""update transactions set donor_id = 'c' || contributor_payee_committee_id
                   where coalesce(contributor_payee_committee_id,'') <> ''""")
    conn.commit()
    log.info("  committee-id rows stamped (%s)", f"{cur.rowcount:,}")

    cur.execute("select min(tran_id), max(tran_id) from transactions")
    lo, hi = cur.fetchone()
    BATCH = 250_000
    done = 0
    start = lo
    while start <= hi:
        end = start + BATCH
        done += stamp_resolved_batch(cur, start, end)
        conn.commit()
        log.info("  stamped %s rows (tran_id < %s)", f"{done:,}", f"{end:,}")
        start = end

    log.info("Write-back: rebuilding donor_id index…")
    cur.execute("create index idx_txn_donor_id on transactions (donor_id)")
    cur.execute("drop table if exists _dmap")
    conn.commit()

    log.info("Write-back: donor aggregates…")
    cur.execute("""
      update donors d set
        total_given    = coalesce(t.given, 0),
        total_inkind   = coalesce(t.given_inkind, 0),
        total_received = coalesce(t.received, 0),
        gift_count     = coalesce(t.gifts, 0),
        first_date     = t.first_date,
        last_date      = t.last_date,
        alias_count    = coalesce(a.n, 1),
        city           = coalesce(t.city, d.city),
        state          = coalesce(t.state, d.state),
        zip            = coalesce(t.zip, d.zip),
        employer       = coalesce(t.employer, d.employer),
        occupation     = coalesce(t.occupation, d.occupation),
        book_type      = case when d.book_type = '' or d.book_type is null
                              then coalesce(t.book_type, d.book_type) else d.book_type end,
        updated_at     = now()
      from (
        select donor_id,
               -- Contributions are CASH ONLY app-wide; in-kind is tracked
               -- separately so it is excluded here too. Without this,
               -- donors.total_given disagreed with the Donors tab by exactly
               -- a donor's in-kind giving (SEIU 503: $2.39M).
               sum(amount) filter (where tran_type = 'C'
                     and coalesce(sub_type,'') <> 'In-Kind Contribution')  as given,
               sum(amount) filter (where tran_type = 'C'
                     and sub_type = 'In-Kind Contribution')                as given_inkind,
               sum(amount) filter (where tran_type = 'E')          as received,
               count(*)    filter (where tran_type = 'C'
                     and coalesce(sub_type,'') <> 'In-Kind Contribution')  as gifts,
               min(tran_date) as first_date, max(tran_date) as last_date,
               mode() within group (order by nullif(city,''))      as city,
               mode() within group (order by nullif(state,''))     as state,
               mode() within group (order by nullif(zip,''))       as zip,
               mode() within group (order by nullif(employer,''))  as employer,
               mode() within group (order by nullif(occupation,'')) as occupation,
               mode() within group (order by nullif(book_type,'')) as book_type
        from transactions where donor_id is not null group by donor_id
      ) t
      left join (select donor_id, count(*) n from donor_aliases group by 1) a
        on a.donor_id = t.donor_id
      where d.donor_id = t.donor_id""")
    conn.commit()
    cur.execute("analyze donors; analyze donor_aliases; analyze transactions")
    conn.commit()


def write_review_queue(review: list):
    review.sort(key=lambda r: -r["score"])
    trimmed = review[:REVIEW_QUEUE_CAP]
    REVIEW_QUEUE.write_text(json.dumps(trimmed, indent=1))
    log.info("Review queue: %d items (%d before cap)", len(trimmed), len(review))
    # The admin UI is database-only in the clean repository. A failed cache
    # write must make the workflow fail instead of reporting a green run with
    # a stale or missing queue.
    sb.upsert_dashboard_cache("donor_review_queue", trimmed)


# ── Daily incremental ────────────────────────────────────────────────────────

def assign_incremental(conn=None) -> None:
    """Assign donor_id to rows that lack one, via committee id or exact alias
    lookup; unknown tuples become provisional single-alias donors. SQL + a tiny
    Python pass — safe inside the daily refresh."""
    own = conn is None
    if own:
        if not sb.sync_enabled():
            return
        conn = sb._connect()
    cur = conn.cursor()
    cur.execute("""update transactions set donor_id = 'c' || contributor_payee_committee_id
                   where donor_id is null
                     and coalesce(contributor_payee_committee_id,'') <> ''""")
    # ensure committee donors exist for any brand-new ids
    cur.execute("""insert into donors (donor_id, display_name, committee_id, book_type)
                   select distinct t.donor_id, t.contributor_payee,
                          t.contributor_payee_committee_id, 'Political Committee'
                   from transactions t
                   where t.donor_id like 'c%'
                     and not exists (select 1 from donors d where d.donor_id = t.donor_id)""")
    cur.execute("""select distinct contributor_payee, coalesce(addr_line1,''), coalesce(zip,'')
                   from transactions
                   where donor_id is null and contributor_payee <> ''""")
    rows = cur.fetchall()
    if rows:
        new_aliases, updates = [], []
        cur2 = conn.cursor()
        for raw, addr, zc in rows:
            ak = alias_key(norm_name(raw), norm_addr(addr, zc))
            cur2.execute("select donor_id from donor_aliases where alias_key = %s", (ak,))
            hit = cur2.fetchone()
            if hit:
                updates.append((hit[0], raw, addr, zc))
            else:
                did = "d" + hashlib.sha1(ak.encode()).hexdigest()[:12]
                cur2.execute("""insert into donors (donor_id, display_name)
                                values (%s, %s) on conflict do nothing""", (did, raw))
                cur2.execute("""insert into donor_aliases
                                (alias_key, donor_id, raw_name, norm_name, addr_key, source)
                                values (%s,%s,%s,%s,%s,'provisional')
                                on conflict do nothing""",
                             (ak, did, raw, norm_name(raw), norm_addr(addr, zc)))
                updates.append((did, raw, addr, zc))
        for did, raw, addr, zc in updates:
            cur2.execute("""update transactions set donor_id = %s
                            where donor_id is null and contributor_payee = %s
                              and coalesce(addr_line1,'') = %s and coalesce(zip,'') = %s""",
                         (did, raw, addr, zc))
        conn.commit()
        log.info("Incremental: assigned %d new tuples", len(rows))
    if own:
        conn.close()


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="full re-resolution")
    ap.add_argument("--dry-run", action="store_true", help="no writes, stats only")
    args = ap.parse_args()
    if not (args.full or args.dry_run):
        ap.error("pass --full or --dry-run")
    if not sb.sync_enabled():
        log.error("SUPABASE_DB_URL not set")
        sys.exit(1)

    conn = sb._connect()
    df = load_tuples(conn)
    ns = load_committee_namespace(conn)
    must, cannot = load_constraints(conn)
    must_alias, cannot_alias = load_alias_constraints(conn)
    assign, donors, aliases, review = resolve(df, ns, must, cannot,
                                              must_alias, cannot_alias)

    n_committee = sum(1 for d in donors if d.startswith("c"))
    log.info("RESULT: %s entities (%s committees, %s clusters) from %s tuples / %s raw names",
             f"{len(donors):,}", f"{n_committee:,}", f"{len(donors)-n_committee:,}",
             f"{len(df):,}", f"{df['raw_name'].nunique():,}")
    log.info("        %s review items", f"{len(review):,}")

    if args.dry_run:
        conn.close()
        return
    write_back(conn, df, assign, donors, aliases)
    write_review_queue(review)
    conn.close()
    log.info("Full resolution complete.")


if __name__ == "__main__":
    main()
