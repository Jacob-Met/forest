"""A remote scan must prove coverage before it may erase saved source evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from runners.remote import RemoteRunner

ROOT = Path(__file__).resolve().parents[1]


def _runner(monkeypatch):
    runner = RemoteRunner({'hostname': 'localhost', 'username': 'observer'})

    def command(argv, timeout=30):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        assert result.returncode == 0, result.stderr
        return result.stdout

    monkeypatch.setattr(runner, 'command', command)
    return runner


def test_remote_inventory_proves_complete_root_and_subtree(tmp_path, monkeypatch):
    runner = _runner(monkeypatch)
    root = tmp_path / 'source'
    root.mkdir()
    (root / 'evidence.py').write_text('evidence = 42\n')
    job = {'workdir': str(root)}
    healthy = runner.observe(job)
    assert healthy['scan_ok'] is True
    assert [item['path'] for item in healthy['files']] == ['evidence.py']
    assert healthy['has_more'] is False and healthy['cursor_found'] is True

    root.rename(tmp_path / 'moved')
    missing = runner.observe(job)
    assert missing['scan_ok'] is False and missing['files'] == []
    assert missing['error'] and missing['has_more'] is False
    (tmp_path / 'moved').rename(root)
    root.chmod(0)
    try:
        denied = runner.observe(job)
        assert denied['scan_ok'] is False, 'Mode-000 root must not certify empty coverage'
    finally:
        root.chmod(0o700)

    subtree = root / 'unavailable'; subtree.mkdir()
    (subtree / 'should-survive.md').write_text('saved work')
    subtree.chmod(0)
    try:
        partial = runner.observe(job)
        assert partial['scan_ok'] is False, 'An unreadable subtree must invalidate deletion'
    finally:
        subtree.chmod(0o700)
    recovered = runner.observe(job)
    assert recovered['scan_ok'] is True
    assert {item['path'] for item in recovered['files']} == {'evidence.py', 'unavailable/should-survive.md'}


def test_capture_keeps_observations_until_verified_complete_scan(tmp_path):
    env = {**os.environ, 'FOREST_DATA_DIR': str(tmp_path / 'data'),
           'FOREST_DATABASE_URL': 'sqlite:///' + str(tmp_path / 'observations.sqlite'),
           'FOREST_OWNER_TOKEN': 'test-local-owner', 'FOREST_MODEL': '',
           'PYTHONPATH': str(ROOT)}
    result = subprocess.run([sys.executable, __file__, 'capture'], cwd=ROOT,
                            env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == '__main__':
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from services.api.main import app
    from services.api.db import Session, TaskRun, uid
    from services.observation.files import sync_scopes
    from services.observation.models import ObservationScope, ObservedFile
    from services.observation.remote import capture

    with TestClient(app) as client:
        response = client.post('/api/projects', json={'name': 'Remote source continuity'})
        assert response.status_code == 200, response.text
        pid = response.json()['id']
        with Session.begin() as session:
            run = TaskRun(id=uid(), project_id=pid, request_id='remote-inventory',
                          kind='command', status='running', output_path='runs/remote',
                          config={'remote': {'hostname': 'example.invalid'},
                                  'execution_attempt': {'id': 'real-attempt'}})
            session.add(run); session.flush(); run_id = run.id
            sync_scopes(session, pid)
            scope = session.scalar(select(ObservationScope).where(
                ObservationScope.object_id == run_id,
                ObservationScope.kind == 'remote_workspace'))
            assert scope is not None
            scope_id = scope.id

        def page(files, *, scan_ok=True, error=None, cursor='', has_more=False,
                 next_cursor=None, cursor_found=True):
            return {'files': files, 'cursor': cursor, 'has_more': has_more,
                    'next_cursor': next_cursor, 'cursor_found': cursor_found,
                    'scan_ok': scan_ok, 'error': error}

        saved = {'path': 'evidence.md', 'content': 'source bytes', 'size': 12,
                 'mtime_ns': 123}
        capture(run_id, page([saved]), 'real-attempt')

        def snapshot():
            with Session() as session:
                scope = session.get(ObservationScope, scope_id)
                rows = list(session.scalars(select(ObservedFile).where(
                    ObservedFile.scope_id == scope_id)))
                return scope.coverage, scope.error, scope.scan_generation, [
                    (row.path, row.state, row.content, row.generation) for row in rows]

        baseline = snapshot()
        assert baseline[0] == 'complete' and baseline[3][0][2] == b'source bytes'
        # A legacy or invalid sender without positive coverage attestation
        # must not cause a destructive sweep even if files=[] and has_more=False.
        for failure in (page([], scan_ok=False, error='Remote root missing'),
                        page([], scan_ok=False, error='Remote subtree inaccessible'),
                        {'files': [], 'cursor': '', 'has_more': False},
                        page([], cursor_found=False),
                        page([], has_more=True, next_cursor=None)):
            capture(run_id, failure, 'real-attempt')
            observed = snapshot()
            assert observed[2] == baseline[2], observed
            assert observed[3] == baseline[3], observed
            assert observed[0] != 'complete' and observed[1], observed

        # A restored, positively confirmed scan can delete genuinely missing
        # files; the failure gate must not prevent future legitimate updates.
        capture(run_id, page([], scan_ok=True), 'real-attempt')
        deleted = snapshot()
        assert deleted[0] == 'complete' and deleted[3][0][1:3] == ('deleted', None)
        capture(run_id, page([saved]), 'real-attempt')
        restored = snapshot()
        assert restored[3][0][1:3] == ('available', b'source bytes')
        capture(run_id, page([], scan_ok=True), 'old-attempt')
        assert snapshot() == restored

        # A multi-page scan may mark files deleted only after every page has
        # been certified. An incomplete page must preserve the last source.
        extra = {'path': 'second.md', 'content': 'more evidence', 'size': 13,
                 'mtime_ns': 456}
        capture(run_id, page([saved, extra]), 'real-attempt')
        capture(run_id, page([saved], has_more=True,
                             next_cursor='evidence.md'), 'real-attempt')
        with Session() as session:
            scope = session.get(ObservationScope, scope_id)
            rows = list(session.scalars(select(ObservedFile).where(
                ObservedFile.scope_id == scope_id)))
            assert scope.coverage == 'partial'
            assert all(row.content is not None and row.state == 'available'
                       for row in rows)
        capture(run_id, page([extra], cursor='evidence.md'), 'real-attempt')
        with Session() as session:
            scope = session.get(ObservationScope, scope_id)
            rows = list(session.scalars(select(ObservedFile).where(
                ObservedFile.scope_id == scope_id)))
            assert scope.coverage == 'complete'
            assert len(rows) == 2 and all(row.state == 'available' for row in rows)
        capture(run_id, page([saved]), 'real-attempt')
        with Session() as session:
            rows = {row.path: row for row in session.scalars(
                select(ObservedFile).where(ObservedFile.scope_id == scope_id))}
            assert rows['evidence.md'].state == 'available'
            assert rows['second.md'].state == 'deleted'
            assert rows['second.md'].content is None
    print('Remote evidence preservation, pagination, recovery and stale attempts passed')
