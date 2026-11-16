"""Imported project figures keep editable source but cannot execute it on render."""

from test_worker import Harness


def test_imported_figure_code_stays_inert_until_owner_reauthors_it(tmp_path):
    harness = Harness(tmp_path)
    try:
        harness.start_api()
        source = harness.request('POST', '/api/projects', json={
            'name': 'Foreign authored plot', 'goal': 'Imported plot provenance',
            'budget': {'allow_paid': False, 'max_runs': 10, 'seconds': 120},
        })
        original_code = (
            'from pathlib import Path\n'
            'Path("imported_code_was_executed.txt").write_text("EXECUTED")\n'
            'Path("figure.svg").write_text("<svg/>")'
        )
        figure_data = {
            'kind': 'bar', 'metric': 'score',
            'data': [{'dataset': 'sample', 'method': 'baseline', 'score': 1}],
            'style': {'metric': 'score', 'unit': 'score'},
            'code_origin': 'custom', 'code': original_code,
        }
        harness.request('POST', '/api/figures', json={
            'project_id': source['id'], 'title': 'Foreign plot', 'data': figure_data,
        })
        archive = harness.client.post(f"/api/projects/{source['id']}/export", json={})
        assert archive.status_code == 200
        imported = harness.client.post('/api/projects/import', files={
            'file': ('foreign-project.zip', archive.content, 'application/zip'),
        })
        assert imported.status_code == 200, imported.text
        project_id = imported.json()['id']
        imported_figures = harness.request('GET', '/api/figures',
                                           params={'project_id': project_id})
        assert len(imported_figures) == 1
        figure = imported_figures[0]
        assert figure['data']['code'] == original_code

        harness.start_worker()
        run = harness.request('POST', f"/api/figures/{figure['id']}/render",
                              json={'request_id': 'foreign-code-render'})
        result = harness.terminal(run, timeout=50)
        assert result['status'] == 'completed', result
        output = harness.output(run) / 'figure'
        assert (output / 'figure.svg').is_file()
        assert not (output / 'imported_code_was_executed.txt').exists()
        after = harness.request('GET', f"/api/figures/{figure['id']}")
        assert after['data']['code_origin'] == 'imported_untrusted'
        assert after['data']['code'] == original_code

        # Only an explicit owner edit promoting changed code can enable the custom path.
        reviewed_code = original_code.replace('imported_code_was_executed.txt',
                                               'owner_reviewed_code_was_executed.txt')
        edited = harness.request('PATCH', f"/api/figures/{figure['id']}", json={
            'expected_revision': after['revision'],
            'data': {'code': reviewed_code, 'code_origin': 'custom'},
        })
        assert edited['data']['code_origin'] == 'custom'
        rerender = harness.request('POST', f"/api/figures/{figure['id']}/render",
                                   json={'request_id': 'reviewed-code-render'})
        assert harness.terminal(rerender, timeout=50)['status'] == 'completed'
        trusted_output = harness.output(rerender) / 'figure'
        assert (trusted_output / 'owner_reviewed_code_was_executed.txt').exists()
        assert not (trusted_output / 'imported_code_was_executed.txt').exists()
    finally:
        harness.cleanup()


def test_retry_of_imported_figure_run_does_not_execute_archived_code(tmp_path):
    harness = Harness(tmp_path)
    try:
        harness.start_api()
        source = harness.request('POST', '/api/projects', json={
            'name': 'Queued foreign plot', 'goal': 'No replay of archive code',
            'budget': {'allow_paid': False, 'max_runs': 10, 'seconds': 120},
        })
        code = ('from pathlib import Path\n'
                'Path("archive_retry_executed.txt").write_text("EXECUTED")\n'
                'Path("figure.svg").write_text("<svg/>")')
        fig = harness.request('POST', '/api/figures', json={
            'project_id': source['id'], 'title': 'Queued foreign plot',
            'data': {
                'kind': 'bar', 'metric': 'score', 'style': {'metric': 'score'},
                'data': [{'dataset': 'sample', 'method': 'baseline', 'score': 1}],
                'code_origin': 'custom', 'code': code,
            },
        })
        queued = harness.request('POST', f"/api/figures/{fig['id']}/render",
                                  json={'request_id': 'source-queued'})
        assert queued['status'] == 'queued'
        archive = harness.client.post(f"/api/projects/{source['id']}/export", json={})
        assert archive.status_code == 200
        imported = harness.client.post('/api/projects/import', files={
            'file': ('queued-foreign.zip', archive.content, 'application/zip'),
        })
        assert imported.status_code == 200, imported.text
        project_id = imported.json()['id']
        rows = harness.request('GET', f'/api/projects/{project_id}/runs')
        assert len(rows) == 1 and rows[0]['status'] == 'interrupted', rows
        saved = rows[0]
        imported_figure = harness.request('GET', '/api/figures',
                                          params={'project_id': project_id})[0]
        assert imported_figure['data']['code_origin'] == 'imported_untrusted'
        assert saved['config']['figure']['code'] == code
        assert saved['config']['figure']['code_origin'] == 'imported_untrusted'

        # Source's original run is not part of this test's execution.
        harness.request('POST', f"/api/runs/{queued['id']}/cancel", json={})
        harness.start_worker()
        retry = harness.request('POST', f"/api/runs/{saved['id']}/retry",
                                json={'request_id': 'retry-imported-figure'})
        result = harness.terminal(retry, timeout=50)
        assert result['status'] == 'completed', result
        target = harness.output(retry) / 'figure'
        assert (target / 'figure.svg').is_file()
        assert not (target / 'archive_retry_executed.txt').exists()
    finally:
        harness.cleanup()


def test_imported_graph_node_renders_without_executing_embedded_python(tmp_path):
    import uuid
    harness = Harness(tmp_path)
    try:
        harness.start_api()
        project = harness.request('POST', '/api/projects', json={
            'name': 'Foreign graph figure', 'goal': 'Trust imported graph configs',
            'budget': {'allow_paid': False, 'max_runs': 10, 'seconds': 120},
        })
        source = ('from pathlib import Path\n'
                  'Path("archived_graph_python_executed.txt").write_text("UNSAFE")\n'
                  'Path("figure.svg").write_text("<svg/>")')
        plot = {
            'kind': 'bar', 'metric': 'score', 'style': {'metric': 'score'},
            'data': [{'dataset': 'sample', 'method': 'baseline', 'score': 1}],
            'code_origin': 'custom', 'code': source,
        }
        figure = harness.request('POST', '/api/figures', json={
            'project_id': project['id'], 'title': 'Embedded custom plot', 'data': plot,
        })
        graph = harness.request('GET', f"/api/projects/{project['id']}/graph")
        harness.request('POST', f"/api/projects/{project['id']}/graph/commands", json={
            'request_id': str(uuid.uuid4()), 'expected_revision': graph['revision'],
            'operation': 'add_node', 'targets': [],
            'params': {
                'id': str(uuid.uuid4()), 'title': 'Graph-renderable plot',
                'type': 'experiment', 'branch_id': graph['branches'][0]['id'],
                'config': {
                    'kind': 'figure', 'figure_id': figure['id'],
                    'figure_revision': figure['revision'], 'figure': plot,
                },
            },
        })
        archive = harness.client.post(f"/api/projects/{project['id']}/export", json={})
        assert archive.status_code == 200
        imported = harness.client.post('/api/projects/import', files={
            'file': ('graph-plot.zip', archive.content, 'application/zip'),
        })
        assert imported.status_code == 200, imported.text
        project_id = imported.json()['id']
        imported_figure = harness.request('GET', '/api/figures',
                                          params={'project_id': project_id})[0]
        assert imported_figure['data']['code_origin'] == 'imported_untrusted'
        imported_graph = harness.request('GET', f'/api/projects/{project_id}/graph')
        node = imported_graph['nodes'][0]
        assert node['config']['figure']['code'] == source
        assert node['config']['figure']['code_origin'] == 'imported_untrusted'

        harness.start_worker()
        run = harness.request('POST', f"/api/nodes/{node['id']}/run", json={
            'request_id': 'imported-graph-figure-run', 'scope': 'single',
        })
        assert harness.terminal(run, timeout=50)['status'] == 'completed'
        output = harness.output(run) / 'figure'
        assert (output / 'figure.svg').is_file()
        assert not (output / 'archived_graph_python_executed.txt').exists()
    finally:
        harness.cleanup()
