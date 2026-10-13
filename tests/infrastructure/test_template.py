"""Exact assertions over a compiled template, supplementing expression-limited scanners."""

import json
import sys
import unittest
from pathlib import Path

TEMPLATE = json.loads(Path(sys.argv.pop(1)).read_text(encoding="utf-8"))


def resources(template):
    for resource in template.get("resources", []):
        yield resource
        nested = resource.get("properties", {}).get("template")
        if nested:
            yield from resources(nested)


ALL_RESOURCES = list(resources(TEMPLATE))


def of_type(kind):
    return [resource for resource in ALL_RESOURCES if resource["type"].lower() == kind.lower()]


class InfrastructureTests(unittest.TestCase):
    def test_vaults_are_private_rbac_purge_protected(self):
        vaults = of_type("Microsoft.KeyVault/vaults")
        self.assertEqual(len(vaults), 2)
        for vault in vaults:
            props = vault["properties"]
            self.assertTrue(props["enableRbacAuthorization"])
            self.assertTrue(props["enablePurgeProtection"])
            self.assertEqual(props["softDeleteRetentionInDays"], 90)
            self.assertEqual(props["publicNetworkAccess"], "Disabled")
            self.assertEqual(props["networkAcls"]["bypass"], "None")

    def test_storage_uses_zrs_and_no_keys_or_public_data(self):
        accounts = of_type("Microsoft.Storage/storageAccounts")
        self.assertEqual(len(accounts), 2)  # One copy-loop definition per stamp, two accounts each.
        for account in accounts:
            self.assertEqual(account["copy"]["count"], "[length(variables('accountNames'))]")
            self.assertEqual(account["sku"]["name"], "Standard_ZRS")
            self.assertFalse(account["properties"]["allowSharedKeyAccess"])
            self.assertFalse(account["properties"]["allowBlobPublicAccess"])
            self.assertEqual(account["properties"]["publicNetworkAccess"], "Disabled")
            self.assertEqual(account["properties"]["networkAcls"]["defaultAction"], "Deny")

    def test_bus_is_private_identity_only(self):
        for bus in of_type("Microsoft.ServiceBus/namespaces"):
            self.assertEqual(bus["sku"]["name"], "Premium")
            self.assertTrue(bus["properties"]["disableLocalAuth"])
            self.assertTrue(bus["properties"]["zoneRedundant"])
            self.assertEqual(bus["properties"]["publicNetworkAccess"], "Disabled")
        for rules in of_type("Microsoft.ServiceBus/namespaces/networkRuleSets"):
            self.assertTrue(rules["properties"]["trustedServiceAccessEnabled"])
            self.assertEqual(rules["properties"]["defaultAction"], "Deny")

    def test_flex_identity_and_private_apps(self):
        apps = of_type("Microsoft.Web/sites")
        self.assertEqual(len(apps), 2)
        for app in apps:
            props = app["properties"]
            self.assertEqual(app["identity"]["type"], "UserAssigned")
            self.assertTrue(props["httpsOnly"])
            self.assertEqual(props["publicNetworkAccess"], "Disabled")
            self.assertEqual(props["siteConfig"]["minTlsVersion"], "1.2")
            self.assertEqual(props["functionAppConfig"]["deployment"]["storage"]["authentication"]["type"], "UserAssignedIdentity")
            self.assertEqual(props["functionAppConfig"]["scaleAndConcurrency"]["maximumInstanceCount"], 40)
            names = {setting["name"] for setting in props["siteConfig"]["appSettings"]}
            self.assertIn("AzureWebJobsStorage__clientId", names)
            self.assertNotIn("AzureWebJobsStorage", names)
            self.assertNotIn("WEBSITE_RUN_FROM_PACKAGE", names)
            self.assertNotIn("WEBSITE_CONTENTAZUREFILECONNECTIONSTRING", names)

    def test_service_bus_receiver_uses_official_role_at_queue_scope(self):
        receivers = [
            assignment for assignment in of_type("Microsoft.Authorization/roleAssignments")
            if assignment["name"] == "[guid(resourceId('Microsoft.ServiceBus/namespaces/queues', format('sb-{0}', parameters('prefix')), 'kv-events'), resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', format('id-{0}-worker', parameters('prefix'))), 'receiver')]"
        ]
        self.assertEqual(len(receivers), 2)
        for assignment in receivers:
            self.assertEqual(
                assignment["properties"]["roleDefinitionId"],
                "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0')]",
            )
            self.assertIn("Microsoft.ServiceBus/namespaces/queues", assignment["scope"])

    def test_monitor_associations_declare_resource_kind(self):
        links = of_type("Microsoft.Insights/privateLinkScopes/scopedResources")
        self.assertEqual(len(links), 4)
        for link in links:
            self.assertEqual(link["apiVersion"], "2023-06-01-preview")
            self.assertEqual(link["properties"]["kind"], "Resource")
            self.assertTrue(link["properties"]["linkedResourceId"])

    def test_monitoring_is_private(self):
        for scope in of_type("Microsoft.Insights/privateLinkScopes"):
            self.assertEqual(scope["properties"]["accessModeSettings"]["queryAccessMode"], "PrivateOnly")
            self.assertEqual(scope["properties"]["accessModeSettings"]["ingestionAccessMode"], "PrivateOnly")
        for workspace in of_type("Microsoft.OperationalInsights/workspaces"):
            self.assertEqual(workspace["properties"]["publicNetworkAccessForQuery"], "Disabled")
            self.assertEqual(workspace["properties"]["publicNetworkAccessForIngestion"], "Disabled")
        for insights in of_type("Microsoft.Insights/components"):
            self.assertTrue(insights["properties"]["DisableLocalAuth"])
            self.assertEqual(insights["properties"]["publicNetworkAccessForQuery"], "Disabled")

    def test_replica_role_has_no_destructive_actions(self):
        roles = of_type("Microsoft.Authorization/roleDefinitions")
        self.assertEqual(len(roles), 1)
        permission = roles[0]["properties"]["permissions"][0]
        self.assertEqual(permission["actions"], [])
        self.assertEqual(set(permission["dataActions"]), {
            "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
            "Microsoft.KeyVault/vaults/secrets/getSecret/action",
            "Microsoft.KeyVault/vaults/secrets/setSecret/action",
            "Microsoft.KeyVault/vaults/secrets/update/action",
        })

    def test_default_activation_is_fenced(self):
        self.assertFalse(TEMPLATE["parameters"]["enableWorker"]["defaultValue"])
        self.assertFalse(TEMPLATE["parameters"]["enableEventRouting"]["defaultValue"])
        self.assertEqual(TEMPLATE["parameters"]["authoritativeRegion"]["allowedValues"], ["primary", "secondary"])
        self.assertFalse(TEMPLATE["parameters"]["allowUnmanagedOverwrite"]["defaultValue"])

    def test_existing_vaults_are_references_not_deployed_resources(self):
        for vault in of_type("Microsoft.KeyVault/vaults"):
            self.assertEqual(vault["condition"], "[empty(parameters('existingVaultResourceId'))]")
        foundations = [
            resource for resource in TEMPLATE["resources"]
            if resource["type"] == "Microsoft.Resources/deployments" and resource["name"].endswith("-foundation")
        ]
        self.assertEqual(len(foundations), 2)
        for foundation in foundations:
            side = foundation["name"].split("-")[0]
            self.assertEqual(
                foundation["properties"]["parameters"]["existingVaultResourceId"]["value"],
                f"[parameters('{side}VaultResourceId')]",
            )
            for diagnostics in foundation["properties"]["template"]["resources"]:
                if diagnostics["type"] == "Microsoft.Insights/diagnosticSettings" and "Microsoft.KeyVault/vaults" in diagnostics.get("scope", ""):
                    self.assertEqual(diagnostics["condition"], "[empty(parameters('existingVaultResourceId'))]")
        for primary_id, secondary_id, expected_creates in [
            ("", "", 2), ("/existing-primary", "", 1),
            ("", "/existing-secondary", 1), ("/existing-primary", "/existing-secondary", 0),
        ]:
            creates = 0
            for foundation in foundations:
                selected_id = primary_id if foundation["name"] == "primary-foundation" else secondary_id
                for resource in foundation["properties"]["template"]["resources"]:
                    if resource["type"] != "Microsoft.KeyVault/vaults":
                        continue
                    condition = resource["condition"]
                    self.assertRegex(condition, r"^\[empty\(parameters\('existingVaultResourceId'\)\)\]$")
                    creates += not bool(selected_id)
            self.assertEqual(creates, expected_creates)
        for deployment in TEMPLATE["resources"]:
            if deployment["type"] == "Microsoft.Resources/deployments" and deployment["name"].endswith("-vault-access"):
                side = deployment["name"].split("-")[0]
                self.assertIn(f"split(parameters('{side}VaultResourceId'), '/')", deployment["resourceGroup"])

    def test_existing_topics_are_not_recreated_or_reconfigured(self):
        for topic in of_type("Microsoft.EventGrid/systemTopics"):
            self.assertEqual(topic["condition"], "[empty(parameters('existingSystemTopicResourceId'))]")
        routes = [
            resource for resource in TEMPLATE["resources"]
            if resource["type"] == "Microsoft.Resources/deployments" and resource["name"].endswith("-routing")
        ]
        for route in routes:
            side = route["name"].split("-")[0]
            self.assertIn(f"split(parameters('{side}SystemTopicResourceId'), '/')", route["resourceGroup"])

    def test_worker_selection_and_collision_settings_are_wired(self):
        for app in of_type("Microsoft.Web/sites"):
            settings = {item["name"]: item["value"] for item in app["properties"]["siteConfig"]["appSettings"]}
            self.assertEqual(settings["SECRET_NAMES"], "[string(parameters('secretNames'))]")
            self.assertEqual(settings["SECRET_PREFIX"], "[parameters('secretPrefix')]")
            self.assertEqual(settings["REPLICATE_ALL_SECRETS"], "[string(parameters('replicateAllSecrets'))]")
            self.assertEqual(settings["ALLOW_UNMANAGED_OVERWRITE"], "[string(parameters('allowUnmanagedOverwrite'))]")
            self.assertEqual(settings["DESTINATION_VAULT_ID"], "[parameters('destinationVaultId')]")

    def test_event_delivery_and_deadletter_are_identity_based(self):
        subscriptions = of_type("Microsoft.EventGrid/systemTopics/eventSubscriptions")
        self.assertEqual(len(subscriptions), 2)
        for subscription in subscriptions:
            props = subscription["properties"]
            self.assertEqual(props["eventDeliverySchema"], "EventGridSchema")
            self.assertEqual(props["deliveryWithResourceIdentity"]["identity"]["type"], "SystemAssigned")
            self.assertEqual(props["deadLetterWithResourceIdentity"]["identity"]["type"], "SystemAssigned")
            self.assertEqual(props["retryPolicy"]["eventTimeToLiveInMinutes"], 1440)
            self.assertIn("__mrkv_inactive__/", props["filter"]["subjectBeginsWith"])


if __name__ == "__main__":
    unittest.main()
