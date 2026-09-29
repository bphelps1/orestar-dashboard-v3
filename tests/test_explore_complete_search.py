"""Rollback-only tests of the Explore RPC; never alter public functions or rows."""
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get('ORESTAR_TEST_DB') != '1', reason='opt-in PostgreSQL test')

@pytest.fixture
def db():
    import psycopg2
    sys.path.insert(0, str(ROOT / 'scraper'))
    import supabase_sync as s
    try:
        conn = psycopg2.connect(**s._parse_dsn(os.environ['ORESTAR_TEST_DSN']), sslmode='require', connect_timeout=15)
    except Exception:
        raise RuntimeError('Could not connect to configured test database') from None
    schema = 'test_explore_' + uuid.uuid4().hex
    try:
        q = conn.cursor()
        q.execute(f'create schema {schema}')
        q.execute(f'set local search_path={schema},public')
        q.execute('create table transactions (like public.transactions including all)')
        q.execute("""create function donor_group_ids(id text) returns text[] language sql as $$
          select case when id in ('a','b') then array['a','b'] else array[id] end $$""")
        for name in ['030_explore_source_name_indexes.sql','031_explore_complete_name_search.sql',
                     '033_explore_sub_type_index.sql','034_explore_sub_types.sql',
                     '035_explore_default_order_index.sql']:
            sql = (ROOT/'supabase/migrations'/name).read_text().replace('public.',schema+'.').replace('search_path = public','search_path = '+schema)
            q.execute(sql)
        q.execute("""insert into transactions(tran_id,filer_id,filer,filer_canonical,contributor_payee,
          contributor_payee_canonical,donor_id,amount,tran_date,tran_type) values
          (1,'19463','Friends of Daniel Nguyen',null,'Original PAC',null,'a',100,'2026-01-01','C'),
          (2,'19463','Friends of Daniel Nguyen','Friends of Daniel Nguyen','Other name','Adopted PAC','b',200,'2026-02-01','C'),
          (3,'other','Other committee','Other committee','Original PAC','Adopted PAC','x',300,'2026-03-01','E')""")
        yield q
    finally:
        conn.rollback()
        conn.close()


def test_source_and_canonical_names_match_without_duplicates(db):
    db.execute("select tran_id,filer_canonical,contributor_payee_canonical from search_transactions(p_filer=>'Daniel Nguyen')")
    rows = db.fetchall()
    assert [r[0] for r in rows] == [2,1]
    assert rows[1][1:] == ('Friends of Daniel Nguyen','Original PAC')
    db.execute("select tran_id from search_transactions(p_payee=>'Original PAC')")
    assert db.fetchall() == [(3,),(1,)]
    db.execute("select tran_id from search_transactions(p_payee=>'Adopted PAC')")
    assert db.fetchall() == [(3,),(2,)]


def test_paging_filters_and_selected_merge_group(db):
    db.execute("select tran_id from search_transactions(p_donor_id=>'b',p_payee=>'ignored',p_sort=>'tran_id',p_asc=>true,p_limit=>1)")
    assert db.fetchall() == [(1,)]
    db.execute("select tran_id from search_transactions(p_donor_id=>'a',p_sort=>'tran_id',p_asc=>true,p_limit=>1,p_offset=>1)")
    assert db.fetchall() == [(2,)]
    db.execute("select tran_id from search_transactions(p_filer=>'Daniel Nguyen',p_payee=>'Original PAC',p_amt_min=>50,p_amt_max=>150,p_date_from=>'2026-01-01',p_date_to=>'2026-01-31',p_tran_type=>'C')")
    assert db.fetchall() == [(1,)]
    db.execute("select tran_id from search_transactions(p_donor_id=>'unknown')")
    assert db.fetchall() == []


def test_actual_daniel_nguyen_rows_are_all_found(db):
    # Copy only public records for this committee into the rollback-only fixture.
    db.execute('delete from transactions')
    db.execute("insert into transactions select * from public.transactions where filer_id='19463'")
    db.execute('select count(*) from transactions')
    count = db.fetchone()[0]
    found = []
    for offset in range(0,count,1000):
        db.execute("select tran_id from search_transactions(p_filer=>'Daniel Nguyen',p_sort=>'tran_id',p_asc=>true,p_limit=>1000,p_offset=>%s)",(offset,))
        found.extend(r[0] for r in db.fetchall())
    assert len(found) == count
    assert len(set(found)) == count


def test_explore_returns_and_filters_sub_types(db):
    db.execute("""update transactions set sub_type = case tran_id when 1 then 'Cash Contribution'
                  when 2 then 'In-Kind Contribution' else 'Cash Expenditure' end where tran_id in (1,2,3)""")
    db.execute("select tran_id,tran_type,sub_type from explore_transactions(p_sub_type=>'In-Kind Contribution')")
    assert db.fetchall() == [(2,'C','In-Kind Contribution')]
    db.execute("select tran_id from explore_transactions(p_tran_type=>'C',p_sub_type=>'Cash Expenditure')")
    assert db.fetchall() == []
    db.execute("select tran_id from explore_transactions(p_sort=>'sub_type',p_asc=>true)")
    assert db.fetchall() == [(1,),(3,),(2,)]


def test_explore_matches_search_transactions_without_a_sub_type(db):
    for args in ("p_filer=>'Daniel Nguyen'", "p_payee=>'Adopted PAC'",
                 "p_donor_id=>'b',p_sort=>'tran_id',p_asc=>true",
                 "p_tran_type=>'C',p_amt_min=>150", "p_limit=>1,p_offset=>1"):
        db.execute(f"select tran_id,amount,filer_canonical,contributor_payee_canonical from search_transactions({args})")
        expected = db.fetchall()
        db.execute(f"select tran_id,amount,filer_canonical,contributor_payee_canonical from explore_transactions({args})")
        assert db.fetchall() == expected, args
