"""One complete-scope identity recovery inside the admitted atomic workflow.

These boundaries do not fetch, merge, synchronize, publish, or invent balance
captures. The workflow owns those commands and one shared 45-search budget.
Post-merge success proves fresh identity completeness only; cash needs a later
newly captured atomic evidence window against the resulting transaction data.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).parent))
import atomic_balance_evidence as A
import balance_snapshot as B
from exact_coverage_evidence import certify_exact_scope_rows
from search_budget import SearchBudget, SearchBudgetError
import supabase_sync

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
TRANSACTIONS = DATA / 'transactions'
RAW = DATA / '_raw'
COMPLETED = DATA / 'completed_backfills.txt'
YEARLY = DATA / 'orestar_yearly_summaries.json'
COVERAGE = DATA / 'coverage_diff.json'
PROGRESS = DATA / 'identity_remediation_windows.json'


class IdentityRecoveryError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise IdentityRecoveryError(message)


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    A._write_json(Path(path), value)


def numeric_ids(values):
    require(isinstance(values, list) and values and
            all(isinstance(fid, str) and re.fullmatch(r'[1-9][0-9]*', fid) for fid in values),
            'Explicit positive physical filer IDs are required')
    require(len(set(values)) == len(values), 'Duplicate requested filer IDs')
    ids = sorted(values)
    require(not set(ids) & {'33', '191'}, 'Deferred filer 33/191 cannot enter identity backfill')
    return ids


def budget_state(previous=None):
    budget = SearchBudget.from_environment()
    require(budget is not None, 'Identity recovery requires the admitted shared search budget')
    state = budget.state()
    require(state['limit'] == 45, 'Identity recovery requires the shared 45-search limit')
    binding = {'path': str(budget.path), 'state': state}
    if previous is not None:
        old = previous['state']
        require(previous['path'] == binding['path'] and old['limit'] == state['limit']
                and state['used'] >= old['used']
                and state['submissions'][:old['used']] == old['submissions'],
                'Shared search budget was reset, changed, or moved')
    return binding


def scoped_budget(plan, previous=None):
    binding = budget_state(previous or plan['budget'])
    require(all(row['filer_id'] in plan['filer_ids'] for row in binding['state']['submissions']),
            'Search budget contains an unplanned physical filer')
    return binding


def raw_manifest(raw_dir):
    result = []
    for path in sorted(Path(raw_dir).glob('*.xls*')):
        require(path.is_file() and not path.is_symlink(), 'Unsafe raw export path')
        raw = path.read_bytes()
        result.append({'name': path.name, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)})
    return result


def validate_plan(plan):
    require(isinstance(plan, dict) and plan.get('version') == 1
            and plan.get('kind') == 'bounded_identity_recovery', 'Malformed identity recovery plan')
    ids = numeric_ids(plan.get('filer_ids'))
    require(ids == plan['filer_ids'] and plan.get('selected_scope_count') == 1,
            'Identity recovery requires exactly one complete canonical scope')
    require(plan.get('range_start') == '2006-01-01', 'Identity range must cover full history')
    end = date.fromisoformat(plan['range_end'])
    require(end >= date(2006, 1, 1) and end <= datetime.fromtimestamp(A._iso_epoch(plan['planned_at']), timezone.utc).date(),
            'Invalid frozen evidence range')
    require(A.SNAPSHOT_RE.fullmatch(str(plan.get('transaction_snapshot_id', ''))), 'Missing frozen raw snapshot')
    require(plan.get('missing_id_count', 0) > 0, 'Plan has no certified missing IDs')
    return ids


def build_plan(source, yearly, coverage, transaction_dir, requested_ids, *, planned_at=None, raw_dir=RAW):
    ids = numeric_ids(requested_ids)
    planned_at = planned_at or B.utc_timestamp()
    planned_epoch = A._iso_epoch(planned_at)
    binding = budget_state()
    require(binding['state']['used'] == 0, 'Plan must precede every search in this attempt')
    require(not raw_manifest(raw_dir), 'Pending raw exports exist; refuse unrelated or resumed mutation')
    snapshot = A._current_snapshot(transaction_dir)
    require(isinstance(source, dict) and source.get('version') == B.FORMAT_VERSION
            and source.get('calculation_version') == B.CALCULATION_VERSION
            and source.get('transaction_snapshot_id') == snapshot,
            'Current aggregate source does not match hydrated raw data')
    scope = A._explicit_recovery_record(ids, source, yearly, snapshot, planned_epoch)
    require(scope is not None, 'Requested IDs must exactly equal one unambiguous current paired canonical scope')
    entries = A._diff_entries(coverage)
    requirements = {fid: scope['requirement'] for fid in ids}
    certified, blocked, error = certify_exact_scope_rows(list(entries.values()), requirements, ids, transaction_dir)
    require(not error and not blocked and set(certified) == set(ids),
            'Every original scope member needs current anchored exact identity evidence')
    bounds = {(row['range_start'], row['range_end']) for row in certified.values()}
    require(len(bounds) == 1 and next(iter(bounds))[0] == '2006-01-01',
            'Exact scope members do not share one full-history range')
    start, end = next(iter(bounds))
    missing = {fid: list(certified[fid]['missing']) for fid in ids}
    require(sum(map(len, missing.values())) > 0, 'Current certified scope has no missing IDs to recover')
    require(A._current_snapshot(transaction_dir) == snapshot, 'Raw data changed during selection')
    value = {'version': 1, 'kind': 'bounded_identity_recovery', 'planned_at': planned_at,
             'selected_scope_count': 1, 'filer_ids': ids, 'scope': scope,
             'transaction_snapshot_id': snapshot, 'range_start': start, 'range_end': end,
             'missing_ids': missing, 'missing_id_count': sum(map(len, missing.values())),
             'certified_selection': {fid: certified[fid] for fid in ids},
             'source_digest': digest(source), 'yearly_digest': digest(yearly),
             'coverage_digest': digest(coverage), 'budget': binding,
             'raw_exports_at_plan': [], 'balance_proof_status': 'new_capture_required_after_raw_mutation'}
    validate_plan(value)
    return value


def validate_export_headers(raw_dir, manifest):
    """Check each original header before concat can hide a missing column."""
    import pandas as pd
    from process import COL_MAP, _detect_engine
    for item in manifest:
        path = Path(raw_dir) / item['name']
        engine = _detect_engine(path)
        require(engine is not None, 'Raw export is an HTML/error response')
        header = pd.read_excel(path, engine=engine, nrows=0).columns
        normalized = [COL_MAP.get(str(column).strip().lower(), str(column).strip().lower()) for column in header]
        require(len(set(normalized)) == len(normalized), 'Duplicate normalized header in export: ' + path.name)
        fields = set(normalized)
        required = {'tran_id', 'tran_date', 'filed_date', 'amount', 'filer', 'contributor_payee', 'sub_type'}
        require(required.issubset(fields) and bool(fields & {'filer id', 'filer_id'})
                and bool(fields & {'original id', 'original_id'}),
                'Required source header missing from individual export: ' + path.name)
        require(len(fields & {'filer id', 'filer_id'}) == 1 and len(fields & {'original id', 'original_id'}) == 1,
                'Ambiguous ownership/original-ID aliases in export: ' + path.name)


def validate_export_frame(frame, plan, expected_files):
    """Check the exact rows process.load_excel_files will pass into the merger."""
    import pandas as pd
    require(not frame.empty, 'Completed recovery produced no mergeable exports')
    require(len(frame.columns) == len(set(frame.columns)), 'Duplicate normalized export columns')
    owner = 'filer_id' if 'filer_id' in frame.columns else 'filer id'
    original = 'original_id' if 'original_id' in frame.columns else 'original id'
    require(all(column in frame.columns for column in
                ('tran_id', owner, original, 'tran_date', 'filed_date', 'amount', '_source_file',
                 'filer', 'contributor_payee', 'sub_type', 'tran_type')),
            'Export lacks transaction, owner, original-ID, amount, or date columns')
    require(set(frame['_source_file']) == set(expected_files),
            'At least one export was skipped or failed to parse; refuse partial merge')
    start, end = date.fromisoformat(plan['range_start']), date.fromisoformat(plan['range_end'])
    claims = {}
    for row in frame.to_dict('records'):
        fid, tid = B._normalized_identifier(row[owner]), B._normalized_identifier(row['tran_id'])
        require(fid in plan['filer_ids'] and re.fullmatch(r'[1-9][0-9]*', tid),
                'Export contains an unplanned owner or malformed transaction ID')
        match = re.fullmatch(r'(?:verified_)?filer([1-9][0-9]*)_.+\.xls[x]?', str(row['_source_file']))
        require(match is not None and match[1] == fid, 'Export filename disagrees with row ownership')
        tran_date = B._transaction_date(row['tran_date'], Path(row['_source_file']), 0)
        require(tran_date is not None and start <= tran_date <= end, 'Export transaction date is outside frozen evidence range')
        filed = pd.to_datetime(row['filed_date'], format='mixed', errors='coerce')
        require(not pd.isna(filed), 'Export filed date would disappear into an invalid shard')
        try:
            amount = float(row['amount'])
        except (ValueError, TypeError, OverflowError) as exc:
            raise IdentityRecoveryError('Export amount would be silently coerced by merge') from exc
        require(math.isfinite(amount), 'Export amount is not finite')
        require(isinstance(row['filer'], str) and bool(row['filer'].strip())
                and isinstance(row['sub_type'], str) and bool(row['sub_type'].strip())
                and row['tran_type'] in {'C', 'E', 'O', 'OA', 'OD', 'OR'},
                'Export lacks a committee name or recognized cash/transaction classification')
        original_id = B._normalized_identifier(row[original])
        require(not original_id or re.fullmatch(r'[1-9][0-9]*', original_id), 'Malformed original-ID linkage')
        for identity in (tid, original_id):
            if identity:
                require(identity not in claims or claims[identity] == fid, 'Exports disagree on transaction/original-ID ownership')
                claims[identity] = fid
    return claims


def validate_existing_ownership(claims, transaction_dir):
    """A fetched ID or amendment cannot overwrite/delete another physical owner."""
    for path in sorted(Path(transaction_dir).glob('txn_*.csv.gz')):
        with gzip.open(path, 'rt', encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            owner = 'filer_id' if 'filer_id' in fields else 'filer id'
            require('tran_id' in fields and owner in fields, 'Raw shard lacks ownership schema')
            for row in reader:
                tid = B._normalized_identifier(row['tran_id'])
                if tid in claims:
                    require(B._normalized_identifier(row[owner]) == claims[tid],
                            'Fetched transaction/original ID belongs to a different existing physical filer: ' + tid)


def check_fetched(plan, transaction_dir, raw_dir, completed_path, *, checked_at=None, frame_loader=None, progress_path=PROGRESS):
    ids = validate_plan(plan)
    require(A._current_snapshot(transaction_dir) == plan['transaction_snapshot_id'], 'Raw data changed before guarded merge')
    binding = scoped_budget(plan)
    require(binding['state']['used'] > 0, 'Fetch has no shared-budget search submissions')
    completed = Path(completed_path)
    require(completed.is_file() and completed.stat().st_mtime >= A._iso_epoch(plan['planned_at']),
            'Missing or stale completion marker; fetch did not finish this run')
    completed_ids = completed.read_text().split()
    require(len(completed_ids) == len(ids) and sorted(completed_ids) == ids,
            'Fetch did not complete every original physical scope member')
    manifest = raw_manifest(raw_dir)
    require(manifest, 'Fetch produced no raw exports')
    if frame_loader is None:
        from process import load_excel_files
        frame_loader = load_excel_files
    validate_export_headers(raw_dir, manifest)
    frame = frame_loader(Path(raw_dir))
    claims = validate_export_frame(frame, plan, [row['name'] for row in manifest])
    progress_path = Path(progress_path)
    require(progress_path.is_file() and progress_path.stat().st_mtime >= A._iso_epoch(plan['planned_at']),
            'Missing or stale forced identity root progress')
    progress = read(progress_path)
    require(isinstance(progress, list), 'Malformed forced identity progress')
    roots = {}
    owner = 'filer_id' if 'filer_id' in frame.columns else 'filer id'
    for fid in ids:
        root_window = {'collector': 'fetch', 'date_field': 'tran', 'start': plan['range_start'],
                       'end': plan['range_end'], 'tran_type': 'ALL', 'amt_from': None,
                       'amt_to': None, 'payee_prefix': None}
        require(any(item['filer_id'] == fid and item['window'] == root_window
                    for item in binding['state']['submissions']),
                'Original root lacks a fresh full-range fetch submission: ' + fid)
        key = ['ALL', plan['range_start'], plan['range_end'], 'None', 'None', 'None', fid]
        matches = [row for row in progress if isinstance(row, dict) and row.get('key') == key]
        require(len(matches) == 1 and type(matches[0].get('reported')) is int and matches[0]['reported'] >= 0,
                'Missing or ambiguous completed original full-range root: ' + fid)
        exported_ids = {B._normalized_identifier(row['tran_id']) for row in frame.to_dict('records')
                        if B._normalized_identifier(row[owner]) == fid}
        require(len(exported_ids) == matches[0]['reported'],
                'Unique exported transaction IDs do not reproduce completed root count: ' + fid)
        roots[fid] = matches[0]
    validate_existing_ownership(claims, transaction_dir)
    require(raw_manifest(raw_dir) == manifest and A._current_snapshot(transaction_dir) == plan['transaction_snapshot_id'],
            'Exports or raw data changed during pre-merge verification')
    return {'version': 1, 'state': 'complete_fetch_verified_merge_pending', 'plan_digest': digest(plan),
            'checked_at': checked_at or B.utc_timestamp(), 'filer_ids': ids, 'exports': manifest,
            'completed_root_counts': roots,
            'budget': binding, 'transaction_snapshot_id': plan['transaction_snapshot_id'],
            'merge_note': 'Standard process.py --merge-only synchronizes Postgres before exact verification. A subsequent failure is incomplete recovery, not identity or cash closure.'}


def start_verification(plan, fetched, transaction_dir, *, started_at=None):
    ids = validate_plan(plan)
    require(fetched.get('state') == 'complete_fetch_verified_merge_pending'
            and fetched.get('plan_digest') == digest(plan) and fetched.get('filer_ids') == ids,
            'Missing complete original fetch proof')
    binding = scoped_budget(plan, fetched['budget'])
    require(binding['state'] == fetched['budget']['state'], 'Unexpected searches between guarded fetch and post-merge verification')
    snapshot = A._current_snapshot(transaction_dir)
    started_at = started_at or B.utc_timestamp()
    require(A._iso_epoch(started_at) > A._iso_epoch(fetched['checked_at']), 'Verification must begin after guarded fetch/merge')
    return {'version': 1, 'state': 'post_merge_exact_verification_pending', 'plan_digest': digest(plan),
            'fetched_digest': digest(fetched), 'started_at': started_at, 'filer_ids': ids,
            'transaction_snapshot_id': snapshot, 'previous_transaction_snapshot_id': plan['transaction_snapshot_id'],
            'raw_changed': snapshot != plan['transaction_snapshot_id'], 'budget': binding,
            'balance_proof_status': 'not_assessed_old_capture_not_reused'}


def verify_recovery(plan, started, coverage, transaction_dir, *, yearly, checked_at=None):
    ids = validate_plan(plan)
    require(started.get('state') == 'post_merge_exact_verification_pending'
            and started.get('plan_digest') == digest(plan) and started.get('filer_ids') == ids,
            'Verification does not bind the complete original plan')
    require(digest(yearly) == plan['yearly_digest'], 'Yearly summaries changed; old captures must remain untouched')
    snapshot = A._current_snapshot(transaction_dir, started['transaction_snapshot_id'])
    binding = scoped_budget(plan, started['budget'])
    submissions = binding['state']['submissions'][started['budget']['state']['used']:]
    counts = {fid: sum(item['filer_id'] == fid for item in submissions) for fid in ids}
    entries = A._diff_entries(coverage)
    require(len(entries) == len(coverage) and all(re.fullmatch(r'[1-9][0-9]*', fid) for fid in entries),
            'Coverage has malformed physical owner records')
    require(all(row.get('complete') is None or type(row.get('complete')) is bool for row in entries.values()),
            'Coverage contains malformed completion states')
    require(all(isinstance(row.get('usable_history', []), list)
                and all(isinstance(item, dict) for item in row.get('usable_history', [])) for row in entries.values()),
            'Coverage contains malformed usable history')
    require(all(fid in entries and B.exact_coverage_result_shape_is_valid(entries[fid]) for fid in ids),
            'Original scope usable rows were lost or structurally corrupted')
    local = B.transaction_filer_snapshots(transaction_dir, ids, date.fromisoformat(plan['range_start']), date.fromisoformat(plan['range_end']))
    rows, failures = {}, []
    checked_at = checked_at or B.utc_timestamp()
    for fid in ids:
        row = entries.get(fid, {})
        current = B.exact_coverage_result_shape_is_valid(row) and B.evidence_is_current(
            row, started['started_at'], require_precise=True, require_collection_started=True, strictly_after=True,
            transaction_snapshot_id=snapshot, filer_transaction_digest=local[fid]['filer_transaction_digest'],
            filer_digest_version=B.FILER_DIGEST_SCHEMA_VERSION,
            range_start=plan['range_start'], range_end=plan['range_end'])
        if not current or A._iso_epoch(row['checked_at']) > A._iso_epoch(checked_at):
            failures.append(fid + ': no fresh usable exact evidence for the post-merge raw/range')
            continue
        if type(row.get('exact_search_count')) is not int or row['exact_search_count'] != counts[fid] or counts[fid] <= 0:
            failures.append(fid + ': measured exact searches do not match shared budget')
        held, superseded = local[fid]['held_ids'], local[fid]['superseded_ids']
        if (row['held'] != len(held) or not set(row['surplus']).issubset(held)
                or not set(row['missing']).isdisjoint(held | superseded)
                or not set(row['superseded']).issubset(superseded)
                or not set(row['superseded']).isdisjoint(held)):
            failures.append(fid + ': exact identity sets disagree with current raw rows')
        if row['missing']:
            failures.append(fid + ': missing transaction IDs remain')
        rows[fid] = row
    require(A._current_snapshot(transaction_dir) == snapshot, 'Raw data changed during identity verification')
    return {'version': 1, 'state': 'identity_verified_balance_pending' if not failures else 'identity_verification_failed',
            'checked_at': checked_at, 'plan_digest': digest(plan), 'verification_start_digest': digest(started),
            'filer_ids': ids, 'transaction_snapshot_id': snapshot, 'range_start': plan['range_start'], 'range_end': plan['range_end'],
            'verified': not failures, 'publication_safe': True, 'failures': failures, 'exact_rows': rows,
            'missing_ids': {fid: row['missing'] for fid, row in rows.items()},
            'surplus_ids': {fid: row['surplus'] for fid, row in rows.items()},
            'budget': binding, 'exact_searches_by_filer': counts,
            'balance_proof_status': 'not_assessed_requires_fresh_capture_against_regenerated_source',
            'terminal_cash_accepted': False}


def fetched_path(plan_path):
    return Path(str(plan_path) + '.fetched.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    plan_parser = sub.add_parser('plan')
    plan_parser.add_argument('--filer-ids', nargs='+', required=True)
    plan_parser.add_argument('--output', type=Path, required=True)
    for name in ('fetched', 'verification-start', 'verify'):
        command = sub.add_parser(name)
        command.add_argument('--plan', type=Path, required=True)
        if name != 'fetched':
            command.add_argument('--output', type=Path, required=True)
        if name == 'verify':
            command.add_argument('--verification-start', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'plan':
            value = build_plan(supabase_sync.require_dashboard_cache('balance_snapshot_source'),
                               read(YEARLY), read(COVERAGE), TRANSACTIONS, args.filer_ids)
            require(not args.output.exists(), 'Identity plan already exists; refuse in-job replay')
        else:
            plan = read(args.plan)
            if args.command == 'fetched':
                args.output = fetched_path(args.plan)
                require(not args.output.exists(), 'Fetch receipt already exists; refuse in-job replay')
                value = check_fetched(plan, TRANSACTIONS, RAW, COMPLETED)
            elif args.command == 'verification-start':
                require(not args.output.exists(), 'Verification start already exists; do not reset freshness')
                value = start_verification(plan, read(fetched_path(args.plan)), TRANSACTIONS)
            else:
                value = verify_recovery(plan, read(args.verification_start), read(COVERAGE), TRANSACTIONS, yearly=read(YEARLY))
        write(args.output, value)
        print('IDENTITY_RECOVERY ' + json.dumps({'stage': args.command, 'filer_ids': value['filer_ids'],
                                                'state': value.get('state', 'planned'),
                                                'range_end': value.get('range_end')}))
        return 1 if value.get('verified') is False else 0
    except (IdentityRecoveryError, A.AtomicEvidenceError, SearchBudgetError, OSError, ValueError, KeyError, EOFError, csv.Error) as exc:
        if args.command == 'verify':
            write(args.output, {'version': 1, 'state': 'identity_verification_failed',
                                'verified': False, 'publication_safe': False, 'failures': [str(exc)], 'checked_at': B.utc_timestamp(),
                                'terminal_cash_accepted': False,
                                'balance_proof_status': 'not_assessed_requires_fresh_capture_against_regenerated_source'})
        print('Identity recovery refused: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
