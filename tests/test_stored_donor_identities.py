"""Opt-in rollback-only tests of the real migration in pg_temp, never public."""
import os
import re
import sys
from pathlib import Path

import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scraper'))
pytestmark = pytest.mark.skipif(os.getenv('ORESTAR_TEST_DB') != '1', reason='Opt-in temporary database fixtures')

@pytest.fixture
def db():
    import supabase_sync as s
    s._load_dotenv()
    import psycopg2
    try:
        c = psycopg2.connect(**s._parse_dsn(os.environ['SUPABASE_DB_URL']), sslmode='require', connect_timeout=15)
    except Exception:
        pytest.fail('Database fixture connection unavailable', pytrace=False)
    try:
        q = c.cursor()
        q.execute("set local search_path=pg_temp,public; set local statement_timeout='30s'")
        q.execute('''
          create temporary table donors(donor_id text primary key,display_name text);
          create temporary table donor_aliases(alias_key text primary key,donor_id text references donors on delete cascade);
          create temporary table donor_identity_anchors(donor_id text primary key,alias_key text);
          create temporary table donor_merge_overrides(merge_key text primary key,alias_a text,alias_b text,
            decision text,keep_alias_key text,decided_at timestamptz default now());
          create temporary table transactions(tran_id int primary key,donor_id text,filer_id text,amount numeric);
          insert into donors values ('a','A'),('b','B'),('c','C'),('x','Unrelated');
          insert into donor_aliases values ('aa','a'),('bb','b'),('cc','c'),('xx','x');
          insert into transactions values (1,'a','one',100),(2,'b','two',200),(3,'c','three',300),(4,'x','four',400);
        ''')
        migration = (ROOT/'supabase/migrations/027_stored_donor_identities.sql').read_text()
        migration = migration.replace('public.', 'pg_temp.').replace('search_path=public,pg_temp', 'search_path=pg_temp,public')
        migration = re.sub(r'^(?:grant|revoke|notify)\b[^;]*;', '', migration, flags=re.M)
        migration = migration.replace('pg_advisory_xact_lock(726027)', 'pg_advisory_xact_lock(726027000000 + pg_backend_pid())')
        assert 'public.' not in migration
        q.execute(migration)
        # Same existing validation trigger as migration 021.
        q.execute('''create constraint trigger validate_entity_merge after insert or update on donor_merge_overrides
          deferrable initially immediate for each row execute function pg_temp.validate_entity_merge()''')
        yield q
    finally:
        c.rollback()
        c.close()

def flush(q):
    q.execute('set constraints finish_donor_identity_refresh immediate; set constraints finish_donor_identity_refresh deferred')

def merge(q, a='aa', b='bb', keep='aa'):
    q.execute('insert into donor_merge_overrides(merge_key,alias_a,alias_b,decision,keep_alias_key) values(%s,%s,%s,\'merged\',%s)', (a+'|'+b,a,b,keep))

def column(q):
    q.execute('select donor_id,canonical_entity_id from donors order by donor_id')
    return dict(q.fetchall())

def test_stored_chain_kept_entity_and_undo(db):
    merge(db); merge(db,'bb','cc','aa'); flush(db)
    assert column(db) == {'a':'a','b':'a','c':'a','x':'x'}
    db.execute('select donor_id,canonical_id from donor_identity_map except select donor_id,canonical_id from donor_identity_graph')
    assert db.fetchall() == []
    db.execute('select filer_id from donor_merge_filers order by 1')
    assert db.fetchall() == [('one',),('three',),('two',)]
    db.execute('select donor_id,amount from transactions order by tran_id')
    assert db.fetchall() == [('a',100),('b',200),('c',300),('x',400)]
    db.execute("delete from donor_merge_overrides where alias_b='cc'"); flush(db)
    assert column(db)['c'] == 'c'
    db.execute('delete from donor_merge_overrides'); flush(db)
    assert column(db) == {'a':'a','b':'b','c':'c','x':'x'}
    db.execute('select * from donor_merge_filers'); assert db.fetchall() == []

def test_new_imports_and_resolver_transaction_reassignments_mark_new_filers(db):
    merge(db); flush(db)
    db.execute("insert into transactions values(5,'b','new',50)")
    db.execute("update transactions set donor_id='b' where tran_id=4")
    db.execute('select filer_id from donor_merge_filers order by 1')
    assert db.fetchall() == [('four',),('new',),('one',),('two',)]
    db.execute("insert into donors(donor_id,display_name) values('new','New donor')")
    flush(db); assert column(db)['new'] == 'new'

def test_full_resolver_rebuild_keeps_historical_redirects_and_saved_choice(db):
    db.execute("insert into donor_identity_anchors values('a','aa'),('b','bb')")
    merge(db); flush(db)
    db.execute('truncate donor_aliases,donors')
    db.execute("insert into donors(donor_id,display_name) values('new','New cluster'),('x','Unrelated')")
    db.execute("insert into donor_aliases values('aa','new'),('bb','new'),('xx','x')")
    flush(db)
    assert column(db) == {'new':'new','x':'x'}
    db.execute("select donor_id,canonical_id from donor_identity_map where donor_id in ('a','b') order by 1")
    assert db.fetchall() == [('a','new'),('b','new')]
    db.execute('select filer_id from donor_merge_filers order by 1')
    assert db.fetchall() == [('one',),('two',)]

def test_conflicting_separate_decision_rejected_using_pending_graph(db):
    db.execute("insert into donor_merge_overrides(merge_key,alias_a,alias_b,decision) values('separate','aa','cc','separate')")
    merge(db)
    with pytest.raises(Exception, match='conflicts with an existing separate'):
        merge(db,'bb','cc','aa')

def test_rollback_reverts_column_and_cache_with_merge(db):
    db.execute('savepoint before_merge')
    merge(db); flush(db)
    assert column(db)['b'] == 'a'
    db.execute('rollback to savepoint before_merge')
    assert column(db)['b'] == 'b'
    db.execute('select * from donor_merge_filers'); assert db.fetchall() == []


def test_migration_backfills_existing_decisions_and_is_rerunnable(db):
    merge(db); flush(db)
    # Model pre-migration storage while keeping the reviewed decisions.
    db.execute('truncate donor_identity_redirects,donor_merge_filer_cache')
    db.execute('update donors set canonical_entity_id=donor_id')
    sql = (ROOT/'supabase/migrations/027_stored_donor_identities.sql').read_text()
    sql = sql.replace('public.', 'pg_temp.').replace('search_path=public,pg_temp', 'search_path=pg_temp,public')
    sql = re.sub(r'^(?:grant|revoke|notify)\b[^;]*;', '', sql, flags=re.M)
    sql = sql.replace('pg_advisory_xact_lock(726027)', 'pg_advisory_xact_lock(726027000000 + pg_backend_pid())')
    db.execute(sql)
    assert column(db)['b'] == 'a'
    db.execute(sql)
    assert column(db)['b'] == 'a'
    db.execute('select filer_id from donor_merge_filers order by 1')
    assert db.fetchall() == [('one',),('two',)]
