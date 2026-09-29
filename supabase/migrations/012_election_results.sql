-- ============================================================================
-- Official election results (2008–2026) and derived general-election margins.
--
-- Extracted from the Secretary of State "Abstract of Votes" PDFs. Feeds the
-- recommendation engine: how competitive a seat is should shape both which
-- committees are comparable and how large an ask is realistic. A donor history
-- from a safe seat is a poor template for a swing seat.
-- ============================================================================

create table if not exists election_results (
  id                serial primary key,
  election          text not null,          -- "2024 General"
  year              int  not null,
  election_type     text not null,          -- General | Primary
  office_normalized text not null,          -- "State Representative"
  district          text,                   -- "15th District"
  ballot_party      text,                   -- primary ballot party
  candidate         text not null,
  candidate_party   text,
  party_code        text,
  votes             int  not null default 0,
  pct               numeric,
  won               boolean not null default false,
  is_measure        boolean not null default false,
  source            text                    -- 'text' | 'ocr' (OCR = lower confidence)
);

create index if not exists idx_results_office_district
  on election_results (office_normalized, district, year);
create index if not exists idx_results_candidate
  on election_results (candidate);

-- ── District eras ──────────────────────────────────────────────────────────
-- Oregon redraws legislative maps two years after each census, so a district
-- number does NOT refer to the same electorate across a redistricting
-- boundary. Comparing 2010 to 2012, or 2020 to 2022, would silently compare
-- different places.
create or replace function district_era(p_year int) returns text
language sql immutable as $$
  select case
    when p_year < 2012 then '2002-2010'
    when p_year < 2022 then '2012-2020'
    else '2022-'
  end
$$;

-- ── General-election margins ───────────────────────────────────────────────
-- Primaries are deliberately excluded: they are far more variable (unopposed
-- incumbents, multi-way fields) and a primary margin says little about how
-- contested the seat itself is.
create or replace view race_margins as
with g as (
  select year, office_normalized, district, candidate, candidate_party,
         party_code, votes, won,
         district_era(year) as era,
         sum(votes) over (partition by year, office_normalized, district) as total_votes,
         row_number() over (partition by year, office_normalized, district
                            order by votes desc) as rank
  from election_results
  where election_type = 'General'
    and not is_measure
    and lower(candidate) not in ('misc.', 'misc', 'write-in')
)
select
  w.year, w.office_normalized, w.district, w.era,
  w.candidate                          as winner,
  w.party_code                         as winner_party,
  w.votes                              as winner_votes,
  r.candidate                          as runner_up,
  r.party_code                         as runner_up_party,
  coalesce(r.votes, 0)                 as runner_up_votes,
  w.total_votes,
  case when w.total_votes > 0
       then round(100.0 * (w.votes - coalesce(r.votes, 0)) / w.total_votes, 2)
       else null end                   as margin_pts,
  -- Unopposed races are the extreme of "safe", not missing data.
  (r.candidate is null)                as unopposed
from g w
left join g r
  on r.year = w.year and r.office_normalized = w.office_normalized
 and coalesce(r.district,'') = coalesce(w.district,'') and r.rank = 2
where w.rank = 1;

alter table election_results enable row level security;
drop policy if exists "Public read election_results" on election_results;
create policy "Public read election_results" on election_results
  for select to anon, authenticated using (true);

grant select on race_margins to anon, authenticated;
