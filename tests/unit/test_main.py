import datetime
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    with patch("app.db.query", return_value=[{"1": 1}]), \
         patch("app.cache.client") as mock_cache:
        mock_cache.return_value.ping.return_value = True
        res = client.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "postgres": True, "redis": True}


def test_health_pg_failure():
    with patch("app.db.query", side_effect=Exception("pg connection refused")), \
         patch("app.cache.client") as mock_cache:
        mock_cache.return_value.ping.return_value = True
        res = client.get("/health")
        assert res.status_code == 503
        data = res.json()
        assert data["postgres"] is False
        assert "pg_error" in data


def test_health_redis_failure():
    with patch("app.db.query", return_value=[{"1": 1}]), \
         patch("app.cache.client") as mock_cache:
        mock_cache.return_value.ping.side_effect = Exception("redis connection refused")
        res = client.get("/health")
        assert res.status_code == 503
        data = res.json()
        assert data["redis"] is False
        assert "redis_error" in data


def test_submit_job_valid():
    with patch("app.db.one", return_value={"id": 42}), \
         patch("app.main.push") as mock_push:
        res = client.post(
            "/jobs?width=200&height=200&filename=test.png",
            content=b"dummy image bytes",
            headers={"content-type": "image/png"},
        )
        assert res.status_code == 202
        data = res.json()
        assert data["job_id"] == 42
        assert data["state"] == "queued"
        mock_push.assert_called_once_with(42)


def test_submit_job_invalid_upload():
    # Empty body
    res = client.post(
        "/jobs?width=200&height=200",
        content=b"",
        headers={"content-type": "image/png"},
    )
    assert res.status_code == 400
    assert "upload is empty" in res.json()["detail"]


def test_list_jobs():
    sample_row = {
        "id": 1,
        "state": "done",
        "attempts": 1,
        "filename": "pic.png",
        "src_bytes": 1000,
        "src_width": 200,
        "src_height": 200,
        "out_width": 100,
        "out_height": 100,
        "last_error": None,
        "leased_at": None,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "content_type": "image/png",
        "box_width": 100,
        "box_height": 100,
        "has_result": True,
    }
    with patch("app.db.query", side_effect=[[sample_row], [{"state": "done"}]]), \
         patch("app.cache.client") as mock_cache:
        mock_cache.return_value.llen.return_value = 0
        mock_cache.return_value.zcard.return_value = 0
        res = client.get("/jobs")
        assert res.status_code == 200
        data = res.json()
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["job_id"] == 1


def test_list_jobs_invalid_state():
    res = client.get("/jobs?state=nonexistent")
    assert res.status_code == 400
    assert "state must be one of" in res.json()["detail"]


def test_list_jobs_invalid_limit():
    res = client.get("/jobs?limit=1000")
    assert res.status_code == 400


def test_show_job_found():
    sample_row = {
        "id": 5,
        "state": "processing",
        "attempts": 1,
        "filename": "pic.png",
        "src_bytes": 1000,
        "src_width": 200,
        "src_height": 200,
        "box_width": 100,
        "box_height": 100,
        "out_width": None,
        "out_height": None,
        "last_error": None,
        "leased_at": datetime.datetime.now(datetime.timezone.utc),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "has_result": False,
    }
    with patch("app.db.one", return_value=sample_row), \
         patch("app.cache.client") as mock_cache:
        mock_cache.return_value.ttl.return_value = 15
        res = client.get("/jobs/5")
        assert res.status_code == 200
        assert res.json()["lease_expires_in"] == 15


def test_show_job_not_found():
    with patch("app.db.one", return_value=None):
        res = client.get("/jobs/999")
        assert res.status_code == 404


def test_result_success():
    sample_row = {
        "state": "done",
        "out_data": b"\x89PNG\r\n\x1a\nfakeimage",
        "out_width": 100,
        "out_height": 100,
    }
    with patch("app.db.one", return_value=sample_row):
        res = client.get("/jobs/1/result")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        assert res.headers["x-image-width"] == "100"
        assert res.content == b"\x89PNG\r\n\x1a\nfakeimage"


def test_result_not_done():
    with patch("app.db.one", return_value={"state": "queued", "out_data": None}):
        res = client.get("/jobs/1/result")
        assert res.status_code == 409


def test_retry_success():
    sample_row = {
        "id": 10,
        "state": "failed",
        "attempts": 1,
        "filename": "f.png",
        "content_type": "",
        "src_bytes": 10,
        "src_width": None,
        "src_height": None,
        "box_width": 100,
        "box_height": 100,
        "out_width": None,
        "out_height": None,
        "last_error": "error",
        "leased_at": None,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "has_result": False,
    }
    with patch("app.db.one", return_value=sample_row), \
         patch("app.db.query"), \
         patch("app.cache.client"), \
         patch("app.main.push") as mock_push:
        res = client.post("/jobs/10/retry")
        assert res.status_code == 200
        assert res.json()["state"] == "queued"
        mock_push.assert_called_once_with(10)


def test_retry_terminal_state_409():
    sample_row = {
        "id": 11,
        "state": "done",
        "attempts": 1,
        "filename": "f.png",
        "content_type": "",
        "src_bytes": 10,
        "src_width": None,
        "src_height": None,
        "box_width": 100,
        "box_height": 100,
        "out_width": None,
        "out_height": None,
        "last_error": None,
        "leased_at": None,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "has_result": True,
    }
    with patch("app.db.one", return_value=sample_row):
        res = client.post("/jobs/11/retry")
        assert res.status_code == 409


def test_admin_reap():
    mock_cache = MagicMock()
    mock_cache.lrange.return_value = ["100"]
    mock_cache.exists.return_value = False
    mock_cache.lrem.return_value = 1
    mock_cache.zrangebyscore.return_value = ["200"]
    mock_cache.llen.return_value = 0

    def fake_db_one(sql, params):
        if params[0] == 100:
            return {"id": 100, "state": "processing", "attempts": 0, "leased_at": None}
        if params[0] == 200:
            return {"state": "failed"}
        return None

    with patch("app.cache.client", return_value=mock_cache), \
         patch("app.db.one", side_effect=fake_db_one), \
         patch("app.db.query"), \
         patch("app.main.push"):
        res = client.post("/admin/reap")
        assert res.status_code == 200
        data = res.json()
        assert len(data["actions"]) == 2


def test_stats():
    with patch("app.db.query", side_effect=[[{"state": "done"}], []]), \
         patch("app.cache.client") as mock_cache:
        mock_cache.return_value.llen.return_value = 1
        mock_cache.return_value.zcard.return_value = 0
        res = client.get("/stats")
        assert res.status_code == 200
        data = res.json()
        assert "summary" in data
        assert "queue" in data
        assert "slowest" in data
