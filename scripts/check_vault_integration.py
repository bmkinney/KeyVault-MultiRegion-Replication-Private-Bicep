"""Read-only existing-vault preflight. Never retrieves secret values or changes Azure."""

import argparse
import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

RESOURCE_ID = re.compile(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
    r"(Microsoft\.KeyVault/vaults|Microsoft\.EventGrid/systemTopics)/([^/]+)$",
    re.IGNORECASE,
)
SECRET_NAME = re.compile(r"^[A-Za-z0-9-]{1,127}$")


def authorization_model(properties):
    enabled = properties.get("enableRbacAuthorization")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("Vault enableRbacAuthorization must be a boolean or legacy null")
    return "rbac" if enabled is True else "accessPolicy"


def azure_json(*arguments):
    # Windows Azure CLI is a .cmd launcher; reject shell metacharacters before invocation.
    if any(re.search(r'[&|<>^%!"()\r\n]', argument) for argument in arguments):
        raise ValueError("Azure resource identifiers must not contain shell metacharacters")
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI executable is not available on PATH")
    result = subprocess.run(
        [executable, *arguments, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"Read-only Azure query failed ({arguments[0]} {arguments[1]}): {result.stderr.strip()}")
    return json.loads(result.stdout)


def validate_id(resource_id, subscription, kind):
    match = RESOURCE_ID.fullmatch(resource_id)
    if not match or match[3].lower() != kind.lower():
        raise ValueError(f"Expected a full {kind} ARM ID")
    if match[1].lower() != subscription.lower():
        raise ValueError("Existing resources must be in the deployment subscription")
    return match


def preflight(parameters, subscription, query=azure_json):
    value = lambda name, default=None: parameters.get(name, {}).get("value", default)
    vault_ids = [value("primaryVaultResourceId", ""), value("secondaryVaultResourceId", "")]
    if vault_ids[0] and vault_ids[0].lower() == vault_ids[1].lower():
        raise ValueError("Source and destination must be different vaults")
    names, prefix = value("secretNames", []), value("secretPrefix", "")
    all_secrets = value("replicateAllSecrets", not any(vault_ids))
    if not isinstance(names, list) or any(not isinstance(n, str) or not SECRET_NAME.fullmatch(n) for n in names):
        raise ValueError("secretNames must be an array of valid secret names")
    if not isinstance(prefix, str) or (prefix and not SECRET_NAME.fullmatch(prefix)):
        raise ValueError("secretPrefix must be a valid name prefix")
    if not isinstance(all_secrets, bool) or (not all_secrets and not names and not prefix):
        raise ValueError("Select names, a prefix, or explicitly set replicateAllSecrets = true")
    if not isinstance(value("allowUnmanagedOverwrite", False), bool):
        raise ValueError("allowUnmanagedOverwrite must be a boolean")
    for side in ("primary", "secondary"):
        if value(f"{side}VaultAuthorizationModel", "accessPolicy") not in ("accessPolicy", "rbac"):
            raise ValueError(f"{side}VaultAuthorizationModel must be accessPolicy or rbac")
    topics = None
    resolved_vaults = {}
    for side, vault_id in zip(("primary", "secondary"), vault_ids):
        topic_id = value(f"{side}SystemTopicResourceId", "")
        if topic_id and not vault_id:
            raise ValueError("Reusing a system topic requires an existing vault")
        if not vault_id:
            continue
        validate_id(vault_id, subscription, "Microsoft.KeyVault/vaults")
        vault = query("resource", "show", "--ids", vault_id, "--subscription", subscription)
        props = vault["properties"]
        if vault["location"].lower() != value(f"{side}Location", "eastus" if side == "primary" else "centralus").lower():
            raise ValueError(f"{side}Location must match the existing vault region")
        model = authorization_model(props)
        if vault.get("id", "").lower() != vault_id.lower():
            raise ValueError("Azure returned a different vault resource ID")
        tenant_id, vault_uri = props.get("tenantId"), props.get("vaultUri")
        if not isinstance(tenant_id, str):
            raise ValueError("Existing vault tenantId is missing")
        UUID(tenant_id)
        if not isinstance(vault_uri, str):
            raise ValueError("Existing vault vaultUri is missing")
        uri = urlsplit(vault_uri)
        if (
            uri.scheme != "https" or not uri.hostname
            or not uri.hostname.lower().startswith(vault_id.split("/")[-1].lower() + ".")
            or uri.username is not None or uri.password is not None
            or uri.port not in (None, 443) or uri.path not in ("", "/") or uri.query or uri.fragment
        ):
            raise ValueError("Existing vault vaultUri must be its HTTPS service root")
        if props.get("enablePurgeProtection") is not True or props.get("enableSoftDelete") is False:
            raise ValueError("Existing vault must have soft delete and purge protection")
        if (props.get("publicNetworkAccess") or "Enabled").lower() != "disabled":
            raise ValueError("Existing vault must already disable public access; shared access is never changed automatically")
        resolved_vaults[vault_id.lower()] = {
            "usesRbac": model == "rbac",
            "tenantId": tenant_id,
            "vaultUri": vault_uri.rstrip("/") + "/",
        }
        if topics is None:
            topics = query("eventgrid", "system-topic", "list", "--subscription", subscription)
        source_topics = [topic for topic in topics if topic.get("source", topic.get("properties", {}).get("source", "")).lower() == vault_id.lower()]
        if len(source_topics) > 1:
            raise ValueError("Multiple system topics found for one vault; review before integrating")
        if topic_id:
            validate_id(topic_id, subscription, "Microsoft.EventGrid/systemTopics")
            matches = [topic for topic in source_topics if topic["id"].lower() == topic_id.lower()]
            if not matches:
                raise ValueError("Configured existing system topic does not belong to the selected vault")
            topic = matches[0]
            if not topic.get("identity", {}).get("principalId"):
                raise ValueError("Existing system topic requires a system-assigned identity; enable separately with approval")
            if topic["location"].lower() != vault["location"].lower():
                raise ValueError("System topic and vault locations must match")
            topic_parts = topic["id"].split("/")
            subscriptions = query(
                "eventgrid", "system-topic", "event-subscription", "list",
                "--subscription", subscription, "--resource-group", topic_parts[4],
                "--system-topic-name", topic_parts[8],
            )
            workload = value("workload", "mrkv")
            for route in subscriptions:
                match = re.fullmatch(rf"{re.escape(workload)}-([a-z0-9]{{8}})-secret-events", route["name"])
                if not match:
                    continue
                props = route.get("properties", route)
                destination = props.get("deliveryWithResourceIdentity", {}).get("destination", {})
                short_side = "p" if side == "primary" else "s"
                stamp = f"{workload}-{short_side}-{match[1]}"
                queue = f"/subscriptions/{subscription}/resourceGroups/rg-{stamp}/providers/Microsoft.ServiceBus/namespaces/sb-{stamp}/queues/kv-events"
                if (
                    "mrkv-replication" not in props.get("labels", [])
                    or destination.get("properties", {}).get("resourceId", "").lower() != queue.lower()
                ):
                    raise ValueError("Reserved replication subscription name is already used by another integration")
        elif source_topics:
            topic = source_topics[0]
            # Permit retry of this deployment's own topic; never adopt an unrelated topic.
            short_side = "p" if side == "primary" else "s"
            workload = value("workload", "mrkv")
            pattern = rf"evgt-{re.escape(workload)}-{short_side}-[a-z0-9]{{8}}"
            own_group = topic["id"].split("/")[4] == "rg-" + topic["name"].removeprefix("evgt-")
            if not (
                re.fullmatch(pattern, topic["name"]) and own_group
                and topic.get("tags", {}).get("workload") == workload
                and topic.get("tags", {}).get("purpose") == "private-secret-replication-demo"
            ):
                raise ValueError("A system topic already exists; supply its ARM ID instead of creating another")
        print(f"Existing {side} vault preflight passed ({model}; model preserved): {vault_id}")
    return resolved_vaults


def existing_role_assignment_names(workload, subscription, query):
    identities = query("identity", "list", "--subscription", subscription)
    owned = {}
    for identity in identities:
        match = re.fullmatch(rf"id-{re.escape(workload)}-([ps])-([a-z0-9]{{8}})-worker", identity["name"])
        if not match:
            continue
        group = f"rg-{workload}-{match[1]}-{match[2]}"
        expected = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identity['name']}"
        if identity["id"].lower() != expected.lower():
            continue
        principal_id = identity.get("principalId", identity.get("properties", {}).get("principalId"))
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("Existing worker identity has no principal ID")
        UUID(principal_id)
        owned[principal_id.lower()] = identity["id"]
    if not owned:
        return {}
    assignments = query(
        "role", "assignment", "list", "--subscription", subscription, "--all",
        "--fill-principal-name", "false", "--fill-role-definition-name", "false",
    )
    names = {}
    for assignment in assignments:
        identity_id = owned.get(assignment.get("principalId", "").lower())
        scope = assignment.get("scope", "")
        if not identity_id or not RESOURCE_ID.fullmatch(scope) or "/microsoft.keyvault/vaults/" not in scope.lower():
            continue
        name, role_id = assignment["name"], assignment["roleDefinitionId"]
        UUID(name)
        key = f"{scope}|{identity_id}|{role_id}".lower()
        if key in names and names[key] != name:
            raise ValueError("Multiple existing role assignments match one worker, vault, and role")
        names[key] = name
    return names


def prepare_parameters(document, subscription, query=azure_json):
    prepared = copy.deepcopy(document)
    parameters = prepared["parameters"]
    resolved = preflight(parameters, subscription, query)
    uses_rbac = any(vault["usesRbac"] for vault in resolved.values()) or any(
        not parameters.get(f"{side}VaultResourceId", {}).get("value", "")
        and parameters.get(f"{side}VaultAuthorizationModel", {}).get("value", "accessPolicy") == "rbac"
        for side in ("primary", "secondary")
    )
    assignments = existing_role_assignment_names(
        parameters.get("workload", {}).get("value", "mrkv"), subscription, query,
    ) if uses_rbac else {}
    parameters["resolvedExistingVaults"] = {"value": resolved}
    parameters["resolvedVaultRoleAssignments"] = {"value": assignments}
    return prepared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--output-parameters", help="Write temporary ARM parameters with fresh non-secret discovery results")
    args = parser.parse_args()
    document = json.loads(Path(args.parameters).read_text(encoding="utf-8-sig"))
    if args.output_parameters:
        prepared = prepare_parameters(document, args.subscription)
        Path(args.output_parameters).write_text(json.dumps(prepared, indent=2), encoding="utf-8")
    else:
        preflight(document["parameters"], args.subscription)


if __name__ == "__main__":
    main()
