"""Read-only validation of reused VNets, subnets, and private DNS zones."""

import copy
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_vault_integration import prepare_parameters, preflight_networks

SUB = "00000000-0000-0000-0000-000000000000"
HUB = "22222222-2222-2222-2222-222222222222"
TENANT = "11111111-1111-1111-1111-111111111111"


def vnet_id(name, group="rg-network"):
    return f"/subscriptions/{SUB}/resourceGroups/{group}/providers/Microsoft.Network/virtualNetworks/{name}"


def zone_id(name, subscription=HUB):
    return f"/subscriptions/{subscription}/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/{name}"


def subnet(name, prefix, delegation=None):
    return {
        "name": name,
        "properties": {
            "addressPrefix": prefix,
            "delegations": [{"properties": {"serviceName": delegation}}] if delegation else [],
        },
    }


def vnet(name, location, subnets):
    return {
        "id": vnet_id(name), "location": location,
        "properties": {
            "provisioningState": "Succeeded", "addressSpace": {"addressPrefixes": ["10.50.0.0/16"]}, "subnets": subnets,
        },
    }


def parameters(**values):
    return {key: {"value": value} for key, value in values.items()}


def existing_parameters(**overrides):
    values = {
        "primaryNetworkMode": "existing", "primaryVnetName": "vnet-eus", "primaryVnetResourceGroupName": "rg-network",
        "primaryIntegrationSubnetName": "snet-func", "primaryPrivateEndpointSubnetName": "snet-pe",
    }
    values.update(overrides)
    return parameters(**values)


class NetworkPreflightTests(unittest.TestCase):
    def setUp(self):
        self.resources = {
            vnet_id("vnet-eus").lower(): vnet("vnet-eus", "eastus", [
                subnet("snet-func", "10.50.1.0/27", "Microsoft.App/environments"),
                subnet("snet-pe", "10.50.2.0/24"),
                subnet("snet-other", "10.50.3.0/24"),
            ]),
            vnet_id("vnet-cus").lower(): vnet("vnet-cus", "centralus", [
                subnet("snet-func", "10.60.1.0/27", "Microsoft.App/environments"),
                subnet("snet-pe", "10.60.2.0/24"),
            ]),
        }
        self.resources[vnet_id("vnet-cus").lower()]["properties"]["addressSpace"]["addressPrefixes"] = ["10.60.0.0/16"]
        self.zones = {}
        self.calls = []

    def query(self, *args):
        self.calls.append(args)
        if args[:2] != ("resource", "show"):
            raise AssertionError(f"Unexpected query: {args}")
        target = args[args.index("--ids") + 1].lower()
        if "/privatednszones/" in target:
            if target not in self.zones:
                raise RuntimeError("(ResourceNotFound) The Resource could not be found")
            result = self.zones[target]
            if isinstance(result, Exception):
                raise result
            return {"id": target if result is True else result}
        return copy.deepcopy(self.resources[target])

    def run_preflight(self, document):
        output = StringIO()
        with redirect_stdout(output):
            preflight_networks(document, SUB, self.query)
        return output.getvalue()

    def vnet(self, side="primary"):
        return self.resources[vnet_id("vnet-eus" if side == "primary" else "vnet-cus").lower()]

    def test_default_and_new_mode_never_query_azure(self):
        self.run_preflight(parameters(workload="mrkv"))
        self.run_preflight(parameters(primaryNetworkMode="new", secondaryNetworkMode="new", primaryVnetName="ignored"))
        self.assertEqual(self.calls, [])

    def test_existing_vnet_with_a_delegated_integration_subnet_passes(self):
        output = self.run_preflight(existing_parameters())
        self.assertIn("primary network preflight passed (existing)", output)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][self.calls[0].index("--subscription") + 1], SUB)

    def test_each_region_is_checked_against_its_own_location(self):
        document = existing_parameters(
            secondaryNetworkMode="existing", secondaryVnetName="vnet-cus", secondaryVnetResourceGroupName="rg-network",
            secondaryIntegrationSubnetName="snet-func", secondaryPrivateEndpointSubnetName="snet-pe",
        )
        output = self.run_preflight(document)
        self.assertIn("secondary network preflight passed", output)
        document["secondaryVnetName"] = {"value": "vnet-eus"}
        with self.assertRaisesRegex(ValueError, "private endpoints and VNet integration must use the VNet region"):
            self.run_preflight(document)

    def test_missing_or_invalid_inputs_are_rejected_before_any_query(self):
        for overrides in (
            {"primaryNetworkMode": "shared"},
            {"primaryVnetName": ""},
            {"primaryVnetResourceGroupName": ""},
            {"primaryIntegrationSubnetName": ""},
            {"primaryPrivateEndpointSubnetName": ""},
            {"primaryPrivateEndpointSubnetName": "snet-func"},
            {"primaryVnetName": "vnet/../other"},
            {"primaryVnetName": None},
            {"primaryEndpointsToCreate": ["primaryStorage"]},
            {"primaryEndpointsToCreate": "primaryVault"},
            {"primaryPrivateDnsZoneIds": []},
            {"primaryPrivateDnsZoneIds": {"file": zone_id("privatelink.file.core.windows.net")}},
            {"primaryPrivateDnsZoneIds": {"vault": 5}},
            {"primaryPrivateDnsZoneIds": {"vault": zone_id("privatelink.blob.core.windows.net")}},
            {"primaryPrivateDnsZoneIds": {"vault": "privatelink.vaultcore.azure.net"}},
            {"primaryPrivateDnsZoneIds": {"vault": zone_id("privatelink.vaultcore.azure.net", "not-a-guid")}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.run_preflight(existing_parameters(**overrides))
            self.assertEqual(self.calls, [])

    def test_vnet_state_identity_and_subnets_are_enforced(self):
        self.vnet()["properties"]["provisioningState"] = "Updating"
        with self.assertRaisesRegex(ValueError, "Succeeded"):
            self.run_preflight(existing_parameters())
        self.vnet()["properties"]["provisioningState"] = "Succeeded"
        self.vnet()["id"] = vnet_id("different")
        with self.assertRaisesRegex(ValueError, "different"):
            self.run_preflight(existing_parameters())
        self.vnet()["id"] = vnet_id("vnet-eus")
        self.run_preflight(existing_parameters())
        with self.assertRaisesRegex(ValueError, "no private endpoint subnet named missing"):
            self.run_preflight(existing_parameters(primaryPrivateEndpointSubnetName="missing"))
        with self.assertRaisesRegex(ValueError, "no integration subnet named missing"):
            self.run_preflight(existing_parameters(primaryIntegrationSubnetName="missing"))

    def test_integration_subnet_must_be_flex_delegated_and_large_enough(self):
        integration = self.vnet()["properties"]["subnets"][0]
        for delegations, prefix in (
            ([], "10.50.1.0/27"),
            ([{"properties": {"serviceName": "Microsoft.Web/serverFarms"}}], "10.50.1.0/27"),
            ([{"properties": {"serviceName": "Microsoft.App/environments"}}, {"properties": {"serviceName": "Microsoft.Sql/managedInstances"}}], "10.50.1.0/27"),
            ([{"properties": {"serviceName": "Microsoft.App/environments"}}], "10.50.1.0/28"),
        ):
            with self.subTest(delegations=delegations, prefix=prefix):
                integration["properties"]["delegations"] = delegations
                integration["properties"]["addressPrefix"] = prefix
                with self.assertRaises(ValueError):
                    self.run_preflight(existing_parameters())
        integration["properties"]["delegations"] = [{"properties": {"serviceName": "Microsoft.App/environments"}}]
        integration["properties"]["addressPrefix"] = "10.50.1.0/26"
        self.run_preflight(existing_parameters())

    def test_private_endpoint_subnet_must_not_be_delegated(self):
        self.vnet()["properties"]["subnets"][1]["properties"]["delegations"] = [
            {"properties": {"serviceName": "Microsoft.App/environments"}},
        ]
        with self.assertRaisesRegex(ValueError, "cannot use a delegated subnet"):
            self.run_preflight(existing_parameters())

    def test_new_subnet_mode_needs_a_free_prefix_inside_the_vnet(self):
        def new_subnet(prefix, name="snet-new"):
            return existing_parameters(
                primaryNetworkMode="newSubnet", primaryIntegrationSubnetName=name, primaryIntegrationSubnetPrefix=prefix,
            )

        self.assertIn("(newSubnet)", self.run_preflight(new_subnet("10.50.4.0/27")))
        # The default name keeps rerun and name-less configurations valid.
        document = new_subnet("10.50.4.0/27", "")
        self.assertIn("(newSubnet)", self.run_preflight(document))
        for prefix, message in (
            ("", "set primaryIntegrationSubnetPrefix"),
            ("not-a-cidr", "CIDR"),
            ("10.50.4.0/28", "/27 or larger"),
            ("10.70.0.0/27", "outside"),
            ("10.50.3.0/27", "overlaps subnet snet-other"),
            ("10.50.1.0/24", "overlaps subnet snet-func"),
        ):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(ValueError, message):
                self.run_preflight(new_subnet(prefix))

    def test_new_subnet_rerun_accepts_the_subnet_this_deployment_created(self):
        self.vnet()["properties"]["subnets"].append(subnet("snet-new", "10.50.4.0/27", "Microsoft.App/environments"))
        document = existing_parameters(
            primaryNetworkMode="newSubnet", primaryIntegrationSubnetName="snet-new", primaryIntegrationSubnetPrefix="10.50.4.0/27",
        )
        self.run_preflight(document)
        document["primaryIntegrationSubnetPrefix"] = {"value": "10.50.5.0/27"}
        with self.assertRaisesRegex(ValueError, "different address prefix"):
            self.run_preflight(document)
        document["primaryIntegrationSubnetPrefix"] = {"value": "10.50.4.0/27"}
        self.vnet()["properties"]["subnets"][-1]["properties"]["delegations"] = []
        with self.assertRaisesRegex(ValueError, "delegated only to Microsoft.App/environments"):
            self.run_preflight(document)

    def test_private_dns_zones_are_matched_by_service_and_must_exist(self):
        vault = zone_id("privatelink.vaultcore.azure.net")
        blob = zone_id("privatelink.blob.core.windows.net")
        self.zones = {vault.lower(): True, blob.lower(): True}
        document = existing_parameters(primaryPrivateDnsZoneIds={"vault": vault, "blob": blob, "queue": ""})
        self.run_preflight(document)
        zone_reads = [call for call in self.calls if "/privatednszones/" in call[call.index("--ids") + 1].lower()]
        self.assertEqual(len(zone_reads), 2)
        self.assertEqual(zone_reads[0][zone_reads[0].index("--subscription") + 1], HUB)
        del self.zones[vault.lower()]
        with self.assertRaisesRegex(ValueError, "Private DNS zone not found"):
            self.run_preflight(document)

    def test_unreadable_zones_warn_but_do_not_block_and_shared_zones_are_read_once(self):
        vault = zone_id("privatelink.vaultcore.azure.net")
        self.zones = {vault.lower(): RuntimeError("AuthorizationFailed: no read access")}
        document = existing_parameters(
            primaryPrivateDnsZoneIds={"vault": vault},
            secondaryNetworkMode="existing", secondaryVnetName="vnet-cus", secondaryVnetResourceGroupName="rg-network",
            secondaryIntegrationSubnetName="snet-func", secondaryPrivateEndpointSubnetName="snet-pe",
            secondaryPrivateDnsZoneIds={"vault": vault.upper().replace("/SUBSCRIPTIONS/", "/subscriptions/")},
        )
        output = self.run_preflight(document)
        self.assertIn("Warning: could not read private DNS zone", output)
        self.assertEqual(len([call for call in self.calls if "/privatednszones/" in call[call.index("--ids") + 1].lower()]), 1)

    def test_prepared_parameters_run_the_network_preflight(self):
        document = {"parameters": existing_parameters(managePermissions=True)}
        with redirect_stdout(StringIO()):
            prepared = prepare_parameters(document, SUB, self.query)
        self.assertEqual(prepared["parameters"]["primaryNetworkMode"]["value"], "existing")
        self.assertEqual(len(self.calls), 1)
        document["parameters"]["primaryIntegrationSubnetName"] = {"value": ""}
        with redirect_stdout(StringIO()), self.assertRaises(ValueError):
            prepare_parameters(document, SUB, self.query)


if __name__ == "__main__":
    unittest.main()
