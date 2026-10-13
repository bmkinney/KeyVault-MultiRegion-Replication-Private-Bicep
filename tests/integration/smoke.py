"""Opt-in live test. Creates synthetic secrets and deliberately does not delete them."""

import argparse
import time
import uuid

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential
from azure.keyvault.secrets import SecretClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--confirm-create-test-secret", action="store_true", required=True)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    name = "mrkv-test-" + uuid.uuid4().hex[:12]
    credential = AzureCliCredential()
    with SecretClient(args.source, credential) as source, SecretClient(args.destination, credential) as dest:
        source_version = source.set_secret(
            name, "synthetic-" + uuid.uuid4().hex,
            content_type="text/plain", tags={"purpose": "mrkv-smoke"},
        )
        start = time.monotonic()
        while time.monotonic() - start < args.timeout:
            try:
                result = dest.get_secret(name)
            except ResourceNotFoundError:
                time.sleep(2)
                continue
            if (
                result.value == source_version.value
                and result.properties.content_type == "text/plain"
                and result.properties.tags.get("purpose") == "mrkv-smoke"
                and result.properties.tags.get("mrkv_version") == source_version.properties.version
            ):
                print(f"PASS {name}: value, metadata, and lineage converged in {time.monotonic() - start:.1f}s")
                return
            time.sleep(2)
        raise TimeoutError(f"{name} did not converge within {args.timeout}s; synthetic secret retained")


if __name__ == "__main__":
    main()
