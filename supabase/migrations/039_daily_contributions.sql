-- Cash contributions per day, for Monthly Cash Flow's "Compare cycles" view.
--
-- The timeline blob is monthly, so "this cycle so far" against "the same point
-- last cycle" could only be compared month by month: on Oct 1 the current
-- month counted one day while last cycle's October counted all 31. Daily
-- totals let the dashboard cut both cycles at the same calendar day.
--
-- Cash only, in-kind left out, as in donor_leaderboard (014) and
-- recipient_leaderboard (036). p_filer_ids null = every committee.
create or replace function public.daily_contributions(
  p_start date,
  p_end date,
  p_filer_ids text[] default null
)
returns jsonb language sql stable security invoker
set search_path = public
set plan_cache_mode = force_custom_plan
-- A statewide two-year range is the same ~230k-entry index-only scan of
-- idx_txn_cash_donor_dates (015) that recipient_leaderboard makes.
set statement_timeout = '30s'
as $$
  select coalesce(jsonb_agg(jsonb_build_object('d', d, 'total', round(total, 2)) order by d), '[]'::jsonb)
  from (
    select t.tran_date as d, sum(t.amount) as total
    from public.transactions t
    where t.tran_type = 'C' and coalesce(t.sub_type, '') not in (
        'In-Kind Contribution', 'In-Kind/Forgiven Account Payable',
        'In-Kind/Forgiven Personal Expenditures'
      )
      and t.tran_date between p_start and p_end
      and (p_filer_ids is null or t.filer_id = any(p_filer_ids))
    group by t.tran_date
  ) days;
$$;

grant execute on function public.daily_contributions(date, date, text[]) to anon, authenticated, service_role;
