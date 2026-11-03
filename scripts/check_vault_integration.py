"""Read-only existing-vault preflight. Never retrieves secret values or changes Azure."""

import argparse
import copy
import ipaddress
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

RESOURCE_ID = re.compile(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/"
    r"(Microsoft\.KeyVault/vaults|Microsoft\.EventGrid/systemTopics|Microsoft\.ManagedIdentity/userAssignedIdentities|Microsoft\.OperationalInsights/workspaces)/([^/]+)$",
    re.IGNORECASE,
)
WORKSPACE_KIND = "Microsoft.OperationalInsights/workspaces"
# Referenced by ID only, so these may live in any subscription of the deployment tenant.
CROSS_SUBSCRIPTION_KINDS = {"microsoft.managedidentity/userassignedidentities", WORKSPACE_KIND.lower()}
SECRET_NAME = re.compile(r"^[A-Za-z0-9-]{1,127}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._()-]{0,89}$")
NETWORK_MODES = ("new", "newSubnet", "existing")
ENDPOINT_VAULTS = ("primaryVault", "secondaryVault")
FLEX_DELEGATION = "microsoft.app/environments"
# Flex Consumption needs a /27 or larger; the portal and ARM reject smaller delegated subnets.
MIN_INTEGRATION_PREFIX = 27
DNS_ZONE_ID = re.compile(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/providers/Microsoft\.Network/privateDnsZones/([^/]+)$",
    re.IGNORECASE,
)
# Storage zone suffixes depend on the cloud, so those names are matched by service prefix.
DNS_ZONE_NAMES = {
    "vault": r"privatelink\.vaultcore\.azure\.net",
    "bus": r"privatelink\.servicebus\.windows\.net",
    "blob": r"privatelink\.blob\..+",
    "queue": r"privatelink\.queue\..+",
    "table": r"privatelink\.table\..+",
    "app": r"privatelink\.azurewebsites\.net",
    "monitor": r"privatelink\.monitor\.azure\.com",
    "oms": r"privatelink\.oms\.opinsights\.azure\.com",
    "ods": r"privatelink\.ods\.opinsights\.azure\.com",
    "agent": r"privatelink\.agentsvc\.azure-automation\.net",
}


def authorization_model(properties):
    enabled = properties.get("enableRbacAuthorization")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError("Vault enableRbacAuthorization must be a boolean or legacy null")
    return "rbac" if enabled is True else "accessPolicy"


def azure_text(*arguments, timeout=120):
    # Windows Azure CLI is a .cmd launcher; reject shell metacharacters before invocation.
    if any(re.search(r'[&|<>^%!"()\r\n]', argument) for argument in arguments):
        raise ValueError("Azure resource identifiers must not contain shell metacharacters")
    executable = shutil.which("az")
    if executable is None:
        raise RuntimeError("Azure CLI executable is not available on PATH")
    result = subprocess.run(
        [executable, *arguments, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"Azure CLI command failed ({arguments[0]} {arguments[1]}): {result.stderr.strip()}")
    return result.stdout


def azure_json(*arguments, timeout=120):
    return json.loads(azure_text(*arguments, timeout=timeout))


def validate_id(resource_id, subscription, kind):
    match = RESOURCE_ID.fullmatch(resource_id)
    if not match or match[3].lower() != kind.lower():
        raise ValueError(f"Expected a full {kind} ARM ID")
    if kind.lower() in CROSS_SUBSCRIPTION_KINDS:
        UUID(match[1])
    elif match[1].lower() != subscription.lower():
        raise ValueError("Existing resources must be in the deployment subscription")
    return match


def preflight(parameters, subscription, query=azure_json):
    value = lambda name, default=None: parameters.get(name, {}).get("value", default)
    manage = value("managePermissions", True)
    if not isinstance(manage, bool):
        raise ValueError("managePermissions must be a boolean")
    worker_ids = [value(f"{side}WorkerIdentityResourceId", "") for side in ("primary", "secondary")]
    for worker_id in worker_ids:
        if not isinstance(worker_id, str):
            raise ValueError("Worker identity resource IDs must be strings")
        if worker_id:
            validate_id(worker_id, subscription, "Microsoft.ManagedIdentity/userAssignedIdentities")
    if worker_ids[0] and worker_ids[0].lower() == worker_ids[1].lower():
        raise ValueError("Regional workers must use different identities")
    if not manage:
        for side in ("primary", "secondary"):
            for suffix in ("WorkerIdentityResourceId", "VaultResourceId", "SystemTopicResourceId"):
                if not value(f"{side}{suffix}", ""):
                    raise ValueError(f"managePermissions = false requires {side}{suffix}; administrators must provision resources and grants first")
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
            topic_group = topic["id"].split("/")[4].lower()
            own_group = topic_group in ("rg-" + topic["name"].removeprefix("evgt-"), vault_id.split("/")[4].lower())
            if not (
                re.fullmatch(pattern, topic["name"]) and own_group
                and topic.get("tags", {}).get("workload") == workload
                and topic.get("tags", {}).get("purpose") == "private-secret-replication-demo"
            ):
                raise ValueError("A system topic already exists; supply its ARM ID instead of creating another")
        print(f"Existing {side} vault preflight passed ({model}; model preserved): {vault_id}")
    return resolved_vaults


def preflight_workers(parameters, subscription, vaults, query):
    worker_ids = [parameters.get(f"{side}WorkerIdentityResourceId", {}).get("value", "") for side in ("primary", "secondary")]
    if not any(worker_ids):
        return {}
    account = query("account", "show", "--subscription", subscription)
    tenant_id = account["tenantId"]
    UUID(tenant_id)
    if any(vault["tenantId"].lower() != tenant_id.lower() for vault in vaults.values()):
        raise ValueError("Selected vaults and worker identities must share the deployment tenant")
    resolved = {}
    for worker_id in worker_ids:
        if not worker_id:
            continue
        identity_subscription = validate_id(worker_id, subscription, "Microsoft.ManagedIdentity/userAssignedIdentities")[1]
        identity = query(
            "resource", "show", "--ids", worker_id, "--subscription", identity_subscription,
            "--api-version", "2024-11-30",
        )
        if identity.get("id", "").lower() != worker_id.lower():
            raise ValueError("Azure returned a different worker identity resource ID")
        props = identity["properties"]
        metadata = {}
        for field in ("principalId", "clientId", "tenantId"):
            value = props.get(field)
            if not isinstance(value, str):
                raise ValueError(f"Existing worker identity {field} is missing")
            UUID(value)
            metadata[field] = value
        if metadata["tenantId"].lower() != tenant_id.lower():
            raise ValueError("Existing worker identity belongs to a different tenant")
        if any(item["principalId"].lower() == metadata["principalId"].lower() for item in resolved.values()):
            raise ValueError("Regional worker identities must have different principal IDs")
        resolved[worker_id.lower()] = metadata
    return resolved


def preflight_workspaces(parameters, subscription, query):
    value = lambda name, default=None: parameters.get(name, {}).get("value", default)
    workspace_ids = [value(f"{side}LogAnalyticsWorkspaceResourceId", "") for side in ("primary", "secondary")]
    for workspace_id in workspace_ids:
        if not isinstance(workspace_id, str):
            raise ValueError("Log Analytics workspace resource IDs must be strings")
    if not isinstance(value("linkExistingWorkspaceToPrivateLinkScope", True), bool):
        raise ValueError("linkExistingWorkspaceToPrivateLinkScope must be a boolean")
    unique = {item.lower(): item for item in workspace_ids if item}
    if not unique:
        return
    deployment_tenant = query("account", "show", "--subscription", subscription)["tenantId"].lower()
    for workspace_id in unique.values():
        workspace_subscription = validate_id(workspace_id, subscription, WORKSPACE_KIND)[1]
        if query("account", "show", "--subscription", workspace_subscription)["tenantId"].lower() != deployment_tenant:
            raise ValueError("Existing Log Analytics workspace must be in the deployment tenant")
        workspace = query(
            "resource", "show", "--ids", workspace_id, "--subscription", workspace_subscription,
            "--api-version", "2025-07-01",
        )
        if workspace.get("id", "").lower() != workspace_id.lower():
            raise ValueError("Azure returned a different Log Analytics workspace resource ID")
        if workspace.get("properties", {}).get("provisioningState") != "Succeeded":
            raise ValueError("Existing Log Analytics workspace is not in a Succeeded state")
        print(f"Existing Log Analytics workspace preflight passed: {workspace_id}")


def location_key(value):
    return re.sub(r"\s", "", value).lower()


def subnet_networks(properties):
    prefixes = ([properties["addressPrefix"]] if properties.get("addressPrefix") else []) + list(properties.get("addressPrefixes") or [])
    return [network for network in (ipaddress.ip_network(item) for item in prefixes) if network.version == 4]


def delegation_names(subnet):
    return sorted(
        item.get("properties", {}).get("serviceName", "").lower() for item in subnet.get("properties", {}).get("delegations") or []
    )


def preflight_networks(parameters, subscription, query):
    value = lambda name, default=None: parameters.get(name, {}).get("value", default)
    workload = value("workload", "mrkv")
    checked_zones = set()
    for side, default_location in (("primary", "eastus"), ("secondary", "centralus")):
        mode = value(f"{side}NetworkMode", "new")
        if mode not in NETWORK_MODES:
            raise ValueError(f"{side}NetworkMode must be one of: {', '.join(NETWORK_MODES)}")
        if mode == "new":
            continue

        def text(suffix):
            item = value(f"{side}{suffix}", "")
            if not isinstance(item, str):
                raise ValueError(f"{side}{suffix} must be a string")
            return item

        vnet_name, vnet_group = text("VnetName"), text("VnetResourceGroupName")
        if not vnet_name or not vnet_group:
            raise ValueError(f"{side}NetworkMode is {mode}, so set {side}VnetName and {side}VnetResourceGroupName")
        integration, endpoint, prefix = text("IntegrationSubnetName"), text("PrivateEndpointSubnetName"), text("IntegrationSubnetPrefix")
        if mode == "existing" and not integration:
            raise ValueError(f"{side}NetworkMode is existing, so set {side}IntegrationSubnetName")
        if mode == "newSubnet" and not prefix:
            raise ValueError(f"{side}NetworkMode is newSubnet, so set {side}IntegrationSubnetPrefix")
        if not endpoint:
            raise ValueError(f"This deployment creates private endpoints in the existing {side} VNet, so set {side}PrivateEndpointSubnetName")
        integration = integration or f"snet-{workload}-integration"
        for name in (vnet_name, vnet_group, integration, endpoint):
            if not SAFE_NAME.fullmatch(name):
                raise ValueError(f"Invalid {side} network resource name: {name}")
        if integration.lower() == endpoint.lower():
            raise ValueError(f"The {side} integration and private endpoint subnets must be different subnets")
        to_create = value(f"{side}EndpointsToCreate", [])
        if not isinstance(to_create, list) or any(item not in ENDPOINT_VAULTS for item in to_create):
            raise ValueError(f"{side}EndpointsToCreate accepts only: {', '.join(ENDPOINT_VAULTS)}")
        zones = value(f"{side}PrivateDnsZoneIds", {})
        if not isinstance(zones, dict):
            raise ValueError(f"{side}PrivateDnsZoneIds must be an object")
        zone_ids = []
        for key, zone_id in zones.items():
            if key not in DNS_ZONE_NAMES:
                raise ValueError(f"{side}PrivateDnsZoneIds key {key} is not one of: {', '.join(DNS_ZONE_NAMES)}")
            if not isinstance(zone_id, str):
                raise ValueError(f"{side}PrivateDnsZoneIds.{key} must be a string")
            if not zone_id:
                continue
            match = DNS_ZONE_ID.fullmatch(zone_id)
            if not match or not re.fullmatch(DNS_ZONE_NAMES[key], match[3], re.IGNORECASE):
                raise ValueError(f"{side}PrivateDnsZoneIds.{key} must be the resource ID of the matching privatelink private DNS zone for {key}")
            UUID(match[1])
            zone_ids.append((zone_id, match[1]))

        location = value(f"{side}Location", default_location)
        vnet_id = f"/subscriptions/{subscription}/resourceGroups/{vnet_group}/providers/Microsoft.Network/virtualNetworks/{vnet_name}"
        vnet = query("resource", "show", "--ids", vnet_id, "--subscription", subscription, "--api-version", "2025-05-01")
        if vnet.get("id", "").lower() != vnet_id.lower():
            raise ValueError(f"Azure returned a different {side} VNet resource ID")
        properties = vnet.get("properties", {})
        if properties.get("provisioningState") != "Succeeded":
            raise ValueError(f"The {side} VNet is not in a Succeeded state")
        if location_key(vnet.get("location", "")) != location_key(location):
            raise ValueError(f"The {side} VNet is in {vnet.get('location')}, but {side}Location is {location}; private endpoints and VNet integration must use the VNet region")
        subnets = {item["name"].lower(): item for item in properties.get("subnets", [])}

        endpoint_subnet = subnets.get(endpoint.lower())
        if endpoint_subnet is None:
            raise ValueError(f"The {side} VNet has no private endpoint subnet named {endpoint}")
        if delegation_names(endpoint_subnet):
            raise ValueError(f"The {side} private endpoint subnet {endpoint} is delegated, and private endpoints cannot use a delegated subnet")

        existing_subnet = subnets.get(integration.lower())
        if mode == "existing":
            if existing_subnet is None:
                raise ValueError(f"The {side} VNet has no integration subnet named {integration}")
        else:
            try:
                requested = ipaddress.ip_network(prefix)
            except ValueError:
                raise ValueError(f"{side}IntegrationSubnetPrefix must be a CIDR prefix such as 10.0.4.0/27") from None
            if requested.version != 4 or requested.prefixlen > MIN_INTEGRATION_PREFIX:
                raise ValueError(f"{side}IntegrationSubnetPrefix must be an IPv4 prefix of /{MIN_INTEGRATION_PREFIX} or larger")
            spaces = [ipaddress.ip_network(item) for item in properties.get("addressSpace", {}).get("addressPrefixes", [])]
            if not any(requested.subnet_of(space) for space in spaces if space.version == 4):
                raise ValueError(f"{side}IntegrationSubnetPrefix {prefix} is outside the {side} VNet address space")
            for name, other in subnets.items():
                if name != integration.lower() and any(requested.overlaps(network) for network in subnet_networks(other.get("properties", {}))):
                    raise ValueError(f"{side}IntegrationSubnetPrefix {prefix} overlaps subnet {other['name']}")
            if existing_subnet is not None and [str(item) for item in subnet_networks(existing_subnet.get("properties", {}))] != [str(requested)]:
                raise ValueError(f"Subnet {integration} already exists in the {side} VNet with a different address prefix")
        if existing_subnet is not None:
            if delegation_names(existing_subnet) != [FLEX_DELEGATION]:
                raise ValueError(f"The {side} integration subnet {integration} must be delegated only to Microsoft.App/environments")
            networks = subnet_networks(existing_subnet.get("properties", {}))
            if not networks or any(network.prefixlen > MIN_INTEGRATION_PREFIX for network in networks):
                raise ValueError(f"The {side} integration subnet {integration} must be /{MIN_INTEGRATION_PREFIX} or larger")

        for zone_id, zone_subscription in zone_ids:
            if zone_id.lower() in checked_zones:
                continue
            checked_zones.add(zone_id.lower())
            try:
                zone = query("resource", "show", "--ids", zone_id, "--subscription", zone_subscription, "--api-version", "2024-06-01")
            except RuntimeError as error:
                if re.search(r"ResourceNotFound|ResourceGroupNotFound|SubscriptionNotFound|could not be found", str(error)):
                    raise ValueError(f"Private DNS zone not found: {zone_id}") from None
                print(f"Warning: could not read private DNS zone {zone_id}; verify it manually. {error}")
                continue
            if zone.get("id", "").lower() != zone_id.lower():
                raise ValueError("Azure returned a different private DNS zone resource ID")
        print(f"Existing {side} network preflight passed ({mode}): {vnet_id}")


def existing_role_assignment_names(workload, subscription, query, known_identities=None, vault_scopes=None):
    known_identities = known_identities or {}
    identities = query("identity", "list", "--subscription", subscription) if len(known_identities) < 2 else []
    owned = {metadata["principalId"].lower(): identity_id for identity_id, metadata in known_identities.items()}
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
    assignments = []
    for scope in vault_scopes or [None]:
        assignments.extend(query(
            "role", "assignment", "list", "--subscription", subscription,
            *(("--scope", scope) if scope else ("--all",)),
            "--fill-principal-name", "false", "--fill-role-definition-name", "false",
        ))
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
    workers = preflight_workers(parameters, subscription, resolved, query)
    preflight_workspaces(parameters, subscription, query)
    preflight_networks(parameters, subscription, query)
    uses_rbac = any(vault["usesRbac"] for vault in resolved.values()) or any(
        not parameters.get(f"{side}VaultResourceId", {}).get("value", "")
        and parameters.get(f"{side}VaultAuthorizationModel", {}).get("value", "accessPolicy") == "rbac"
        for side in ("primary", "secondary")
    )
    assignments = existing_role_assignment_names(
        parameters.get("workload", {}).get("value", "mrkv"), subscription, query, workers,
    ) if uses_rbac and parameters.get("managePermissions", {}).get("value", True) else {}
    parameters["resolvedExistingVaults"] = {"value": resolved}
    parameters["resolvedVaultRoleAssignments"] = {"value": assignments}
    parameters["resolvedWorkerIdentities"] = {"value": workers}
    parameters["resolvedOwnedFoundationDiagnostics"] = {"value": {}}
    if not parameters.get("managePermissions", {}).get("value", True):
        print("This infrastructure stage omits IAM writes and role-assignment inventory. Effective runtime access must be verified separately.")
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
        vaults = preflight(document["parameters"], args.subscription)
        preflight_workers(document["parameters"], args.subscription, vaults, azure_json)
        preflight_workspaces(document["parameters"], args.subscription, azure_json)
        preflight_networks(document["parameters"], args.subscription, azure_json)


if __name__ == "__main__":
    main()
