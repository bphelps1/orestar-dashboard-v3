-- ============================================================================
-- Read-only SQL surface for the public "run your own SQL" box.
--
-- Security model — enforced by Postgres, not by string parsing:
--   • A dedicated login role `public_query` that the sql-query Edge Function
--     connects as (never the service role / anon).
--   • The role can see ONLY the view `query.transactions`, which is owned by a
--     privileged role, so the role never touches base tables, auth.*, or the
--     admin tables. It cannot reach anything in the public schema.
--   • Every session is read-only and has a 5s statement timeout, so a runaway
--     or abusive query cannot lock up or exhaust the database.
--
-- The Edge Function adds a second layer (reject non-SELECT, force a LIMIT),
-- but this role is the real boundary.
--
-- OPERATOR STEP (out of band, not committed): set a password and wire the DSN
-- into the Edge Function secret:
--   alter role public_query password '<strong-random-secret>';
--   supabase secrets set QUERY_DB_URL="postgresql://public_query:<secret>@<host>:6543/postgres"
-- ============================================================================

create schema if not exists query;

-- Projection researchers query. It exposes the full transactions dataset with
-- friendly column names; being a (definer) view, it runs with the owner's
-- rights so `public_query` needs no privileges on public.transactions itself.
-- NOTE: contributor type is carried in `book_type` on transaction rows.
-- ORESTAR's transaction export leaves `contributor_type_label`, `party`, and
-- `office` empty (party/office are filer-level metadata), so those are not
-- exposed here — they would only ever return NULL and mislead researchers.
create or replace view query.transactions as
  select
    tran_id, tran_date, filed_date, tran_type, sub_type, amount, aggregate_amount,
    filer_canonical      as filer,
    contributor_payee_canonical as contributor_payee,
    book_type           as contributor_type,
    purpose,
    employer, occupation, city, state, zip, county, country,
    filer_id, contributor_payee_committee_id,
    filer               as filer_raw,
    contributor_payee   as contributor_payee_raw
  from public.transactions;

-- Dedicated least-privilege role. Created with LOGIN but NO password here;
-- the operator sets the password out of band (see header). Until then it
-- cannot authenticate, so committing this file exposes nothing.
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'public_query') then
    create role public_query login;
  end if;
end
$$;

-- Session hardening applied on every connection as this role.
-- 30s backstop; the sql-query Edge Function also sets its own per-transaction
-- statement_timeout (25s), since the pooler doesn't reliably propagate this
-- role-level GUC to pooled connections.
alter role public_query set statement_timeout = '30s';
alter role public_query set default_transaction_read_only = on;
alter role public_query set search_path = query;
alter role public_query set idle_in_transaction_session_timeout = '10000ms';

-- Lock the role out of everything except the query view.
revoke all on schema public from public_query;
revoke all on all tables in schema public from public_query;
grant usage on schema query to public_query;
grant select on query.transactions to public_query;

-- Make sure it can never create objects anywhere.
revoke create on schema public from public_query;
revoke create on schema query  from public_query;
