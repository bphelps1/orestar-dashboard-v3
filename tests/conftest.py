"""Test-wide isolation from live infrastructure.

The suite builds its own fixtures — `tests/test_identity_remediation.py`
writes a whole `data/` tree into `tmp_path` and runs the selector with
`cwd=tmp_path` so it reads those files and nothing else.

That isolation was incomplete. `supabase_sync._load_dotenv()` resolves `.env`
from the MODULE's location (`Path(__file__).resolve().parent.parent`), not
from the working directory, so a repo-root `.env` is picked up no matter what
`cwd` the subprocess runs under. `sync_enabled()` then returns True and
`auto_backfill_ids.py` loads committee comparison details from live Supabase
instead of the fixture the test just wrote.

The failure is silent and backwards: green on a fresh clone and in CI, where
no `.env` exists, and red only for a developer who has credentials configured
— fourteen tests flipped, all in the paired-snapshot and identity-remediation
area. A test must not depend on whether the machine running it happens to
hold production credentials.

Clearing the variables here (rather than stubbing `sync_enabled`) keeps the
guard in one place and makes it total: every entry point consults the same
environment, including ones invoked as subprocesses, which inherit this
process's `os.environ`. A test that genuinely wants sync sets the variable
itself with monkeypatch.
"""

import os

import pytest

# Every variable that can make a code path reach the network. SUPABASE_DB_URL
# alone gates sync_enabled(), but the Storage helpers read the others, and
# leaving them set would make a partial connection look plausible.
_LIVE_INFRA_VARS = (
    "SUPABASE_DB_URL",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_SERVICE_KEY",
    "SUPABASE_STORAGE_BUCKET",
)


@pytest.fixture(autouse=True, scope="session")
def _no_live_infrastructure() -> None:
    """Neutralize live credentials for the whole session.

    Set to empty, NOT deleted. `_load_dotenv()` populates with
    `os.environ.setdefault(...)`, so an ABSENT variable is precisely the case
    it fills in from the file — deleting the name invites the load rather than
    preventing it. An empty value is already "set", so setdefault leaves it
    alone, and every gate downstream is a truthiness check
    (`bool(os.environ.get("SUPABASE_DB_URL"))`), which empty fails. That also
    honours the loader's own documented contract: never override a value the
    caller has already chosen.

    autouse + session scope, because the point is that no test can opt out by
    forgetting to ask for it. `os.environ` is process-global and subprocesses
    inherit it, which is exactly the reach needed: the selector under test is
    spawned with `subprocess.run(..., env=os.environ.copy())`.
    """
    for name in _LIVE_INFRA_VARS:
        os.environ[name] = ""
