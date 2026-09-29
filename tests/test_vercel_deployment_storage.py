"""Regression contracts for keeping Vercel deployments small."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vercel_output_excludes_legacy_aggregate_mirror() -> None:
    config = json.loads((ROOT / "vercel.json").read_text())

    assert config["outputDirectory"] == "docs"
    assert not (ROOT / "docs/data/aggregated").exists()
    # A static review queue becomes stale now that generated state is external.
    assert not (ROOT / "docs/data/review_queue.json").exists()
    assert "/docs/data/aggregated/" in (ROOT / ".gitignore").read_text()


def test_data_only_commits_skip_vercel_deployments() -> None:
    config = json.loads((ROOT / "vercel.json").read_text())
    command = config["ignoreCommand"]

    assert 'if [ -n "$VERCEL_GIT_PREVIOUS_SHA" ]' in command
    assert 'git diff --quiet "$VERCEL_GIT_PREVIOUS_SHA" HEAD --' in command
    # A missing/too-old baseline in Vercel's shallow clone must build safely.
    assert command.endswith("fi; exit 1")
    for deployment_input in ("docs/", "api/", "vercel.json", ".vercelignore"):
        assert deployment_input in command

    for workflow_name in ("earliest-balances.yml", "filer-metadata.yml"):
        workflow = (ROOT / ".github/workflows" / workflow_name).read_text()
        assert "docs/data/" not in workflow


def test_ignore_command_is_fail_safe(tmp_path: Path) -> None:
    command = json.loads((ROOT / "vercel.json").read_text())["ignoreCommand"]

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=tmp_path, check=True
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "docs/index.html").write_text("initial")
    (tmp_path / "vercel.json").write_text("{}")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    baseline = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()

    (tmp_path / "data/cache.json").write_text("data only")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "data"], cwd=tmp_path, check=True)
    env = {**os.environ, "VERCEL_GIT_PREVIOUS_SHA": baseline}
    assert subprocess.run(command, cwd=tmp_path, env=env, shell=True).returncode == 0

    (tmp_path / "docs/index.html").write_text("changed")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "site"], cwd=tmp_path, check=True)
    assert subprocess.run(command, cwd=tmp_path, env=env, shell=True).returncode == 1

    # The comparison is cumulative: a later data-only tip cannot hide the
    # earlier site change if Vercel dropped that site's queued build.
    (tmp_path / "data/cache.json").write_text("newer data")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "newer data"], cwd=tmp_path, check=True)
    assert subprocess.run(command, cwd=tmp_path, env=env, shell=True).returncode == 1

    # A missing or unavailable baseline must build instead of skipping.
    no_baseline = {k: v for k, v in os.environ.items() if k != "VERCEL_GIT_PREVIOUS_SHA"}
    assert subprocess.run(command, cwd=tmp_path, env=no_baseline, shell=True).returncode == 1
    env["VERCEL_GIT_PREVIOUS_SHA"] = "0" * 40
    assert subprocess.run(command, cwd=tmp_path, env=env, shell=True).returncode == 1


def test_browser_code_has_no_static_aggregate_loader() -> None:
    for script in ("docs/app.js",):
        source = (ROOT / script).read_text()
        assert "data/aggregated" not in source
        assert "fetchJSON(" not in source


def test_reconciliation_uses_canonical_aggregate_data() -> None:
    source = (ROOT / "scraper/reconcile.py").read_text()

    assert "DOCS_DIR" not in source
    assert 'AGGREGATED_DIR / "filer_index.json"' in source
    assert 'AGGREGATED_DIR / "filers"' in source


def test_recommend_tool_is_not_in_this_repository() -> None:
    """The Recommend tool lives in a separate private repository and reaches
    signed-in users from a private Storage bucket. None of it may come back into
    this public repository, or into the public site."""
    config = json.loads((ROOT / "vercel.json").read_text())
    for gone in ("private", "api/private.js", "docs/recommend.js", "docs/RECOMMENDATIONS.md",
                 "docs/lib/lobbyists.js", "docs/admin/lobbyists.js",
                 "docs/assets/primary_campaign_exclusions.json",
                 "scraper/refresh_primary_campaigns.py", "tests/test_phase4.py",
                 # Lobbyist data, sources and matching also live privately.
                 "docs/lib/lobbyist-photos.js", "docs/assets/lobbyist-photos",
                 "docs/assets/lobbyist-photos.json", "docs/admin/lobbyists.css",
                 "scraper/fetch_capitol_club.py", "scraper/match_lobbyists.py",
                 "scraper/lobby_match.py", "scraper/refresh_lobbyist_photos.py",
                 "supabase/migrations/016_lobbyists.sql",
                 "supabase/migrations/017_plan_designations.sql",
                 "supabase/migrations/023_lobbyist_client_editor.sql"):
        assert not (ROOT / gone).exists(), gone
    assert "functions" not in config
    # The lobbyist directory's name, hex-encoded so this check doesn't publish it.
    source = bytes.fromhex("63617069746f6c20636c7562").decode()
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for path in tracked:
        if path.endswith((".py", ".js", ".html", ".css", ".md", ".yml", ".json", ".sql")):
            assert source not in (ROOT / path).read_text(errors="ignore").lower(), path

    for page in ("docs/recommend.html", "docs/admin/lobbyists.html"):
        html = (ROOT / page).read_text()
        assert "loadPrivatePage(" in html, page
        assert "lobbyists.css" not in html.split("loadPrivatePage(")[0], page
        assert 'class="mode-tabs"' not in html and 'id="tab-queue"' not in html, page
    assert 'const PRIVATE_BUCKET = "recommend";' in (ROOT / "docs/lib/supabase.js").read_text()

    # Notes stay in the repository: never uploaded, and redirected if they were.
    assert "docs/**/*.md" in (ROOT / ".vercelignore").read_text().split()
    assert any(r["source"] == "/(.*)\\.md" for r in config["redirects"])

def test_admin_only_tools_are_not_named_here() -> None:
    """Admin-only tools load through the generic /admin/tools shell from a
    private manifest; this repository must not name them or their data."""
    shell = (ROOT / "docs/admin/tools.html").read_text()
    assert "adminTools()" in shell and "loadPrivatePage(tool)" in shell
    assert 'fetchPrivate("admin-only/tools.json")' in (ROOT / "docs/lib/supabase.js").read_text()
    # Hex-encoded so this check doesn't name what it forbids.
    forbidden = [bytes.fromhex(h).decode() for h in ("6361756375732064756573", "6475657320747261636b6572", "747261636b65725f726f7773")]
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    for path in tracked:
        if path.endswith((".py", ".js", ".html", ".css", ".md", ".yml", ".json", ".sql")):
            text = (ROOT / path).read_text(errors="ignore").lower()
            for phrase in forbidden:
                assert phrase not in text, (path, "names an admin-only tool")
