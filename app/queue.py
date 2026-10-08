"""The Redis queue itself. Shared by the API and the worker.

Three structures, and the reason for each:

  imgq:queue       a list. RPUSH to submit, BLMOVE to take. BLMOVE is what
                   makes a crash survivable: it moves the id onto
                   imgq:processing in the same operation that takes it off
                   the queue, so there is no instant where the id exists
                   only inside a worker's memory.
  imgq:processing  a list of ids a worker claims to be working on.
  imgq:lease:<id>  a key with a TTL, written right after the BLMOVE. If the
                   worker dies the key expires; an id sitting in
                   imgq:processing with no lease key is a job nobody is
                   doing, and the reaper puts it back.
  imgq:retry       a sorted set scored by the time a failed job is allowed
                   to be tried again.
"""
import os

from . import cache

QUEUE = "imgq:queue"
PROCESSING = "imgq:processing"
RETRY = "imgq:retry"
LEASE = "imgq:lease"


def lease_seconds():
    return float(os.environ.get("LEASE_SECONDS", "30"))


def lease_key(job_id):
    return f"{LEASE}:{job_id}"


def push(job_id):
    """Put a job on the back of the queue."""
    cache.client().rpush(QUEUE, str(job_id))


def push_front(job_id):
    cache.client().lpush(QUEUE, str(job_id))


def schedule_retry(job_id, at):
    cache.client().zadd(RETRY, {str(job_id): float(at)})


def claim(timeout=5):
    """Take the next job, atomically recording that we have it."""
    raw = cache.client().blmove(QUEUE, PROCESSING, timeout, "LEFT", "RIGHT")
    return int(raw) if raw else None


def take_lease(job_id):
    cache.client().set(lease_key(job_id), "1", ex=int(lease_seconds()))


def renew_lease(job_id):
    cache.client().expire(lease_key(job_id), int(lease_seconds()))


def finish(job_id):
    """Drop the job from the in-flight list and release its lease.

    Only the worker that holds the job calls this.
    """
    c = cache.client()
    c.lrem(PROCESSING, 0, str(job_id))
    c.delete(lease_key(job_id))


def release_duplicate(job_id):
    """Put down a job that turned out not to be ours.

    Removes ONE copy of the id and leaves the lease alone: with two workers
    running, the lease belongs to whoever is actually doing the work, and
    deleting it here would hand their job to the reaper.
    """
    cache.client().lrem(PROCESSING, 1, str(job_id))


def depth():
    c = cache.client()
    return {"waiting": c.llen(QUEUE), "in_flight": c.llen(PROCESSING),
            "waiting_on_backoff": c.zcard(RETRY)}
