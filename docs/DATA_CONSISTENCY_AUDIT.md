# Data Consistency Audit: Findings & Required Fixes

## Executive Summary

There is a confirmed mismatch between stat card values and account summary detail values for filtered filers. The root cause is two different definitions of "contributions" and "expenditures" being used in different code paths. Additionally, ORESTAR line-item comparisons are not being surfaced per-year, only ending-balance comparisons exist.

---

## Finding 1: Stat Card / Account Summary Mismatch

### The Problem

When viewing a single filer (e.g., Friends of Ben Bowman) without a date filter:

- **Stat card "Contributions"** shows `total_in` = sum of transactions where `sub_type == "Cash Contribution"` ONLY
- **Account Summary "Cash Contributions" tile** shows sum of `timeline[].contributions` = all type-C transactions MINUS in-kind

These are different numbers because the timeline includes Loan Received, Pledge of Cash, Items Sold at Fundraising Event, Interest/Investment Returns, etc. — all of which are cash-affecting type-C transactions excluded from the strict "Cash Contribution" sub_type filter.

When a date filter IS applied, both the stat cards and account summary use `statsFromTimeline()` which sums `timeline[].contributions` (the broader definition), so they agree. But this creates a different inconsistency: the stat card number changes when you apply an all-encompassing date filter that should show the same total.

### Source of Truth

ORESTAR's "Cash Contributions" line item on the account summary page corresponds to the broader definition (all cash-affecting type-C transactions, not just sub_type "Cash Contribution"). The timeline/COH definition is correct.

### Required Fix

**File: `scraper/process.py`, lines 1153-1158**

Move `total_in` and `total_out` computation to after `_c_for_coh` and `_e_for_coh` are defined (after line 1181), and derive them from the COH-filtered frames:

```python
# Current (WRONG — too narrow):
_cash_contrib = filer_contrib[filer_contrib["sub_type"] == "Cash Contribution"]
total_in = round(float(_cash_contrib["amount"].sum()), 2)

# Fixed (matches ORESTAR, COH, and timeline):
total_in = round(float(_c_for_coh["amount"].sum()), 2)
total_out = round(float(_e_for_coh["amount"].sum()), 2)
```

**File: `docs/index.html`, line 76**

Update help text from "All cash and check donations" to "All cash-affecting contributions (includes cash donations, loans received, and other cash receipts classified as contributions in ORESTAR)."

---

## Finding 2: ORESTAR Line-Item Comparisons Not Surfaced

### The Problem

The `yearly_discrepancies` field only compares our calculated ending balance vs ORESTAR's ending balance per year. It does NOT compare individual line items (contributions, expenditures, other receipts, other disbursements, beginning balance). When there IS an ending-balance discrepancy, there's no way to see which line item caused it.

### Required Fix

**File: `scraper/process.py`, lines 1237-1255**

Extend `yearly_discrepancies` to include per-line-item comparisons:

For each year with ORESTAR yearly data, add:
- `our_contributions` / `orestar_contributions` / `delta_contributions`
- `our_expenditures` / `orestar_expenditures` / `delta_expenditures`
- `our_other_receipts` / `orestar_other_receipts` / `delta_other_receipts`
- `our_other_disbursements` / `orestar_other_disbursements` / `delta_other_disbursements`
- `our_begin` / `orestar_begin` / `delta_begin`

Include a year if ANY line-item delta exceeds $0.01 (not just ending balance delta).

**File: `docs/app.js`, lines 1064-1097**

Update the discrepancy table to show per-line-item columns, or make each year row expandable to show line-item detail.

---

## Finding 3: No Automated Consistency Validation

### The Problem

There is no standalone tool that audits all filers for internal consistency (stat cards vs timeline vs COH) or for ORESTAR agreement across all line items. The existing `verify_filer.py` downloads raw transactions from ORESTAR for comparison, but doesn't check the processed aggregated data for internal consistency.

### Required Fix

**New file: `scraper/audit_consistency.py`**

Standalone script that loads all filer JSONs from `data/aggregated/filers/` and runs these checks:

1. **Internal consistency**: `total_in` == sum of `timeline[].contributions`
2. **Internal consistency**: `total_out` == sum of `timeline[].expenditures`
3. **COH consistency**: `cash_on_hand` == `beginning_balances[first_year] + total_contributions + total_other_receipts - total_expenditures - total_other_disbursements`
4. **Beginning balance continuity**: `beginning_balances[yr+1]` == `beginning_balances[yr] + yearly_net[yr]`
5. **ORESTAR line-item agreement**: For each year with ORESTAR data, compare all 5 line items

Output: CSV report and summary counts.

---

## Finding 4: Missing Test Coverage

### Required Fix

**New file: `tests/test_consistency.py`**

Tests to add:
- `total_in` matches timeline contributions sum (verifies Finding 1 fix)
- `total_out` matches timeline expenditures sum
- `statsFromTimeline` on full timeline matches `total_in`/`total_out`
- `_c_for_coh` includes Loan Received but excludes In-Kind Contribution
- Beginning balance continuity across years
- ORESTAR line-item delta computation populates correctly

---

## Comparison Points & Source of Truth

| Field | Stat Card Source | Account Summary Source | Source of Truth | Match? |
|-------|-----------------|----------------------|-----------------|--------|
| Contributions | `profile.total_in` (Cash Contribution only) | `timeline[].contributions` (all cash type-C minus in-kind) | ORESTAR "Cash Contributions" ≈ timeline definition | **NO** |
| Expenditures | `profile.total_out` (Cash Expenditure only) | `timeline[].expenditures` (all type-E minus AP/PER) | ORESTAR "Cash Expenditures" ≈ timeline definition | **NO** |
| Cash on Hand | `profile.cash_on_hand` (rolling from timeline) | `buildCalcSummary().ending_cash_balance` (from timeline) | ORESTAR "Ending Cash Balance" | Yes |
| In-Kind | `profile.total_inkind` | `timeline[].inkind` sum | ORESTAR "In-Kind Contributions" | Yes |
| Other Receipts | `profile.total_or` | `timeline[].other_receipts` sum | ORESTAR "Other Receipts" | Needs verification |
| Other Disbursements | `profile.total_od` | `timeline[].other_disbursements` sum | ORESTAR "Other Disbursements" | Needs verification |

---

## Implementation Priority

1. **HIGH**: Fix `total_in`/`total_out` definition (Finding 1) — eliminates the user-visible mismatch
2. **MEDIUM**: Extend yearly_discrepancies with line-item data (Finding 2) — enables root-cause analysis of remaining discrepancies
3. **MEDIUM**: Create audit script (Finding 3) — ongoing monitoring
4. **LOW**: Add test coverage (Finding 4) — prevents regressions
