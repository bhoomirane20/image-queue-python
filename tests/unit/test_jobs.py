import pytest
from app.jobs import (
    MAX_EDGE,
    MAX_UPLOAD_BYTES,
    STATES,
    JobError,
    backoff_seconds,
    can_transition,
    check_state,
    is_terminal,
    lease_deadline,
    lease_expired,
    plan_after_failure,
    progress,
    reclaim,
    should_retry,
    summarise,
    target_size,
    transition,
    validate_box,
    validate_upload,
)


# ============================================================================
# State machine and transition tests
# ============================================================================

def test_check_state_valid():
    for state in STATES:
        assert check_state(state) == state


def test_check_state_invalid():
    for bad_state in ["unknown", "pending", "", None, 123]:
        with pytest.raises(JobError, match="is not a job state"):
            check_state(bad_state)


def test_is_terminal():
    assert is_terminal("done") is True
    assert is_terminal("dead") is True
    assert is_terminal("queued") is False
    assert is_terminal("processing") is False
    assert is_terminal("failed") is False
    with pytest.raises(JobError):
        is_terminal("bogus")


def test_can_transition():
    # Valid transitions
    assert can_transition("queued", "processing") is True
    assert can_transition("queued", "dead") is True
    assert can_transition("processing", "done") is True
    assert can_transition("processing", "failed") is True
    assert can_transition("processing", "queued") is True
    assert can_transition("processing", "dead") is True
    assert can_transition("failed", "queued") is True
    assert can_transition("failed", "dead") is True

    # Terminal states have no outbound transitions
    for dest in STATES:
        assert can_transition("done", dest) is False
        assert can_transition("dead", dest) is False

    # Invalid transitions from active states
    assert can_transition("queued", "done") is False
    assert can_transition("queued", "failed") is False
    assert can_transition("queued", "queued") is False
    assert can_transition("failed", "processing") is False
    assert can_transition("failed", "done") is False

    # Bad states raise JobError
    with pytest.raises(JobError):
        can_transition("invalid", "processing")
    with pytest.raises(JobError):
        can_transition("queued", "invalid")


def test_transition_success():
    assert transition("queued", "processing") == "processing"
    assert transition("processing", "done") == "done"
    assert transition("processing", "dead") == "dead"
    assert transition("failed", "queued") == "queued"


def test_transition_failure_names_both_states():
    with pytest.raises(JobError) as exc_info:
        transition("done", "processing")
    assert "a job cannot go from 'done' to 'processing'" in str(exc_info.value)

    with pytest.raises(JobError) as exc_info2:
        transition("dead", "queued")
    assert "a job cannot go from 'dead' to 'queued'" in str(exc_info2.value)


# ============================================================================
# Retry, backoff, and failure planning tests
# ============================================================================

def test_should_retry():
    assert should_retry(0, max_attempts=3) is True
    assert should_retry(1, max_attempts=3) is True
    assert should_retry(2, max_attempts=3) is True
    assert should_retry(3, max_attempts=3) is False
    assert should_retry(4, max_attempts=3) is False

    with pytest.raises(JobError, match="attempts cannot be negative"):
        should_retry(-1)


def test_backoff_seconds():
    # Attempt 1 is not zero
    assert backoff_seconds(1) == 2.0
    assert backoff_seconds(2) == 4.0
    assert backoff_seconds(3) == 8.0
    assert backoff_seconds(4) == 16.0
    assert backoff_seconds(5) == 32.0
    # Capped at BACKOFF_CAP (60.0)
    assert backoff_seconds(6) == 60.0
    assert backoff_seconds(10) == 60.0

    # Custom base and cap
    assert backoff_seconds(1, base=3.0, cap=10.0) == 3.0
    assert backoff_seconds(2, base=3.0, cap=10.0) == 9.0
    assert backoff_seconds(3, base=3.0, cap=10.0) == 10.0

    with pytest.raises(JobError, match="attempts are counted from 1"):
        backoff_seconds(0)
    with pytest.raises(JobError, match="attempts are counted from 1"):
        backoff_seconds(-2)


def test_plan_after_failure():
    now = 1000.0

    # Attempt 1 of 3 -> retry after 2.0s
    plan1 = plan_after_failure(1, now, max_attempts=3)
    assert plan1 == {
        "state": "failed",
        "then": "queued",
        "attempts": 1,
        "retry_in": 2.0,
        "retry_at": 1002.0,
    }

    # Attempt 2 of 3 -> retry after 4.0s
    plan2 = plan_after_failure(2, now, max_attempts=3)
    assert plan2 == {
        "state": "failed",
        "then": "queued",
        "attempts": 2,
        "retry_in": 4.0,
        "retry_at": 1004.0,
    }

    # Attempt 3 of 3 -> buries the job
    plan3 = plan_after_failure(3, now, max_attempts=3)
    assert plan3 == {
        "state": "dead",
        "then": None,
        "attempts": 3,
        "retry_in": 0.0,
        "retry_at": None,
    }

    # Beyond max attempts
    plan4 = plan_after_failure(4, now, max_attempts=3)
    assert plan4["state"] == "dead"
    assert plan4["then"] is None


# ============================================================================
# Lease calculation and expiry tests
# ============================================================================

def test_lease_deadline():
    assert lease_deadline(100.0, 30.0) == 130.0
    assert lease_deadline(100, 10) == 110.0

    with pytest.raises(JobError, match="a lease must last at least one second"):
        lease_deadline(100.0, 0)
    with pytest.raises(JobError, match="a lease must last at least one second"):
        lease_deadline(100.0, -5)


def test_lease_expired():
    leased_at = 1000.0
    lease_sec = 30.0
    # Deadline is 1030.0

    # Before deadline: live
    assert lease_expired(leased_at, 1029.0, lease_sec) is False
    assert lease_expired(leased_at, 1029.999, lease_sec) is False

    # Exact boundary: expired (inclusive boundary check: float(now) >= deadline)
    assert lease_expired(leased_at, 1030.0, lease_sec) is True

    # After deadline: expired
    assert lease_expired(leased_at, 1031.0, lease_sec) is True


# ============================================================================
# Reclaim tests (core requirements & edge cases)
# ============================================================================

def test_reclaim_lease_expired_requeues():
    """Lease expired 1s ago -> 'requeue' with attempts incremented."""
    now = 1031.0
    job = {"state": "processing", "attempts": 0, "leased_at": 1000.0}
    decision = reclaim(job, now, lease_seconds=30.0, max_attempts=3)
    assert decision["action"] == "requeue"
    assert decision["reason"] == "lease expired"
    assert decision["attempts"] == 1


def test_reclaim_attempts_exhausted_buries():
    """Same job with attempts exhausted -> 'bury'."""
    now = 1031.0
    # Prior attempts = 2, max_attempts = 3. Increment makes it 3 -> bury!
    job = {"state": "processing", "attempts": 2, "leased_at": 1000.0}
    decision = reclaim(job, now, lease_seconds=30.0, max_attempts=3)
    assert decision["action"] == "bury"
    assert decision["reason"] == "lease expired and out of attempts"
    assert decision["attempts"] == 3


def test_reclaim_already_done_leaves():
    """Already done -> 'leave'."""
    now = 2000.0
    job = {"state": "done", "attempts": 1, "leased_at": 1000.0}
    decision = reclaim(job, now, lease_seconds=30.0)
    assert decision["action"] == "leave"
    assert "done" in decision["reason"]


def test_reclaim_already_dead_leaves():
    now = 2000.0
    job = {"state": "dead", "attempts": 3, "leased_at": 1000.0}
    decision = reclaim(job, now, lease_seconds=30.0)
    assert decision["action"] == "leave"
    assert "dead" in decision["reason"]


def test_reclaim_queued_or_failed_leaves():
    now = 2000.0
    for st in ("queued", "failed"):
        job = {"state": st, "attempts": 1, "leased_at": 1000.0}
        decision = reclaim(job, now, lease_seconds=30.0)
        assert decision["action"] == "leave"
        assert st in decision["reason"]


def test_reclaim_lease_not_yet_expired_leaves():
    """Lease not yet expired -> 'leave' with remaining time."""
    now = 1010.0
    job = {"state": "processing", "attempts": 0, "leased_at": 1000.0}
    decision = reclaim(job, now, lease_seconds=30.0)
    assert decision["action"] == "leave"
    assert decision["reason"] == "lease is still live"
    assert decision["expires_in"] == 20.0


def test_reclaim_boundary_exactly_at_expiry():
    """Boundary test: exactly at expiry time."""
    leased_at = 1000.0
    lease_sec = 30.0
    now = leased_at + lease_sec  # 1030.0

    # With attempts remaining: requeue
    job = {"state": "processing", "attempts": 0, "leased_at": leased_at}
    res = reclaim(job, now, lease_seconds=lease_sec, max_attempts=3)
    assert res["action"] == "requeue"
    assert res["attempts"] == 1

    # With attempts at max-1 (2): bury
    job_last = {"state": "processing", "attempts": 2, "leased_at": leased_at}
    res_last = reclaim(job_last, now, lease_seconds=lease_sec, max_attempts=3)
    assert res_last["action"] == "bury"
    assert res_last["attempts"] == 3


def test_reclaim_boundary_just_before_expiry():
    """Boundary test: 1 millisecond before expiry -> still live."""
    leased_at = 1000.0
    lease_sec = 30.0
    now = leased_at + lease_sec - 0.001
    job = {"state": "processing", "attempts": 0, "leased_at": leased_at}
    res = reclaim(job, now, lease_seconds=lease_sec)
    assert res["action"] == "leave"
    assert res["reason"] == "lease is still live"
    assert res["expires_in"] == 0.001


def test_reclaim_no_lease_recorded():
    """A job found in processing with no lease recorded -> requeue immediately."""
    now = 1000.0
    job = {"state": "processing", "attempts": 1, "leased_at": None}
    decision = reclaim(job, now, lease_seconds=30.0)
    assert decision["action"] == "requeue"
    assert decision["reason"] == "no lease recorded"
    assert decision["attempts"] == 2


def test_reclaim_missing_attempts_defaults_to_zero():
    """Job dictionary without 'attempts' key increments from 0 to 1."""
    now = 1050.0
    job = {"state": "processing", "leased_at": 1000.0}
    decision = reclaim(job, now, lease_seconds=30.0, max_attempts=3)
    assert decision["action"] == "requeue"
    assert decision["attempts"] == 1


# ============================================================================
# Target size calculation tests
# ============================================================================

def test_target_size_preserves_aspect_ratio():
    # Landscape: 1000x500 into 200x200 -> 200x100
    assert target_size(1000, 500, 200, 200) == (200, 100)
    # Portrait: 500x1000 into 200x200 -> 100x200
    assert target_size(500, 1000, 200, 200) == (100, 200)
    # Square: 800x800 into 400x400 -> 400x400
    assert target_size(800, 800, 400, 400) == (400, 400)


def test_target_size_already_inside_box_unchanged():
    # Image smaller than box is not upscaled
    assert target_size(100, 50, 200, 200) == (100, 50)
    # Exactly matching dimensions
    assert target_size(320, 320, 320, 320) == (320, 320)
    # One dimension equals max, one smaller
    assert target_size(320, 100, 320, 320) == (320, 100)


def test_target_size_extreme_ratio_never_zero():
    # 4000x3 into 320x320: scale = 320/4000 = 0.08. 3 * 0.08 = 0.24.
    # round() gives 0, but max(1, ...) ensures non-zero dimension
    out_w, out_h = target_size(4000, 3, 320, 320)
    assert out_w == 320
    assert out_h == 1

    # Vertical extreme ratio: 3x4000 into 320x320
    out_w2, out_h2 = target_size(3, 4000, 320, 320)
    assert out_w2 == 1
    assert out_h2 == 320


def test_target_size_invalid_inputs():
    with pytest.raises(JobError, match="width must be at least 1 pixel"):
        target_size(0, 100, 200, 200)
    with pytest.raises(JobError, match="height must be at least 1 pixel"):
        target_size(100, -1, 200, 200)
    with pytest.raises(JobError, match="max_width must be at least 1 pixel"):
        target_size(100, 100, 0, 200)
    with pytest.raises(JobError, match="max_height must be at least 1 pixel"):
        target_size(100, 100, 200, -10)


# ============================================================================
# Validation tests
# ============================================================================

def test_validate_upload():
    # Valid sizes
    assert validate_upload(1024) == 1024
    assert validate_upload(MAX_UPLOAD_BYTES) == MAX_UPLOAD_BYTES

    # Empty uploads
    with pytest.raises(JobError, match="the upload is empty"):
        validate_upload(None)
    with pytest.raises(JobError, match="the upload is empty"):
        validate_upload(0)
    with pytest.raises(JobError, match="the upload is empty"):
        validate_upload(-5)

    # Exceeding size limit
    with pytest.raises(JobError, match="larger than"):
        validate_upload(MAX_UPLOAD_BYTES + 1)

    # Valid content types
    assert validate_upload(100, "image/png") == 100
    assert validate_upload(100, "image/jpeg") == 100
    assert validate_upload(100, "application/octet-stream") == 100
    assert validate_upload(100, None) == 100

    # Invalid content types
    with pytest.raises(JobError, match="is not an image"):
        validate_upload(100, "application/pdf")
    with pytest.raises(JobError, match="is not an image"):
        validate_upload(100, "text/plain")


def test_validate_box():
    assert validate_box(320, 240) == (320, 240)
    assert validate_box("320", "240") == (320, 240)
    assert validate_box(MAX_EDGE, MAX_EDGE) == (MAX_EDGE, MAX_EDGE)

    with pytest.raises(JobError, match="width must be at least 1 pixel"):
        validate_box(0, 100)
    with pytest.raises(JobError, match="height must be at least 1 pixel"):
        validate_box(100, 0)
    with pytest.raises(JobError, match="width must not exceed"):
        validate_box(MAX_EDGE + 1, 100)
    with pytest.raises(JobError, match="height must not exceed"):
        validate_box(100, MAX_EDGE + 1)


# ============================================================================
# Progress and Summarise tests
# ============================================================================

def test_progress():
    assert progress("queued") == 0
    assert progress("processing") == 50
    assert progress("failed") == 50
    assert progress("done") == 100
    assert progress("dead") == 100

    with pytest.raises(JobError):
        progress("other")


def test_summarise():
    jobs = [
        {"state": "queued"},
        {"state": "queued"},
        {"state": "processing"},
        {"state": "done"},
        {"state": "dead"},
    ]
    summary = summarise(jobs)
    assert summary["queued"] == 2
    assert summary["processing"] == 1
    assert summary["done"] == 1
    assert summary["dead"] == 1
    assert summary["failed"] == 0  # Missing state counted as 0
    assert summary["total"] == 5
    assert summary["outstanding"] == 3  # queued (2) + processing (1) + failed (0)


def test_summarise_empty():
    summary = summarise([])
    for st in STATES:
        assert summary[st] == 0
    assert summary["total"] == 0
    assert summary["outstanding"] == 0


def test_summarise_invalid_state():
    with pytest.raises(JobError):
        summarise([{"state": "unknown"}])
