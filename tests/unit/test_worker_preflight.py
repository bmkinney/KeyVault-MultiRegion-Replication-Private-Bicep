"""Read-only identity discovery and Contributor-only prerequisites."""

import copy
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from itertools import product
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_vault_integration import prepare_parameters

SUB = "00000000-0000-0000-0000-000000000000"
BASE = f"/subscriptions/{SUB}/resourceGroups/platform/providers"
WORKERS = [f"{BASE}/Microsoft.ManagedIdentity/userAssignedIdentities/platform-{side}" for side in ("primary", "secondary")]
VAULTS = [f"{BASE}/Microsoft.KeyVault/vaults/{side}" for side in ("primary", "secondary")]
TOPICS = [f"{BASE}/Microsoft.EventGrid/systemTopics/{side}" for side in ("primary", "secondary")]


def parameters(**values):
    return {"parameters": {key: {"value": value} for key, value in values.items()}}


class WorkerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.resources = {}
        self.topics = []
        values = {"managePermissions": False, "secretNames": ["selected"]}
        for index, side in enumerate(("primary", "secondary")):
            principal = str(index + 1) * 8 + "-1111-1111-1111-111111111111"
            self.resources[WORKERS[index]] = {
                "id": WORKERS[index],
                "properties": {"principalId": principal, "clientId": principal, "tenantId": SUB},
            }
            self.resources[VAULTS[index]] = {
                "id": VAULTS[index], "location": "eastus" if index == 0 else "centralus",
                "properties": {
                    "tenantId": SUB, "vaultUri": f"https://{side}.vault.azure.net/",
                    "enableRbacAuthorization": False, "enablePurgeProtection": True,
                    "publicNetworkAccess": "Disabled",
                },
            }
            self.topics.append({
                "id": TOPICS[index], "source": VAULTS[index], "name": side,
                "location": self.resources[VAULTS[index]]["location"],
                "identity": {"principalId": principal},
            })
            for suffix, resource_id in (
                ("WorkerIdentityResourceId", WORKERS[index]), ("VaultResourceId", VAULTS[index]),
                ("SystemTopicResourceId", TOPICS[index]),
            ):
                values[f"{side}{suffix}"] = resource_id
        self.document = parameters(**values)

    def query(self, *args):
        if args[:2] == ("account", "show"):
            return {"tenantId": SUB}
        if args[:2] == ("resource", "show"):
            return self.resources[args[3]]
        if args[:3] == ("eventgrid", "system-topic", "list"):
            return self.topics
        if args[:4] == ("eventgrid", "system-topic", "event-subscription", "list"):
            return []
        raise AssertionError(f"Forbidden or unexpected query: {args}")

    def prepare(self, document=None, query=None):
        with redirect_stdout(StringIO()):
            return prepare_parameters(document or self.document, SUB, query or self.query)

    def test_external_mode_all_vault_models_needs_no_role_or_identity_inventory(self):
        for models in product((False, True), repeat=2):
            for vault_id, model in zip(VAULTS, models):
                self.resources[vault_id]["properties"]["enableRbacAuthorization"] = model
            before = copy.deepcopy(self.document)
            original_resources = copy.deepcopy(self.resources)
            prepared = self.prepare()
            self.assertEqual(self.document, before)
            self.assertEqual(self.resources, original_resources)
            self.assertEqual(prepared["parameters"]["resolvedVaultRoleAssignments"]["value"], {})
            snapshot = prepared["parameters"]["resolvedWorkerIdentities"]["value"]
            self.assertEqual(set(snapshot), {worker.lower() for worker in WORKERS})
            for worker in WORKERS:
                self.assertEqual(snapshot[worker.lower()], self.resources[worker]["properties"])

    def test_external_mode_rejects_each_missing_prerequisite_before_queries(self):
        for side in ("primary", "secondary"):
            for suffix in ("WorkerIdentityResourceId", "VaultResourceId", "SystemTopicResourceId"):
                with self.subTest(side=side, suffix=suffix):
                    document = copy.deepcopy(self.document)
                    del document["parameters"][side + suffix]
                    query = Mock(side_effect=AssertionError("unexpected Azure query"))
                    with self.assertRaisesRegex(ValueError, side + suffix):
                        self.prepare(document, query)
                    query.assert_not_called()

    def test_invalid_flags_identity_ids_and_shared_identity_are_rejected(self):
        for values in (
            {"managePermissions": "false"}, {"primaryWorkerIdentityResourceId": "not-an-id"},
            {"primaryWorkerIdentityResourceId": WORKERS[0].replace(SUB, "another-subscription")},
            {"primaryWorkerIdentityResourceId": VAULTS[0]},
            {"primaryWorkerIdentityResourceId": None},
            {"secondaryWorkerIdentityResourceId": WORKERS[0].upper()},
        ):
            document = copy.deepcopy(self.document)
            document["parameters"].update(parameters(**values)["parameters"])
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.prepare(document, Mock(side_effect=AssertionError("unexpected query")))

    def test_bad_metadata_and_cross_tenant_identity_are_rejected(self):
        original = copy.deepcopy(self.resources[WORKERS[0]])
        for field, value in (
            ("clientId", None), ("principalId", "invalid"), ("tenantId", None),
            ("tenantId", "11111111-1111-1111-1111-111111111111"),
        ):
            self.resources[WORKERS[0]] = copy.deepcopy(original)
            self.resources[WORKERS[0]]["properties"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.prepare()
        self.resources[WORKERS[0]] = {**original, "id": WORKERS[1]}
        with self.assertRaisesRegex(ValueError, "different worker identity"):
            self.prepare()

    def test_stale_metadata_is_replaced_and_read_failures_stop_preparation(self):
        self.document["parameters"]["resolvedWorkerIdentities"] = {"value": {"stale": {}}}
        prepared = self.prepare()
        self.assertNotIn("stale", prepared["parameters"]["resolvedWorkerIdentities"]["value"])
        def query(*args):
            if args[:2] == ("resource", "show") and args[3] in WORKERS:
                raise RuntimeError("identity read denied")
            return self.query(*args)

        with self.assertRaisesRegex(RuntimeError, "identity read denied"):
            self.prepare(query=query)

    def test_managed_mode_can_mix_new_and_existing_workers(self):
        for side in ("primary", "secondary"):
            document = parameters(**{f"{side}WorkerIdentityResourceId": WORKERS[0]})
            prepared = self.prepare(document)
            self.assertEqual(set(prepared["parameters"]["resolvedWorkerIdentities"]["value"]), {WORKERS[0].lower()})

    def test_managed_rbac_preserves_grant_names_for_arbitrarily_named_existing_workers(self):
        self.document["parameters"]["managePermissions"]["value"] = True
        self.resources[VAULTS[0]]["properties"]["enableRbacAuthorization"] = True
        role = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/4633458b-17de-408a-b874-0445c86b69e6"

        def query(*args):
            if args[:3] == ("role", "assignment", "list"):
                return [{
                    "name": SUB, "principalId": self.resources[WORKERS[0]]["properties"]["principalId"],
                    "scope": VAULTS[0], "roleDefinitionId": role,
                }]
            return self.query(*args)

        prepared = self.prepare(query=query)
        self.assertEqual(prepared["parameters"]["resolvedVaultRoleAssignments"]["value"], {
            f"{VAULTS[0]}|{WORKERS[0]}|{role}".lower(): SUB,
        })

    def test_duplicate_principals_and_vault_tenant_mismatch_are_rejected(self):
        self.resources[WORKERS[1]]["properties"]["principalId"] = self.resources[WORKERS[0]]["properties"]["principalId"]
        with self.assertRaisesRegex(ValueError, "different principal"):
            self.prepare()
        self.resources[VAULTS[0]]["properties"]["tenantId"] = "11111111-1111-1111-1111-111111111111"
        with self.assertRaisesRegex(ValueError, "deployment tenant"):
            self.prepare()
