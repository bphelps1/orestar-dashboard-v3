# ORESTAR parsing traps

Failure modes this project has hit **more than once**, written down so the next
person — or the next session — does not rediscover them by spending days on a
phantom discrepancy.

Every entry here shares one shape: **a silent zero**. Nothing raises, nothing
logs, a number is quietly wrong, and the wrongness is then explained away as a
real difference between ORESTAR's accounting and ours.

---

## 1. Label casing must match what the page prints

`orestar_parse.parse_dollar(html, label)` looks for a **literal** label and
returns its `default` when it does not match. It does not raise. It cannot tell
you it found nothing.

ORESTAR prints these labels in **lowercase parentheticals**:

```
Cash Contributions            $117,487.62
Loans Received (non-exempt)   $0.00          <- NOT "(Non-Exempt)"
In-Kind                       $0.00
Total Contributions           $117,487.62
Expenditures
  Cash Expenditures           $50.00
  Loan Payments (non-exempt)  $0.00
  Loans Received (exempt)     $0.00
  Loan Payments (exempt)      $0.00
```

**What went wrong twice:**

| when | label asked for | label printed | consequence |
|---|---|---|---|
| #56 | `In-Kind Contributions` | `In-Kind` | in-kind $0.00 in all 45,938 yearly records |
| #90 | `Loans Received (Non-Exempt)` | `Loans Received (non-exempt)` | all four loan fields $0.00 in all 46,945 records |

The loan case cost the most. It produced a **$3.08M phantom discrepancy across
66 committees in 2006**, which was twice attributed to "ORESTAR treats loans
differently in 2006–07". There was no difference. We were not reading the
figures.

`parse_dollar` now matches case-insensitively, which removes the class rather
than the instances. No label on this page differs only by case.

**How to catch it:** after any parse change, count non-zero values per field
across the whole cache. A field that is zero in *every* record is a parse bug,
not a fact about Oregon politics:

```python
sum(1 for e in ys.values() for y in e["years"].values()
    if abs(float(y.get(FIELD) or 0)) > 0.01)
```

## 2. Two rows can share one label

Both in-kind rows are labelled simply `In-Kind` — once under Contributions,
once under Expenditures. Only the enclosing section distinguishes them, which
is why `parse_dollar_between` exists. Reach for it whenever a label is not
unique on the page.

## 3. A blank statement is not a missing statement

ORESTAR keeps issuing an annual Account Summary after a committee stops
operating, and those statements are entirely blank — no activity **and** no
cash balance. That is the record saying the committee is finished, not a
scraping failure.

Distinguishing it from a **dormant** committee matters: a dormant committee has
ORESTAR carrying a real balance forward while filing nothing (Oregon Strong,
$889,626.78). The `$0.00` cash balance is the only thing separating the two.
See the `closed` flag in `process.py`.

## 4. The summary you are comparing against is a snapshot

Account summaries are captured at a point in time; transactions keep arriving.
Comparing today's transactions against a summary scraped ten days ago produces
a divergence that is neither side's fault. This accounted for **95% of the 2026
balance divergence** — $3,473,683 across 465 committees fell to $189,706 once
the summaries were re-scraped.

`scrape_ts` records when the ORESTAR page was read, but a timestamp alone does
not make the other side reproducible. `filed_date` is only a calendar date and
describes when the filer submitted a row — not when this app collected it. A
same-day filing and a five-year-old row found by a backfill can both arrive
after the summary capture.

The authoritative check is therefore a **paired capture window**. When the
current ORESTAR page is parsed, the scraper binds its balance to the app balance
already published from a fingerprinted set of transaction shards. The app-side
timestamp and the ORESTAR read timestamp bound that window; they are not
pretended to be one instant. That frozen `delta_at_capture` is the audit fact.
If either side changes afterward, the pair becomes `refresh_needed` rather than
remaining an actionable discrepancy.

Legacy summaries without an app-side pair are `legacy_unpaired`, not
discrepancies. Multi-committee canonical names are paired only when every
physical filer ID was captured against the same app snapshot.

The inexpensive weekly pass reads only the current summary page. A monthly
pass retains the historical opening-balance crawl. Every year carries its own
scrape timestamp, so refreshing 2026 cannot make untouched 2006–2025 pages look
current. Per-year live deltas remain diagnostic only until per-year app values
are captured too; they must not drive warnings or automated backfills.

Exact transaction-ID evidence uses the same rule. After a successful current
summary sweep, `atomic-balance-evidence.yml` pulls transaction, summary, and
auxiliary state once; captures fresh summaries; then exact-diffs only complete
canonical scopes without another pull or checkout. Multi-ID scopes are staged,
certified, and saved all-or-none. A summary refusal prevents the heavier diff,
and a partial scope remains unknown. Only a clean batch self-chains.

## 5. Compare like with like, or the difference is your own

Recurring source of phantom discrepancies. Before treating a delta as real,
confirm both sides define the quantity identically:

- **Date window** — a filer-scoped ORESTAR search covers 2006→today; an
  unbounded local count includes rows outside it.
- **In-kind** — does not move cash, so it belongs in contribution totals but
  not in a cash-balance net.
- **Balance adjustments** — ORESTAR's ending balance reflects them; a net that
  omits them will not reconcile.
- **Amendments** — only the newest version counts. Both the superseded original
  *and* older amendments in a chain must be dropped.
- **Multi-committee names** — our transactions are summed across every filer a
  canonical name covers, so the ORESTAR side must cover the same set or the
  comparison reports the missing committee's whole volume as a discrepancy.

## 6. A green run is not evidence

Most defects in this pipeline reported success while doing nothing:

- a backfill that marked filers complete without requesting them
- 59 consecutive runs that re-derived the same narrowing tree and recovered
  nothing
- a daily refresh that deleted the race map's prior-cycle history every day
- a survey that measured 96 committees and committed none of them

Judge a run by what it **changed** — rows landed, counts moved — never by its
exit status.

### And when reading the logs

`gh run view --log` interleaves GitHub's **echoed script source** with actual
output, and the step-name column reads `UNKNOWN STEP` for many runs. Grepping
naively matches the workflow's own text: `Targeted scrape of:` and
`targeted=true` were both "found" in a run where neither executed. Strip the
`\x1b[36` echo prefix before concluding anything.
