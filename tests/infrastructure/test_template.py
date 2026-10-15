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


def deployment(name):
    return next(resource for resource in TEMPLATE["resources"]
                if resource["type"] == "Microsoft.Resources/deployments" and resource["name"] == name)


def module_id(side, name):
    return (
        "extensionResourceId(format('/subscriptions/{0}/resourceGroups/{1}', "
        f"subscription().subscriptionId, format('rg-{{0}}', variables('{side}Prefix'))), "
        f"'Microsoft.Resources/deployments', '{side}-{name}')"
    )


def module_reference(side, name):
    return f"reference({module_id(side, name)}, '2025-04-01')"


class InfrastructureTests(unittest.TestCase):
    def test_vaults_are_private_with_explicit_authorization_and_purge_protection(self):
        vaults = of_type("Microsoft.KeyVault/vaults")
        self.assertEqual(len(vaults), 2)
        for vault in vaults:
            props = vault["properties"]
            self.assertEqual(
                props["enableRbacAuthorization"],
                "[equals(parameters('vaultAuthorizationModel'), 'rbac')]",
            )
            self.assertTrue(props["enablePurgeProtection"])
            self.assertEqual(props["softDeleteRetentionInDays"], 90)
            self.assertEqual(props["publicNetworkAccess"], "Disabled")
            self.assertEqual(props["networkAcls"]["bypass"], "None")

    def test_authorization_defaults_and_independent_model_parameters(self):
        for side in ("primary", "secondary"):
            parameter = TEMPLATE["parameters"][f"{side}VaultAuthorizationModel"]
            self.assertEqual(parameter["defaultValue"], "accessPolicy")
            self.assertEqual(parameter["allowedValues"], ["accessPolicy", "rbac"])
            foundation = deployment(f"{side}-foundation")["properties"]
            self.assertEqual(
                foundation["parameters"]["vaultAuthorizationModel"]["value"],
                f"[parameters('{side}VaultAuthorizationModel')]",
            )
            model = foundation["template"]["parameters"]["vaultAuthorizationModel"]
            self.assertEqual(model["defaultValue"], "accessPolicy")
            self.assertEqual(model["allowedValues"], ["accessPolicy", "rbac"])

    def test_reused_vault_model_and_tenant_are_detected_with_legacy_policy_fallback(self):
        existing_reference = (
            "reference(extensionResourceId(format('/subscriptions/{0}/resourceGroups/{1}', "
            "subscription().subscriptionId, if(empty(parameters('existingVaultResourceId')), "
            "resourceGroup().name, split(parameters('existingVaultResourceId'), '/')[4])), "
            "'Microsoft.KeyVault/vaults', last(split(parameters('existingVaultResourceId'), '/'))), "
            "'2026-02-01')"
        )
        for side in ("primary", "secondary"):
            outputs = deployment(f"{side}-foundation")["properties"]["template"]["outputs"]
            self.assertEqual(
                outputs["vaultUsesRbac"]["value"],
                "[if(empty(parameters('existingVaultResourceId')), "
                "equals(parameters('vaultAuthorizationModel'), 'rbac'), "
                f"coalesce(tryGet({existing_reference}, 'enableRbacAuthorization'), false()))]",
            )
            self.assertEqual(
                outputs["vaultTenantId"]["value"],
                "[if(empty(parameters('existingVaultResourceId')), "
                f"subscription().tenantId, {existing_reference}.tenantId)]",
            )
            main_outputs = TEMPLATE["outputs"][f"{side}Resources"]["value"]
            self.assertEqual(
                main_outputs["vaultAuthorizationModel"],
                f"[if({module_reference(side, 'foundation')}.outputs.vaultUsesRbac.value, 'rbac', 'accessPolicy')]",
            )
            self.assertEqual(
                main_outputs["identityPrincipalId"],
                f"[{module_reference(side, 'foundation')}.outputs.principalId.value]",
            )

    def test_vault_grants_use_effective_model_tenant_and_correct_worker_principals(self):
        for side, other in (("primary", "secondary"), ("secondary", "primary")):
            parameters = deployment(f"{side}-vault-access")["properties"]["parameters"]
            for parameter, output in (
                ("useRbac", "vaultUsesRbac"), ("tenantId", "vaultTenantId"),
                ("vaultName", "vaultName"), ("readerPrincipalId", "principalId"),
            ):
                self.assertEqual(
                    parameters[parameter]["value"],
                    f"[{module_reference(side, 'foundation')}.outputs.{output}.value]",
                )
            self.assertEqual(
                parameters["writerPrincipalId"]["value"],
                f"[{module_reference(other, 'foundation')}.outputs.principalId.value]",
            )

    def test_access_policies_are_additive_and_exactly_secret_read_or_replica_write(self):
        policies = of_type("Microsoft.KeyVault/vaults/accessPolicies")
        self.assertEqual(len(policies), 2)
        for policy in policies:
            self.assertEqual(policy["condition"], "[not(parameters('useRbac'))]")
            self.assertEqual(policy["name"], "[format('{0}/{1}', parameters('vaultName'), 'add')]")
            self.assertEqual(policy["properties"]["accessPolicies"], [
                {
                    "tenantId": "[parameters('tenantId')]",
                    "objectId": "[parameters('readerPrincipalId')]",
                    "permissions": {"secrets": ["get", "list"]},
                },
                {
                    "tenantId": "[parameters('tenantId')]",
                    "objectId": "[parameters('writerPrincipalId')]",
                    "permissions": {"secrets": ["get", "list", "set"]},
                },
            ])

    def test_managed_vaults_keep_complete_worker_policies_on_redeployment(self):
        identities = of_type("Microsoft.ManagedIdentity/userAssignedIdentities")
        self.assertEqual(len(identities), 2)
        for identity in identities:
            self.assertEqual(identity["name"], "[format('id-{0}-worker', parameters('prefix'))]")
        for side, other in (("primary", "secondary"), ("secondary", "primary")):
            identity_deployment = deployment(f"{side}-worker-identity")
            self.assertEqual(identity_deployment["resourceGroup"],
                             f"[format('rg-{{0}}', variables('{side}Prefix'))]")
            self.assertEqual(identity_deployment["properties"]["parameters"]["prefix"]["value"],
                             f"[variables('{side}Prefix')]")
            foundation = deployment(f"{side}-foundation")
            for worker_side in ("primary", "secondary"):
                self.assertIn(f"[{module_id(worker_side, 'worker-identity')}]", foundation["dependsOn"])
            properties = foundation["properties"]
            self.assertEqual(
                properties["parameters"]["replicaWriterPrincipalId"]["value"],
                f"[{module_reference(other, 'worker-identity')}.outputs.principalId.value]",
            )
            nested = properties["template"]
            reader = (
                "createObject('tenantId', subscription().tenantId, 'objectId', "
                "reference(resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', "
                "format('id-{0}-worker', parameters('prefix'))), '2024-11-30').principalId, "
                "'permissions', createObject('secrets', createArray('get', 'list')))"
            )
            writer = (
                "createObject('tenantId', subscription().tenantId, 'objectId', "
                "parameters('replicaWriterPrincipalId'), 'permissions', "
                "createObject('secrets', createArray('get', 'list', 'set')))"
            )
            vault = next(resource for resource in nested["resources"] if resource["type"] == "Microsoft.KeyVault/vaults")
            self.assertEqual(
                vault["properties"]["accessPolicies"],
                "[if(equals(parameters('vaultAuthorizationModel'), 'rbac'), createArray(), "
                f"createArray({reader}, {writer}))]",
            )

    def test_rbac_grants_and_role_definition_are_gated_by_effective_model(self):
        role = deployment("[format('{0}-{1}-replica-writer-role', parameters('workload'), variables('token'))]")
        self.assertEqual(
            role["properties"]["parameters"]["enabled"]["value"],
            f"[or({module_reference('primary', 'foundation')}.outputs.vaultUsesRbac.value, "
            f"{module_reference('secondary', 'foundation')}.outputs.vaultUsesRbac.value)]",
        )
        definition = of_type("Microsoft.Authorization/roleDefinitions")[0]
        self.assertEqual(definition["condition"], "[parameters('enabled')]")
        self.assertEqual(definition["name"], "[guid(subscription().id, parameters('workload'), 'secret-replica-writer')]")
        for side in ("primary", "secondary"):
            access = deployment(f"{side}-vault-access")["properties"]["template"]
            assignments = [resource for resource in access["resources"]
                           if resource["type"] == "Microsoft.Authorization/roleAssignments"]
            self.assertEqual(len(assignments), 1)
            self.assertEqual(assignments[0]["condition"], "[parameters('useRbac')]")
            self.assertEqual(assignments[0]["scope"], "[resourceId('Microsoft.KeyVault/vaults', parameters('vaultName'))]")
            self.assertEqual(access["variables"]["assignments"], [
                {
                    "principal": "[parameters('readerPrincipalId')]",
                    "role": "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')]",
                },
                {"principal": "[parameters('writerPrincipalId')]", "role": "[parameters('writerRoleId')]"},
            ])

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
