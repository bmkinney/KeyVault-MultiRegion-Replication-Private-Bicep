import logging
import json
import os
from functools import lru_cache

import azure.functions as func
from azure.identity import ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from azure.storage.blob import BlobServiceClient

from coordination import BlobLock
from replication import EXPIRY_EVENTS, Replicator, SecretSelection, parse_events, report

app = func.FunctionApp()


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required setting: {name}")
    return value


def ensure_enabled() -> None:
    if required("REPLICATION_ENABLED").lower() != "true":
        raise RuntimeError("Worker is fenced; its triggers must also be disabled")

def boolean_setting(name: str) -> bool:
    value = required(name).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def selection() -> SecretSelection:
    return SecretSelection(
        json.loads(required("SECRET_NAMES")),
        os.environ.get("SECRET_PREFIX", ""),
        boolean_setting("REPLICATE_ALL_SECRETS"),
    )


@lru_cache(maxsize=1)
def worker() -> Replicator:
    credential = ManagedIdentityCredential(client_id=required("WORKER_CLIENT_ID"))
    source_uri = required("SOURCE_VAULT_URI")
    destination_uri = required("DESTINATION_VAULT_URI")
    if source_uri.rstrip("/").lower() == destination_uri.rstrip("/").lower():
        raise ValueError("Source and destination vaults must differ")
    source = SecretClient(source_uri, credential, retry_total=3, logging_enable=False)
    destination = SecretClient(destination_uri, credential, retry_total=3, logging_enable=False)
    blobs = BlobServiceClient(
        required("COORDINATION_BLOB_URI"), credential,
        retry_total=3, logging_enable=False,
    )
    container = blobs.get_container_client("coordination")
    return Replicator(
        source, destination, required("SOURCE_VAULT_ID"),
        lambda name: BlobLock(container, name),
        selection(), required("DESTINATION_VAULT_ID"),
        boolean_setting("ALLOW_UNMANAGED_OVERWRITE"),
    )


@app.function_name(name="Replicate")
@app.service_bus_queue_trigger(
    arg_name="message", queue_name="kv-events", connection="ServiceBusConnection"
)
def replicate(message: func.ServiceBusMessage) -> None:
    try:
        ensure_enabled()
        events = parse_events(
            message.get_body().decode("utf-8"),
            required("SOURCE_VAULT_ID"),
            required("SOURCE_VAULT_URI"),
        )
        for event in events:
            name = event["data"]["ObjectName"]
            if not selection().includes(name):
                report("excluded", secret=name)
                continue
            if event["eventType"] in EXPIRY_EVENTS:
                report(EXPIRY_EVENTS[event["eventType"]], secret=name)
            worker().sync(name)
    except Exception as error:
        # Do not log SDK response bodies, messages, or secret values.
        report("failure", stage="event", errorType=type(error).__name__)
        raise


@app.function_name(name="Reconcile")
@app.timer_trigger(
    arg_name="timer", schedule="%RECONCILIATION_SCHEDULE%",
    run_on_startup=False, use_monitor=True,
)
def reconcile(timer: func.TimerRequest) -> None:
    try:
        ensure_enabled()
        if timer.past_due:
            logging.warning('MRKV {"outcome":"timer_past_due"}')
        worker().reconcile()
    except Exception as error:
        report("failure", stage="timer", errorType=type(error).__name__)
        raise
