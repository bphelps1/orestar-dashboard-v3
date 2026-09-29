# Oregon Campaign Finance Dashboard

A self-maintaining, daily-refreshing dashboard and **research platform** for Oregon campaign finance data (2006–present), built on Oregon's [ORESTAR](https://secure.sos.state.or.us/orestar/) public records system.

**Live site:** https://orestar-dashboard.vercel.app

Every transaction is queryable: filter ~3 million records by committee, donor, date, amount, and contributor type; download exactly what you filtered; or run your own read-only SQL.

---

## Features

- **Daily automatic updates** via GitHub Actions (no manual work required)
- **~3 million transactions**, 2006–present, live-queryable in Postgres
- **Dashboard tabs:** Overview, Donors, Recipients
- **Explore tab:** filter/sort/paginate the full dataset, download filtered CSV, run read-only SQL
- **Donor Lookup:** search resolved donor entities and view profiles — one page per donor covering every name and address variant
- **Races:** House/Senate district map with the candidate field and cycle fundraising
- **Committee search** by name, candidate name, filer ID, or race/office
- **Fuzzy name deduplication** with a manual correction override file

---

## Architecture

```
[GitHub Actions: daily cron at 8am PST]
        ↓
[scraper/fetch.py]  downloads Excel exports from ORESTAR (last 14 days)
        ↓
[scraper/process.py]  cleans, deduplicates, aggregates
        ↓
[Supabase Postgres]  ─ transactions      (~3M rows, the queryable table)
                     ─ donors            (resolved donor entities)
                     ─ donor_aliases     (name/address variant → entity)
                     ─ dashboard_cache   (aggregate blobs as jsonb)
                     ─ filer_detail      (one jsonb row per committee)
        ↓
[docs/ on Vercel]  dashboard + Explore query live from Postgres
```

The browser reads **directly from Postgres** via Supabase's API — there are no static data files to serve. Durable scraper checkpoints and exact transaction shards live in Supabase Storage rather than Git. Two distinct paths:

- **Dashboard tabs** read pre-computed aggregate blobs from `dashboard_cache` / `filer_detail`. These are computed in Python because they blend transactions with ORESTAR account summaries, cash balances, and filer metadata — logic that doesn't reduce to a single SQL view.
- **Explore + SQL box** query the `transactions` table live, so any filter combination works without pre-computation.

### Why the aggregates aren't SQL views

`summary`, `filer_index`, and the per-filer detail blobs incorporate account-summary and filer-metadata checkpoints hydrated from Supabase Storage (party, office, cash-on-hand reconciliation). Reimplementing that in SQL would duplicate several hundred lines of carefully-tuned, ORESTAR-matching logic in `scraper/process.py`. Instead Python stays the source of truth and writes its results into jsonb tables, which are still queryable like any other table.

### Donor entity resolution

ORESTAR records donors as free text, so one person or PAC appears under many spellings and addresses. `scraper/resolve_donors.py` resolves them into `donors` entities, with `donor_aliases` mapping every observed variant and `transactions.donor_id` stamped on every row. It runs in layers, strongest signal first:

1. **Committee IDs.** 243k rows carry ORESTAR's own `contributor/payee committee id`; each becomes entity `c<id>`. Most link to a committee page we already track.
2. **Guarded name matching.** Names ending in `(1234)`, or matching a committee name exactly, join that committee — but only if the row looks like committee activity. **Rows with book type Individual or Candidate & Immediate Family are never folded into a committee.** This is deliberate: "GELSER, SARA" giving personally is a different entity from "Sara Gelser for State Senate", even though the names match. Instead the person's profile carries a `related_filer_slug` link to the committee.
3. **Clustering.** Remaining names merge on strong corroboration — near-identical name plus the same normalized address, or the same ZIP and employer. Weaker signals (similar name with no address match, or a same-address/same-first-name/different-surname pair that may be a marriage name change) go to the review queue rather than merging automatically.

Admin decisions in `donor_review_decisions` feed back in as must-link/cannot-link constraints, so reviewing a pair is permanent. Unmatched names still become single-alias entities, so every transaction resolves to something.

Cadence: the weekly `donor-resolve` workflow rebuilds the whole graph; the daily refresh only assigns new rows via alias lookup (seconds).

---

## Repository Structure

```
orestar-dashboard/
├── .github/workflows/
│   ├── daily-refresh.yml     # daily at 8am PST — scrape, aggregate, sync to Supabase
│   ├── backfill.yml          # manual historical pull
│   ├── supabase-load.yml     # one-off full reload of the transactions table
│   ├── donor-resolve.yml     # weekly full donor entity resolution
│   └── district-geojson.yml  # one-off district boundary build (after redistricting)
├── scraper/
│   ├── fetch.py              # downloads Excel exports from ORESTAR
│   ├── process.py            # cleans, deduplicates, aggregates, syncs
│   ├── resolve_donors.py     # donor entity resolution (see Architecture)
│   ├── supabase_sync.py      # COPY/upsert into Postgres, jsonb upserts, CSV upload
│   ├── db_admin.py           # apply migrations / seed aggregates / verify
│   ├── build_district_geojson.py  # TIGER → simplified district GeoJSON
│   └── entity_map.json       # manual name correction overrides
├── supabase/
│   ├── migrations/           # 004 transactions · 005 aggregates · 006 SQL role
│   │                         # 007 donors · 008 donor_profile()
│   └── functions/sql-query/  # read-only SQL endpoint for the Explore page
├── scripts/
│   └── pipeline_state.py     # verified Supabase Storage hydrate/publish
├── data/
│   └── README.md             # generated state is ignored by Git
└── docs/                     # site root (deployed by Vercel)
    ├── index.html, app.js    # dashboard
    ├── explore.html/.js      # filter + download + SQL
    ├── donors.html/.js       # donor search + profiles
    ├── races.html/.js        # legislative district map
    ├── assets/               # or_house.geojson, or_senate.geojson
    └── lib/
        ├── supabase.js       # client + auth
        └── data.js           # dashboard data access layer
```

> **Note:** A pipeline checkout materializes per-year transaction shards and checkpoints under `data/`, but they are ignored by Git and never deployed to Vercel.

---

## Setup

### 1. Supabase

Create a project, then apply the schema and load the data:

```bash
export SUPABASE_DB_URL="postgresql://postgres.<ref>:<password>@<host>:5432/postgres"

python scraper/db_admin.py apply             # migrations 004-008
python scripts/pipeline_state.py pull transactions summaries auxiliary
python scraper/process.py --supabase-full-load   # load all transaction shards
python scraper/process.py                   # regenerate dashboard_cache + filer_detail
python scraper/resolve_donors.py --full      # build donor entities (or run the workflow)
python scraper/db_admin.py verify
```

The full load moves ~1.1 GB and takes 10–15 minutes. **Run it from GitHub Actions** (the *Supabase Full Load* workflow) rather than a laptop — it needs a stable connection.

The existing project already has its state snapshot. A brand-new Supabase project
must bootstrap one explicitly from a trusted hydrated legacy checkout with
`python scripts/pipeline_state.py bootstrap`; that command refuses to replace an
existing snapshot.

Set the publishable URL/key in `docs/lib/supabase.js`, and add `SUPABASE_DB_URL`, `SUPABASE_URL`, and `SUPABASE_SERVICE_ROLE_KEY` as GitHub repository secrets so the daily workflow can sync. Keep the repository variable `PIPELINE_SCHEDULES_ENABLED=false` until the manual **Pipeline State Check** workflow succeeds, then set it to `true` to enable scheduled jobs.

### 2. The SQL box (optional)

The Explore page's SQL panel runs on an Edge Function backed by a locked-down Postgres role:

```sql
alter role public_query password '<a-strong-secret>';
```

```bash
supabase functions deploy sql-query
supabase secrets set QUERY_DB_URL="postgresql://public_query.<ref>:<secret>@<host>:5432/postgres"
```

The role can only `SELECT` from the `query.transactions` view — no access to `auth.*`, admin tables, or base tables — and is read-only with a statement timeout. The function additionally rejects anything that isn't a single `SELECT` and caps results at 5,000 rows.

### 3. Hosting

Deployed on Vercel with `docs/` as the output directory. `vercel.json` defines the `/explore`, `/recommend`, and `/admin/*` routes — these rewrites are required, so the site will not work correctly on GitHub Pages.

The signed-in tools are not part of that output or of this repository. Their code lives in a separate private repository, which publishes it to a private Supabase Storage bucket; `docs/recommend.html` and `docs/admin/lobbyists.html` are shells that load it after sign-in (`loadPrivatePage()` in `docs/lib/supabase.js`), and Storage's row-level security decides who may read it. Markdown under `docs/` is excluded from deployments by `.vercelignore`.

### 4. Historical backfill

Run the **Historical Backfill** workflow from the Actions tab. It processes in batches and re-triggers itself until every window is fetched.

---

## Local Development

```bash
pip install -r scraper/requirements.txt

# Credentials (gitignored) — needed for anything that touches the database
cat > .env <<'EOF'
SUPABASE_DB_URL=postgresql://postgres.<ref>:<password>@<host>:5432/postgres
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
EOF

python scraper/fetch.py --mode=test --days=7   # test ORESTAR connectivity
python scraper/process.py                      # process + sync to Supabase

python -m http.server 8000 --directory docs    # then open http://localhost:8000/
```

The frontend reads from Supabase over the network, so the local server only serves HTML/JS — you'll see live production data. Opening `docs/index.html` via `file://` won't work; use the server.

If your network drops large payloads, set `SUPABASE_COPY_CHUNK=500` to make the loader use smaller COPY batches.

---

## Correcting Name Variations

When one donor appears under multiple spellings:

1. Open `scraper/entity_map.json`
2. Add entries like:
   ```json
   {
     "Nike Inc.": "Nike Inc",
     "NIKE INC": "Nike Inc"
   }
   ```
3. Commit and push — corrections apply on the next daily run

Uncertain matches (80–95% fuzzy confidence) are published to `dashboard_cache('donor_review_queue')`, and there's a reviewer UI at `/admin/donors` for accepting or rejecting them.

---

## Data Notes & Limitations

- **Coverage.** Contributions (1.9M), expenditures (971k), other receipts (149k), and other disbursements (17k) — including in-kind contributions, personal expenditure reimbursements, and items sold at fair market value. Records run from 2006 to present (a handful of earlier filings exist).
- **Contributor type lives in `book_type`.** ORESTAR's transaction export leaves `contributor_type_label`, `party`, and `office` blank on every row — party and office are committee-level metadata, available in `filer_index`, not per transaction. Filter on `book_type` (Individual, Business Entity, Political Committee, Labor Organization, …).
- **The API caps every response at 1,000 rows.** Anything needing a complete result set must paginate; the Explore download does this automatically and assembles the full CSV client-side.
- **Donor entities are conservative by design.** A person and their own candidate committee are kept separate (linked, not merged), and merges require address or employer corroboration — so a donor's totals may under-count where ORESTAR's data is too thin to link variants safely. Unresolved pairs surface in the admin review queue rather than being merged on a guess.
- **The race map only shows candidates with a current ORESTAR election on file.** Districts with no declared candidate render gray. Senate coverage is about half the chamber in any cycle — Oregon staggers Senate terms, so only half the seats are up.
- **5,000-record limit per ORESTAR export.** The scraper uses short date windows to stay under it and logs a warning when a window comes close.
- **ORESTAR is session-based.** No API key required, but its URL structure could change; check the Actions logs if the daily workflow starts failing.
- **Amendments.** When a transaction is amended, ORESTAR issues a new row referencing the original; the pipeline drops superseded originals so totals aren't double-counted.

---

## Cost

| | |
|---|---|
| Supabase Pro | ~$25/mo — the ~3 GB database exceeds the free tier's 500 MB |
| Vercel | Free tier (static hosting) |
| GitHub Actions | Free for public repos |

---

## Future Improvements

- Saved/shareable queries and permalinks for Explore filter states
- Candidate-level drill-down pages
- Email/Slack alerts for large contributions

---

## License

Data is public record from Oregon's Secretary of State. Code is MIT licensed.


### Explore source-name search deployment

Apply `030_explore_source_name_indexes.sql` before
`031_explore_complete_name_search.sql`, then deploy the frontend. Use
`scraper/db_admin.py apply <filename>` for each: the runner builds the two source
name trigram indexes concurrently, one statement at a time, and repairs invalid
indexes left by interrupted builds. Migration 031 searches both recorded and
canonical names, supplies recorded-name display fallbacks, and expands selected
donors through their stored merge group. Existing RPC arguments and output columns
remain compatible. Filtered CSV downloads use the same name and merge filters.

Validation includes a rollback-only database test that copies Daniel Nguyen's
committee transactions into a private test schema and checks complete, duplicate-free
pagination. Production search performance should be checked after the indexes are
built; the test does not create production indexes or deploy either migration.
