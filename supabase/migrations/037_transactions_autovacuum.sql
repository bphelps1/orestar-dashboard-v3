-- Keep transactions vacuumed after each daily refresh.
--
-- At the default thresholds (20% of rows) autovacuum waits for about 620,000
-- dead rows on this 3.1M-row table, so it had never run: 260,000 dead rows had
-- piled up and 36% of pages were not marked all-visible — mostly the recent
-- ones. The date-range rankings (donor_leaderboard, recipient_leaderboard) are
-- index-only scans of idx_txn_cash_donor_dates, and every page not marked
-- all-visible sends them back to the 1.7 GB heap, far larger than shared
-- buffers: the 2026 cycle took 11–20 s while the settled 2024 cycle took 0.3 s.
-- At 2% a daily refresh's churn is enough to trigger a vacuum and an analyze.
alter table public.transactions set (
  autovacuum_vacuum_scale_factor = 0.02,
  autovacuum_vacuum_insert_scale_factor = 0.02,
  autovacuum_analyze_scale_factor = 0.02
);
