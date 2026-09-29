"""Every long Postgres step must carry its own timeout-minutes.

SUPABASE_DB_URL goes through the Supavisor pooler. When Postgres crashed on
2026-09-23, the pooler kept each client's socket open, so nothing errored:
`refresh_donor_aggregates.py` sat for 1h44m and `process.py` for 2h10m until
they were cancelled by hand. TCP keepalives are answered by the pooler, and
`statement_timeout` is enforced by the server that died, so neither fires
(see `supabase_sync._connect`). The step timeout is what ends the wait.

Without it the only bound is the job's timeout, up to six hours, and a hung
ORESTAR-lane job holds `await-orestar` for all of it.
"""
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parents[1] / '.github/workflows').glob('*.yml'))
LONG_DB_SCRIPTS = ('scraper/process.py', 'scraper/refresh_donor_aggregates.py',
                   'scraper/resolve_donors.py')
# One-off manual load, never run in v2. Its 60-minute job timeout is already
# shorter than the budget any of these steps would get.
EXEMPT = {'supabase-load.yml'}


def long_db_steps(workflow):
    data = yaml.safe_load(workflow.read_text())
    for job_name, job in (data.get('jobs') or {}).items():
        for step in job.get('steps') or []:
            run = step.get('run')
            if not isinstance(run, str):
                continue
            lines = [line.strip() for line in run.split('\n')]
            if any(s in line for line in lines if not line.startswith('#')
                   for s in LONG_DB_SCRIPTS):
                yield job_name, job, step


@pytest.mark.parametrize('workflow', [w for w in WORKFLOWS if w.name not in EXEMPT],
                         ids=lambda w: w.name)
def test_every_long_db_step_has_its_own_timeout(workflow):
    for job_name, job, step in long_db_steps(workflow):
        name = step.get('name') or step.get('id') or '?'
        budget = step.get('timeout-minutes')
        assert budget is not None, (
            f'{workflow.name} job "{job_name}" step "{name}" runs a long Postgres script '
            f'with no timeout-minutes. A backend that dies behind the pooler never '
            f'errors, so this step would wait until the job timeout.')
        if isinstance(budget, int):
            assert budget < job['timeout-minutes'], (
                f'{workflow.name} step "{name}": a {budget}-minute step timeout never '
                f'fires inside a {job["timeout-minutes"]}-minute job.')
        else:
            assert budget.startswith('${{'), f'{workflow.name} step "{name}": {budget!r}'


def test_the_check_would_catch_a_missing_timeout(tmp_path):
    bad = tmp_path / 'bad.yml'
    bad.write_text('jobs:\n  build:\n    timeout-minutes: 180\n    steps:\n'
                   '      - name: Re-key\n        run: python scraper/refresh_donor_aggregates.py\n')
    with pytest.raises(AssertionError, match='no timeout-minutes'):
        test_every_long_db_step_has_its_own_timeout(bad)


def test_the_check_ignores_scripts_named_only_in_comments(tmp_path):
    fine = tmp_path / 'fine.yml'
    fine.write_text('jobs:\n  build:\n    timeout-minutes: 30\n    steps:\n'
                    '      - name: Note\n        run: |\n'
                    '          # scraper/process.py is not run here\n          true\n')
    test_every_long_db_step_has_its_own_timeout(fine)
