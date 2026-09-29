"""Every full aggregation must be followed by the donor re-key.

`scraper/process.py` rebuilds each committee's `top_donors` /
`top_donors_by_year` blob in `filer_detail` **from the raw transaction
labels**, which drops `donor_key` / `donor_id` and with them the resolver's
work and every merge saved at `/admin/donors`. The dashboard then cannot read
a contributor category for any donor.

That is not hypothetical. On 2026-09-20 `earliest-balances.yml` left 24 of
6,878 committees with resolved identities (PR #28). On 2026-09-23 the same
gap in `atomic-balance-evidence.yml` left 12 of 3,000, and the signed-in
tools' donor lists broke visibly.

So the pairing is checked here rather than remembered. `--merge-only` does not
rebuild the blobs and needs no re-key.
"""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parents[1] / '.github/workflows').glob('*.yml'))
REKEY = 'scraper/refresh_donor_aggregates.py'
# Neither flag rebuilds the donor blobs: --merge-only stops after merging raw
# Excel into the transaction shards, and --supabase-full-load only reloads
# those shards into Postgres and uploads the combined CSV. A bare run is the
# one that re-derives the aggregates from labels.
NOT_AGGREGATION = ('--merge-only', '--supabase-full-load')


def events(run):
    """(line, kind) for each aggregation or re-key in one shell block, in order."""
    for line in run.split('\n'):
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        if REKEY in stripped:
            yield stripped, 'rekey'
        elif 'scraper/process.py' in stripped and not any(f in stripped for f in NOT_AGGREGATION):
            yield stripped, 'aggregate'


def job_events(workflow):
    """(job, step name, line, kind) across a workflow, in file order."""
    data = yaml.safe_load(workflow.read_text())
    for job_name, job in (data.get('jobs') or {}).items():
        out = []
        for step in job.get('steps') or []:
            run = step.get('run')
            if not isinstance(run, str):
                continue
            for line, kind in events(run):
                out.append((step.get('name') or step.get('id') or '?', line, kind))
        yield job_name, out


@pytest.mark.parametrize('workflow', WORKFLOWS, ids=lambda w: w.name)
def test_every_full_aggregation_is_followed_by_the_donor_rekey(workflow):
    for job_name, sequence in job_events(workflow):
        for index, (name, line, kind) in enumerate(sequence):
            if kind != 'aggregate':
                continue
            # Scanning forward, a re-key has to come before the next
            # aggregation — otherwise that run publishes unkeyed blobs.
            following = [k for _, _, k in sequence[index + 1:]]
            nxt = next((k for k in following if k in ('rekey', 'aggregate')), None)
            assert nxt == 'rekey', (
                f'{workflow.name} job "{job_name}" step "{name}" runs a full aggregation '
                f'({line!r}) with no {REKEY} after it. That publishes donor blobs with no '
                f'donor_key, which silently undoes the resolver and every saved merge.')


def test_the_check_would_catch_a_missing_rekey(tmp_path):
    bad = tmp_path / 'bad.yml'
    bad.write_text('jobs:\n  build:\n    steps:\n'
                   '      - name: Aggregate\n        run: python scraper/process.py\n')
    with pytest.raises(AssertionError, match='no scraper/refresh_donor_aggregates.py'):
        test_every_full_aggregation_is_followed_by_the_donor_rekey(bad)


@pytest.mark.parametrize('flag', NOT_AGGREGATION)
def test_runs_that_do_not_rebuild_the_blobs_need_no_rekey(tmp_path, flag):
    fine = tmp_path / 'fine.yml'
    fine.write_text('jobs:\n  build:\n    steps:\n'
                    f'      - name: Load\n        run: python scraper/process.py {flag}\n')
    test_every_full_aggregation_is_followed_by_the_donor_rekey(fine)
