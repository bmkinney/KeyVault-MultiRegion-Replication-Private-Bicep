"""Network migration plans delete only endpoints this deployment created, and only in the wrong network."""

import copy
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from migrate_network import migrate

SUB = "00000000-0000-0000-0000-000000000000"
TOKEN = "abcd1234"
PRIMARY_GROUP, SECONDARY_GROUP = f"rg-mrkv-p-{TOKEN}", f"rg-mrkv-s-{TOKEN}"
PRIMARY_PREFIX, SECONDARY_PREFIX = f"mrkv-p-{TOKEN}", f"mrkv-s-{TOKEN}"
REUSED_VAULT = f"/subscriptions/{SUB}/resourceGroups/customer-vaults/providers/Microsoft.KeyVault/vaults/primary-vault"
SECONDARY_VAULT = f"/subscriptions/{SUB}/resourceGroups/{SECONDARY_GROUP}/providers/Microsoft.KeyVault/vaults/kv-{SECONDARY_PREFIX}"
NEW_PE_SUBNET = f"/subscriptions/{SUB}/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-eus/subnets/snet-pe"
NEW_APP_SUBNET = f"/subscriptions/{SUB}/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-eus/subnets/snet-func"
OLD_SUBNET = f"/subscriptions/{SUB}/resourceGroups/{PRIMARY_GROUP}/providers/Microsoft.Network/virtualNetworks/vnet-{PRIMARY_PREFIX}/subnets/endpoints"


def provider(group, kind, name):
    return f"/subscriptions/{SUB}/resourceGroups/{group}/providers/{kind}/{name}"


def endpoint(name, target, group_id, subnet=OLD_SUBNET):
    return {
        "name": name, "id": provider(PRIMARY_GROUP, "Microsoft.Network/privateEndpoints", name), "subnet": {"id": subnet},
        "privateLinkServiceConnections": [{"privateLinkServiceId": target, "groupIds": [group_id]}],
    }


def primary_endpoints():
    storage = provider(PRIMARY_GROUP, "Microsoft.Storage/storageAccounts", f"stmrkvp{TOKEN}run")
    return [
        endpoint(f"pe-{PRIMARY_PREFIX}-vault-local", REUSED_VAULT, "vault"),
        endpoint(f"pe-{PRIMARY_PREFIX}-vault-remote", SECONDARY_VAULT, "vault"),
        endpoint(f"pe-{PRIMARY_PREFIX}-bus", provider(PRIMARY_GROUP, "Microsoft.ServiceBus/namespaces", f"sb-{PRIMARY_PREFIX}"), "namespace"),
        endpoint(f"pe-{PRIMARY_PREFIX}-blob", storage, "blob"),
        endpoint(f"pe-{PRIMARY_PREFIX}-queue", storage, "queue"),
        endpoint(f"pe-{PRIMARY_PREFIX}-table", storage, "table"),
        endpoint(f"pe-{PRIMARY_PREFIX}-deadletter", provider(PRIMARY_GROUP, "Microsoft.Storage/storageAccounts", f"stmrkvp{TOKEN}dl"), "blob"),
        endpoint(f"pe-{PRIMARY_PREFIX}-monitor", provider(PRIMARY_GROUP, "Microsoft.Insights/privateLinkScopes", f"ampls-{PRIMARY_PREFIX}"), "azuremonitor"),
        endpoint(f"pe-{PRIMARY_PREFIX}-app", provider(PRIMARY_GROUP, "Microsoft.Web/sites", f"func-{PRIMARY_PREFIX}"), "sites"),
    ]


def values(**overrides):
    result = {
        "workload": "mrkv", "primaryNetworkMode": "existing", "primaryVnetName": "vnet-eus",
        "primaryVnetResourceGroupName": "rg-network", "primaryIntegrationSubnetName": "snet-func",
        "primaryPrivateEndpointSubnetName": "snet-pe", "primaryVaultResourceId": REUSED_VAULT,
        "primaryEndpointsToCreate": ["secondaryVault"],
    }
    result.update(overrides)
    return result


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.groups = [PRIMARY_GROUP, SECONDARY_GROUP, "rg-unrelated"]
        self.endpoints = {PRIMARY_GROUP: primary_endpoints(), SECONDARY_GROUP: []}
        self.settings = [{"name": "REPLICATION_ENABLED", "value": "false"}]
        self.vnet = {
            "id": f"/subscriptions/{SUB}/resourceGroups/rg-network/providers/Microsoft.Network/virtualNetworks/vnet-eus",
            "location": "eastus",
            "properties": {
                "provisioningState": "Succeeded", "addressSpace": {"addressPrefixes": ["10.50.0.0/16"]},
                "subnets": [
                    {"name": "snet-func", "properties": {"addressPrefix": "10.50.1.0/27", "delegations": [{"properties": {"serviceName": "Microsoft.App/environments"}}]}},
                    {"name": "snet-pe", "properties": {"addressPrefix": "10.50.2.0/24", "delegations": []}},
                ],
            },
        }
        self.old_vnet_exists = True
        self.keep_after_delete = False
        self.app_subnets = {
            group: f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Network/virtualNetworks/vnet-{group.removeprefix('rg-')}/subnets/integration"
            for group in (PRIMARY_GROUP, SECONDARY_GROUP)
        }
        self.keep_app_subnet = False
        self.fail_app_update = False
        self.secondary_vnet = copy.deepcopy(self.vnet)
        self.secondary_vnet["id"] = self.vnet["id"].replace("vnet-eus", "vnet-cus")
        self.secondary_vnet["location"] = "centralus"

    def query(self, *args):
        self.calls.append(args)
        option = lambda key: args[args.index(key) + 1]
        if args[:2] == ("group", "list"):
            return [{"name": name} for name in self.groups]
        if args[:3] == ("network", "private-endpoint", "list"):
            return copy.deepcopy(self.endpoints[option("--resource-group")])
        if args[:3] == ("network", "private-endpoint", "delete"):
            if not self.keep_after_delete:
                for items in self.endpoints.values():
                    items[:] = [item for item in items if item["id"].lower() != option("--ids").lower()]
            return None
        if args[:2] == ("resource", "show"):
            target = option("--ids")
            if "/virtualNetworks/vnet-eus" in target:
                return copy.deepcopy(self.vnet)
            if "/virtualNetworks/vnet-cus" in target:
                return copy.deepcopy(self.secondary_vnet)
            if "/virtualNetworks/vnet-mrkv" in target:
                if not self.old_vnet_exists:
                    raise RuntimeError("(ResourceNotFound) The Resource could not be found")
                return {"id": target}
            if "/sites/func-" in target:
                subnet = self.app_subnets[target.split("/")[4]]
                if subnet is None:
                    raise RuntimeError("(ResourceNotFound) The Resource could not be found")
                return {"properties": {"virtualNetworkSubnetId": subnet}}
        if args[:2] == ("resource", "update"):
            if self.fail_app_update:
                raise RuntimeError("Function app update failed")
            if not self.keep_app_subnet:
                self.app_subnets[option("--ids").split("/")[4]] = option("--set").split("=", 1)[1]
            return {}
        if args[:4] == ("network", "vnet", "subnet", "create"):
            self.vnet["properties"]["subnets"].append({
                "name": option("--name"), "properties": {
                    "addressPrefix": option("--address-prefixes"),
                    "delegations": [{"properties": {"serviceName": option("--delegations")}}],
                },
            })
            return {}
        if args[:4] == ("functionapp", "config", "appsettings", "list"):
            return copy.deepcopy(self.settings)
        if args[:4] == ("functionapp", "config", "appsettings", "set"):
            self.settings = [{"name": "REPLICATION_ENABLED", "value": "false"}]
            return []
        if args[:4] == ("network", "private-dns", "zone", "list"):
            return [{"name": "privatelink.vaultcore.azure.net"}]
        raise AssertionError(f"Unexpected query: {args}")

    def run_migration(self, config, apply=False, fence=False):
        output = StringIO()
        with redirect_stdout(output):
            migrate(config, SUB, apply, fence, self.query)
        return output.getvalue()

    def deleted(self):
        return [call[call.index("--ids") + 1].split("/")[-1] for call in self.calls if call[:3] == ("network", "private-endpoint", "delete")]

    def test_preview_reports_the_plan_and_changes_nothing(self):
        output = self.run_migration(values())
        self.assertIn(f"would delete pe-{PRIMARY_PREFIX}-bus", output)
        self.assertIn("Preview only", output)
        self.assertEqual(self.deleted(), [])
        self.assertFalse([call for call in self.calls if call[:4] == ("functionapp", "config", "appsettings", "set")])
        self.assertIn(f"would move Function app func-{PRIMARY_PREFIX} to {NEW_APP_SUBNET}", output)
        self.assertFalse([call for call in self.calls if call[:2] == ("resource", "update")
                          or call[:4] == ("network", "vnet", "subnet", "create")])

    def test_function_app_integration_moves_and_is_verified_before_endpoint_deletion(self):
        self.run_migration(values(), apply=True)
        self.assertEqual(self.app_subnets[PRIMARY_GROUP], NEW_APP_SUBNET)
        self.assertNotEqual(self.app_subnets[SECONDARY_GROUP], NEW_APP_SUBNET)
        update = next(index for index, call in enumerate(self.calls) if call[:2] == ("resource", "update"))
        deletion = next(index for index, call in enumerate(self.calls) if call[:3] == ("network", "private-endpoint", "delete"))
        self.assertLess(update, deletion)
        self.assertTrue(any(call[:2] == ("resource", "show") and "/sites/func-" in call[call.index("--ids") + 1]
                            for call in self.calls[update + 1:deletion]))

    def test_missing_unintegrated_and_already_moved_apps(self):
        for subnet in (None, "", NEW_APP_SUBNET.upper()):
            with self.subTest(subnet=subnet):
                self.calls.clear()
                self.app_subnets[PRIMARY_GROUP] = subnet
                self.run_migration(values(), apply=True)
                updates = [call for call in self.calls if call[:2] == ("resource", "update")]
                self.assertEqual(len(updates), 1 if subnet == "" else 0)

    def test_both_regions_move_to_their_own_configured_integration_subnets(self):
        self.run_migration(values(
            secondaryNetworkMode="existing", secondaryVnetName="vnet-cus",
            secondaryVnetResourceGroupName="rg-network", secondaryIntegrationSubnetName="snet-func",
            secondaryPrivateEndpointSubnetName="snet-pe",
        ), apply=True)
        self.assertEqual(self.app_subnets[PRIMARY_GROUP], NEW_APP_SUBNET)
        self.assertEqual(self.app_subnets[SECONDARY_GROUP], NEW_APP_SUBNET.replace("vnet-eus", "vnet-cus"))
        self.assertEqual(len([call for call in self.calls if call[:2] == ("resource", "update")]), 2)

    def test_failed_or_unverified_app_move_stops_before_endpoint_deletion(self):
        self.settings = [{"name": "REPLICATION_ENABLED", "value": "true"}]
        self.fail_app_update = True
        with self.assertRaisesRegex(RuntimeError, "Function app update failed"):
            self.run_migration(values(), apply=True, fence=True)
        self.assertEqual(self.deleted(), [])
        self.fail_app_update = False
        self.keep_app_subnet = True
        with self.assertRaisesRegex(RuntimeError, "integration did not move"):
            self.run_migration(values(), apply=True)
        self.assertEqual(self.deleted(), [])
        self.assertEqual(self.settings, [{"name": "REPLICATION_ENABLED", "value": "false"}])

    def test_new_subnet_is_created_only_when_missing_and_apply_is_requested(self):
        config = values(primaryNetworkMode="newSubnet", primaryIntegrationSubnetPrefix="10.50.1.0/27")
        self.vnet["properties"]["subnets"].pop(0)
        output = self.run_migration(config)
        self.assertIn(f"would create delegated integration subnet {NEW_APP_SUBNET}", output)
        self.assertFalse([call for call in self.calls if call[:4] == ("network", "vnet", "subnet", "create")])
        self.run_migration(config, apply=True)
        creation = next(index for index, call in enumerate(self.calls) if call[:4] == ("network", "vnet", "subnet", "create"))
        update = next(index for index, call in enumerate(self.calls) if call[:2] == ("resource", "update"))
        self.assertLess(creation, update)
        self.assertEqual(self.app_subnets[PRIMARY_GROUP], NEW_APP_SUBNET)
        self.calls.clear()
        self.run_migration(config, apply=True)
        self.assertFalse([call for call in self.calls if call[:4] == ("network", "vnet", "subnet", "create")
                          or call[:2] == ("resource", "update")])

    def test_existing_new_subnet_is_reused_without_being_modified(self):
        self.run_migration(values(primaryNetworkMode="newSubnet", primaryIntegrationSubnetPrefix="10.50.1.0/27"), apply=True)
        self.assertEqual(self.app_subnets[PRIMARY_GROUP], NEW_APP_SUBNET)
        self.assertFalse([call for call in self.calls if call[:4] == ("network", "vnet", "subnet", "create")])

    def test_only_endpoints_created_by_this_deployment_in_the_wrong_subnet_are_deleted(self):
        foreign = endpoint("pe-customer-sql", provider(PRIMARY_GROUP, "Microsoft.Sql/servers", "customer"), "sqlServer")
        lookalike = endpoint(f"pe-{PRIMARY_PREFIX}-queue-extra", REUSED_VAULT, "vault")
        wrong_target = endpoint(f"pe-{PRIMARY_PREFIX}-table", provider("customer-vaults", "Microsoft.Storage/storageAccounts", "othertable"), "table")
        already_moved = endpoint(f"pe-{PRIMARY_PREFIX}-monitor", provider(PRIMARY_GROUP, "Microsoft.Insights/privateLinkScopes", f"ampls-{PRIMARY_PREFIX}"), "azuremonitor", NEW_PE_SUBNET)
        endpoints = [item for item in self.endpoints[PRIMARY_GROUP] if not item["name"].endswith(("-table", "-monitor"))]
        self.endpoints[PRIMARY_GROUP] = endpoints + [foreign, lookalike, wrong_target, already_moved]
        output = self.run_migration(values(), apply=True)
        deleted = self.deleted()
        self.assertEqual(
            sorted(deleted),
            sorted(f"pe-{PRIMARY_PREFIX}-{suffix}" for suffix in ("vault-local", "vault-remote", "bus", "blob", "queue", "deadletter", "app")),
        )
        remaining = {item["name"] for item in self.endpoints[PRIMARY_GROUP]}
        self.assertEqual(remaining, {"pe-customer-sql", f"pe-{PRIMARY_PREFIX}-queue-extra", f"pe-{PRIMARY_PREFIX}-table", f"pe-{PRIMARY_PREFIX}-monitor"})
        self.assertIn("leave pe-customer-sql (not created by this deployment)", output)
        self.assertIn(f"keep pe-{PRIMARY_PREFIX}-monitor (already in the target subnet)", output)

    def test_other_resource_groups_and_new_regions_are_never_listed_or_changed(self):
        self.run_migration(values(), apply=True)
        listed = [call[call.index("--resource-group") + 1] for call in self.calls if call[:3] == ("network", "private-endpoint", "list")]
        self.assertEqual(set(listed), {PRIMARY_GROUP})

    def test_reused_vault_endpoints_are_flagged_when_they_will_not_be_recreated(self):
        output = self.run_migration(values())
        self.assertIn(f"WARNING pe-{PRIMARY_PREFIX}-vault-local is not recreated", output)
        self.assertNotIn(f"WARNING pe-{PRIMARY_PREFIX}-vault-remote", output)
        output = self.run_migration(values(primaryEndpointsToCreate=["primaryVault", "secondaryVault"]))
        self.assertNotIn("WARNING", output)

    def test_active_replication_blocks_deletion_unless_workers_are_fenced_first(self):
        self.settings = [{"name": "REPLICATION_ENABLED", "value": "True"}]
        with self.assertRaisesRegex(ValueError, "Fence the workers first"):
            self.run_migration(values(), apply=True)
        self.assertEqual(self.deleted(), [])
        self.assertFalse([call for call in self.calls if call[:2] == ("resource", "update")])
        output = self.run_migration(values(), apply=True, fence=True)
        self.assertIn("Fencing func-", output)
        order = [call[:3] if call[:3] == ("network", "private-endpoint", "delete") else call[:4] for call in self.calls]
        self.assertLess(order.index(("functionapp", "config", "appsettings", "set")), order.index(("network", "private-endpoint", "delete")))
        update = next(index for index, call in enumerate(self.calls) if call[:2] == ("resource", "update"))
        fencing = next(index for index, call in enumerate(self.calls) if call[:4] == ("functionapp", "config", "appsettings", "set"))
        self.assertLess(fencing, update)
        fence = next(call for call in self.calls if call[:4] == ("functionapp", "config", "appsettings", "set"))
        self.assertIn("REPLICATION_ENABLED=false", fence)
        self.assertIn("AzureWebJobs.Replicate.Disabled=true", fence)

    def test_invalid_targets_or_ambiguous_groups_stop_before_any_change(self):
        self.vnet["properties"]["subnets"][1]["properties"]["delegations"] = [{"properties": {"serviceName": "Microsoft.App/environments"}}]
        with self.assertRaises(ValueError):
            self.run_migration(values(), apply=True)
        self.assertEqual(self.deleted(), [])
        self.vnet["properties"]["subnets"][1]["properties"]["delegations"] = []
        self.groups.append("rg-mrkv-p-zzzz9999")
        self.groups.append("rg-mrkv-s-zzzz9999")
        with self.assertRaisesRegex(ValueError, "exactly one deployed pair"):
            self.run_migration(values(), apply=True)
        self.groups = [PRIMARY_GROUP]
        with self.assertRaisesRegex(ValueError, "exactly one deployed pair"):
            self.run_migration(values(), apply=True)
        self.assertEqual(self.deleted(), [])

    def test_nothing_to_migrate_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "nothing to migrate"):
            self.run_migration({"workload": "mrkv"}, apply=True)
        self.assertEqual(self.calls, [])

    def test_failed_deletion_is_reported_and_leftover_networks_are_listed_not_deleted(self):
        self.keep_after_delete = True
        with self.assertRaisesRegex(RuntimeError, "still present"):
            self.run_migration(values(), apply=True)
        self.keep_after_delete = False
        self.endpoints[PRIMARY_GROUP] = primary_endpoints()
        output = self.run_migration(values(), apply=True)
        self.assertIn(f"VNet vnet-{PRIMARY_PREFIX}", output)
        self.assertIn("private DNS zone privatelink.vaultcore.azure.net", output)
        self.assertFalse([call for call in self.calls if call[:3] in (("network", "vnet", "delete"), ("network", "private-dns", "zone"))
                          and "delete" in call])

    def test_rerun_after_migration_finds_nothing_to_delete(self):
        self.endpoints[PRIMARY_GROUP] = [
            {**item, "subnet": {"id": NEW_PE_SUBNET}} for item in primary_endpoints()
        ]
        output = self.run_migration(values(), apply=True)
        self.assertEqual(self.deleted(), [])
        self.assertNotIn("DELETE", output)


if __name__ == "__main__":
    unittest.main()
