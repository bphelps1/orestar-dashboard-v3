-- The Recommend tool is for signed-in users, and so are the two database
-- objects only it reads: the race_margins view and recommendation_first_gifts.
-- 012 and 025 grant both to anon, and db_admin re-runs every migration on each
-- apply, so this is registered after them and takes that back every time.
-- Functions are executable by PUBLIC unless revoked, hence both roles below.

revoke select on public.race_margins from anon;

revoke execute on function public.recommendation_first_gifts(text[], text[], date) from public, anon;
grant execute on function public.recommendation_first_gifts(text[], text[], date) to authenticated, service_role;
