"""Concurrent project setting changes must not lose accepted config edits.

The subprocess owns its app/database. PostgreSQL coverage is opt-in with
FOREST_TEST_POSTGRES_URL; SQLite is always exercised without shared test data.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ("publication_fields", "publication_and_objective"))
def test_concurrent_config_edits_serialize_without_lost_updates(tmp_path, scenario):
    env = {
        **os.environ,
        "FOREST_DATABASE_URL": "sqlite:///" + str(tmp_path / "config.sqlite"),
        "FOREST_DATA_DIR": str(tmp_path / "data"),
        "FOREST_MODEL": "",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run([sys.executable, __file__, scenario], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", ("publication_fields", "publication_and_objective"))
def test_postgres_concurrent_config_edits(tmp_path, scenario):
    url = os.environ.get("FOREST_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set FOREST_TEST_POSTGRES_URL to test genuine PostgreSQL row locks")
    env = {**os.environ, "FOREST_DATABASE_URL": url,
           "FOREST_DATA_DIR": str(tmp_path / "data"), "FOREST_MODEL": "", "PYTHONPATH": str(ROOT)}
    result = subprocess.run([sys.executable, __file__, scenario], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


def exercise(scenario):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError
    from fastapi.testclient import TestClient
    from services.api.main import app
    from services.api.db import Project, Session
    from services.api import research_runtime as runtime

    with TestClient(app) as client:
        project_response = client.post('/api/projects', json={
            'name': 'Concurrent settings fixture',
            'goal': 'Preserve both accepted settings changes',
            'mode': 'manual'})
        assert project_response.status_code == 200, project_response.text
        project = project_response.json()
        project_id = project["id"]

        # Synchronize both stale readers after loading the row on the
        # unfixed handlers. The corrected handlers take _lock_project first
        # and correctly serialize without this instrumentation.
        barrier = Barrier(2)
        original_get = runtime.get

        def synchronize_stale_reads(session, model, ident, *args, **kwargs):
            row = original_get(session, model, ident, *args, **kwargs)
            if model is Project and ident == project_id:
                try:
                    barrier.wait(timeout=5)
                except BrokenBarrierError as exc:
                    raise AssertionError("Only one config writer reached the old unlocked read") from exc
            return row

        changes = [
            ('publication', {'accepted_papers': 16}),
            ('publication', {'datasets': 4}) if scenario == 'publication_fields'
                else ('objective', {'metric': 'f1', 'direction': 'max'}),
        ]

        def write(change):
            suffix, patch = change
            response = client.patch(f'/api/projects/{project_id}/{suffix}', json=patch)
            return response.status_code, response.json()

        runtime.get = synchronize_stale_reads
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(write, changes))
        finally:
            runtime.get = original_get

        assert all(status == 200 for status, _ in results), results
        saved_response = client.get(f'/api/projects/{project_id}')
        assert saved_response.status_code == 200, saved_response.text
        saved = saved_response.json()
        publication = saved['config']['publication_profile']
        assert publication['accepted_papers'] == 16, saved
        if scenario == 'publication_fields':
            assert publication['datasets'] == 4, saved
        else:
            assert saved['config']['objective']['metric'] == 'f1', saved
            assert saved['config']['objective']['direction'] == 'max', saved
        assert saved['revision'] == project['revision'] + 2, saved
        print(f"{scenario}: both successful settings edits durable; revision {saved['revision']}")


if __name__ == "__main__":
    exercise(sys.argv[1])
