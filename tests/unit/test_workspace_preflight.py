"""Read-only validation of reused Log Analytics workspaces."""

import copy
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_vault_integration import prepare_parameters, preflight_workspaces

SUB = "00000000-0000-0000-0000-000000000000"
OTHER = "22222222-2222-2222-2222-222222222222"
TENANT = "11111111-1111-1111-1111-111111111111"
KIND = "Microsoft.OperationalInsights/workspaces"
WORKSPACE = f"/subscriptions/{OTHER}/resourceGroups/monitor/providers/{KIND}/shared"
SECOND = f"/subscriptions/{SUB}/resourceGroups/monitor/providers/{KIND}/second"


def parameters(**values):
    return {key: {"value": value} for key, value in values.items()}


class WorkspacePreflightTests(unittest.TestCase):
    def setUp(self):
        self.resources = {
            item: {"id": item, "properties": {"provisioningState": "Succeeded"}} for item in (WORKSPACE, SECOND)
        }
        self.tenants = {SUB: TENANT, OTHER: TENANT}
        self.calls = []

    def query(self, *args):
        self.calls.append(args)
        option = lambda key: args[args.index(key) + 1]
        if args[:2] == ("account", "show"):
            return {"tenantId": self.tenants[option("--subscription")]}
        if args[:2] == ("resource", "show"):
            return copy.deepcopy({key.lower(): value for key, value in self.resources.items()}[option("--ids").lower()])
        raise AssertionError(f"Unexpected query: {args}")

    def run_preflight(self, document):
        with redirect_stdout(StringIO()):
            preflight_workspaces(document, SUB, self.query)

    def test_nothing_is_queried_when_no_workspace_is_selected(self):
        self.run_preflight(parameters(workload="mrkv"))
        self.run_preflight(parameters(primaryLogAnalyticsWorkspaceResourceId="", secondaryLogAnalyticsWorkspaceResourceId=""))
        self.assertEqual(self.calls, [])

    def test_workspace_in_another_subscription_is_read_in_its_own_subscription(self):
        self.run_preflight(parameters(primaryLogAnalyticsWorkspaceResourceId=WORKSPACE))
        shown = [call for call in self.calls if call[:2] == ("resource", "show")]
        self.assertEqual(len(shown), 1)
        self.assertEqual(shown[0][shown[0].index("--subscription") + 1], OTHER)

    def test_a_workspace_shared_by_both_regions_is_checked_once(self):
        self.run_preflight(parameters(
            primaryLogAnalyticsWorkspaceResourceId=WORKSPACE, secondaryLogAnalyticsWorkspaceResourceId=WORKSPACE.upper(),
        ))
        self.assertEqual(len([call for call in self.calls if call[:2] == ("resource", "show")]), 1)

    def test_distinct_regional_workspaces_are_each_checked(self):
        self.run_preflight(parameters(
            primaryLogAnalyticsWorkspaceResourceId=WORKSPACE, secondaryLogAnalyticsWorkspaceResourceId=SECOND,
        ))
        self.assertEqual(len([call for call in self.calls if call[:2] == ("resource", "show")]), 2)

    def test_invalid_selections_are_rejected(self):
        vault = f"/subscriptions/{SUB}/resourceGroups/monitor/providers/Microsoft.KeyVault/vaults/shared"
        for values in (
            {"primaryLogAnalyticsWorkspaceResourceId": "not-an-id"},
            {"primaryLogAnalyticsWorkspaceResourceId": vault},
            {"primaryLogAnalyticsWorkspaceResourceId": None},
            {"secondaryLogAnalyticsWorkspaceResourceId": f"/subscriptions/not-a-guid/resourceGroups/r/providers/{KIND}/w"},
            {"primaryLogAnalyticsWorkspaceResourceId": WORKSPACE, "linkExistingWorkspaceToPrivateLinkScope": "yes"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.run_preflight(parameters(**values))

    def test_foreign_tenant_unusable_state_and_mismatched_id_are_rejected(self):
        self.tenants[OTHER] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        with self.assertRaisesRegex(ValueError, "deployment tenant"):
            self.run_preflight(parameters(primaryLogAnalyticsWorkspaceResourceId=WORKSPACE))
        self.tenants[OTHER] = TENANT
        self.resources[WORKSPACE]["properties"]["provisioningState"] = "Failed"
        with self.assertRaisesRegex(ValueError, "Succeeded"):
            self.run_preflight(parameters(primaryLogAnalyticsWorkspaceResourceId=WORKSPACE))
        self.resources[WORKSPACE] = {"id": SECOND, "properties": {"provisioningState": "Succeeded"}}
        with self.assertRaisesRegex(ValueError, "different"):
            self.run_preflight(parameters(primaryLogAnalyticsWorkspaceResourceId=WORKSPACE))

    def test_prepared_parameters_keep_the_selection_and_validate_it(self):
        document = {"parameters": parameters(
            managePermissions=False, primaryLogAnalyticsWorkspaceResourceId=WORKSPACE,
            secondaryLogAnalyticsWorkspaceResourceId=SECOND,
        )}
        document["parameters"]["managePermissions"] = {"value": True}
        with redirect_stdout(StringIO()):
            prepared = prepare_parameters(document, SUB, self.query)
        self.assertEqual(prepared["parameters"]["primaryLogAnalyticsWorkspaceResourceId"]["value"], WORKSPACE)
        self.assertEqual(len([call for call in self.calls if call[:2] == ("resource", "show")]), 2)


if __name__ == "__main__":
    unittest.main()
