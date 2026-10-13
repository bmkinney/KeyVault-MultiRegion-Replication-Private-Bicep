import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from check_vault_integration import preflight, azure_json

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
        preflight(self.config, SUBSCRIPTION, self.query)

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

    def test_no_implicit_authorization_or_security_changes(self):
        for key, value in [
            ("enableRbacAuthorization", False), ("enablePurgeProtection", False),
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
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                preflight(params(**values), SUBSCRIPTION, Mock())
