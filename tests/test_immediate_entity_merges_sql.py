"""Database regression for the immediate merge migration.

Opt in with ORESTAR_TEST_DB=1. Uses a private, uncommitted schema and always
rolls it back; never applies the migration to public or changes source data.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scraper'))

@pytest.mark.skipif(os.environ.get('ORESTAR_TEST_DB') != '1', reason='requires opt-in database connection')
@pytest.mark.parametrize('stored', [False, True])
def test_immediate_merges_across_reads_and_undo(stored):
    import supabase_sync as s
    import psycopg2
    try:
        conn = psycopg2.connect(**s._parse_dsn(os.environ['ORESTAR_TEST_DSN']), sslmode='require')
    except Exception:
        raise RuntimeError('Could not connect to the explicitly configured test database') from None
    schema = 'test_merge_' + uuid.uuid4().hex
    try:
        q = conn.cursor()
        q.execute(f'create schema {schema}')
        q.execute(f'set local search_path={schema}')
        for table in ['donors','donor_aliases','donor_merge_overrides','donor_lobbyist_links',
                      'donor_client_links','donor_contacts','transactions','filer_detail','lobbyists','lobbyist_clients','donor_review_decisions']:
            q.execute(f'create table {schema}.{table} (like public.{table} including all)')
        q.execute(f'alter table {schema}.donors drop column if exists canonical_entity_id')
        normalize = (ROOT/'supabase/migrations/014_donor_leaderboard.sql').read_text().split('create or replace view')[0]
        migration = (ROOT/'supabase/migrations/021_immediate_entity_merges.sql').read_text() + '\n' + (ROOT/'supabase/migrations/022_donor_display_aliases.sql').read_text() + '\n' + (ROOT/'supabase/migrations/024_donor_profile_lookup_performance.sql').read_text() + '\n' + (ROOT/'supabase/migrations/025_recommendation_first_gift_performance.sql').read_text()
        if stored:
            migration += '\n' + (ROOT/'supabase/migrations/026_donor_filer_index.sql').read_text()
            migration += '\n' + (ROOT/'supabase/migrations/027_stored_donor_identities.sql').read_text()
            migration += '\n' + (ROOT/'supabase/migrations/028_donor_identity_label_indexes.sql').read_text()
            migration += '\n' + (ROOT/'supabase/migrations/029_stored_donor_identity_labels.sql').read_text()
        # All tables/functions/views/policies and grants stay in this schema.
        sql = (normalize + migration).replace('public.', schema + '.').replace('search_path = public', 'search_path = '+schema).replace('search_path=public', 'search_path='+schema)
        sql = sql.replace('pg_advisory_xact_lock(726027)', 'pg_advisory_xact_lock(726027000000 + pg_backend_pid())')
        q.execute(sql)
        if stored:
            for role in ('anon', 'authenticated'):
                q.execute("select has_function_privilege(%s,%s,'execute')", (role,schema+'.refresh_stored_donor_identities()'))
                assert q.fetchone()[0] is False
                q.execute("select has_table_privilege(%s,%s,'select')", (role,schema+'.donor_identity_graph'))
                assert q.fetchone()[0] is False
                q.execute("select has_table_privilege(%s,%s,'update')", (role,schema+'.donor_identity_redirects'))
                assert q.fetchone()[0] is False
            # Each statement below models a separate API save/commit. Dedicated
            # stored-identity tests exercise deferred bulk-import behavior.
            q.execute('set constraints finish_donor_identity_refresh immediate')
        q.execute("insert into donor_review_decisions(pair_key,decision,merged_name,kept_name) values ('old|||new','merged','Old Brand','eBay PAC')")
        q.execute("select alias,display_name from donor_display_aliases order by alias")
        assert q.fetchall()==[('ebay pac','eBay PAC'),('old brand','eBay PAC')]
        q.execute("insert into donors(donor_id,display_name,total_given,gift_count) values ('a','Acme',100,1),('b','Acme Services',200,1),('c','Acme LLC',300,1),('x','Unrelated',400,1)")
        q.execute("insert into donor_aliases(alias_key,donor_id,raw_name,norm_name,addr_key,source) values ('aa','a','Acme','acme','','test'),('bb','b','Acme Services','acme services','','test'),('bb2','b','Acme Services Other','acme services other','','test'),('cc','c','Acme LLC','acme llc','','test'),('xx','x','Unrelated','unrelated','','test')")
        q.execute("insert into transactions(tran_id,donor_id,tran_type,tran_date,filer_id,filer,amount,contributor_payee) values (1,'a','C','2026-02-01','f','Candidate',100,'Acme'),(2,'b','C','2026-01-01','f','Candidate',200,'Acme Services'),(3,'c','C','2026-03-01','f','Candidate',300,'Acme LLC'),(4,'x','C','2026-01-01','f','Candidate',400,'Unrelated')")
        q.execute("insert into filer_detail(slug,name,filer_id,detail) values ('candidate','Candidate','f','{}')")
        # B connects through two DIFFERENT alias keys; transitivity is by ID.
        q.execute("insert into donor_merge_overrides(merge_key,alias_a,alias_b,decision,keep_alias_key) values ('ab','aa','bb','merged','aa'),('bc','bb2','cc','merged',null)")
        q.execute("select donor_identity('b')")
        identity = q.fetchone()[0]
        assert identity['donor_id'] == 'a' and identity['total_given'] == 600
        assert set(identity['member_ids']) == {'a','b','c'}
        q.execute("select * from search_donors('Acme',12)")
        search=q.fetchall(); assert len(search)==1 and search[0][0]=='a' and search[0][5]==600
        q.execute("select * from donor_search('Services',50)")
        assert q.fetchone()[0]=='a'
        q.execute("select donor_profile('c')")
        profile=q.fetchone()[0]; assert profile['top_recipients'][0]['total']==600
        assert profile['top_recipients'][0]['n']==3
        q.execute("select donor_leaderboard(null,null,array['f'])")
        rows=q.fetchone()[0]['all_time']; assert len(rows)==2
        assert rows[0]['donor_id']=='a' and rows[0]['total']==600
        q.execute("select sum(amount) from donor_contribution_rows where donor_id='a'")
        assert q.fetchone()[0]==600
        q.execute("select * from recommendation_first_gifts(array['a'],array['f'],date '2026-12-31')")
        first=q.fetchone(); assert first[0]=='a' and first[3]==200
        q.execute("select * from recommendation_first_gifts(array['b','a','b'],array['f'],date '2026-12-31')")
        assert q.fetchall()==[first]
        q.execute("select * from recommendation_first_gifts(array[]::text[],array['f'],date '2026-12-31')")
        assert q.fetchall()==[]
        q.execute("select * from donor_merge_filers")
        assert q.fetchall()==[('f',)]
        # No rewrite or full resolver was necessary; raw IDs are untouched.
        q.execute('select donor_id from transactions order by tran_id')
        assert q.fetchall()==[('a',),('b',),('c',),('x',)]
        # Conflicting explicit separate decisions cannot be overridden by a
        # transitive new merge. The whole statement is rejected.
        q.execute("insert into donor_merge_overrides(merge_key,alias_a,alias_b,decision) values ('cx','cc','xx','separate')")
        q.execute('savepoint conflict_test')
        try:
            q.execute("insert into donor_merge_overrides(merge_key,alias_a,alias_b,decision) values ('ax','aa','xx','merged')")
        except Exception as error:
            assert 'conflicts with an existing separate decision' in str(error)
            q.execute('rollback to savepoint conflict_test')
        else:
            raise AssertionError('conflicting merge was accepted')
        # Reviewed attributions survive on the group; any explicit rejection
        # remains a veto, even when it was filed under a different member.
        q.execute("insert into lobbyists(lobbyist_id,name) overriding system value values (1,'Lobbyist')")
        q.execute("insert into donor_lobbyist_links(donor_id,lobbyist_id,method,status) values ('b',1,'manual','confirmed')")
        q.execute("select donor_id from donor_lobbyists")
        assert q.fetchall()==[('a',)]
        q.execute("insert into donor_lobbyist_links(donor_id,lobbyist_id,method,status) values ('c',1,'manual','rejected')")
        q.execute("select donor_id from donor_lobbyists")
        assert q.fetchall()==[]
        # Undo changes the read-through immediately, with no lossy data split.
        q.execute("delete from donor_merge_overrides where merge_key='ab'")
        q.execute("select donor_identity('a')")
        assert q.fetchone()[0]['total_given']==100
        q.execute("select donor_identity('c')")
        assert q.fetchone()[0]['total_given']==500
        # Historical IDs route to the current cluster after physical resolution.
        q.execute("insert into donors(donor_id,display_name,total_given,gift_count) values ('new','Combined',500,2)")
        q.execute("update donor_aliases set donor_id='new' where donor_id in ('b','c')")
        q.execute("delete from donors where donor_id in ('b','c')")
        q.execute("select donor_group_ids('b')")
        assert set(q.fetchone()[0])=={'new','b','c'}
        # A full re-resolution must replace stale non-null IDs. Source rows
        # with an authoritative ORESTAR committee ID remain protected.
        import resolve_donors
        q.execute("create table _dmap(raw_name text,addr text,zip text,donor_id text)")
        q.execute("insert into _dmap values ('Acme Services','','','new'),('Acme LLC','','','new'),('Unrelated','','','bad')")
        q.execute("update transactions set contributor_payee_committee_id='123' where tran_id=4")
        assert resolve_donors.stamp_resolved_batch(q,0,10)==2
        q.execute('select donor_id from transactions order by tran_id')
        assert q.fetchall()==[('a',),('new',),('new',),('x',)]
        # Read functions are usable by the actual web role, with no write grant.
        q.execute(f'grant usage on schema {schema} to anon')
        q.execute(f'grant select on all tables in schema {schema} to anon')
        q.execute('set local role anon')
        q.execute(f"select {schema}.donor_identity('b')")
        assert q.fetchone()[0]['donor_id']=='new'
        q.execute('reset role')
        # Exact IDs win over aliases; historical IDs still find a consolidated
        # profile. Missing metadata retains raw/fallback labels and totals.
        q.execute("insert into filer_detail(slug,name,filer_id,detail) values ('exact','Exact Committee','exact','{}'),('aaa-alias','Consolidated Committee','current','{\"filer_ids\":[\"exact\",\"old\"]}')")
        q.execute("insert into transactions(tran_id,donor_id,tran_type,tran_date,filer_id,filer,filer_canonical,amount) values (10,'labels','C','2026-01-01','exact','Raw Exact','',100),(11,'labels','C','2026-01-01','old','Raw Old','',200),(12,'labels','C','2026-01-01','missing','Raw Fallback','',300),(13,'labels','C',null,'unknown','','',400),(14,'labels','E','2026-01-01','exact','Exact','',50)")
        q.execute('set local role anon')
        q.execute("select donor_profile('labels')")
        result=q.fetchone()[0]
        recipients={r['filer_id']:r for r in result['top_recipients']}
        assert recipients['exact']['filer']=='Exact Committee'
        assert recipients['old']['slug']=='aaa-alias'
        assert recipients['missing']['filer']=='Raw Fallback'
        assert recipients['unknown']['filer']=='Committee unknown'
        assert result['by_year']==[{'year':2026,'given':600,'received':50}]
        q.execute("select donor_profile('absent')")
        assert q.fetchone()[0]=={'by_year':[],'top_recipients':[]}

    finally:
        conn.rollback()
        conn.close()
