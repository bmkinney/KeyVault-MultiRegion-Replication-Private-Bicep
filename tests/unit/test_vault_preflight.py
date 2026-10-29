import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock
from contextlib import redirect_stdout
from io import StringIO
from itertools import product

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_vault_integration import preflight, azure_json, authorization_model, prepare_parameters, existing_role_assignment_names

SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
VAULT_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/existing/providers/Microsoft.KeyVault/vaults/source"
TOPIC_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/events/providers/Microsoft.EventGrid/systemTopics/source-events"


def params(**values):
    return {key: {"value": value} for key, value in values.items()}


class PreflightTests(unittest.TestCase):
    def test_cli_rejects_shell_metacharacters(self):
        with self.assertRaisesRegex(ValueError, "metacharacters"):
            azure_json("resource", "show", "--ids", VAULT_ID + "&unexpected")

    def setUp(self):
        self.vault = {
            "id": VAULT_ID, "location": "eastus",
            "properties": {
                "tenantId": SUBSCRIPTION,
                "vaultUri": "https://source.vault.azure.net/",
                "enableRbacAuthorization": True,
                "enablePurgeProtection": True,
                "enableSoftDelete": True,
                "publicNetworkAccess": "Disabled",
            },
        }
        self.topic = {
            "id": TOPIC_ID, "name": "source-events", "source": VAULT_ID,
            "location": "eastus", "identity": {"principalId": "system-principal"},
        }
        self.config = params(primaryVaultResourceId=VAULT_ID, secretNames=["selected"])

    def query(self, *arguments):
        if arguments[:2] == ("resource", "show"):
            return self.vault
        if arguments[:3] == ("eventgrid", "system-topic", "list"):
            return []
        raise AssertionError(arguments)

    def test_new_vaults_need_no_azure_queries(self):
        query = Mock(side_effect=AssertionError("unexpected Azure call"))
        preflight({}, SUBSCRIPTION, query)
        query.assert_not_called()

    def test_compatible_existing_vault(self):
        self.assertEqual(authorization_model(self.vault["properties"]), "rbac")
        preflight(self.config, SUBSCRIPTION, self.query)

    def test_access_policy_vaults_including_legacy_null_are_preserved(self):
        for enabled in (False, None):
            with self.subTest(enabled=enabled):
                self.vault["properties"]["enableRbacAuthorization"] = enabled
                original = copy.deepcopy(self.vault)
                output = StringIO()
                with redirect_stdout(output):
                    preflight(self.config, SUBSCRIPTION, self.query)
                self.assertIn("accessPolicy; model preserved", output.getvalue())
                self.assertEqual(self.vault, original)

    def test_legacy_missing_model_uses_access_policies(self):
        del self.vault["properties"]["enableRbacAuthorization"]
        self.assertEqual(authorization_model(self.vault["properties"]), "accessPolicy")
        preflight(self.config, SUBSCRIPTION, self.query)

    def test_new_vault_model_does_not_override_reused_vault(self):
        for actual, configured in [(True, "accessPolicy"), (False, "rbac")]:
            with self.subTest(actual=actual, configured=configured):
                self.vault["properties"]["enableRbacAuthorization"] = actual
                self.config["primaryVaultAuthorizationModel"] = {"value": configured}
                original = copy.deepcopy(self.vault)
                preflight(self.config, SUBSCRIPTION, self.query)
                self.assertEqual(self.vault, original)

    def test_both_new_vault_models_are_valid(self):
        for primary in ("accessPolicy", "rbac"):
            for secondary in ("accessPolicy", "rbac"):
                query = Mock(side_effect=AssertionError("unexpected Azure call"))
                preflight(params(
                    primaryVaultAuthorizationModel=primary,
                    secondaryVaultAuthorizationModel=secondary,
                ), SUBSCRIPTION, query)
                query.assert_not_called()

    def test_mixed_existing_vault_models(self):
        secondary = copy.deepcopy(self.vault)
        secondary.update(id=VAULT_ID + "-replica", location="centralus")
        secondary["properties"]["vaultUri"] = "https://source-replica.vault.azure.net/"
        for primary_model, secondary_model in [(True, False), (False, True)]:
            with self.subTest(primary=primary_model, secondary=secondary_model):
                self.vault["properties"]["enableRbacAuthorization"] = primary_model
                secondary["properties"]["enableRbacAuthorization"] = secondary_model
                query = Mock(side_effect=[self.vault, [], secondary])
                config = {**self.config, "secondaryVaultResourceId": {"value": secondary["id"]}}
                preflight(config, SUBSCRIPTION, query)
                self.assertEqual(query.call_count, 3)
                self.assertTrue(all("list" in call.args or "show" in call.args for call in query.call_args_list))

    def test_all_new_existing_and_authorization_combinations_are_read_only(self):
        for primary_exists, secondary_exists, primary_rbac, secondary_rbac in product((False, True), repeat=4):
            with self.subTest(existing=(primary_exists, secondary_exists), rbac=(primary_rbac, secondary_rbac)):
                vaults = {}
                values = {"secretNames": ["selected"]}
                for side, exists, rbac in [
                    ("primary", primary_exists, primary_rbac), ("secondary", secondary_exists, secondary_rbac),
                ]:
                    values[f"{side}VaultAuthorizationModel"] = "rbac" if rbac else "accessPolicy"
                    if exists:
                        vault = copy.deepcopy(self.vault)
                        vault["id"] = VAULT_ID + f"-{side}"
                        vault["location"] = "eastus" if side == "primary" else "centralus"
                        vault["properties"]["enableRbacAuthorization"] = rbac
                        vault["properties"]["vaultUri"] = f"https://source-{side}.vault.azure.net/"
                        vaults[vault["id"]] = vault
                        values[f"{side}VaultResourceId"] = vault["id"]
                original = copy.deepcopy(vaults)

                def read_only_query(*args):
                    if args[:2] == ("resource", "show"):
                        return vaults[args[3]]
                    if args[:3] == ("eventgrid", "system-topic", "list"):
                        return []
                    if args[:2] == ("identity", "list"):
                        return []
                    raise AssertionError(f"Unexpected non-read-only query: {args}")

                query = Mock(side_effect=read_only_query)
                with redirect_stdout(StringIO()):
                    preflight(params(**values), SUBSCRIPTION, query)
                self.assertEqual(query.call_count, len(vaults) + bool(vaults))
                self.assertEqual(vaults, original)
                query.reset_mock()
                document = {"parameters": params(**values)}
                original_document = copy.deepcopy(document)
                with redirect_stdout(StringIO()):
                    prepared = prepare_parameters(document, SUBSCRIPTION, query)
                self.assertEqual(document, original_document)
                snapshot = prepared["parameters"]["resolvedExistingVaults"]["value"]
                self.assertEqual(set(snapshot), {vault_id.lower() for vault_id in vaults})
                for vault in vaults.values():
                    self.assertEqual(snapshot[vault["id"].lower()], {
                        "usesRbac": vault["properties"]["enableRbacAuthorization"],
                        "tenantId": SUBSCRIPTION, "vaultUri": vault["properties"]["vaultUri"],
                    })
                self.assertEqual(query.call_count, len(vaults) + bool(vaults) + bool(primary_rbac or secondary_rbac))

    def test_prepared_snapshot_is_fresh_and_does_not_mutate_user_parameters(self):
        for actual in (True, False, None):
            with self.subTest(actual=actual):
                self.vault["properties"]["enableRbacAuthorization"] = actual
                document = {"parameters": {
                    **self.config,
                    "resolvedExistingVaults": {"value": {"stale": {}}},
                    "resolvedVaultRoleAssignments": {"value": {"stale": "assignment"}},
                }}
                original = copy.deepcopy(document)

                def query(*args):
                    if args[:2] == ("identity", "list"):
                        return []
                    return self.query(*args)

                prepared = prepare_parameters(document, SUBSCRIPTION, query)
                self.assertEqual(document, original)
                self.assertEqual(prepared["parameters"]["resolvedExistingVaults"]["value"], {
                    VAULT_ID.lower(): {
                        "usesRbac": actual is True, "tenantId": SUBSCRIPTION,
                        "vaultUri": "https://source.vault.azure.net/",
                    },
                })
                self.assertEqual(prepared["parameters"]["resolvedVaultRoleAssignments"]["value"], {})

    def test_default_preparation_needs_no_azure_inventory(self):
        query = Mock(side_effect=AssertionError("unexpected Azure query"))
        prepared = prepare_parameters({"parameters": {}}, SUBSCRIPTION, query)
        self.assertEqual(prepared["parameters"]["resolvedExistingVaults"]["value"], {})
        query.assert_not_called()

    def test_invalid_metadata_is_rejected(self):
        for field, invalid in [
            ("tenantId", None), ("tenantId", "invalid"),
            ("vaultUri", None), ("vaultUri", "http://source.vault.azure.net/"),
            ("vaultUri", "https://different.vault.azure.net/"),
            ("vaultUri", "https://source.vault.azure.net/secrets"),
            ("vaultUri", "https://source.vault.azure.net/?query=value"),
        ]:
            with self.subTest(field=field, invalid=invalid):
                original = copy.deepcopy(self.vault)
                self.vault["properties"][field] = invalid
                with self.assertRaises(ValueError):
                    prepare_parameters({"parameters": self.config}, SUBSCRIPTION, self.query)
                self.vault = original
        self.vault["id"] = VAULT_ID + "-unexpected"
        with self.assertRaisesRegex(ValueError, "different vault"):
            preflight(self.config, SUBSCRIPTION, self.query)

    def test_rbac_inventory_preserves_old_and_new_assignment_names(self):
        identity_id = (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mrkv-p-abcdefgh"
            "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-mrkv-p-abcdefgh-worker"
        )
        identity = {"id": identity_id, "name": "id-mrkv-p-abcdefgh-worker", "principalId": SUBSCRIPTION}
        role = f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{SUBSCRIPTION}"
        for name in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"):
            with self.subTest(name=name):
                assignment = {"name": name, "scope": VAULT_ID, "roleDefinitionId": role, "principalId": SUBSCRIPTION}
                query = Mock(side_effect=[[identity], [assignment]])
                self.assertEqual(existing_role_assignment_names("mrkv", SUBSCRIPTION, query), {
                    f"{VAULT_ID}|{identity_id}|{role}".lower(): name,
                })
                self.assertEqual(query.call_args.args, (
                    "role", "assignment", "list", "--subscription", SUBSCRIPTION, "--all",
                    "--fill-principal-name", "false", "--fill-role-definition-name", "false",
                ))

    def test_rbac_inventory_ignores_unrelated_identities_and_grants(self):
        query = Mock(return_value=[{
            "name": "id-mrkv-p-abcdefgh-worker",
            "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/unrelated/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-mrkv-p-abcdefgh-worker",
            "principalId": SUBSCRIPTION,
        }])
        self.assertEqual(existing_role_assignment_names("mrkv", SUBSCRIPTION, query), {})
        self.assertEqual(query.call_count, 1)

    def test_rbac_inventory_failures_stop_preparation(self):
        query = Mock(side_effect=RuntimeError("read denied"))
        with self.assertRaisesRegex(RuntimeError, "read denied"):
            prepare_parameters({"parameters": params(primaryVaultAuthorizationModel="rbac")}, SUBSCRIPTION, query)

    def test_rbac_inventory_rejects_ambiguous_assignments(self):
        identity = {
            "name": "id-mrkv-p-abcdefgh-worker", "principalId": SUBSCRIPTION,
            "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mrkv-p-abcdefgh/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-mrkv-p-abcdefgh-worker",
        }
        assignment = {"name": SUBSCRIPTION, "scope": VAULT_ID, "roleDefinitionId": SUBSCRIPTION, "principalId": SUBSCRIPTION}
        other = {**assignment, "name": "11111111-1111-1111-1111-111111111111"}
        with self.assertRaisesRegex(ValueError, "Multiple existing"):
            existing_role_assignment_names("mrkv", SUBSCRIPTION, Mock(side_effect=[[identity], [assignment, other]]))

    def test_invalid_detected_authorization_model_rejected(self):
        for invalid in ("false", "true", 0, 1, [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "boolean"):
                authorization_model({"enableRbacAuthorization": invalid})

    def test_existing_vault_requires_explicit_selection(self):
        with self.assertRaisesRegex(ValueError, "Select"):
            preflight(params(primaryVaultResourceId=VAULT_ID), SUBSCRIPTION, self.query)

    def test_all_secrets_explicit_opt_in(self):
        preflight(params(primaryVaultResourceId=VAULT_ID, replicateAllSecrets=True), SUBSCRIPTION, self.query)

    def test_cross_subscription_rejected_before_query(self):
        with self.assertRaisesRegex(ValueError, "subscription"):
            preflight(self.config, "another-subscription", Mock())

    def test_same_vault_for_both_directions_rejected(self):
        self.config["secondaryVaultResourceId"] = {"value": VAULT_ID.upper()}
        with self.assertRaisesRegex(ValueError, "different"):
            preflight(self.config, SUBSCRIPTION, self.query)

    def test_region_mismatch_rejected(self):
        self.vault["location"] = "centralus"
        with self.assertRaisesRegex(ValueError, "region"):
            preflight(self.config, SUBSCRIPTION, self.query)

    def test_no_implicit_security_changes(self):
        for key, value in [
            ("enablePurgeProtection", False),
            ("enableSoftDelete", False), ("publicNetworkAccess", "Enabled"),
        ]:
            with self.subTest(key=key):
                original = copy.deepcopy(self.vault)
                self.vault["properties"][key] = value
                with self.assertRaises(ValueError):
                    preflight(self.config, SUBSCRIPTION, self.query)
                self.vault = original

    def test_existing_system_topic_must_be_explicitly_reused(self):
        query = Mock(side_effect=[self.vault, [self.topic]])
        with self.assertRaisesRegex(ValueError, "system topic already exists"):
            preflight(self.config, SUBSCRIPTION, query)

    def test_supplied_topic_reused_without_modification(self):
        self.config["primarySystemTopicResourceId"] = {"value": TOPIC_ID}
        query = Mock(side_effect=[self.vault, [self.topic], []])
        preflight(self.config, SUBSCRIPTION, query)
        self.assertEqual(query.call_count, 3)
        self.assertTrue(all("list" in call.args or "show" in call.args for call in query.call_args_list))

    def test_reused_topic_needs_system_identity(self):
        self.config["primarySystemTopicResourceId"] = {"value": TOPIC_ID}
        self.topic["identity"] = {}
        with self.assertRaisesRegex(ValueError, "system-assigned"):
            preflight(self.config, SUBSCRIPTION, Mock(side_effect=[self.vault, [self.topic]]))

    def test_foreign_topic_source_rejected(self):
        self.config["primarySystemTopicResourceId"] = {"value": TOPIC_ID}
        self.topic["source"] = "foreign-vault"
        with self.assertRaisesRegex(ValueError, "does not belong"):
            preflight(self.config, SUBSCRIPTION, Mock(side_effect=[self.vault, [self.topic]]))

    def test_own_created_topic_can_be_retried(self):
        self.topic.update({
            "name": "evgt-mrkv-p-abcdefgh",
            "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mrkv-p-abcdefgh/providers/Microsoft.EventGrid/systemTopics/evgt-mrkv-p-abcdefgh",
            "tags": {"workload": "mrkv", "purpose": "private-secret-replication-demo"},
        })
        preflight(self.config, SUBSCRIPTION, Mock(side_effect=[self.vault, [self.topic]]))

    def test_own_topic_in_the_vault_resource_group_can_be_retried(self):
        vault_group = VAULT_ID.split("/")[4]
        self.topic.update({
            "name": "evgt-mrkv-p-abcdefgh",
            "id": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{vault_group}/providers/Microsoft.EventGrid/systemTopics/evgt-mrkv-p-abcdefgh",
            "tags": {"workload": "mrkv", "purpose": "private-secret-replication-demo"},
        })
        preflight(self.config, SUBSCRIPTION, Mock(side_effect=[self.vault, [self.topic]]))
        self.topic["tags"] = {}
        with self.assertRaisesRegex(ValueError, "already exists"):
            preflight(self.config, SUBSCRIPTION, Mock(side_effect=[self.vault, [self.topic]]))

    def test_reserved_route_collision_rejected(self):
        self.config["primarySystemTopicResourceId"] = {"value": TOPIC_ID}
        route = {"name": "mrkv-abcdefgh-secret-events", "properties": {"labels": []}}
        with self.assertRaisesRegex(ValueError, "subscription name"):
            preflight(self.config, SUBSCRIPTION, Mock(side_effect=[self.vault, [self.topic], [route]]))

    def test_topic_reuse_without_existing_vault_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires an existing vault"):
            preflight(params(primarySystemTopicResourceId=TOPIC_ID), SUBSCRIPTION, Mock())

    def test_malformed_ids_and_selection_rejected(self):
        for values in [
            {"primaryVaultResourceId": "source", "secretNames": ["selected"]},
            {"secretNames": ["../invalid"]},
            {"secretPrefix": "*"},
            {"replicateAllSecrets": "false"},
            {"allowUnmanagedOverwrite": "false"},
            {"primaryVaultAuthorizationModel": "RBAC"},
            {"secondaryVaultAuthorizationModel": "unknown"},
            {"primaryVaultAuthorizationModel": False},
            {"secondaryVaultAuthorizationModel": None},
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                preflight(params(**values), SUBSCRIPTION, Mock())
