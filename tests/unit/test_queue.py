from unittest.mock import MagicMock, patch

from app import queue


def test_lease_seconds_default(monkeypatch):
    monkeypatch.delenv("LEASE_SECONDS", raising=False)
    assert queue.lease_seconds() == 30.0


def test_lease_seconds_custom(monkeypatch):
    monkeypatch.setenv("LEASE_SECONDS", "45.5")
    assert queue.lease_seconds() == 45.5


def test_lease_key():
    assert queue.lease_key(42) == "imgq:lease:42"


def test_queue_operations():
    mock_client = MagicMock()
    with patch("app.cache.client", return_value=mock_client):
        queue.push(10)
        mock_client.rpush.assert_called_once_with(queue.QUEUE, "10")

        queue.push_front(11)
        mock_client.lpush.assert_called_once_with(queue.QUEUE, "11")

        queue.schedule_retry(12, 1234.5)
        mock_client.zadd.assert_called_once_with(queue.RETRY, {"12": 1234.5})

        mock_client.blmove.return_value = "15"
        assert queue.claim(timeout=2) == 15
        mock_client.blmove.assert_called_once_with(
            queue.QUEUE, queue.PROCESSING, 2, "LEFT", "RIGHT"
        )

        mock_client.blmove.return_value = None
        assert queue.claim() is None

        with patch("app.queue.lease_seconds", return_value=25.0):
            queue.take_lease(16)
            mock_client.set.assert_called_once_with("imgq:lease:16", "1", ex=25)

            queue.renew_lease(16)
            mock_client.expire.assert_called_once_with("imgq:lease:16", 25)

        queue.finish(17)
        mock_client.lrem.assert_called_once_with(queue.PROCESSING, 0, "17")
        mock_client.delete.assert_called_once_with("imgq:lease:17")

        queue.release_duplicate(18)
        mock_client.lrem.assert_called_with(queue.PROCESSING, 1, "18")

        mock_client.llen.side_effect = [3, 2]
        mock_client.zcard.return_value = 1
        d = queue.depth()
        assert d == {"waiting": 3, "in_flight": 2, "waiting_on_backoff": 1}
