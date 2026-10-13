import json
import logging
import threading

from azure.core.exceptions import AzureError, ResourceExistsError


class BlobLock:
    def __init__(self, container, name):
        self.blob = container.get_blob_client(name + ".json")
        self.lease = None
        self.stopped = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def __enter__(self):
        try:
            self.blob.upload_blob(b"{}", overwrite=False)
        except ResourceExistsError:
            pass
        self.lease = self.blob.acquire_lease(lease_duration=60)
        self.thread = threading.Thread(target=self._renew, daemon=True)
        self.thread.start()
        return self

    def _renew(self):
        while not self.stopped.wait(15):
            try:
                self.lease.renew()
            except AzureError:
                self.lost.set()
                logging.error('MRKV {"outcome":"failure","stage":"lease_renewal"}')
                return

    def assert_owned(self):
        if self.lost.is_set():
            raise RuntimeError("Coordination lease was lost")
        # Renew synchronously at write boundaries, not just on the background timer.
        self.lease.renew()

    def record(self, state):
        self.assert_owned()
        self.blob.upload_blob(json.dumps(state).encode(), overwrite=True, lease=self.lease)

    def __exit__(self, error_type, error, traceback):
        self.stopped.set()
        self.thread.join()
        try:
            self.lease.release()
        except AzureError:
            logging.error('MRKV {"outcome":"failure","stage":"lease_release"}')
            if error_type is None:
                raise
