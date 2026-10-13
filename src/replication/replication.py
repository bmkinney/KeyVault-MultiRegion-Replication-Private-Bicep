"""Single-writer current-state convergence; no secret values in coordination or logs."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

NEW_VERSION = "Microsoft.KeyVault.SecretNewVersionCreated"
EXPIRY_EVENTS = {
    "Microsoft.KeyVault.SecretNearExpiry": "near_expiry",
    "Microsoft.KeyVault.SecretExpired": "expired",
}
MARKER_PREFIX = "mrkv_"
NAME_PATTERN = re.compile(r"^[a-zA-Z0-9-]{1,127}$")
VERSION_PATTERN = re.compile(r"^[a-fA-F0-9]{32}$")

class SecretSelection:
    def __init__(self, names: list[str], prefix: str, all_secrets: bool):
        if not isinstance(names, list) or any(
            not isinstance(name, str) or not NAME_PATTERN.fullmatch(name) for name in names
        ):
            raise ValueError("Secret selection names must be valid Key Vault secret names")
        if not isinstance(prefix, str) or (prefix and not NAME_PATTERN.fullmatch(prefix)):
            raise ValueError("Secret prefix must contain only letters, numbers, and hyphens")
        if not isinstance(all_secrets, bool):
            raise ValueError("Whole-vault selection must be a boolean")
        if not all_secrets and not names and not prefix:
            raise ValueError("Select secret names, a prefix, or explicitly opt into all secrets")
        self.names = {name.lower() for name in names}
        self.prefix = prefix.lower()
        self.all_secrets = all_secrets

    def includes(self, name: str) -> bool:
        return self.all_secrets or name.lower() in self.names or (
            bool(self.prefix) and name.lower().startswith(self.prefix)
        )


def report(outcome: str, **fields: Any) -> None:
    message = "MRKV " + json.dumps({"outcome": outcome, **fields}, separators=(",", ":"))
    if outcome in {"failure", "source_missing", "destination_only", "expired", "near_expiry"}:
        logging.warning(message)
    else:
        logging.info(message)


def parse_events(payload: str, expected_source: str, source_uri: str) -> list[dict]:
    data = json.loads(payload)
    events = data if isinstance(data, list) else [data]
    if not events:
        raise ValueError("An event message must not be empty")
    validated = []
    source_host = urlparse(source_uri).hostname
    if not source_host:
        raise ValueError("Source vault URI has no hostname")
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
            raise ValueError("Expected Event Grid schema and an object data field")
        if not isinstance(event.get("id"), str) or not event["id"]:
            raise ValueError("Event ID is missing")
        if str(event.get("topic", "")).lower() != expected_source.lower():
            raise ValueError("Event source does not match the configured vault")
        if event.get("eventType") not in {NEW_VERSION, *EXPIRY_EVENTS}:
            raise ValueError("Event type is not configured for this worker")
        detail = event["data"]
        name, version = detail.get("ObjectName"), detail.get("Version")
        if detail.get("ObjectType") != "Secret":
            raise ValueError("Only secret events are supported")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            raise ValueError("Invalid secret name")
        if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
            raise ValueError("Invalid source version")
        if str(detail.get("VaultName", "")).lower() != source_host.split(".")[0].lower():
            raise ValueError("Event vault name does not match the source")
        expected_id = f"{source_uri.rstrip('/')}/secrets/{name}/{version}"
        if str(detail.get("Id", "")).lower() != expected_id.lower():
            raise ValueError("Event secret identifier does not match its fields")
        timestamp = event.get("eventTime")
        if not isinstance(timestamp, str):
            raise ValueError("Event time is missing")
        parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed_time.tzinfo is None:
            raise ValueError("Event time must include a timezone")
        validated.append(event)
    return validated


def user_tags(properties: Any) -> dict[str, str]:
    return {
        key: value for key, value in (properties.tags or {}).items()
        if not key.startswith(MARKER_PREFIX)
    }


def fingerprint(properties: Any) -> str:
    state = {
        "enabled": properties.enabled is not False,
        "content_type": properties.content_type,
        "not_before": properties.not_before.isoformat() if properties.not_before else None,
        "expires_on": properties.expires_on.isoformat() if properties.expires_on else None,
        "tags": user_tags(properties),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def latest(client: Any, name: str) -> tuple[Any, Any]:
    """Obtain latest value, or unambiguously identify unreadable latest metadata."""
    try:
        secret = client.get_secret(name)
        return secret.properties, secret.value
    except HttpResponseError as error:
        if error.status_code != 403:
            raise
        versions = list(client.list_properties_of_secret_versions(name))
        if not versions:
            raise ResourceNotFoundError("No secret versions found") from error
        if any(item.created_on is None for item in versions):
            raise RuntimeError("Cannot order versions without creation timestamps") from error
        newest_time = max(item.created_on for item in versions)
        newest = [item for item in versions if item.created_on == newest_time]
        if len(newest) != 1:
            raise RuntimeError("Unreadable latest version is ambiguous; operator review required") from error
        properties = newest[0]
        now = datetime.now(timezone.utc)
        unreadable = (
            properties.enabled is False
            or (properties.not_before is not None and properties.not_before > now)
            or (properties.expires_on is not None and properties.expires_on <= now)
        )
        if not unreadable:
            raise error
        return properties, None


class Replicator:
    def __init__(
        self, source: Any, destination: Any, source_id: str, lock_factory: Callable,
        selection: SecretSelection, destination_id: str, allow_unmanaged_overwrite: bool = False,
    ):
        self.source = source
        self.destination = destination
        self.source_id = source_id.lower()
        self.lock_factory = lock_factory
        self.selection = selection
        self.destination_id = destination_id.lower()
        self.allow_unmanaged_overwrite = allow_unmanaged_overwrite

    def _managed_destination(self, source_properties: Any, dest_properties: Any) -> bool:
        destination_tags = dest_properties.tags or {}
        source_tags = source_properties.tags or {}
        if (
            destination_tags.get("mrkv_source", "").lower() == self.source_id
            and VERSION_PATTERN.fullmatch(destination_tags.get("mrkv_version", ""))
            and destination_tags.get("mrkv_fingerprint")
        ):
            return True
        # A promoted replica may copy back to its original, still-unmarked source.
        return (
            source_tags.get("mrkv_source", "").lower() == self.destination_id
            and source_tags.get("mrkv_version") == dest_properties.version
            and source_tags.get("mrkv_fingerprint") == fingerprint(dest_properties)
        )

    def sync(self, name: str) -> str:
        if not NAME_PATTERN.fullmatch(name):
            raise ValueError("Invalid secret name")
        if not self.selection.includes(name):
            report("excluded", secret=name)
            return "excluded"
        with self.lock_factory(name.lower()) as lock:
            try:
                properties, value = latest(self.source, name)
            except ResourceNotFoundError:
                report("source_missing", secret=name)
                raise
            if any(key.startswith(MARKER_PREFIX) for key in (properties.tags or {})):
                # Old replica markers are non-authoritative after a controlled promotion.
                report("source_has_lineage", secret=name)
            try:
                dest_properties, _ = latest(self.destination, name)
            except ResourceNotFoundError:
                dest_properties = None
            if (
                dest_properties is not None
                and not self._managed_destination(properties, dest_properties)
                and not self.allow_unmanaged_overwrite
            ):
                report("failure", secret=name, stage="unmanaged_destination_collision")
                raise ValueError("Destination name collision requires explicit overwrite approval")
            now = datetime.now(timezone.utc)
            inactive = (
                properties.enabled is False
                or (properties.not_before is not None and properties.not_before > now)
                or (properties.expires_on is not None and properties.expires_on <= now)
            )
            lock.assert_owned()
            if inactive:
                if dest_properties is not None and dest_properties.enabled is not False:
                    self.destination.update_secret_properties(
                        name, dest_properties.version, enabled=False
                    )
                lock.assert_owned()
                if properties.expires_on is not None and properties.expires_on <= now:
                    report("expired", secret=name)
                report("source_inactive", secret=name, destinationExists=dest_properties is not None)
                return "inactive"
            if value is None:
                raise RuntimeError("Readable source secret has no value")
            digest = fingerprint(properties)
            markers = {
                "mrkv_source": self.source_id,
                "mrkv_version": properties.version,
                "mrkv_fingerprint": digest,
            }
            dest_tags = dest_properties.tags or {} if dest_properties is not None else {}
            if (
                dest_properties is not None
                and all(dest_tags.get(key) == val for key, val in markers.items())
                and fingerprint(dest_properties) == digest
            ):
                report("unchanged", secret=name, sourceVersion=properties.version)
                return "unchanged"
            tags = {**user_tags(properties), **markers}
            if len(tags) > 15:
                raise ValueError("Source tags leave insufficient room for three lineage tags")
            lock.assert_owned()
            result = self.destination.set_secret(
                name,
                value,
                enabled=properties.enabled is not False,
                content_type=properties.content_type,
                not_before=properties.not_before,
                expires_on=properties.expires_on,
                tags=tags,
            )
            lock.assert_owned()
            lock.record({
                "sourceVersion": properties.version,
                "destinationVersion": result.properties.version,
            })
            changed_at = properties.updated_on or properties.created_on
            lag = max(0, (now - changed_at).total_seconds()) if changed_at else None
            report("replicated", secret=name, sourceVersion=properties.version, lagSeconds=lag)
            return "replicated"

    def reconcile(self) -> None:
        failures = 0
        source_names = set()
        for properties in self.source.list_properties_of_secrets():
            if not self.selection.includes(properties.name):
                continue
            source_names.add(properties.name.lower())
            try:
                self.sync(properties.name)
            except (HttpResponseError, ValueError, RuntimeError):
                failures += 1
                report("failure", secret=properties.name, stage="reconciliation")
        for properties in self.destination.list_properties_of_secrets():
            if self.selection.includes(properties.name) and properties.name.lower() not in source_names:
                report("destination_only", secret=properties.name)
        report("reconciliation_complete", secretCount=len(source_names), failures=failures)
        if failures:
            raise RuntimeError(f"Reconciliation failed for {failures} secrets")
