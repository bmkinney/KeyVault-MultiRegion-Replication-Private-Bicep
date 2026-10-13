import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "replication"))
from azure.core.exceptions import ResourceExistsError, ServiceRequestError
from coordination import BlobLock


class CoordinationTests(unittest.TestCase):
    def test_existing_lock_blob_is_leased_and_released(self):
        container = Mock()
        blob = container.get_blob_client.return_value
        blob.upload_blob.side_effect = ResourceExistsError("exists")
        with BlobLock(container, "sample") as lock:
            lock.assert_owned()
        blob.acquire_lease.assert_called_once_with(lease_duration=60)
        blob.acquire_lease.return_value.release.assert_called_once()

    def test_lease_renewal_transport_failure_marks_lost(self):
        lock = BlobLock(Mock(), "sample")
        lock.lease = Mock()
        lock.lease.renew.side_effect = ServiceRequestError("network")
        lock.stopped = Mock()
        lock.stopped.wait.return_value = False
        lock._renew()
        self.assertTrue(lock.lost.is_set())
        with self.assertRaisesRegex(RuntimeError, "lost"):
            lock.assert_owned()

    def test_unrelated_upload_failure_is_not_swallowed(self):
        container = Mock()
        container.get_blob_client.return_value.upload_blob.side_effect = ServiceRequestError("network")
        with self.assertRaises(ServiceRequestError):
            with BlobLock(container, "sample"):
                self.fail("failed upload must not enter critical section")
