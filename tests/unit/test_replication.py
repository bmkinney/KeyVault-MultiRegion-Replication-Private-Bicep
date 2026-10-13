import copy
import json
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "replication"))
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from replication import NEW_VERSION, Replicator, fingerprint, latest, parse_events

SOURCE_ID = "/subscriptions/demo/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/source"
SOURCE_URI = "https://source.vault.azure.net/"
VERSION = "a" * 32


def properties(name="sample", **overrides):
    values = {
        "name": name,
        "version": VERSION,
        "enabled": True,
        "content_type": "text/plain",
        "not_before": None,
        "expires_on": None,
        "tags": {"owner": "demo"},
        "created_on": datetime.now(timezone.utc) - timedelta(seconds=10),
        "updated_on": datetime.now(timezone.utc) - timedelta(seconds=10),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Vault:
    def __init__(self):
        self.secrets = {}
        self.writes = 0
        self.updates = 0
        self.forbidden = False

    def get_secret(self, name):
        if name not in self.secrets:
            raise ResourceNotFoundError("missing")
        if self.forbidden or self.secrets[name].properties.enabled is False:
            error = HttpResponseError("forbidden")
            error.status_code = 403
            raise error
        return self.secrets[name]

    def set_secret(self, name, value, **options):
        self.writes += 1
        result = SimpleNamespace(
            properties=properties(name, version=f"{self.writes:032x}", **options), value=value
        )
        self.secrets[name] = result
        return result

    def update_secret_properties(self, name, version, **options):
        self.updates += 1
        for key, value in options.items():
            setattr(self.secrets[name].properties, key, value)
        return self.secrets[name].properties

    def list_properties_of_secret_versions(self, name):
        return [self.secrets[name].properties]

    def list_properties_of_secrets(self):
        return [secret.properties for secret in self.secrets.values()]


class Lock:
    def assert_owned(self):
        pass

    def record(self, state):
        self.state = state


@contextmanager
def lock_factory(name):
    yield Lock()


def event(**overrides):
    result = {
        "id": "event-id",
        "topic": SOURCE_ID,
        "eventType": NEW_VERSION,
        "eventTime": datetime.now(timezone.utc).isoformat(),
        "data": {
            "Id": f"{SOURCE_URI}secrets/sample/{VERSION}",
            "ObjectType": "Secret",
            "ObjectName": "sample",
            "VaultName": "source",
            "Version": VERSION,
        },
    }
    result.update(overrides)
    return result


class EventTests(unittest.TestCase):
    def test_valid_single_and_array(self):
        for payload in [event(), [event()]]:
            self.assertEqual(len(parse_events(json.dumps(payload), SOURCE_ID, SOURCE_URI)), 1)

    def test_malformed_inputs_fail(self):
        for payload in ["bad json", "[]", "null", '"text"', '{"data":{}}']:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_events(payload, SOURCE_ID, SOURCE_URI)

    def test_unknown_source_fails(self):
        with self.assertRaises(ValueError):
            parse_events(json.dumps(event(topic=SOURCE_ID + "other")), SOURCE_ID, SOURCE_URI)

    def test_cloud_events_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            parse_events(json.dumps(event(eventType=None, type=NEW_VERSION)), SOURCE_ID, SOURCE_URI)

    def test_identity_fields_must_agree(self):
        for key, value in [
            ("VaultName", "other"), ("Version", "invalid"),
            ("ObjectType", "Key"), ("ObjectName", "../bad"), ("Id", "https://evil.test/secret"),
        ]:
            item = event()
            item["data"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_events(json.dumps(item), SOURCE_ID, SOURCE_URI)

    def test_entire_batch_validated_before_processing(self):
        with self.assertRaises(ValueError):
            parse_events(json.dumps([event(), event(topic="other")]), SOURCE_ID, SOURCE_URI)

    def test_timestamp_required_and_zoned(self):
        for timestamp in [None, "bad", "2026-10-09T00:00:00"]:
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                parse_events(json.dumps(event(eventTime=timestamp)), SOURCE_ID, SOURCE_URI)


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.source = Vault()
        self.dest = Vault()
        self.source.secrets["sample"] = SimpleNamespace(properties=properties(), value="synthetic-value")
        self.worker = Replicator(self.source, self.dest, SOURCE_ID, lock_factory)

    def test_value_and_metadata_copied(self):
        self.assertEqual(self.worker.sync("sample"), "replicated")
        result = self.dest.secrets["sample"]
        self.assertEqual(result.value, "synthetic-value")
        self.assertEqual(result.properties.content_type, "text/plain")
        self.assertEqual(result.properties.tags["owner"], "demo")
        self.assertEqual(result.properties.tags["mrkv_version"], VERSION)
        self.assertNotEqual(result.properties.version, VERSION)

    def test_duplicate_event_creates_no_version(self):
        self.worker.sync("sample")
        self.assertEqual(self.worker.sync("sample"), "unchanged")
        self.assertEqual(self.dest.writes, 1)

    def test_latest_state_not_event_version_is_authoritative(self):
        self.worker.sync("sample")
        self.source.secrets["sample"].properties.version = "b" * 32
        self.source.secrets["sample"].value = "newest-value"
        self.worker.sync("sample")
        self.assertEqual(self.worker.sync("sample"), "unchanged")
        self.assertEqual(self.dest.secrets["sample"].value, "newest-value")
        self.assertEqual(self.dest.writes, 2)

    def test_metadata_change_is_reconciled(self):
        self.worker.sync("sample")
        self.source.secrets["sample"].properties.tags["owner"] = "changed"
        self.worker.reconcile()
        self.assertEqual(self.dest.secrets["sample"].properties.tags["owner"], "changed")
        self.assertEqual(self.dest.writes, 2)

    def test_disabled_source_disables_destination_without_reading_value(self):
        self.worker.sync("sample")
        self.source.secrets["sample"].properties.enabled = False
        self.assertEqual(self.worker.sync("sample"), "inactive")
        self.assertFalse(self.dest.secrets["sample"].properties.enabled)
        self.assertEqual(self.dest.writes, 1)

    def test_reenabled_source_restores_replica(self):
        self.worker.sync("sample")
        self.source.secrets["sample"].properties.enabled = False
        self.worker.sync("sample")
        self.source.secrets["sample"].properties.enabled = True
        self.assertEqual(self.worker.sync("sample"), "replicated")
        self.assertTrue(self.dest.secrets["sample"].properties.enabled)

    def test_expired_source_disables_destination(self):
        self.worker.sync("sample")
        self.source.secrets["sample"].properties.expires_on = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertEqual(self.worker.sync("sample"), "inactive")
        self.assertFalse(self.dest.secrets["sample"].properties.enabled)

    def test_future_source_does_not_publish_value(self):
        self.source.secrets["sample"].properties.not_before = datetime.now(timezone.utc) + timedelta(days=1)
        self.assertEqual(self.worker.sync("sample"), "inactive")
        self.assertEqual(self.dest.writes, 0)

    def test_tags_cannot_exceed_service_limit(self):
        self.source.secrets["sample"].properties.tags = {f"tag{i}": str(i) for i in range(13)}
        with self.assertRaises(ValueError):
            self.worker.sync("sample")
        self.assertEqual(self.dest.writes, 0)

    def test_source_not_found_is_not_acknowledged(self):
        self.source.secrets.clear()
        with self.assertRaises(ResourceNotFoundError):
            self.worker.sync("sample")

    def test_rbac_denial_does_not_masquerade_as_disabled_secret(self):
        self.source.forbidden = True
        with self.assertRaises(HttpResponseError):
            self.worker.sync("sample")

    def test_reconciliation_reports_destination_only_without_deleting(self):
        self.dest.secrets["extra"] = SimpleNamespace(properties=properties("extra"), value="synthetic")
        with patch("replication.report") as report:
            self.worker.reconcile()
            report.assert_any_call("destination_only", secret="extra")
        self.assertIn("extra", self.dest.secrets)

    def test_crash_after_destination_write_does_not_duplicate_retry(self):
        @contextmanager
        def failing_lock(name):
            lock = Lock()
            lock.record = lambda state: (_ for _ in ()).throw(RuntimeError("crash"))
            yield lock

        self.worker.lock_factory = failing_lock
        with self.assertRaises(RuntimeError):
            self.worker.sync("sample")
        self.worker.lock_factory = lock_factory
        self.assertEqual(self.worker.sync("sample"), "unchanged")
        self.assertEqual(self.dest.writes, 1)

    def test_lost_lease_prevents_write(self):
        @contextmanager
        def lost_lock(name):
            lock = Lock()
            lock.assert_owned = lambda: (_ for _ in ()).throw(RuntimeError("lost"))
            yield lock

        self.worker.lock_factory = lost_lock
        with self.assertRaises(RuntimeError):
            self.worker.sync("sample")
        self.assertEqual(self.dest.writes, 0)

    def test_ambiguous_unreadable_version_requires_review(self):
        self.source.forbidden = True
        original = self.source.secrets["sample"].properties
        other = copy.copy(original)
        other.version = "b" * 32
        self.source.list_properties_of_secret_versions = lambda name: [original, other]
        with self.assertRaisesRegex(RuntimeError, "ambiguous"):
            latest(self.source, "sample")

    def test_lineage_is_ignored_as_source_authority_after_promotion(self):
        self.source.secrets["sample"].properties.tags["mrkv_source"] = "old-source"
        self.worker.sync("sample")
        self.assertEqual(self.dest.secrets["sample"].properties.tags["mrkv_source"], SOURCE_ID.lower())

    def test_fingerprint_excludes_lineage(self):
        original = properties()
        replica = copy.deepcopy(original)
        replica.tags["mrkv_source"] = "old"
        self.assertEqual(fingerprint(original), fingerprint(replica))

    def test_secret_value_never_enters_application_log(self):
        with self.assertLogs(level="INFO") as logs:
            self.worker.sync("sample")
        self.assertNotIn("synthetic-value", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
