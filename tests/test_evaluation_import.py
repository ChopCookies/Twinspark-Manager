from __future__ import annotations

import httpx
import pytest

from tests.conftest import draft
from twinspark.api.routes_integrations import EvaluationRequest, record_evaluation
from twinspark.controller.app import create_app


def request_for(cluster, report=None):
    profile = cluster.controller.create_profile(draft())
    return EvaluationRequest(alias="default", revision_id=profile.latest().revision_id, results=report or {
        "results": {"gsm8k": {"exact_match,strict-match": 0.5, "exact_match_stderr,strict-match": 0.02}},
        "config": {"model": "local-chat-completions", "model_args": {"model": "default"}, "limit": 20}})


def test_report_records_only_metrics_and_explicit_unverified_revision(cluster):
    req = request_for(cluster)
    req.results["config"]["api_key"] = "secret-api-key"
    req.results["samples"] = [{"prompt": "private prompt", "answer": "private answer"}]
    req.results["results"]["gsm8k"]["alias"] = "untrusted display text"
    job = record_evaluation(cluster.controller, req)
    saved = cluster.controller.store.load_job(job.job_id)
    assert saved.kind == "evaluation" and saved.state.value == "completed"
    assert saved.profile_revision == req.revision_id
    assert saved.payload["evidence"] == "reported" and saved.payload["verified"] is False
    assert saved.payload["sample_limit"] == 20
    assert saved.payload["metrics"]["gsm8k"] == {
        "exact_match,strict-match": 0.5, "exact_match_stderr,strict-match": 0.02}
    assert not any(s in saved.model_dump_json() for s in (
        "secret-api-key", "private prompt", "private answer", "untrusted display text"))
    assert not cluster.controller.get_profile("qwen").latest().known_good
    assert not cluster.controller.active()


@pytest.mark.parametrize("change", ["wrong-alias", "missing-revision", "mismatched-model", "no-metrics",
                                    "huge-metric", "nan", "unsafe-task", "unsafe-metric"])
def test_invalid_reports_do_not_create_jobs(cluster, change):
    req = request_for(cluster)
    if change == "wrong-alias":
        req.alias = "other"
    elif change == "missing-revision":
        req.revision_id = "missing-r1"
    elif change == "mismatched-model":
        req.results["config"]["model_args"] = "model=other,base_url=http://localhost:8000/v1"
    elif change == "no-metrics":
        req.results["results"] = {"gsm8k": {"alias": "label"}}
    elif change in ("huge-metric", "nan"):
        req.results["results"]["gsm8k"]["bad"] = 10**400 if change == "huge-metric" else float("nan")
    elif change == "unsafe-task":
        req.results["results"] = {"<script>unsafe</script>": {"score": 0.5}}
    else:
        req.results["results"]["gsm8k"]["<script>"] = 0.5
    with pytest.raises(ValueError):
        record_evaluation(cluster.controller, req)
    assert not cluster.controller.store.list_jobs()


def test_old_revision_report_stays_attributed_to_old_revision(cluster):
    req = request_for(cluster)
    profile = cluster.controller.get_profile("qwen")
    changed = profile.working_draft().model_copy(deep=True)
    changed.simple.context_length = 4096
    latest = cluster.controller.save_revision(changed)
    job = record_evaluation(cluster.controller, req)
    assert job.profile_revision == req.revision_id != latest.revision_id
    assert not cluster.controller.get_profile("qwen").get_revision(req.revision_id).known_good


def test_oversized_integer_sample_limit_does_not_crash(cluster):
    req = request_for(cluster)
    req.results["config"]["limit"] = 10**400
    job = record_evaluation(cluster.controller, req)
    assert job.payload["sample_limit"] is None


async def test_api_auth_body_bound_and_safe_validation(cluster):
    req = request_for(cluster)
    app = create_app(cluster.controller, management_key="manager-secret", run_startup=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://manager") as client:
        path = "/api/v1/integrations/evaluations"
        assert (await client.post(path, json=req.model_dump())).status_code == 401
        headers = {"x-api-key": "manager-secret"}
        assert (await client.post(path, json=req.model_dump(), headers=headers)).status_code == 201
        bad = await client.post(path, content=b'{"api_key":"secret-client-input"}', headers=headers)
        assert bad.status_code == 422
        assert "secret-client-input" not in bad.text
        huge = await client.post(path, content=b" " * (1024 * 1024 + 1), headers=headers)
        assert huge.status_code == 413
