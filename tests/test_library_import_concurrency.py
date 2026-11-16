"""The same source identifier must keep one Library identity across concurrent importers."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("api", "agent_and_api", "agent_distinct_actions", "agent_same_action")


@pytest.mark.parametrize("scenario", CASES)
def test_library_import_concurrency_sqlite(tmp_path, scenario):
    env = {**os.environ, "FOREST_DATABASE_URL": "sqlite:///" + str(tmp_path / "library.sqlite"),
           "FOREST_DATA_DIR": str(tmp_path / "data"), "FOREST_MODEL": "", "PYTHONPATH": str(ROOT)}
    result = subprocess.run([sys.executable, __file__, scenario], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("scenario", CASES)
def test_library_import_concurrency_postgresql(tmp_path, scenario):
    url = os.environ.get("FOREST_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set FOREST_TEST_POSTGRES_URL for real PostgreSQL import races")
    env = {**os.environ, "FOREST_DATABASE_URL": url,
           "FOREST_DATA_DIR": str(tmp_path / "data"), "FOREST_MODEL": "", "PYTHONPATH": str(ROOT)}
    result = subprocess.run([sys.executable, __file__, scenario], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


def exercise(scenario):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, BrokenBarrierError
    from fastapi.testclient import TestClient
    from research.literature import sources, agent_tools
    from services.api.main import app
    from services.api.common import project_dir
    from services.api.db import SourcePaper, SourcePassage, Session
    from sqlalchemy import select

    doi = "10.9999/forest-concurrent-identical"
    control_doi = "10.9999/forest-seed-for-race"
    with TestClient(app) as client:
        created = client.post("/api/projects", json={
            "name": "Simultaneous DOI import",
            "goal": "A paper is one record regardless of simultaneous clicks",
            "mode": "manual"})
        assert created.status_code == 200, created.text
        pid = created.json()["id"]

        def import_api(title, passage):
            response = client.post("/api/library/import", json={"project_id": pid,
                "paper": {"title": title, "doi": doi,
                          "passages": [{"text": passage, "page": 1}]}})
            assert response.status_code == 200, response.text
            return {"id": response.json()["id"], "replayed": False}

        control = client.post("/api/library/import", json={"project_id": pid,
            "paper": {"title": "Different older article", "doi": control_doi}})
        assert control.status_code == 200, control.text

        original_api_match = sources.matches_publication
        original_agent_match = agent_tools.matches_publication
        original_agent_import = agent_tools.import_identifier
        barrier = Barrier(2)

        def race_after_lookup(incoming, saved, title):
            result = original_api_match(incoming, saved, title)
            if incoming.get("doi") == doi and saved.get("doi") == control_doi:
                # Existing unlocked code reads the same result set before the
                # two inserts; serialized code safely times out on the first
                # import while its second caller waits for the project lock.
                try:
                    barrier.wait(timeout=0.8)
                except BrokenBarrierError:
                    pass
            return result

        def offline_identifier(identifier, output_dir):
            assert identifier == doi
            return {
                "title": "Imported by Agent",
                "doi": doi,
                "source": "test_metadata",
                "read_scope": "metadata",
                "passages": [{"text": Path(output_dir).name, "page": 1}],
            }

        def import_agent(action):
            answer = agent_tools.import_source(pid, project_dir(pid), {"identifier": doi}, action)
            return {"id": answer["source"]["id"], "replayed": answer.get("replayed", False)}

        sources.matches_publication = race_after_lookup
        agent_tools.matches_publication = race_after_lookup
        agent_tools.import_identifier = offline_identifier
        try:
            if scenario == "api":
                actions = [lambda: import_api("Browser A", "A VERSION"),
                           lambda: import_api("Browser B", "B VERSION")]
            elif scenario == "agent_and_api":
                actions = [lambda: import_api("Browser A", "A VERSION"),
                           lambda: import_agent("agent-import-123")]
            elif scenario == "agent_distinct_actions":
                actions = [lambda: import_agent("agent-import-123"),
                           lambda: import_agent("agent-import-456")]
            else:
                actions = [lambda: import_agent("agent-import-same"),
                           lambda: import_agent("agent-import-same")]
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda f: f(), actions))
        finally:
            sources.matches_publication = original_api_match
            agent_tools.matches_publication = original_agent_match
            agent_tools.import_identifier = original_agent_import

        assert results[0]["id"] == results[1]["id"], results
        with Session() as session:
            records = list(session.scalars(select(SourcePaper).where(
                SourcePaper.project_id == pid)))
            assert len(records) == 2, [(r.id, r.data.get("doi")) for r in records]
            imports = [r for r in records if r.data.get("doi") == doi]
            assert len(imports) == 1
            passages = [r for r in session.scalars(select(SourcePassage).where(
                SourcePassage.project_id == pid)) if r.data.get("paper_id") == imports[0].id]
            if scenario == "agent_same_action":
                assert sorted(r["replayed"] for r in results) == [False, True], results
                assert len(passages) == 1, passages
            elif scenario == "agent_distinct_actions":
                assert len(passages) == 2, passages
            else:
                assert len(passages) in (1, 2), passages
        print(f"{scenario}: single DOI record retained from two concurrent imports")


if __name__ == "__main__":
    exercise(sys.argv[1])
