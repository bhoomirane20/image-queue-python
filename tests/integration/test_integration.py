import io
import os
import subprocess
import sys
import time
from PIL import Image
import psycopg
from fastapi.testclient import TestClient
import pytest
import redis

from app.main import app

pytestmark = pytest.mark.integration
client = TestClient(app)


def make_test_png(width=300, height=200, color=(100, 150, 200)):
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=color)
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_health():
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "postgres": True, "redis": True}


def test_submit_job():
    png_bytes = make_test_png()
    res = client.post(
        "/jobs?width=150&height=150&filename=test.png",
        content=png_bytes,
        headers={"content-type": "image/png"},
    )
    assert res.status_code == 202
    data = res.json()
    assert "job_id" in data
    assert data["state"] == "queued"
    assert data["box"] == {"width": 150, "height": 150}

    # Verify job id is in Redis queue
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    queued_jobs = r.lrange("imgq:queue", 0, -1)
    assert str(data["job_id"]) in queued_jobs


def test_get_job():
    res = client.get("/jobs/1")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == 1
    assert data["state"] == "done"
    assert data["filename"] == "graduation-group.jpg"
    assert data["result_available"] is True

    # 404 for non-existent job
    res_404 = client.get("/jobs/99999")
    assert res_404.status_code == 404


def test_get_jobs_and_filter():
    res = client.get("/jobs")
    assert res.status_code == 200
    data = res.json()
    assert "jobs" in data
    assert len(data["jobs"]) >= 5
    assert "summary" in data
    assert "queue" in data

    # Filter by state
    res_done = client.get("/jobs?state=done")
    assert res_done.status_code == 200
    done_jobs = res_done.json()["jobs"]
    assert len(done_jobs) >= 3
    for j in done_jobs:
        assert j["state"] == "done"


def test_stats():
    res = client.get("/stats")
    assert res.status_code == 200
    data = res.json()
    assert "summary" in data
    assert "queue" in data
    assert "slowest" in data
    assert len(data["slowest"]) >= 1


def test_retry_endpoint():
    # Insert a failed job into postgres
    db_url = os.environ["DATABASE_URL"]
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (state, attempts, filename, last_error) "
                "VALUES ('failed', 1, 'retry-test.png', 'temp error') RETURNING id"
            )
            job_id = cur.fetchone()[0]

    # Retry the failed job
    res = client.post(f"/jobs/{job_id}/retry")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job_id
    assert data["state"] == "queued"

    # Confirm it's back in Redis queue
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    assert str(job_id) in r.lrange("imgq:queue", 0, -1)

    # Attempting to retry a terminal job (e.g. job 1 which is done) must 409
    res_conflict = client.post("/jobs/1/retry")
    assert res_conflict.status_code == 409


def test_admin_reap_expired_lease():
    # Simulate an expired lease:
    # 1. Job in DB is in 'processing' state with an expired leased_at (or null)
    db_url = os.environ["DATABASE_URL"]
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (state, attempts, filename, leased_at) "
                "VALUES ('processing', 0, 'crashed-job.png', now() - interval '60 seconds') RETURNING id"
            )
            job_id = cur.fetchone()[0]

    # 2. In Redis, place the ID in imgq:processing with NO lease key
    r = redis.Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    r.rpush("imgq:processing", str(job_id))
    r.delete(f"imgq:lease:{job_id}")

    # Call /admin/reap
    res = client.post("/admin/reap")
    assert res.status_code == 200
    reap_data = res.json()

    # Verify action was taken for this job
    actions = [a for a in reap_data["actions"] if a.get("job_id") == job_id]
    assert len(actions) == 1
    assert actions[0]["action"] == "requeue"
    assert actions[0]["attempts"] == 1

    # Verify job is now queued in Postgres with attempts incremented
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state, attempts FROM jobs WHERE id = %s", (job_id,))
            row = cur.fetchone()
            assert row[0] == "queued"
            assert row[1] == 1

    # Verify job is now in imgq:queue and removed from imgq:processing
    assert str(job_id) in r.lrange("imgq:queue", 0, -1)
    assert str(job_id) not in r.lrange("imgq:processing", 0, -1)


def test_job_processing_to_done_with_worker_subprocess():
    png_bytes = make_test_png(width=300, height=200)

    # Submit job
    res = client.post(
        "/jobs?width=150&height=150&filename=processed-pic.png",
        content=png_bytes,
        headers={"content-type": "image/png"},
    )
    assert res.status_code == 202
    job_id = res.json()["job_id"]

    # Start worker in a subprocess
    worker_env = os.environ.copy()
    worker_env["SLOW_MS"] = "0"
    worker_env["LEASE_SECONDS"] = "30"

    worker_proc = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        env=worker_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Poll for job to reach 'done'
        deadline = time.time() + 15.0
        final_state = None
        while time.time() < deadline:
            poll_res = client.get(f"/jobs/{job_id}")
            if poll_res.status_code == 200:
                final_state = poll_res.json()["state"]
                if final_state == "done":
                    break
            time.sleep(0.5)

        assert final_state == "done", f"Job failed to finish in time. State was: {final_state}"

        # Fetch result
        result_res = client.get(f"/jobs/{job_id}/result")
        assert result_res.status_code == 200
        assert result_res.headers["content-type"] == "image/png"
        assert result_res.headers["x-image-width"] == "150"
        assert result_res.headers["x-image-height"] == "100"

        # Validate image format and dimensions with Pillow
        out_img = Image.open(io.BytesIO(result_res.content))
        assert out_img.format == "PNG"
        assert out_img.size == (150, 100)
    finally:
        worker_proc.terminate()
        try:
            worker_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            worker_proc.kill()
