"""Complete-scope, ownership and fresh raw boundaries; no network or collector."""
from __future__ import annotations
import copy
import csv
from datetime import date, datetime, timezone
import gzip
import io
import json
import os
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scraper'))
import bounded_identity_recovery as I
import balance_snapshot as B
from search_budget import SearchBudget


START = '2006-01-01'
END = '2026-09-15'
PLANNED = '2026-09-15T10:00:00Z'


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def transaction(tid, fid, original=''):
    return {'tran_id': str(tid), 'filer id': str(fid), 'original id': original,
            'tran_date': '2026-09-01', 'filed_date': '2026-09-02', 'amount': '10',
            'sub_type': 'Cash Contribution', 'filer': 'Committee ' + str(fid),
            'contributor_payee': 'Person ' + str(tid)}


def shards(directory, rows):
    directory.mkdir(exist_ok=True)
    out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=list(rows[0]))
    writer.writeheader(); writer.writerows(rows)
    (directory / 'txn_2026.csv.gz').write_bytes(gzip.compress(out.getvalue().encode(), mtime=0))


def exact(directory, fid, *, missing=(), surplus=(), after='2026-09-15T09:00:00Z', checked='2026-09-15T09:01:00Z'):
    local = B.transaction_filer_snapshots(directory, [fid], date.fromisoformat(START), date.fromisoformat(END))[fid]
    held = len(local['held_ids'])
    return {'filer_id': fid, 'evidence_version': B.COVERAGE_EVIDENCE_VERSION,
            'filer_digest_version': B.FILER_DIGEST_SCHEMA_VERSION,
            'transaction_snapshot_id': B.transaction_snapshot_id(directory),
            'filer_transaction_digest': local['filer_transaction_digest'],
            'collection_started_at': after, 'checked_at': checked,
            'range_start': START, 'range_end': END, 'exact_search_count': 1,
            'held': held, 'orestar': held - len(surplus) + len(missing),
            'missing': list(missing), 'surplus': list(surplus), 'superseded': [],
            'complete': not missing and not surplus}


@pytest.fixture
def scope(tmp_path, monkeypatch):
    tx = tmp_path / 'transactions'; raw = tmp_path / 'raw'; raw.mkdir()
    rows = [transaction(100, 10), transaction(200, 20), transaction(300, 30)]
    shards(tx, rows)
    snapshot = B.transaction_snapshot_id(tx)
    source = B.build_source(snapshot, [{'filer_ids': ['10', '20'], 'cash_on_hand': 100,
                          'tran_count': 2, 'app_scope_transaction_digest': 'sha256:'+'a'*64,
                          'app_year_transaction_digests': {'2026': 'sha256:'+'b'*64}}],
                          created_at='2026-09-15T08:00:00Z')
    yearly = {}
    for index, fid in enumerate(('10', '20')):
        captured_at = stamp('2026-09-15T08:01:00Z') + index
        summary = {'ending_cash_balance': 50, 'scrape_ts': captured_at}
        capture = B.make_summary_capture(fid, 2026, summary, captured_at, source, snapshot)
        capture['scope_capture_id'] = 'scope-capture-10-20'
        yearly[fid] = {'comparison_capture': capture, 'years': {'2026': {
            **summary, 'scope_capture_id': capture['scope_capture_id'],
            'calculation_version': B.CALCULATION_VERSION,
            'app_year_transaction_digest': 'sha256:'+'b'*64}}}
    coverage = [exact(tx, '10', missing=['101']), exact(tx, '20')]
    path = tmp_path / 'budget.json'; monkeypatch.setenv('ORESTAR_SEARCH_BUDGET_PATH', str(path))
    budget = SearchBudget.initialize(path, 45)
    return {'source': source, 'yearly': yearly, 'coverage': coverage, 'tx': tx, 'raw': raw,
            'budget': budget, 'rows': rows, 'tmp': tmp_path}


def plan(s, ids=None):
    return I.build_plan(s['source'], s['yearly'], s['coverage'], s['tx'], ids or ['20', '10'],
                        planned_at=PLANNED, raw_dir=s['raw'])


def fetched(s, p, *, frames=None, completed_ids='10\n20\n', roots=None):
    frames = frames or {'10': [transaction(100, 10), transaction(101, 10)], '20': [transaction(200, 20)]}
    for fid, rows in frames.items():
        pd.DataFrame(rows).to_excel(s['raw'] / f'filer{fid}_ALL_2006-01-01_2026-09-15.xlsx', index=False)
    completed = s['tmp'] / 'completed.txt'; completed.write_text(completed_ids)
    progress = s['tmp'] / 'progress.json'
    roots = roots if roots is not None else [
        {'key': ['ALL', START, END, 'None', 'None', 'None', fid], 'reported': len(rows)}
        for fid, rows in frames.items()]
    # Exercise the same key serialization and durable format as the fetcher.
    import fetch
    old_progress = fetch.IDENTITY_PROGRESS_FILE
    try:
        fetch.IDENTITY_PROGRESS_FILE = progress
        payload = {}
        for item in roots:
            fid = item['key'][-1]
            key = fetch._filer_progress_key(('ALL', date.fromisoformat(START), date.fromisoformat(END), None, None, None), fid)
            payload[key] = item['reported']
        fetch._save_identity_progress(payload)
    finally:
        fetch.IDENTITY_PROGRESS_FILE = old_progress
    for path in (completed, progress): os.utime(path, (stamp(PLANNED)+60, stamp(PLANNED)+60))
    for fid in ('10', '20'):
        s['budget'].consume(fid, {'collector': 'fetch', 'date_field': 'tran', 'start': START,
                                 'end': END, 'tran_type': 'ALL', 'amt_from': None,
                                 'amt_to': None, 'payee_prefix': None})
    return I.check_fetched(p, s['tx'], s['raw'], completed, progress_path=progress,
                           checked_at='2026-09-15T10:02:00Z')


def verification(s, p, f):
    shards(s['tx'], s['rows'] + [transaction(101, 10)])
    return I.start_verification(p, f, s['tx'], started_at='2026-09-15T10:03:00Z')


def post_rows(s):
    return [exact(s['tx'], fid, after='2026-09-15T10:04:00Z', checked='2026-09-15T10:05:00Z')
            for fid in ('10', '20')]


def test_real_certifier_requires_one_full_scope_and_freezes_genuine_range(scope):
    before = copy.deepcopy((scope['source'], scope['yearly'], scope['coverage']))
    p = plan(scope)
    assert p['filer_ids'] == ['10', '20'] and p['missing_ids'] == {'10': ['101'], '20': []}
    assert p['range_end'] == END and p['missing_id_count'] == 1
    assert p['scope']['requirement']['scope_ids'] == ['10', '20']
    assert (scope['source'], scope['yearly'], scope['coverage']) == before


@pytest.mark.parametrize('ids', [['10'], ['10', '20', '30'], ['10', '10'], ['33'], ['191'], ['abc']])
def test_partial_multiple_deferred_or_malformed_scope_refused(scope, ids):
    with pytest.raises(I.IdentityRecoveryError): plan(scope, ids)


@pytest.mark.parametrize('fault', ['raw', 'pair', 'overlap', 'failed-member', 'no-missing', 'mixed-range'])
def test_selection_refuses_stale_ambiguous_or_incomplete_evidence(scope, fault):
    if fault == 'raw': scope['source']['transaction_snapshot_id'] = 'sha256:'+'f'*64
    elif fault == 'pair': scope['yearly']['20']['comparison_capture']['scope_capture_id'] = 'other'
    elif fault == 'overlap': scope['source']['scopes']['20|30'] = {'filer_ids': ['20', '30']}
    elif fault == 'failed-member': scope['coverage'][1]['complete'] = None
    elif fault == 'no-missing': scope['coverage'][0] = exact(scope['tx'], '10')
    else: scope['coverage'][1]['range_end'] = '2026-09-14'
    with pytest.raises(I.IdentityRecoveryError): plan(scope)


def test_pending_raw_exports_refuse_before_mutation(scope):
    (scope['raw'] / 'unrelated.xlsx').write_bytes(b'old')
    with pytest.raises(I.IdentityRecoveryError, match='Pending raw exports'): plan(scope)
    assert (scope['raw'] / 'unrelated.xlsx').read_bytes() == b'old'


def test_budget_must_exist_and_be_fresh(scope, monkeypatch):
    monkeypatch.delenv('ORESTAR_SEARCH_BUDGET_PATH')
    with pytest.raises(I.IdentityRecoveryError, match='shared search budget'): plan(scope)
    monkeypatch.setenv('ORESTAR_SEARCH_BUDGET_PATH', str(scope['budget'].path))
    scope['budget'].consume('10', {})
    with pytest.raises(I.IdentityRecoveryError, match='precede every search'): plan(scope)


def test_complete_exports_prove_all_members_and_preserve_manifest(scope):
    p = plan(scope); f = fetched(scope, p)
    assert f['state'] == 'complete_fetch_verified_merge_pending'
    assert len(f['exports']) == 2 and f['completed_root_counts']['10']['reported'] == 2
    assert f['budget']['state']['used'] == 2
    assert len(list(scope['raw'].glob('*.xlsx'))) == 2


@pytest.mark.parametrize('fault', ['partial-marker', 'root-missing', 'root-short', 'unrelated', 'wrong-owner', 'cross-original', 'bad-date', 'bad-amount'])
def test_premerge_gate_refuses_partial_wrong_owner_or_invalid_exports(scope, fault):
    p = plan(scope)
    frames = {'10': [transaction(100, 10), transaction(101, 10)], '20': [transaction(200, 20)]}
    kwargs = {'frames': frames}
    if fault == 'partial-marker': kwargs['completed_ids'] = '10\n'
    elif fault == 'root-missing': kwargs['roots'] = []
    elif fault == 'root-short': kwargs['roots'] = [{'key': ['ALL', START, END, 'None', 'None', 'None', fid], 'reported': 1} for fid in frames]
    elif fault == 'unrelated': frames['30'] = [transaction(300, 30)]
    elif fault == 'wrong-owner': frames['10'][1]['tran_id'] = '300'
    elif fault == 'cross-original': frames['10'][1]['original id'] = '300'
    elif fault == 'bad-date': frames['10'][1]['tran_date'] = '2026-09-16'
    else: frames['10'][1]['amount'] = '$10.00'
    before = B.transaction_snapshot_id(scope['tx'])
    with pytest.raises(I.IdentityRecoveryError): fetched(scope, p, **kwargs)
    assert B.transaction_snapshot_id(scope['tx']) == before


def test_postmerge_fresh_exact_can_retain_surplus_without_cash_claim(scope):
    yearly_before = copy.deepcopy(scope['yearly'])
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f)
    rows = post_rows(scope)
    rows[0] = exact(scope['tx'], '10', surplus=['101'], after='2026-09-15T10:04:00Z', checked='2026-09-15T10:05:00Z')
    for fid in ('10', '20'): scope['budget'].consume(fid, {'phase': 'exact'})
    report = I.verify_recovery(p, start, rows, scope['tx'], yearly=scope['yearly'], checked_at='2026-09-15T10:06:00Z')
    assert report['publication_safe'] is True
    assert report['verified'] is True and report['state'] == 'identity_verified_balance_pending'
    assert report['terminal_cash_accepted'] is False and report['surplus_ids']['10'] == ['101']
    assert scope['yearly'] == yearly_before
    assert start['transaction_snapshot_id'] != p['transaction_snapshot_id']


@pytest.mark.parametrize('fault', ['old-exact', 'missing', 'no-member', 'wrong-snapshot', 'wrong-held', 'telemetry', 'outside-budget'])
def test_postmerge_verification_refuses_incomplete_or_stale_proof(scope, fault):
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f); rows = post_rows(scope)
    for fid in ('10', '20'): scope['budget'].consume(fid, {'phase': 'exact'})
    if fault == 'old-exact': rows = scope['coverage']
    elif fault == 'missing': rows[0] = exact(scope['tx'], '10', missing=['102'], after='2026-09-15T10:04:00Z', checked='2026-09-15T10:05:00Z')
    elif fault == 'no-member':
        rows.pop()
        with pytest.raises(I.IdentityRecoveryError, match='lost or structurally'):
            I.verify_recovery(p, start, rows, scope['tx'], yearly=scope['yearly'])
        return
    elif fault == 'wrong-snapshot': rows[0]['transaction_snapshot_id'] = p['transaction_snapshot_id']
    elif fault == 'wrong-held': rows[0].update(held=3, orestar=3)
    elif fault == 'telemetry': rows[0]['exact_search_count'] = 2
    else:
        scope['budget'].consume('30', {})
        with pytest.raises(I.IdentityRecoveryError, match='unplanned physical'): I.verify_recovery(p, start, rows, scope['tx'], yearly=scope['yearly'])
        return
    report = I.verify_recovery(p, start, rows, scope['tx'], yearly=scope['yearly'], checked_at='2026-09-15T10:06:00Z')
    assert report['verified'] is False and report['failures'] and report['terminal_cash_accepted'] is False


def test_budget_reset_between_fetch_and_exact_refused(scope):
    p = plan(scope); f = fetched(scope, p)
    scope['budget'].path.unlink(); SearchBudget.initialize(scope['budget'].path, 45)
    with pytest.raises(I.IdentityRecoveryError, match='reset'): verification(scope, p, f)


def test_current_raw_drift_after_verification_start_refused(scope):
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f)
    shards(scope['tx'], scope['rows'] + [transaction(101, 10), transaction(102, 10)])
    with pytest.raises(I.A.AtomicEvidenceError, match='snapshot changed'):
        I.verify_recovery(p, start, post_rows(scope), scope['tx'], yearly=scope['yearly'])


def test_verify_cli_archives_failure_even_on_guard_exception(scope, monkeypatch):
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f)
    plan_path = scope['tmp'] / 'plan.json'; plan_path.write_text(json.dumps(p))
    start_path = scope['tmp'] / 'start.json'; start_path.write_text(json.dumps(start))
    output = scope['tmp'] / 'report.json'
    monkeypatch.setattr(I, 'TRANSACTIONS', scope['tx'])
    monkeypatch.setattr(I, 'COVERAGE', scope['tmp'] / 'missing-coverage.json')
    result = I.main(['verify', '--plan', str(plan_path), '--verification-start', str(start_path), '--output', str(output)])
    assert result == 1 and json.loads(output.read_text())['verified'] is False


@pytest.mark.parametrize('column', ['filer', 'contributor_payee', 'sub_type'])
def test_real_export_loader_refuses_missing_source_classification(scope, column):
    p = plan(scope)
    frames = {'10': [transaction(100, 10), transaction(101, 10)], '20': [transaction(200, 20)]}
    for rows in frames.values():
        for row in rows: row.pop(column)
    with pytest.raises(I.IdentityRecoveryError): fetched(scope, p, frames=frames)


def test_changed_yearly_cache_blocks_publication(scope):
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f)
    yearly = copy.deepcopy(scope['yearly'])
    yearly['10']['comparison_capture']['captured_at'] += 1
    with pytest.raises(I.IdentityRecoveryError, match='Yearly summaries changed'):
        I.verify_recovery(p, start, post_rows(scope), scope['tx'], yearly=yearly)


def test_stale_usable_evidence_is_publishable_but_not_verified(scope):
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f)
    report = I.verify_recovery(p, start, scope['coverage'], scope['tx'], yearly=scope['yearly'],
                              checked_at='2026-09-15T10:06:00Z')
    assert report['publication_safe'] is True and report['verified'] is False


def test_one_member_missing_payee_header_cannot_hide_behind_other_export(scope):
    p = plan(scope)
    frames = {'10': [transaction(100, 10), transaction(101, 10)], '20': [transaction(200, 20)]}
    for row in frames['10']: row.pop('contributor_payee')
    with pytest.raises(I.IdentityRecoveryError, match='individual export'):
        fetched(scope, p, frames=frames)


def test_fetched_zero_row_member_still_requires_actual_root_search(scope):
    p = plan(scope)
    f = fetched(scope, p)
    state = scope['budget'].state()
    state['submissions'] = [item for item in state['submissions'] if item['filer_id'] != '20']
    state['used'] = len(state['submissions'])
    scope['budget'].path.write_text(json.dumps(state))
    with pytest.raises(I.IdentityRecoveryError, match='fresh full-range fetch submission'):
        I.check_fetched(p, scope['tx'], scope['raw'], scope['tmp'] / 'completed.txt',
                        progress_path=scope['tmp'] / 'progress.json', checked_at='2026-09-15T10:02:00Z')


def test_verification_cli_preserves_publishable_missing_outcome(scope, monkeypatch):
    p = plan(scope); f = fetched(scope, p); start = verification(scope, p, f)
    for fid in ('10', '20'): scope['budget'].consume(fid, {'phase': 'exact'})
    rows = post_rows(scope)
    rows[0] = exact(scope['tx'], '10', missing=['102'], after='2026-09-15T10:04:00Z', checked='2026-09-15T10:05:00Z')
    paths = {key: scope['tmp'] / (key + '.json') for key in ('plan', 'start', 'coverage', 'yearly', 'report')}
    for key, value in [('plan', p), ('start', start), ('coverage', rows), ('yearly', scope['yearly'])]:
        paths[key].write_text(json.dumps(value))
    monkeypatch.setattr(I, 'TRANSACTIONS', scope['tx'])
    monkeypatch.setattr(I, 'COVERAGE', paths['coverage']); monkeypatch.setattr(I, 'YEARLY', paths['yearly'])
    monkeypatch.setattr(I.B, 'utc_timestamp', lambda: '2026-09-15T10:06:00Z')
    result = I.main(['verify', '--plan', str(paths['plan']), '--verification-start', str(paths['start']), '--output', str(paths['report'])])
    report = json.loads(paths['report'].read_text())
    assert result == 1 and report['verified'] is False and report['publication_safe'] is True
    assert report['missing_ids']['10'] == ['102'] and report['terminal_cash_accepted'] is False
