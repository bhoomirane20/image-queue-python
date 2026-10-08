"""Pure job state machine and resize arithmetic. No database, no HTTP, no clock.

A job moves through five states:

    queued -> processing -> done
                 |
                 +-------> failed -> queued   (retry, after a backoff)
                 |            |
                 |            +--------------> dead   (gave up)
                 +--------------------------> dead   (last attempt, or the
                 |                                    reaper buried it)
                 +--------> queued   (a worker died holding the job and the
                                      reaper put it back)

`processing -> dead` is not a shortcut: it is what the worker writes on the
failure that uses up the last attempt, and what the reaper writes when it
reclaims a job that has none left.

Every function takes `now` as an argument. Nothing here reads the clock, so
lease expiry and backoff can be tested without waiting for them.

What is worth testing
---------------------
  * `can_transition("queued","processing")` is True
  * `can_transition("done","processing")` is False - a finished job is finished
  * `can_transition("processing","dead")` is True - the worker writes exactly
    that on the failure that uses up the last attempt
  * `done` and `dead` are terminal: nothing leaves them, including to themselves
  * `transition` raises JobError with both states named, rather than returning
    None, so a bad transition cannot be ignored by accident
  * an unknown state is an error, not False
  * `plan_after_failure`: attempt 1 of 3 retries, attempt 3 of 3 buries
  * `backoff_seconds` grows and then stops at `cap`, and attempt 1 is not zero
  * `lease_expired` is exclusive at the boundary - a lease that ends exactly
    now has expired
  * `reclaim` returns "requeue" for an expired lease on a processing job,
    "bury" once the attempts are used up, and "leave" for a live lease
  * `reclaim` on a job that is already done returns "leave" - a late reaper
    must never resurrect a finished job
  * `target_size` preserves the aspect ratio, never upscales, and never
    returns a zero dimension for an extreme ratio such as 4000x3
  * `target_size` on an image already inside the box returns it unchanged
  * `validate_upload` rejects empty bodies and anything over the size limit
  * `summarise` counts every state, including ones with no jobs
"""

STATES = ("queued", "processing", "done", "failed", "dead")
TERMINAL = frozenset({"done", "dead"})

TRANSITIONS = {
    "queued":     frozenset({"processing", "dead"}),
    "processing": frozenset({"done", "failed", "queued", "dead"}),
    "failed":     frozenset({"queued", "dead"}),
    "done":       frozenset(),
    "dead":       frozenset(),
}

MAX_ATTEMPTS = 3
LEASE_SECONDS = 30.0
BACKOFF_BASE = 2.0
BACKOFF_CAP = 60.0
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EDGE = 4096


class JobError(ValueError):
    pass


def check_state(state):
    if state not in TRANSITIONS:
        raise JobError(f"{state!r} is not a job state")
    return state


def is_terminal(state):
    return check_state(state) in TERMINAL


def can_transition(current, nxt):
    check_state(current)
    check_state(nxt)
    return nxt in TRANSITIONS[current]


def transition(current, nxt):
    """Return the new state, or refuse loudly."""
    if not can_transition(current, nxt):
        raise JobError(f"a job cannot go from {current!r} to {nxt!r}")
    return nxt


def should_retry(attempts, max_attempts=MAX_ATTEMPTS):
    """`attempts` is the number of tries already spent, including the one
    that just failed."""
    if attempts < 0:
        raise JobError("attempts cannot be negative")
    return attempts < max_attempts


def backoff_seconds(attempt, base=BACKOFF_BASE, cap=BACKOFF_CAP):
    """Wait longer after each failure, but never forever."""
    if attempt < 1:
        raise JobError("attempts are counted from 1")
    return float(min(cap, base ** attempt))


def plan_after_failure(attempts, now, max_attempts=MAX_ATTEMPTS,
                       base=BACKOFF_BASE, cap=BACKOFF_CAP):
    """What happens to a job that just blew up. `attempts` already includes
    this failure."""
    if should_retry(attempts, max_attempts):
        wait = backoff_seconds(attempts, base, cap)
        return {"state": "failed", "then": "queued", "attempts": attempts,
                "retry_in": wait, "retry_at": float(now) + wait}
    return {"state": "dead", "then": None, "attempts": attempts,
            "retry_in": 0.0, "retry_at": None}


def lease_deadline(leased_at, lease_seconds=LEASE_SECONDS):
    if lease_seconds <= 0:
        raise JobError("a lease must last at least one second")
    return float(leased_at) + float(lease_seconds)


def lease_expired(leased_at, now, lease_seconds=LEASE_SECONDS):
    """A lease that ends exactly now has expired. Being generous here is how
    two workers end up holding the same job."""
    return float(now) >= lease_deadline(leased_at, lease_seconds)


def reclaim(job, now, lease_seconds=LEASE_SECONDS, max_attempts=MAX_ATTEMPTS):
    """Decide what the reaper does with one job it found mid-flight.

    `job` is {"state":..., "attempts":..., "leased_at": float or None}.
    Returns {"action": "leave"|"requeue"|"bury", ...}.
    """
    state = check_state(job["state"])
    if state != "processing":
        return {"action": "leave", "reason": f"state is {state}"}
    leased_at = job.get("leased_at")
    if leased_at is None:
        return {"action": "requeue", "reason": "no lease recorded",
                "attempts": int(job.get("attempts", 0)) + 1}
    if not lease_expired(leased_at, now, lease_seconds):
        return {"action": "leave", "reason": "lease is still live",
                "expires_in": round(lease_deadline(leased_at, lease_seconds)
                                    - float(now), 3)}
    attempts = int(job.get("attempts", 0)) + 1
    if should_retry(attempts, max_attempts):
        return {"action": "requeue", "reason": "lease expired", "attempts": attempts}
    return {"action": "bury", "reason": "lease expired and out of attempts",
            "attempts": attempts}


def target_size(width, height, max_width, max_height):
    """Fit an image inside a box without distorting it or blowing it up."""
    for name, value in (("width", width), ("height", height),
                        ("max_width", max_width), ("max_height", max_height)):
        if int(value) < 1:
            raise JobError(f"{name} must be at least 1 pixel")
    width, height = int(width), int(height)
    scale = min(float(max_width) / width, float(max_height) / height, 1.0)
    # round() alone can produce a zero edge on something like 4000x3.
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def validate_upload(size_bytes, content_type=None, max_bytes=MAX_UPLOAD_BYTES):
    """Cheap checks worth doing before anything touches the queue."""
    if size_bytes is None or int(size_bytes) <= 0:
        raise JobError("the upload is empty")
    if int(size_bytes) > max_bytes:
        raise JobError(f"the upload is larger than {max_bytes} bytes")
    if content_type and not str(content_type).lower().startswith(
            ("image/", "application/octet-stream")):
        raise JobError(f"{content_type!r} is not an image")
    return int(size_bytes)


def validate_box(width, height, max_edge=MAX_EDGE):
    for name, value in (("width", width), ("height", height)):
        if int(value) < 1:
            raise JobError(f"{name} must be at least 1 pixel")
        if int(value) > max_edge:
            raise JobError(f"{name} must not exceed {max_edge} pixels")
    return int(width), int(height)


def progress(state):
    """A number to show next to a spinner."""
    return {"queued": 0, "processing": 50, "failed": 50,
            "done": 100, "dead": 100}[check_state(state)]


def summarise(jobs):
    """Count every state, including the ones with nothing in them, so a
    dashboard does not have to guess at missing keys."""
    out = {s: 0 for s in STATES}
    for job in jobs:
        out[check_state(job["state"])] += 1
    out["total"] = len(jobs)
    out["outstanding"] = out["queued"] + out["processing"] + out["failed"]
    return out
