"""Name-only merges remain unambiguous without read-time statewide scans."""
import re
import pytest
from test_stored_donor_identities import ROOT, db, flush, merge, pytestmark

@pytest.fixture
def labels_db(db):
    db.execute('alter table donor_aliases add column raw_name text')
    db.execute("update donor_aliases set raw_name=upper(donor_id)")
    for name in ('028_donor_identity_label_indexes.sql','029_stored_donor_identity_labels.sql'):
        sql = (ROOT/'supabase/migrations'/name).read_text()
        sql = sql.replace('public.', 'pg_temp.').replace('search_path=public,pg_temp','search_path=pg_temp,public')
        sql = sql.replace('pg_advisory_xact_lock(726027)','pg_advisory_xact_lock(726027000000 + pg_backend_pid())')
        sql = re.sub(r'^(?:grant|revoke|notify)\b[^;]*;', '', sql, flags=re.M)
        db.execute(sql)
    return db

def labels(q):
    q.execute('select label,canonical_id,canonical_name from donor_identity_labels order by label')
    return {label:(identity,name) for label,identity,name in q.fetchall()}

def test_alias_collisions_are_not_merged_and_alias_edits_refresh_cache(labels_db):
    q=labels_db
    q.execute("insert into donor_aliases(alias_key,donor_id,raw_name) values ('b2','b','Shared'),('x2','x',' SHARED '),('b3','b','  Unique   B ')")
    merge(q);flush(q)
    found=labels(q)
    assert found['unique b']==('a','A')
    assert 'shared' not in found
    q.execute("delete from donor_aliases where alias_key='x2'");flush(q)
    assert labels(q)['shared']==('a','A')
    q.execute("update donors set display_name='SHARED' where donor_id='x'");flush(q)
    assert 'shared' not in labels(q)

def test_canonical_name_changes_and_undo_refresh_labels_atomically(labels_db):
    q=labels_db;merge(q);flush(q)
    q.execute("update donors set display_name='AT&T' where donor_id='a'");flush(q)
    assert labels(q)['at&t']==('a','AT&T')
    assert labels(q)['b']==('a','AT&T')
    q.execute('delete from donor_merge_overrides');flush(q)
    assert labels(q)=={}

def test_full_import_rebuild_preserves_name_lookup_and_historical_id(labels_db):
    q=labels_db
    q.execute("insert into donor_identity_anchors values ('a','aa'),('b','bb')")
    merge(q);flush(q)
    q.execute('truncate donor_aliases,donors')
    q.execute("insert into donors(donor_id,display_name) values ('new','New Canonical')")
    q.execute("insert into donor_aliases(alias_key,donor_id,raw_name) values ('aa','new','Old A'),('bb','new','Old B')")
    flush(q)
    assert labels(q)['old b']==('new','New Canonical')
    q.execute("select canonical_id from donor_identity_map where donor_id='b'")
    assert q.fetchone()[0]=='new'
