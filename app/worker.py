"""The background worker. Same image as the API, different command.

Run with:  python -m app.worker

The loop is deliberately boring:

    reap anything a dead worker left behind
    BLMOVE one id off the queue and onto the in-flight list
    take a lease with a TTL
    do the work
    on success  -> done, drop the lease
    on failure  -> failed, and either schedule a retry or bury it

Nothing is ever simply dropped. If this process is killed at any point the
id is still on imgq:processing, its lease key expires on its own, and the
next reap puts it back.
"""
import io
import os
import time
import traceback

from PIL import Image

from . import db
from .jobs import plan_after_failure, target_size, transition
from .queue import (claim, finish, lease_seconds, push, release_duplicate,
                    schedule_retry, take_lease)

# A deliberate stall, so you can kill the worker mid-job and watch the reaper
# do its work. Leave it at 0 for normal running.
SLOW_MS = int(os.environ.get("SLOW_MS", "0"))
POLL_SECONDS = 5


def log(*parts):
    print("[worker]", *parts, flush=True)


def reconcile():
    """Jobs can be in Postgres but not in Redis - the seed data inserts rows
    directly, and a flushed Redis loses the queue. Put them back."""
    from . import cache
    c = cache.client()
    known = set(c.lrange("imgq:queue", 0, -1)) | set(c.lrange("imgq:processing", 0, -1))
    restored = 0
    for row in db.query("SELECT id FROM jobs WHERE state='queued' ORDER BY id"):
        if str(row["id"]) not in known:
            push(row["id"])
            restored += 1
    if restored:
        log(f"reconciled {restored} queued job(s) that were not on the queue")


def reap():
    """Identical in intent to POST /admin/reap, run on every loop."""
    from . import cache
    from .jobs import reclaim
    c = cache.client()
    now = time.time()

    for raw in c.lrange("imgq:processing", 0, -1):
        job_id = int(raw)
        row = db.one("SELECT id, state, attempts, leased_at FROM jobs WHERE id=%s",
                     (job_id,))
        if not row:
            c.lrem("imgq:processing", 0, raw)
            continue
        if c.exists(f"imgq:lease:{job_id}"):
            continue
        decision = reclaim(
            {"state": row["state"], "attempts": row["attempts"],
             "leased_at": row["leased_at"].timestamp() if row["leased_at"] else None},
            now, lease_seconds=0.0001)
        if decision["action"] == "leave":
            continue
        if c.lrem("imgq:processing", 1, raw) != 1:
            continue
        if decision["action"] == "requeue":
            log(f"job {job_id}: lease expired, requeueing "
                f"(attempt {decision['attempts']})")
            db.query("UPDATE jobs SET state='queued', attempts=%s, leased_at=NULL,"
                     " last_error='worker died holding this job', updated_at=now()"
                     " WHERE id=%s", (decision["attempts"], job_id), fetch=False)
            push(job_id)
        else:
            log(f"job {job_id}: lease expired and out of attempts, burying")
            db.query("UPDATE jobs SET state='dead', attempts=%s, leased_at=NULL,"
                     " last_error='worker died and the job is out of attempts',"
                     " updated_at=now() WHERE id=%s",
                     (decision["attempts"], job_id), fetch=False)

    for raw in c.zrangebyscore("imgq:retry", 0, now):
        c.zrem("imgq:retry", raw)
        job_id = int(raw)
        row = db.one("SELECT state FROM jobs WHERE id=%s", (job_id,))
        if row and row["state"] == "failed":
            log(f"job {job_id}: backoff elapsed, requeueing")
            db.query("UPDATE jobs SET state='queued', updated_at=now() WHERE id=%s",
                     (job_id,), fetch=False)
            push(job_id)


def resize(job):
    """The actual work. Raises on anything that is not a readable image."""
    image = Image.open(io.BytesIO(bytes(job["src_data"])))
    image.load()
    src_w, src_h = image.size
    out_w, out_h = target_size(src_w, src_h, job["box_width"], job["box_height"])
    thumb = image.convert("RGB").resize((out_w, out_h), Image.LANCZOS)
    buf = io.BytesIO()
    thumb.save(buf, format="PNG", optimize=True)
    return src_w, src_h, out_w, out_h, buf.getvalue()


def run_one(job_id):
    row = db.one("SELECT id, state, attempts, filename, src_data, box_width,"
                 " box_height FROM jobs WHERE id=%s", (job_id,))
    if not row:
        log(f"job {job_id}: vanished from the database, dropping")
        finish(job_id)
        return
    if row["state"] not in ("queued", "failed"):
        log(f"job {job_id}: state is {row['state']}, not mine to run")
        release_duplicate(job_id)
        return

    state = transition(row["state"], "processing") if row["state"] == "queued" \
        else transition(transition(row["state"], "queued"), "processing")
    take_lease(job_id)
    db.query("UPDATE jobs SET state=%s, leased_at=now(), updated_at=now()"
             " WHERE id=%s", (state, job_id), fetch=False)
    log(f"job {job_id}: processing {row['filename']!r} "
        f"(lease {int(lease_seconds())}s)")

    if SLOW_MS:
        time.sleep(SLOW_MS / 1000.0)

    try:
        src_w, src_h, out_w, out_h, data = resize(row)
    except Exception as exc:
        attempts = row["attempts"] + 1
        plan = plan_after_failure(attempts, time.time())
        message = f"{type(exc).__name__}: {exc}"[:500]
        log(f"job {job_id}: failed ({message}) -> {plan['state']}")
        traceback.print_exc()
        db.query("UPDATE jobs SET state=%s, attempts=%s, last_error=%s,"
                 " leased_at=NULL, updated_at=now() WHERE id=%s",
                 (plan["state"], attempts, message, job_id), fetch=False)
        finish(job_id)
        if plan["then"] == "queued":
            schedule_retry(job_id, plan["retry_at"])
        return

    db.query("UPDATE jobs SET state='done', src_width=%s, src_height=%s,"
             " out_width=%s, out_height=%s, out_data=%s, last_error=NULL,"
             " leased_at=NULL, updated_at=now() WHERE id=%s",
             (src_w, src_h, out_w, out_h, data, job_id), fetch=False)
    finish(job_id)
    log(f"job {job_id}: done {src_w}x{src_h} -> {out_w}x{out_h}")


def main():
    log(f"starting, lease={int(lease_seconds())}s slow={SLOW_MS}ms")
    for _ in range(60):
        try:
            db.query("SELECT 1")
            break
        except Exception:
            time.sleep(1)
    reconcile()
    while True:
        try:
            reap()
            job_id = claim(POLL_SECONDS)
            if job_id is None:
                continue
            run_one(job_id)
        except KeyboardInterrupt:
            log("stopping")
            return
        except Exception:
            log("loop error:")
            traceback.print_exc()
            time.sleep(1)


if __name__ == "__main__":
    main()
