"""Publication failure paths must never expose an incomplete generation."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scraper'))
import supabase_sync as sync


class Database:
    def __init__(self, fail_at=None):
        self.visible = {'old': 'generation'}
        self.pending = None
        self.fail_at = fail_at
        self.writes = 0
        self.commits = 0
        self.closed = False
        self.batch_sizes = []
        self.settings = []

    def __enter__(self):
        self.pending = copy.deepcopy(self.visible)
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.visible = self.pending
            self.commits += 1
        self.pending = None

    def close(self):
        self.closed = True

    def cursor(self):
        db = self
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def execute(self, sql, values=None):
                if sql.startswith('SET'):
                    db.settings.append((sql, values))
                    return
                db.write(values[0], json.loads(values[1]))
        return Cursor()

    def write(self, key, value):
        self.writes += 1
        if self.writes == self.fail_at:
            raise RuntimeError('injected write failure')
        self.pending[key] = value
        assert self.visible == {'old': 'generation'}


@pytest.fixture
def publisher(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, 'sync_enabled', lambda: True)
    monkeypatch.setenv('BALANCE_PUBLICATION_RECEIPT_PATH', str(tmp_path / 'receipt.json'))
    def setup(fail_at=None):
        db = Database(fail_at)
        monkeypatch.setattr(sync, '_connect', lambda: db)
        def execute_values(cur, sql, batch, **kwargs):
            db.batch_sizes.append(len(batch))
            for row in batch:
                db.write('detail:' + row[0], json.loads(row[3]))
        monkeypatch.setattr('psycopg2.extras.execute_values', execute_values)
        return db
    return setup


def stage(count=101, mismatch=False):
    sync.upsert_dashboard_cache('filer_index', [{'slug': str(i)} for i in range(count)])
    sync.upsert_dashboard_cache('balance_snapshot_source', {'snapshot': 'new'})
    sync.bulk_upsert_filer_detail([{'slug': str(i), 'detail': {'cash': i}}
                                  for i in range(count - int(mismatch))])
    sync.upsert_dashboard_cache('balance_discrepancies', {'flagged': 1})


def test_all_outputs_and_receipt_commit_once(publisher, tmp_path):
    db = publisher()
    with sync.dashboard_publication():
        stage()
        assert db.writes == 0
    assert db.batch_sizes == [50, 50, 1]
    assert db.commits == 1 and db.closed
    assert ('SET LOCAL lock_timeout = 30000', None) in db.settings
    assert any(sql == 'SET LOCAL statement_timeout = %s' for sql, _ in db.settings)
    assert db.visible['balance_publication']['detail_count'] == 101
    assert json.loads((tmp_path / 'receipt.json').read_text()) == db.visible['balance_publication']
    assert sync._PUBLICATION.get() is None


@pytest.mark.parametrize('fail_at', [1, 51, 103, 105])
def test_failure_in_details_caches_or_receipt_rolls_back(publisher, tmp_path, fail_at):
    db = publisher(fail_at)
    with pytest.raises(RuntimeError, match='injected'):
        with sync.dashboard_publication(): stage()
    assert db.visible == {'old': 'generation'}
    assert db.commits == 0 and db.closed
    assert not (tmp_path / 'receipt.json').exists()
    assert sync._PUBLICATION.get() is None


def test_aggregation_failure_never_opens_transaction(publisher):
    db = publisher()
    with pytest.raises(ValueError):
        with sync.dashboard_publication():
            stage()
            raise ValueError('aggregation failed')
    assert db.writes == 0 and db.commits == 0


def test_scope_mismatch_refused_before_database(publisher):
    db = publisher()
    with pytest.raises(RuntimeError, match='scopes differ'):
        with sync.dashboard_publication(): stage(mismatch=True)
    assert db.writes == 0


def test_deadline_rolls_back(publisher, monkeypatch):
    db = publisher()
    ticks = iter([0, 0, 0, 901])
    monkeypatch.setattr(sync.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(TimeoutError):
        with sync.dashboard_publication(): stage()
    assert db.visible == {'old': 'generation'} and db.closed


def test_connection_bounds_connecting_but_not_long_statements(monkeypatch):
    # Publication sets its own SET LOCAL limits. Other callers run single
    # statements longer than two minutes (the resolve's donor aggregate
    # UPDATE, concurrent index builds), so the session default stays off.
    # tcp_user_timeout is not set: through the pooler it never fires while a
    # query waits on a dead backend (see #69).
    calls = []
    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def execute(self, sql, args=None): calls.append((sql, args))
    class Connection:
        def cursor(self): return Cursor()
        def commit(self): pass
        def close(self): pass
    def connect(**kwargs):
        assert kwargs['connect_timeout'] == 15
        assert 'tcp_user_timeout' not in kwargs
        return Connection()
    monkeypatch.setenv('SUPABASE_DB_URL', 'postgresql://user:password@localhost/database')
    monkeypatch.setattr('psycopg2.connect', connect)
    sync._connect(attempts=1)
    assert calls == [('SET statement_timeout = 0', None)]


def test_workflow_publication_can_be_cancelled_and_is_bounded():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/atomic-balance-evidence.yml').read_text()
    for line in workflow.splitlines():
        if 'always()' in line:
            assert '!cancelled()' in line
    assert workflow.count('timeout-minutes: 60\n        run: python scraper/process.py') == 3
