"""Moves solution Function App integration and private endpoints onto reused networks.

Read-only unless --apply. It lists endpoints only in the two deployed solution resource
groups and deletes one only when its name, Private Link target, and subresource all match what
this deployment creates and it is not already in the configured endpoint subnet. Endpoints that
existed before the deployment, such as those a customer created for a reused vault, are never
touched. Old VNets and private DNS zones are reported, never deleted.
"""

import argparse
import json
import re
from pathlib import Path

from check_vault_integration import azure_text, preflight_networks

PROVIDERS = {
    "vault": "Microsoft.KeyVault/vaults",
    "bus": "Microsoft.ServiceBus/namespaces",
    "storage": "Microsoft.Storage/storageAccounts",
    "monitor": "Microsoft.Insights/privateLinkScopes",
    "app": "Microsoft.Web/sites",
}
FENCE_SETTINGS = ("REPLICATION_ENABLED=false", "AzureWebJobs.Replicate.Disabled=true", "AzureWebJobs.Reconcile.Disabled=true")
NOT_FOUND = re.compile(r"ResourceNotFound|ResourceGroupNotFound|could not be found")
SIDES = (("primary", "p"), ("secondary", "s"))


def azure_command(*arguments):
    text = azure_text(*arguments)
    return json.loads(text) if text.strip() else None


def lower(value):
    return (value or "").lower()


def solution_groups(workload, subscription, query):
    pattern = re.compile(rf"^rg-{re.escape(workload)}-([ps])-([a-z0-9]{{8}})$", re.IGNORECASE)
    found = {}
    for group in query("group", "list", "--subscription", subscription):
        match = pattern.fullmatch(group["name"])
        if match:
            found.setdefault(match[2].lower(), {})[match[1].lower()] = group["name"]
    complete = {token: sides for token, sides in found.items() if set(sides) == {"p", "s"}}
    if len(complete) != 1:
        raise ValueError(
            f"Expected exactly one deployed pair of rg-{workload}-p-<token> and rg-{workload}-s-<token> groups; "
            f"found {len(complete)}. Nothing was changed."
        )
    return next(iter(complete.values()))


def side_plan(side, short, group, other_vault, values, subscription, workload):
    """Return the endpoints this deployment creates for one region, keyed by name suffix."""
    prefix = group.removeprefix("rg-")
    base = f"/subscriptions/{subscription}/resourceGroups/{group}/providers/"
    token = prefix.rsplit("-", 1)[1]
    storage = f"{base}{PROVIDERS['storage']}/st{workload}{short}{token}"
    vault = values.get(f"{side}VaultResourceId") or f"{base}{PROVIDERS['vault']}/kv-{prefix}"
    expected = {
        "vault-local": (vault, "vault"),
        "vault-remote": (other_vault, "vault"),
        "bus": (f"{base}{PROVIDERS['bus']}/sb-{prefix}", "namespace"),
        "blob": (f"{storage}run", "blob"),
        "queue": (f"{storage}run", "queue"),
        "table": (f"{storage}run", "table"),
        "deadletter": (f"{storage}dl", "blob"),
        "monitor": (f"{base}{PROVIDERS['monitor']}/ampls-{prefix}", "azuremonitor"),
        "app": (f"{base}{PROVIDERS['app']}/func-{prefix}", "sites"),
    }
    return prefix, {f"pe-{prefix}-{suffix}".lower(): (suffix, *target) for suffix, target in expected.items()}


def connections(endpoint):
    for key in ("privateLinkServiceConnections", "manualPrivateLinkServiceConnections"):
        for item in endpoint.get(key) or []:
            properties = item.get("properties", item)
            yield lower(properties.get("privateLinkServiceId")), [lower(group) for group in properties.get("groupIds") or []]


def is_created_here(endpoint, expectation):
    _, target, group_id = expectation
    return any(service == lower(target) and group_id in groups for service, groups in connections(endpoint))


def target_subnets(side, values, subscription, workload):
    vnet = f"/subscriptions/{subscription}/resourceGroups/{values[side + 'VnetResourceGroupName']}/providers/Microsoft.Network/virtualNetworks/{values[side + 'VnetName']}"
    integration = values.get(side + "IntegrationSubnetName") or f"snet-{workload}-integration"
    return f"{vnet}/subnets/{values[side + 'PrivateEndpointSubnetName']}", f"{vnet}/subnets/{integration}"


def read_app(app_id, subscription, query):
    try:
        return query("resource", "show", "--ids", app_id, "--subscription", subscription, "--api-version", "2025-03-01")
    except RuntimeError as error:
        if NOT_FOUND.search(str(error)):
            return None
        raise


def worker_active(app_name, group, subscription, query):
    try:
        settings = query("functionapp", "config", "appsettings", "list", "--name", app_name,
                         "--resource-group", group, "--subscription", subscription)
    except RuntimeError as error:
        if NOT_FOUND.search(str(error)):
            return False
        raise
    return any(item.get("name") == "REPLICATION_ENABLED" and lower(item.get("value")) == "true" for item in settings)


def build_plan(values, subscription, query):
    workload = values.get("workload", "mrkv")
    modes = {side: values.get(f"{side}NetworkMode", "new") for side, _ in SIDES}
    if all(mode == "new" for mode in modes.values()):
        raise ValueError("Set primaryNetworkMode and/or secondaryNetworkMode to existing or newSubnet; there is nothing to migrate to")
    groups = solution_groups(workload, subscription, query)
    vaults = {}
    for side, short in SIDES:
        group = groups[short]
        vaults[side] = values.get(f"{side}VaultResourceId") or (
            f"/subscriptions/{subscription}/resourceGroups/{group}/providers/{PROVIDERS['vault']}/kv-{group.removeprefix('rg-')}"
        )
    plan = []
    for side, short in SIDES:
        if modes[side] == "new":
            plan.append({"side": side, "migrate": False})
            continue
        group = groups[short]
        other = "secondary" if side == "primary" else "primary"
        prefix, expected = side_plan(side, short, group, vaults[other], values, subscription, workload)
        endpoint_subnet, integration_subnet = target_subnets(side, values, subscription, workload)
        listed = query("network", "private-endpoint", "list", "--resource-group", group, "--subscription", subscription) or []
        item = {
            "side": side, "migrate": True, "group": group, "prefix": prefix, "app": f"func-{prefix}",
            "endpoint_subnet": endpoint_subnet, "integration_subnet": integration_subnet,
            "delete": [], "keep": [], "foreign": [], "not_recreated": [],
        }
        to_create = values.get(f"{side}EndpointsToCreate") or []
        for endpoint in listed:
            expectation = expected.get(endpoint["name"].lower())
            if expectation is None or not is_created_here(endpoint, expectation):
                item["foreign"].append(endpoint["name"])
            elif lower(endpoint.get("subnet", {}).get("id")) == lower(endpoint_subnet):
                item["keep"].append(endpoint["name"])
            else:
                item["delete"].append({"name": endpoint["name"], "id": endpoint["id"], "subnet": endpoint.get("subnet", {}).get("id", "")})
                suffix = expectation[0]
                if suffix.startswith("vault-"):
                    vault_side = side if suffix == "vault-local" else other
                    if values.get(f"{vault_side}VaultResourceId") and f"{vault_side}Vault" not in to_create:
                        item["not_recreated"].append(endpoint["name"])
        app = read_app(f"/subscriptions/{subscription}/resourceGroups/{group}/providers/{PROVIDERS['app']}/{item['app']}", subscription, query)
        item["app_subnet"] = None if app is None else (app.get("properties", {}).get("virtualNetworkSubnetId") or "")
        item["active"] = app is not None and worker_active(item["app"], group, subscription, query)
        item["move_app"] = app is not None and lower(item["app_subnet"]) != lower(integration_subnet)
        item["create_integration_subnet"] = False
        if item["move_app"] and modes[side] == "newSubnet":
            vnet_id, _, subnet_name = integration_subnet.rpartition("/subnets/")
            vnet = query("resource", "show", "--ids", vnet_id, "--subscription", subscription, "--api-version", "2025-05-01")
            item["create_integration_subnet"] = not any(
                lower(subnet["name"]) == lower(subnet_name) for subnet in vnet.get("properties", {}).get("subnets", [])
            )
        plan.append(item)
    return plan


def report_old_network(item, subscription, query):
    group = item["group"]
    leftovers = []
    vnet = f"vnet-{item['prefix']}"
    try:
        query("resource", "show", "--ids",
              f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.Network/virtualNetworks/{vnet}",
              "--subscription", subscription, "--api-version", "2025-05-01")
        leftovers.append(f"VNet {vnet}")
    except RuntimeError as error:
        if not NOT_FOUND.search(str(error)):
            raise
    for zone in query("network", "private-dns", "zone", "list", "--resource-group", group, "--subscription", subscription) or []:
        leftovers.append(f"private DNS zone {zone['name']}")
    return leftovers


def migrate(values, subscription, apply, fence, query=azure_command):
    preflight_networks({key: {"value": value} for key, value in values.items()}, subscription, query)
    plan = build_plan(values, subscription, query)
    for item in plan:
        if not item["migrate"]:
            print(f"{item['side']}: network mode is new; nothing to migrate.")
            continue
        print(f"{item['side']} ({item['group']}) -> endpoint subnet {item['endpoint_subnet']}")
        if item["app_subnet"] is None:
            print(f"  Function app {item['app']}: not found")
        else:
            print(f"  Function app {item['app']}: integration subnet is {item['app_subnet'] or 'not set'}; "
                  f"replication active: {item['active']}")
            if item["create_integration_subnet"]:
                print(f"  {'CREATE' if apply else 'would create'} delegated integration subnet {item['integration_subnet']}")
            if item["move_app"]:
                print(f"  {'MOVE' if apply else 'would move'} Function app {item['app']} to {item['integration_subnet']}")
            else:
                print(f"  keep Function app {item['app']} integration (already in the target subnet)")
        for entry in item["delete"]:
            print(f"  {'DELETE' if apply else 'would delete'} {entry['name']} (created by this deployment, currently in {entry['subnet']})")
        for name in item["keep"]:
            print(f"  keep {name} (already in the target subnet)")
        for name in item["foreign"]:
            print(f"  leave {name} (not created by this deployment)")
        for name in item["not_recreated"]:
            print(f"  WARNING {name} is not recreated: its vault is reused and not listed in {item['side']}EndpointsToCreate; "
                  "the reused network must already reach that vault privately")
    active = [item for item in plan if item.get("active")]
    if active and not fence:
        names = ", ".join(item["app"] for item in active)
        if apply:
            raise ValueError(f"Replication is active on {names}. Fence the workers first (operations guide) or pass -FenceWorkers. Nothing was deleted.")
        print(f"Replication is active on {names}; -Apply requires -FenceWorkers or a prior manual fence.")
    if not apply:
        print("Preview only. Nothing was changed. Rerun with -Apply to move Function App integration and replace the listed endpoints.")
        return plan
    for item in active:
        print(f"Fencing {item['app']}")
        query("functionapp", "config", "appsettings", "set", "--name", item["app"], "--resource-group", item["group"],
              "--subscription", subscription, "--settings", *FENCE_SETTINGS)
    for item in plan:
        if not item["migrate"]:
            continue
        if item["move_app"]:
            side = item["side"]
            if item["create_integration_subnet"]:
                query("network", "vnet", "subnet", "create", "--resource-group", values[side + "VnetResourceGroupName"],
                      "--vnet-name", values[side + "VnetName"], "--name", item["integration_subnet"].rsplit("/", 1)[1],
                      "--address-prefixes", values[side + "IntegrationSubnetPrefix"],
                      "--delegations", "Microsoft.App/environments", "--subscription", subscription)
            app_id = f"/subscriptions/{subscription}/resourceGroups/{item['group']}/providers/{PROVIDERS['app']}/{item['app']}"
            # The preflight already validated the Flex delegation; skip the CLI's Microsoft.Web/serverFarms check.
            query("functionapp", "vnet-integration", "add", "--name", item["app"], "--resource-group", item["group"],
                  "--vnet", item["integration_subnet"].rpartition("/subnets/")[0], "--subnet", item["integration_subnet"],
                  "--skip-delegation-check", "true", "--subscription", subscription)
            app = read_app(app_id, subscription, query)
            actual_subnet = "" if app is None else app.get("properties", {}).get("virtualNetworkSubnetId")
            if lower(actual_subnet) != lower(item["integration_subnet"]):
                raise RuntimeError(f"Function app {item['app']} integration did not move to {item['integration_subnet']}; "
                                   "no endpoints in this region were deleted. Workers remain fenced if fencing was requested.")
        for entry in item["delete"]:
            query("network", "private-endpoint", "delete", "--ids", entry["id"], "--subscription", subscription)
        remaining = {lower(endpoint["name"]) for endpoint in
                     query("network", "private-endpoint", "list", "--resource-group", item["group"], "--subscription", subscription) or []}
        stuck = [entry["name"] for entry in item["delete"] if entry["name"].lower() in remaining]
        if stuck:
            raise RuntimeError(f"Endpoints still present after deletion: {', '.join(stuck)}")
    print("Function App integration migrated and old endpoints removed. Run Deploy.ps1 -Apply with the same parameters "
          "to recreate the endpoints in the reused subnets. Keep workers fenced until deployment and verification complete.")
    for item in plan:
        if item["migrate"]:
            for leftover in report_old_network(item, subscription, query):
                print(f"  Not deleted, review after verifying the new network ({item['side']}): {leftover}")
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--apply", action="store_true", help="Move Function App integration and delete solution endpoints in the wrong network")
    parser.add_argument("--fence-workers", action="store_true", help="Fence active workers before moving integration or deleting endpoints")
    args = parser.parse_args()
    document = json.loads(Path(args.parameters).read_text(encoding="utf-8-sig"))
    values = {key: item.get("value") for key, item in document["parameters"].items()}
    migrate(values, args.subscription, args.apply, args.fence_workers)


if __name__ == "__main__":
    main()
