"""Remote cancellation must outlive a lost SSH supervisor."""
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from runners.remote import RemoteRunner


def _local_runner(monkeypatch):
    """Run the exact SSH Python scripts on this host, without external SSH."""
    runner = RemoteRunner({'hostname': 'localhost', 'username': 'tester'})

    def command(argv, timeout=30):
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if completed.returncode:
            raise RuntimeError(completed.stderr[-4000:])
        return completed.stdout

    monkeypatch.setattr(runner, 'command', command)
    return runner


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.025)
    raise AssertionError('Remote command did not start')


def _live_group(pgid):
    rows = subprocess.check_output(['ps', '-eo', 'pid=,pgid=,stat='], text=True).splitlines()
    return [int(parts[0]) for line in rows if len(parts := line.split()) >= 3
            and parts[1] == str(pgid) and not parts[2].startswith('Z')]


def test_cancel_stops_surviving_command_and_child_after_supervisor_is_killed(tmp_path, monkeypatch):
    runner = _local_runner(monkeypatch)
    # This launches a real supervised command plus its live subprocess in the
    # supervisor's own detached session, exactly as remote SSH would do.
    child_marker = tmp_path / 'child.pid'
    command = ['/bin/sh', '-c', f'sleep 60 & echo $! > {child_marker}; wait']
    job = runner.start(command, str(tmp_path), task_id='lost-supervisor')
    pgid = job['pid']
    try:
        state_path = tmp_path/'.forest-tasks/lost-supervisor/state.json'
        def ready():
            if not state_path.exists() or not child_marker.exists():
                return None
            current = json.loads(state_path.read_text())
            return current if current.get('command_pid') else None
        state = _wait_until(ready)
        shell_pid = state['command_pid']
        child_pid = int(child_marker.read_text())
        assert {shell_pid, child_pid}.issubset(set(_live_group(pgid)))
        os.kill(pgid, signal.SIGKILL)  # Only kill supervisor; leave process group alive.
        assert _wait_until(lambda: runner.status(job)['status'] == 'lost')
        assert {shell_pid, child_pid}.issubset(set(_live_group(pgid)))
        stopped = runner.stop(job)
        assert stopped['status'] == 'cancelled' and stopped['stop_confirmed'] is True
        assert not _live_group(pgid), 'surviving command descendants were not stopped'
        assert runner.stop(job)['stop_confirmed'] is True  # retry/recovery is idempotent
    finally:
        # Target only verified members of the group created by this test.
        if _live_group(pgid):
            os.killpg(pgid, signal.SIGKILL)


@pytest.mark.parametrize('forged_field', ['command_created', 'command_pgid', 'command_sid', 'both'])
def test_stop_does_not_signal_group_without_matching_command_identity(tmp_path, monkeypatch, forged_field):
    runner = _local_runner(monkeypatch)
    marker = tmp_path/'command.pid'
    job = runner.start(['/bin/sh','-c',f'echo $$ > {marker}; sleep 60'], str(tmp_path), task_id='stale-identity')
    pgid = job['pid']
    try:
        state_path = tmp_path/'.forest-tasks/stale-identity/state.json'
        state = _wait_until(lambda: json.loads(state_path.read_text())
                            if state_path.exists() and marker.exists() and
                            json.loads(state_path.read_text()).get('command_pid') else None)
        command_pid = state['command_pid']
        os.kill(pgid, signal.SIGKILL)
        if forged_field == 'both':
            state['command_created'] = 'FORGED-OUTDATED-IDENTITY'
            state['process_created'] = ''  # missing supervisor identity cannot authorize a signal
        else:
            state[forged_field] = ('FORGED-OUTDATED-IDENTITY' if forged_field == 'command_created'
                                   else state[forged_field] + 100000)
        state_path.write_text(json.dumps(state))
        with pytest.raises(RuntimeError, match='unconfirmed|identity'):
            runner.stop(job)
        assert command_pid in _live_group(pgid), 'stop killed a group with a forged command identity'
    finally:
        if _live_group(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_stop_live_supervisor_kills_command_and_descendants(tmp_path, monkeypatch):
    runner = _local_runner(monkeypatch)
    marker = tmp_path/'child.pid'
    job = runner.start(['/bin/sh','-c',f'sleep 60 & echo $! > {marker}; wait'],
                       str(tmp_path), task_id='healthy-supervisor')
    pgid = job['pid']
    try:
        path = tmp_path/'.forest-tasks/healthy-supervisor/state.json'
        _wait_until(lambda: marker.exists() and path.exists()
                    and json.loads(path.read_text()).get('command_pid'))
        assert len(_live_group(pgid)) >= 3
        stopped = runner.stop(job)
        assert stopped['stop_confirmed'] is True
        assert _live_group(pgid) == []
    finally:
        if _live_group(pgid):
            os.killpg(pgid, signal.SIGKILL)


def test_short_remote_command_still_completes_with_identity_receipt(tmp_path, monkeypatch):
    runner = _local_runner(monkeypatch)
    job = runner.start([sys.executable, '-c', 'print(12345)'], str(tmp_path),
                       task_id='short-lived')
    state = _wait_until(lambda: (result if result.get('status') == 'completed' else None)
                        if (result := runner.status(job)) else None)
    assert state['exit_code'] == 0
    assert state['command_pid'] > 0
    assert state['command_created']
    assert state['command_pgid'] == state['command_sid'] == state['pid']
    assert '12345' in (tmp_path/'.forest-tasks/short-lived/stdout.txt').read_text()


def test_pause_resume_and_stop_share_verified_supervisor_identity(tmp_path, monkeypatch):
    runner = _local_runner(monkeypatch)
    marker = tmp_path/'command.pid'
    job = runner.start(['/bin/sh','-c',f'echo $$ > {marker}; sleep 60'],
                       str(tmp_path), task_id='control-healthy')
    pgid = job['pid']
    try:
        path = tmp_path/'.forest-tasks/control-healthy/state.json'
        _wait_until(lambda: path.exists() and marker.exists()
                    and json.loads(path.read_text()).get('command_pid'))
        paused = runner.control(job,'pause')
        assert paused['status'] == 'paused' and paused['control_confirmed'] is True
        assert runner.status(job)['status'] == 'paused'
        running = runner.control(job,'resume')
        assert running['status'] == 'running' and running['control_confirmed'] is True
        assert runner.stop(job)['stop_confirmed'] is True
        assert not _live_group(pgid)
    finally:
        if _live_group(pgid):
            os.killpg(pgid, signal.SIGKILL)
