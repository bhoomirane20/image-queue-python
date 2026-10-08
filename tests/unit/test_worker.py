import io
from unittest.mock import MagicMock, patch
from PIL import Image
import pytest

from app import worker


def create_test_image_bytes(w=200, h=100, format="PNG"):
    buf = io.BytesIO()
    img = Image.new("RGB", (w, h), color=(10, 20, 30))
    img.save(buf, format=format)
    return buf.getvalue()


def test_resize_valid_image():
    raw_bytes = create_test_image_bytes(200, 100)
    job = {
        "src_data": raw_bytes,
        "box_width": 100,
        "box_height": 100,
    }
    src_w, src_h, out_w, out_h, out_bytes = worker.resize(job)
    assert src_w == 200
    assert src_h == 100
    assert out_w == 100
    assert out_h == 50
    out_img = Image.open(io.BytesIO(out_bytes))
    assert out_img.format == "PNG"
    assert out_img.size == (100, 50)


def test_resize_invalid_data_raises():
    job = {
        "src_data": b"not an image",
        "box_width": 100,
        "box_height": 100,
    }
    with pytest.raises(Exception):
        worker.resize(job)


def test_worker_log(capsys):
    worker.log("hello", "world")
    captured = capsys.readouterr()
    assert "[worker] hello world\n" in captured.out


def test_reconcile():
    mock_cache = MagicMock()
    # First lrange for queue, second for processing
    mock_cache.lrange.side_effect = [["1"], ["2"]]
    with patch("app.cache.client", return_value=mock_cache), \
         patch("app.db.query", return_value=[{"id": 1}, {"id": 3}]), \
         patch("app.worker.push") as mock_push:
        worker.reconcile()
        # id 1 is already in known ({"1", "2"}), so only id 3 is pushed
        mock_push.assert_called_once_with(3)


def test_worker_reap():
    mock_cache = MagicMock()
    # imgq:processing contains job "10"
    mock_cache.lrange.return_value = ["10"]
    # lease expired (does not exist)
    mock_cache.exists.return_value = False
    mock_cache.lrem.return_value = 1
    # imgq:retry contains job "20"
    mock_cache.zrangebyscore.return_value = ["20"]

    db_jobs = {
        10: {"id": 10, "state": "processing", "attempts": 0, "leased_at": None},
        20: {"id": 20, "state": "failed"},
    }

    def fake_db_one(sql, params):
        return db_jobs.get(params[0])

    with patch("app.cache.client", return_value=mock_cache), \
         patch("app.db.one", side_effect=fake_db_one), \
         patch("app.db.query"), \
         patch("app.worker.push") as mock_push:
        worker.reap()
        # Job 10 should be requeued
        mock_push.assert_any_call(10)
        # Job 20 backoff elapsed, should be requeued
        mock_push.assert_any_call(20)


def test_run_one_vanished_job():
    with patch("app.db.one", return_value=None), \
         patch("app.worker.finish") as mock_finish:
        worker.run_one(999)
        mock_finish.assert_called_once_with(999)


def test_run_one_not_mine_to_run():
    # Job state is already "done"
    with patch("app.db.one", return_value={"id": 1, "state": "done"}), \
         patch("app.worker.release_duplicate") as mock_release:
        worker.run_one(1)
        mock_release.assert_called_once_with(1)


def test_run_one_success():
    img_bytes = create_test_image_bytes(200, 200)
    job_row = {
        "id": 1,
        "state": "queued",
        "attempts": 0,
        "filename": "pic.png",
        "src_data": img_bytes,
        "box_width": 100,
        "box_height": 100,
    }
    with patch("app.db.one", return_value=job_row), \
         patch("app.worker.take_lease") as mock_take_lease, \
         patch("app.db.query") as mock_db_query, \
         patch("app.worker.finish") as mock_finish:
        worker.run_one(1)
        mock_take_lease.assert_called_once_with(1)
        mock_finish.assert_called_once_with(1)
        # Verify db was updated with state='done'
        assert any("state='done'" in call[0][0] for call in mock_db_query.call_args_list)


def test_run_one_failure():
    job_row = {
        "id": 2,
        "state": "queued",
        "attempts": 0,
        "filename": "corrupt.png",
        "src_data": b"corrupt bytes",
        "box_width": 100,
        "box_height": 100,
    }
    with patch("app.db.one", return_value=job_row), \
         patch("app.worker.take_lease"), \
         patch("app.db.query") as mock_db_query, \
         patch("app.worker.finish") as mock_finish, \
         patch("app.worker.schedule_retry") as mock_retry:
        worker.run_one(2)
        mock_finish.assert_called_once_with(2)
        mock_retry.assert_called_once()
        # Verify db was updated with state='failed'
        assert any("state=%s" in call[0][0] for call in mock_db_query.call_args_list)
