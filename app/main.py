import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import cache, db
from .jobs import (JobError, MAX_ATTEMPTS, STATES, progress, reclaim,
                   summarise, transition, validate_box, validate_upload)
from .queue import (PROCESSING, QUEUE, RETRY, lease_key, lease_seconds, push)

app = FastAPI(title="image-queue")


@app.get("/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as e:
        out["pg_error"] = str(e)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)
    return out if out["postgres"] and out["redis"] else JSONResponse(out, status_code=503)


def _job(job_id):
    row = db.one("SELECT id, state, attempts, filename, content_type, src_bytes,"
                 " src_width, src_height, box_width, box_height, out_width,"
                 " out_height, last_error, leased_at, created_at, updated_at,"
                 " (out_data IS NOT NULL) AS has_result"
                 " FROM jobs WHERE id=%s", (job_id,))
    if not row:
        raise HTTPException(404, "no such job")
    return row


def _public(row):
    return {"job_id": row["id"], "state": row["state"],
            "progress": progress(row["state"]),
            "attempts": row["attempts"], "max_attempts": MAX_ATTEMPTS,
            "filename": row["filename"], "size_bytes": row["src_bytes"],
            "source": {"width": row["src_width"], "height": row["src_height"]},
            "requested_box": {"width": row["box_width"], "height": row["box_height"]},
            "result": ({"width": row["out_width"], "height": row["out_height"]}
                       if row["state"] == "done" else None),
            "result_available": bool(row.get("has_result")),
            "last_error": row["last_error"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@app.post("/jobs", status_code=202)
async def submit(request: Request, width: int = 320, height: int = 320,
                 filename: str = "upload.bin"):
    """Takes the raw image body. Returns immediately - a worker does the work.

    Deliberately not a multipart form: that would need python-multipart, and
    `curl --data-binary @photo.png` is a simpler thing to explain.
    """
    body = await request.body()
    try:
        size = validate_upload(len(body), request.headers.get("content-type"))
        box_w, box_h = validate_box(width, height)
    except JobError as e:
        raise HTTPException(400, str(e))

    row = db.one(
        "INSERT INTO jobs (state, filename, content_type, src_bytes, src_data,"
        " box_width, box_height) VALUES ('queued',%s,%s,%s,%s,%s,%s) RETURNING id",
        (filename, request.headers.get("content-type", ""), size, body, box_w, box_h))
    push(row["id"])
    return {"job_id": row["id"], "state": "queued", "poll": f"/jobs/{row['id']}",
            "size_bytes": size, "box": {"width": box_w, "height": box_h}}


@app.get("/jobs")
def list_jobs(state: str = "", limit: int = 50):
    if state and state not in STATES:
        raise HTTPException(400, f"state must be one of {', '.join(STATES)}")
    if not 1 <= limit <= 500:
        raise HTTPException(400, "limit must be between 1 and 500")
    cols = ("SELECT id, state, attempts, filename, src_bytes, src_width,"
            " src_height, out_width, out_height, last_error, leased_at,"
            " created_at, updated_at, content_type, box_width, box_height,"
            " (out_data IS NOT NULL) AS has_result FROM jobs")
    if state:
        rows = db.query(cols + " WHERE state=%s ORDER BY id DESC LIMIT %s",
                        (state, limit))
    else:
        rows = db.query(cols + " ORDER BY id DESC LIMIT %s", (limit,))
    c = cache.client()
    return {"jobs": [_public(r) for r in rows],
            "summary": summarise(db.query("SELECT state FROM jobs")),
            "queue": {"waiting": c.llen(QUEUE), "in_flight": c.llen(PROCESSING),
                      "waiting_on_backoff": c.zcard(RETRY)}}


@app.get("/jobs/{job_id}")
def show(job_id: int):
    row = _job(job_id)
    out = _public(row)
    if row["state"] == "processing":
        ttl = cache.client().ttl(lease_key(job_id))
        out["lease_expires_in"] = ttl if ttl and ttl > 0 else 0
    if row["state"] == "done" and row["has_result"]:
        out["result_url"] = f"/jobs/{job_id}/result"
    return out


@app.get("/jobs/{job_id}/result")
def result(job_id: int):
    row = db.one("SELECT state, out_data, out_width, out_height FROM jobs WHERE id=%s",
                 (job_id,))
    if not row:
        raise HTTPException(404, "no such job")
    if row["state"] != "done":
        raise HTTPException(409, f"that job is {row['state']}, not done")
    if row["out_data"] is None:
        raise HTTPException(409, "that job has no stored image")
    return Response(bytes(row["out_data"]), media_type="image/png", headers={
        "X-Image-Width": str(row["out_width"]),
        "X-Image-Height": str(row["out_height"])})


@app.post("/jobs/{job_id}/retry")
def retry(job_id: int):
    """Push a failed job back onto the queue by hand.

    `done` and `dead` are terminal, so this answers 409 for them. If you think
    a dead job deserves another go, that is a new job.
    """
    row = _job(job_id)
    try:
        transition(row["state"], "queued")
    except JobError as e:
        raise HTTPException(409, str(e))
    db.query("UPDATE jobs SET state='queued', last_error=NULL, leased_at=NULL,"
             " updated_at=now() WHERE id=%s", (job_id,), fetch=False)
    cache.client().zrem(RETRY, str(job_id))
    push(job_id)
    return {"job_id": job_id, "state": "queued", "attempts": row["attempts"]}


@app.post("/admin/reap")
def reap():
    """Put back anything a dead worker was holding.

    The worker runs this on a loop too; it is exposed so you can watch it
    work. Nothing here trusts the worker to have cleaned up after itself.
    """
    c = cache.client()
    now = time.time()
    actions = []

    for raw in c.lrange(PROCESSING, 0, -1):
        job_id = int(raw)
        row = db.one("SELECT id, state, attempts, leased_at FROM jobs WHERE id=%s",
                     (job_id,))
        if not row:
            c.lrem(PROCESSING, 0, raw)
            actions.append({"job_id": job_id, "action": "dropped",
                            "reason": "no such job"})
            continue
        alive = c.exists(lease_key(job_id))
        leased = None if alive else (row["leased_at"].timestamp()
                                     if row["leased_at"] else None)
        if alive:
            decision = {"action": "leave", "reason": "lease is still live"}
        else:
            decision = reclaim({"state": row["state"], "attempts": row["attempts"],
                                "leased_at": leased}, now, lease_seconds=0.0001)
        if decision["action"] == "leave":
            continue
        # Claim the entry. If somebody else (the worker's own reaper) removed
        # it first, it is theirs to requeue - doing it twice runs the job twice.
        if c.lrem(PROCESSING, 1, raw) != 1:
            continue
        if decision["action"] == "requeue":
            db.query("UPDATE jobs SET state='queued', attempts=%s, leased_at=NULL,"
                     " last_error='worker died holding this job', updated_at=now()"
                     " WHERE id=%s", (decision["attempts"], job_id), fetch=False)
            push(job_id)
        else:
            db.query("UPDATE jobs SET state='dead', attempts=%s, leased_at=NULL,"
                     " last_error='worker died and the job is out of attempts',"
                     " updated_at=now() WHERE id=%s", (decision["attempts"], job_id),
                     fetch=False)
        actions.append({"job_id": job_id, **decision})

    due = c.zrangebyscore(RETRY, 0, now)
    for raw in due:
        c.zrem(RETRY, raw)
        job_id = int(raw)
        row = db.one("SELECT state FROM jobs WHERE id=%s", (job_id,))
        if not row or row["state"] != "failed":
            continue
        db.query("UPDATE jobs SET state='queued', updated_at=now() WHERE id=%s",
                 (job_id,), fetch=False)
        push(job_id)
        actions.append({"job_id": job_id, "action": "backoff_elapsed"})

    return {"checked_in_flight": c.llen(PROCESSING) + len(actions),
            "actions": actions, "lease_seconds": lease_seconds()}


@app.get("/stats")
def stats():
    c = cache.client()
    return {"summary": summarise(db.query("SELECT state FROM jobs")),
            "queue": {"waiting": c.llen(QUEUE), "in_flight": c.llen(PROCESSING),
                      "waiting_on_backoff": c.zcard(RETRY)},
            "slowest": db.query(
                "SELECT id, filename, state,"
                " extract(epoch FROM updated_at - created_at) AS seconds"
                " FROM jobs WHERE state='done' ORDER BY seconds DESC LIMIT 5")}
