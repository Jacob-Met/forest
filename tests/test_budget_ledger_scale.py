"""Budget aggregation must not hydrate the lifetime request ledger in Python."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_provider_budget_queries_aggregate_without_loading_history(tmp_path):
    env = {**os.environ, "FOREST_DATABASE_URL": "sqlite:///" + str(tmp_path / "ledger.db"),
           "FOREST_DATA_DIR": str(tmp_path / "data"), "PYTHONPATH": str(ROOT)}
    result = subprocess.run([sys.executable, __file__, "check"], env=env, cwd=ROOT,
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr


if __name__ == "__main__":
    from sqlalchemy import event
    from services.api.db import migrate, Session, Provider, ModelRequest, engine, asdict
    from research.agents.budget import make_request_guard, usage_summary
    migrate()
    pricing = {"input_per_million": 1, "output_per_million": 1, "currency": "USD"}
    with Session.begin() as session:
        provider = Provider(name="Bounded provider ledger", kind="openai",
                            base_url="https://example.invalid", model="offline",
                            allow_paid=True, config={"budget_usd": 20, "pricing": pricing})
        session.add(provider)
        session.flush()
        snapshot = asdict(provider, True)
        records = []
        for i in range(1234):
            status = ("settled", "reserved", "uncertain", "rejected", "completed")[i % 5]
            records.append(ModelRequest(provider_id=provider.id, model="offline", status=status,
                                        estimated_microusd=2500 if status == "settled" else None,
                                        reserved_microusd=1000, details={
                                            "cost_source": "unpriced_local_provider" if status == "completed" else "configured_rates_estimate"}))
        session.add_all(records)
    queries = []
    @event.listens_for(engine, "before_cursor_execute")
    def observe(conn, cursor, statement, parameters, context, executemany):
        if "FROM model_requests" in statement:
            queries.append(statement)
            if "SELECT " in statement.upper() and not any(token in statement.upper() for token in ("LIMIT", "SUM(", "COUNT(")):
                raise AssertionError("Provider budget hydrated the entire request history")
    try:
        summary = usage_summary(provider.id)
        assert summary["request_count"] == 1234
        assert len(summary["requests"]) == 100
        assert summary["uncertain_requests"] == 247
        assert summary["unknown_cost_requests"] == 246
        assert summary["estimated_cost_usd"] is None
        assert summary["cost_source"] == "mixed_known_and_unknown"
        assert summary["known_cost_usd"] == 247 * .0025
        assert summary["reserved_usd"] == (247 + 247) * .001
        guard = make_request_guard(snapshot)
        request = guard({"phase": "before", "model": "offline", "input_bytes": 0,
                         "max_output_tokens": 1, "pricing": pricing})
        assert request and len(queries) >= 2
        assert len([statement for statement in queries if "LIMIT" in statement.upper()]) == 1
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    print("Budget ledger aggregation and bounded provider history passed")
