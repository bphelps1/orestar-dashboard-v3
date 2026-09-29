"""Execute bounded recovery workflow shells offline and exercise their real gates.

Collector and publication commands are spies. Their actual argument lists,
workflow ordering, shared budget and GitHub failure/continue-on-error semantics
are exercised without network, collection, or production writes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest

from test_atomic_single_pass_workflow import shell, execute, script

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/atomic-balance-evidence.yml'


def blocks():
    chunks = re.split(r'^      - name: ', WORKFLOW.read_text(), flags=re.M)[1:]
    return [(chunk.split('\n', 1)[0], '      - name: ' + chunk) for chunk in chunks]


def block(name):
    return dict(blocks())[name]


def scalar(text, key):
    match = re.search(r'^        ' + re.escape(key) + r': (.*)$', text, re.M)
    if not match:
        return ''
    value = match[1]
    if value in ('>-', '|'):
        lines = []
        for line in text[match.end()+1:].splitlines():
            if not line.startswith('          '):
                break
            lines.append(line.strip())
        return ' '.join(lines)
    return value


def run_body(text):
    match = re.search(r'^        run: (.*)$', text, re.M)
    assert match
    if match[1] == '|':
        return textwrap.dedent(text[match.end()+1:]).strip() + '\n'
    return match[1] + '\n'


def value(expression, inputs, states):
    expression = expression.strip()
    if expression.startswith('inputs.'):
        return inputs.get(expression[7:], '')
    match = re.fullmatch(r'steps\.([a-z_]+)\.(outcome|outputs\.([a-z_]+))', expression)
    if match:
        state = states.get(match[1], {})
        return state.get('outcome', '') if match[2] == 'outcome' else state.get('outputs', {}).get(match[3], '')
    raise AssertionError('Unexpected expression: ' + expression)


def enabled(text, inputs, states, *, failed=False, cancelled=False):
    condition = scalar(text, 'if') or 'success()'
    condition = condition.removeprefix('${{').removesuffix('}}').strip()
    # GitHub applies success() implicitly unless a status function is present.
    if not re.search(r'\b(?:always|success|failure|cancelled)\(', condition) and (failed or cancelled):
        return False
    condition = re.sub(r'(?:inputs\.[a-z_]+|steps\.[a-z_]+\.(?:outcome|outputs\.[a-z_]+))',
                       lambda m: repr(value(m[0], inputs, states)), condition)
    condition = condition.replace('&&', ' and ').replace('||', ' or ')
    condition = re.sub(r'!(?!=)', ' not ', condition)
    return bool(eval(condition.strip(), {'__builtins__': {}}, {
        'success': lambda: not failed and not cancelled,
        'failure': lambda: failed, 'always': lambda: True,
        'cancelled': lambda: cancelled,
    }))


@pytest.mark.parametrize('mode,passes,ids,scopes', [
    ('evidence', '3', '', '12'), ('identity_backfill', '3', '10 20', '1'),
])
def test_both_modes_use_actual_existing_effort_admission(shell, mode, passes, ids, scopes):
    result = execute(shell, 'Admit this attempt against the persistent effort limit',
                     RECOVERY_MODE=mode, MAX_PASSES=passes, REQUESTED_FILER_IDS=ids,
                     REQUESTED_MAX_SCOPES=scopes)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['admitted'] is True and receipt['attempts_used'] == 1
    assert receipt['effort_id'] == 'balance-recovery-20260922'
    assert receipt['max_attempts'] == 8 and receipt['max_searches'] == 45
    assert receipt['excluded_filer_ids'] == ['33', '191']
    calls = [json.loads(line) for line in shell['calls'].read_text().splitlines()]
    assert all(row[:3] == ['api', '--method', 'GET'] for row in calls)
    assert any('atomic-balance-evidence.yml' in row[3] for row in calls)


@pytest.mark.parametrize('mode,passes,ids,scopes', [
    ('unknown', '3', '10', '1'), ('identity_backfill', '1', '191', '1'),
    ('identity_backfill', '3', '', '1'), ('identity_backfill', '3', '10', '12'),
    ('identity_backfill', '3', '10 --policy replacement', '1'),
    ('identity_backfill', '3', '10\n20', '1'),
])
def test_invalid_identity_mode_input_fails_before_api_or_budget(shell, mode, passes, ids, scopes):
    result = execute(shell, 'Admit this attempt against the persistent effort limit',
                     RECOVERY_MODE=mode, MAX_PASSES=passes, REQUESTED_FILER_IDS=ids,
                     REQUESTED_MAX_SCOPES=scopes)
    assert result.returncode != 0
    assert not shell['calls'].exists() and not shell['output'].exists()


def test_identity_does_not_get_a_new_budget_when_chain_index_resets(shell):
    # All statuses and reruns remain chargeable under the same title. A reset
    # chain index cannot make an over-budget current run admissible.
    transport = (shell['bin'] / 'gh').read_text()
    transport = transport.replace('"run_attempt": 1', '"run_attempt": 13')
    (shell['bin'] / 'gh').write_text(transport)
    result = execute(shell, 'Admit this attempt against the persistent effort limit',
                     RECOVERY_MODE='identity_backfill', MAX_PASSES='3', REQUESTED_FILER_IDS='10 20',
                     REQUESTED_MAX_SCOPES='1', GITHUB_RUN_ATTEMPT='13', CHAIN_INDEX='1')
    assert result.returncode != 0
    assert not shell['output'].exists()
    assert shell['calls'].exists()  # Refused by actual history accounting.


SPY = r'''
import json, os, pathlib, sys
from search_budget import SearchBudget, SearchBudgetError
args = sys.argv[1:]
root = pathlib.Path(os.environ['CASE_ROOT'])
fault = os.environ.get('FAULT', '')
if args[:2] == ['scraper/bounded_identity_recovery.py', 'plan']:
    event = 'plan'
    out = pathlib.Path(args[args.index('--output')+1])
    out.write_text(json.dumps({'filer_ids':['10','20'], 'range_end':'2026-09-15'}))
elif args[:2] == ['scraper/bounded_identity_recovery.py', 'fetched']: event = 'fetched'
elif args[:2] == ['scraper/bounded_identity_recovery.py', 'verification-start']: event = 'start'
elif args[:2] == ['scraper/bounded_identity_recovery.py', 'verify']:
    event = 'verify'
    valid = fault not in {'diff', 'budget', 'missing', 'unsafe'}
    pathlib.Path(args[args.index('--output')+1]).write_text(json.dumps({
        'verified':valid, 'publication_safe':fault != 'unsafe', 'terminal_cash_accepted':False}))
elif args[0] == 'scraper/fetch.py': event = 'fetch'
elif args[0] == 'scraper/diff_coverage.py': event = 'diff'
elif args == ['scraper/process.py', '--merge-only']: event = 'merge'
elif args == ['scraper/process.py']: event = 'aggregate'
elif args == ['scraper/refresh_donor_aggregates.py']: event = 'donors'
elif args == ['scripts/pipeline_state.py', 'push', 'transactions', 'auxiliary']: event = 'raw_publish'
elif args == ['scripts/pipeline_state.py', 'push', 'auxiliary']: event = 'publish'
elif args == ['-']:
    source = sys.stdin.read()
    event = 'projection_start' if 'BALANCE_PROJECTION_INPUT' in source else 'projection_verify'
else: raise AssertionError('Unexpected or unbounded command: ' + repr(args))
budget = SearchBudget.from_environment()
assert budget is not None
with (root/'events').open('a') as out:
    out.write(json.dumps({'event':event, 'argv':args, 'budget':str(budget.path), 'used':budget.used})+'\n')
if event == 'fetch':
    count = 44 if fault == 'budget' else 2
    for index in range(count): budget.consume(['10','20'][index % 2], {'collector':'fetch'})
if event == 'diff':
    try:
        for fid in ['10','20']: budget.consume(fid, {'collector':'exact'})
    except SearchBudgetError: sys.exit(1)
if event == fault or (event == 'verify' and fault in {'diff','budget','missing','unsafe'}): sys.exit(1)
'''


@pytest.fixture
def runner(tmp_path):
    binaries = tmp_path / 'bin'; binaries.mkdir()
    (tmp_path / 'data').mkdir()
    script(binaries / 'python', f'#!{sys.executable}\n' + SPY)
    # A regressed chain condition must fail offline, never reach real GitHub.
    script(binaries / 'gh', '#!/bin/sh\necho forbidden-dispatch >> "$CASE_ROOT/forbidden"\nexit 99\n')
    script(binaries / 'xvfb-run', '#!/bin/sh\n[ "$1" = --auto-servernum ] || exit 90\nshift\nexec "$@"\n')
    script(binaries / 'jq', f'#!{sys.executable}\n' + r'''
import json,sys
value=json.load(open(sys.argv[-1])); query=sys.argv[-2]
if query == '.filer_ids | join(" ")': print(' '.join(value['filer_ids']))
elif query == '.range_end': print(value['range_end'])
elif query == '.publication_safe == true': print(str(value.get('publication_safe') is True).lower())
elif query == '.verified == true and .publication_safe == true and .terminal_cash_accepted == false':
    sys.exit(0 if value['verified'] and value['publication_safe'] and not value['terminal_cash_accepted'] else 1)
else: raise AssertionError(query)
''')
    env = {**os.environ, 'PATH':str(binaries)+os.pathsep+os.environ['PATH'],
           'PYTHONPATH':str(ROOT/'scraper'), 'CASE_ROOT':str(tmp_path),
           'IDENTITY_PLAN_PATH':str(tmp_path/'plan.json'),
           'IDENTITY_VERIFY_START_PATH':str(tmp_path/'start.json'),
           'IDENTITY_REPORT_PATH':str(tmp_path/'report.json'),
           'PROJECTION_INPUTS_PATH':str(tmp_path/'projection.json'),
           'ORESTAR_SEARCH_BUDGET_PATH':str(tmp_path/'budget.json')}
    sys.path.insert(0, str(ROOT/'scraper'))
    from search_budget import SearchBudget
    budget = SearchBudget.initialize(env['ORESTAR_SEARCH_BUDGET_PATH'], 45)

    def run(fault=''):
        inputs = {'recovery_mode':'identity_backfill', 'filer_ids':'10 20', 'max_passes':'3'}
        states = {'effort':{'outcome':'success', 'outputs':{'max_passes':'3'}},
                  'state':{'outcome':'success'}, 'plan':{'outcome':'skipped'}}
        failed = False; invoked = []; terminal = None; started = False
        for name, text in blocks():
            if name == 'Plan one currently certified missing-ID scope': started = True
            elif name == 'Plan complete canonical scopes': started = False
            if not started:
                continue
            if name.startswith('Install '):
                continue  # Installation is not part of this offline execution.
            ident = scalar(text, 'id') or name
            if not enabled(text, inputs, states, failed=failed):
                states[ident] = {'outcome':'skipped', 'outputs':{}}
                continue
            invoked.append(name)
            if scalar(text, 'uses'):
                assert ident == 'identity_artifact'
                code = 1 if fault == 'artifact' else 0
            else:
                output = tmp_path / ('output-' + str(len(invoked)))
                step_env = {**env, 'FAULT':fault, 'GITHUB_OUTPUT':str(output)}
                for key, expression in re.findall(r'^          ([A-Z_]+): \$\{\{ (.*?) \}\}$', text, re.M):
                    step_env[key] = value(expression, inputs, states)
                body = re.sub(r'\$\{\{ (.*?) \}\}', lambda m:value(m[1], inputs, states), run_body(text))
                result = subprocess.run(['bash','-e','-o','pipefail','-c',body], cwd=tmp_path,
                                        env=step_env, text=True, capture_output=True, timeout=15)
                code = result.returncode
                if name == 'Enforce truthful identity recovery terminal status': terminal = result
            outputs = dict(line.split('=',1) for line in output.read_text().splitlines()) if not scalar(text,'uses') and output.exists() else {}
            states[ident] = {'outcome':'success' if code == 0 else 'failure', 'outputs':outputs}
            if code and scalar(text, 'continue-on-error') != 'true': failed = True
        assert not (tmp_path/'forbidden').exists(), 'Unexpected GitHub/dispatch command'
        events = [json.loads(line) for line in (tmp_path/'events').read_text().splitlines()]
        return {'states':states, 'events':events, 'invoked':invoked, 'terminal':terminal, 'budget':budget.state()}
    return run


def test_success_executes_one_complete_scope_and_preserves_shared_budget(runner):
    run = runner()
    assert run['terminal'].returncode == 0, run['terminal'].stderr
    assert [item['event'] for item in run['events']] == [
        'plan','fetch','fetched','merge','raw_publish','start','diff','verify',
        'publish','projection_start','aggregate','donors','projection_verify']
    assert len({item['budget'] for item in run['events']}) == 1
    assert run['budget']['used'] == 4 and run['budget']['limit'] == 45
    fetch = next(item['argv'] for item in run['events'] if item['event'] == 'fetch')
    exact = next(item['argv'] for item in run['events'] if item['event'] == 'diff')
    assert fetch[1:4] == ['--filer-ids','10','20'] and '--reset-identity-progress' in fetch
    assert exact[1:4] == ['--filer-ids','10','20'] and '--require-no-missing' in exact
    for command in (fetch, exact):
        assert command[command.index('--end-date')+1] == '2026-09-15'
        assert command[command.index('--start-year')+1] == '2006'
    assert 'balances still require a new paired capture window' in run['terminal'].stdout
    assert 'Continue bounded evidence chain' not in run['invoked']


@pytest.mark.parametrize('fault', ['fetch','fetched'])
def test_failed_fetch_or_zero_exit_incomplete_proof_never_merges(runner, fault):
    run = runner(fault)
    events = [item['event'] for item in run['events']]
    assert 'merge' not in events and 'raw_publish' not in events and 'diff' not in events
    assert run['states']['identity_artifact']['outcome'] == 'success'
    assert run['terminal'].returncode != 0


@pytest.mark.parametrize('fault', ['diff','missing','budget'])
def test_failed_exact_still_projects_durable_truth_and_fails_without_retry(runner, fault):
    run = runner(fault)
    events = [item['event'] for item in run['events']]
    assert events.index('raw_publish') < events.index('diff') < events.index('publish') < events.index('aggregate')
    assert events.count('fetch') == events.count('diff') == 1
    assert run['states']['identity_publish']['outcome'] == run['states']['identity_projection_verify']['outcome'] == 'success'
    assert run['states']['identity_verify']['outcome'] == 'failure'
    assert run['terminal'].returncode != 0
    assert 'Continue bounded evidence chain' not in run['invoked']
    if fault == 'budget': assert run['budget']['used'] == 45


@pytest.mark.parametrize('fault,forbidden', [
    ('raw_publish','diff'), ('unsafe','publish'), ('publish','aggregate'),
    ('projection_start','aggregate'), ('aggregate','donors'), ('donors','projection_verify'),
])
def test_failed_durability_or_integrity_gate_stops_dependent_work(runner, fault, forbidden):
    run = runner(fault)
    assert forbidden not in [item['event'] for item in run['events']]
    assert run['terminal'].returncode != 0


def test_missing_audit_artifact_cannot_finish_green(runner):
    run = runner('artifact')
    assert run['states']['identity_projection_verify']['outcome'] == 'success'
    assert run['terminal'].returncode != 0


def test_default_evidence_branch_and_chain_remain_explicitly_separate():
    text = WORKFLOW.read_text()
    assert 'run-name: \'Atomic balance evidence: ${{ inputs.effort_id }}\'' in text
    assert text.count('scraper/search_budget.py init') == 1
    order = [text.index('      - name: '+name) for name in [
        'Wait for other ORESTAR jobs','Refresh branch after coordination wait',
        'Admit this attempt against the persistent effort limit','Initialize shared exact search budget',
        'Hydrate one immutable evidence window','Plan one currently certified missing-ID scope',
        'Fetch fresh exports for every original scope member']]
    assert order == sorted(order)
    evidence = {'recovery_mode':'', 'filer_ids':''}
    identity = {'recovery_mode':'identity_backfill', 'filer_ids':''}
    states = {'plan':{'outcome':'success','outputs':{'filer_ids':'10'}},
              'verify':{'outputs':{'certified_scopes':'1'}},
              'effort':{'outputs':{'max_passes':'3'}}}
    assert enabled(block('Plan complete canonical scopes'), evidence, states)
    assert enabled(block('Capture fresh summaries for planned scopes'), evidence, states)
    assert not enabled(block('Plan one currently certified missing-ID scope'), evidence, states)
    chain = block('Continue bounded evidence chain')
    assert enabled(chain, evidence, states)
    assert not enabled(chain, identity, states)
    assert not enabled(chain, {**evidence,'filer_ids':'10'}, states)
    assert not enabled(chain, evidence, {**states, 'effort':{'outputs':{'max_passes':'1'}}})
    assert not enabled(chain, evidence, states, failed=True)
    assert not enabled(chain, evidence, states, cancelled=True)
    for name, text in blocks():
        if scalar(text,'id').startswith('identity_') and scalar(text,'id') not in {'identity_artifact'}:
            assert not enabled(text, identity, states, cancelled=True)
    for name in ['Capture fresh summaries for planned scopes','Select scopes paired inside this window',
                 'Diff exactly the freshly paired scopes','Verify atomic exact evidence','Publish atomic evidence state',
                 'Re-aggregate from durable evidence','Stabilize captured cash against fresh exact evidence',
                 'Recover aggregation when the atomic plan is empty','Replan after recovering empty-plan aggregation',
                 'Enforce truthful terminal status']:
        assert "inputs.recovery_mode != 'identity_backfill'" in scalar(block(name),'if')
