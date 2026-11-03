"""Exact assertions over a compiled template, supplementing expression-limited scanners."""

import json
import re
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


def foundation_module(side, name):
    return next(item for item in deployment(f"{side}-foundation")["properties"]["template"]["resources"]
                if item["type"] == "Microsoft.Resources/deployments" and item["name"] == name)


def module_id(side, name):
    return (
        "extensionResourceId(format('/subscriptions/{0}/resourceGroups/{1}', "
        f"subscription().subscriptionId, format('rg-{{0}}', variables('{side}Prefix'))), "
        f"'Microsoft.Resources/deployments', '{side}-{name}')"
    )


def module_reference(side, name):
    return f"reference({module_id(side, name)}, '2025-04-01')"


def identity_id(parameter):
    return (
        "extensionResourceId(format('/subscriptions/{0}/resourceGroups/{1}', "
        f"split(parameters('{parameter}'), '/')[2], split(parameters('{parameter}'), '/')[4]), "
        f"'Microsoft.ManagedIdentity/userAssignedIdentities', last(split(parameters('{parameter}'), '/')))"
    )


def identity_reference(parameter):
    return f"reference({identity_id(parameter)}, '2024-11-30').principalId"


def selected_principal(identity_parameter, principal_parameter):
    return (
        f"if(empty(parameters('{principal_parameter}')), {identity_reference(identity_parameter)}, "
        f"parameters('{principal_parameter}'))"
    )


class InfrastructureTests(unittest.TestCase):
    def test_reused_workspace_is_optional_never_created_and_wired_everywhere(self):
        for side in ("primary", "secondary"):
            self.assertEqual(TEMPLATE["parameters"][f"{side}LogAnalyticsWorkspaceResourceId"]["defaultValue"], "")
        self.assertTrue(TEMPLATE["parameters"]["linkExistingWorkspaceToPrivateLinkScope"]["defaultValue"])
        workspaces = of_type("Microsoft.OperationalInsights/workspaces")
        self.assertEqual(len(workspaces), 2)
        for workspace in workspaces:
            self.assertEqual(workspace["condition"], "[empty(parameters('existingWorkspaceResourceId'))]")
        selected = "[variables('workspaceId')]"
        links = [item for item in of_type("Microsoft.Insights/privateLinkScopes/scopedResources")
                 if item["properties"]["linkedResourceId"] == selected]
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(
                link["condition"],
                "[or(empty(parameters('existingWorkspaceResourceId')), parameters('linkExistingWorkspaceToPrivateLinkScope'))]",
            )
        for insights in of_type("Microsoft.Insights/components"):
            self.assertEqual(insights["properties"]["WorkspaceResourceId"], selected)
        diagnostics = [item for item in of_type("Microsoft.Insights/diagnosticSettings") if "workspaceId" in item["properties"]]
        self.assertGreaterEqual(len(diagnostics), 8)
        for item in diagnostics:
            # The function-app module receives the same selected ID through its foundation input.
            self.assertIn(item["properties"]["workspaceId"], (selected, "[parameters('foundation').workspaceId]"))

    def test_existing_worker_ids_and_known_principals_are_wired_without_redeployment(self):
        self.assertTrue(TEMPLATE["parameters"]["managePermissions"]["defaultValue"])
        self.assertEqual(TEMPLATE["parameters"]["resolvedWorkerIdentities"]["defaultValue"], {})
        stamp = next(item for item in TEMPLATE["variables"]["copy"] if item["name"] == "stamps")["input"]
        self.assertEqual(stamp["runtime"]["identityId"], stamp["identityId"])
        self.assertIn("parameters('resolvedWorkerIdentities')", stamp["principalId"])
        for side, index in (("primary", 0), ("secondary", 1)):
            self.assertEqual(TEMPLATE["parameters"][f"{side}WorkerIdentityResourceId"]["defaultValue"], "")
            creation = deployment(f"{side}-worker-identity")
            self.assertEqual(
                creation["condition"],
                f"[and(parameters('managePermissions'), empty(parameters('{side}WorkerIdentityResourceId')))]",
            )
            foundation = deployment(f"{side}-foundation")["properties"]["parameters"]
            self.assertEqual(foundation["workerIdentityResourceId"]["value"], f"[variables('stamps')[{index}].identityId]")
            self.assertEqual(foundation["workerPrincipalId"]["value"], f"[variables('stamps')[{index}].principalId]")
            access = deployment(f"{side}-vault-access")["properties"]["parameters"]
            self.assertEqual(access["readerPrincipalId"]["value"], f"[variables('stamps')[{index}].principalId]")
            self.assertEqual(access["writerPrincipalId"]["value"], f"[variables('stamps')[{1 - index}].principalId]")
            self.assertEqual(TEMPLATE["outputs"][f"{side}Resources"]["value"]["identityResourceId"],
                             f"[variables('stamps')[{index}].identityId]")

    def test_external_permissions_mode_has_no_active_iam_or_principal_creation_paths(self):
        protected = 0

        def check(template, inherited=False):
            nonlocal protected
            for resource in template.get("resources", []):
                condition = resource.get("condition", "")
                gated = inherited or condition == "[parameters('managePermissions')]" or condition.startswith(
                    "[and(parameters('managePermissions'), "
                )
                kind = resource["type"]
                if kind.startswith("Microsoft.Authorization/") or kind in (
                    "Microsoft.ManagedIdentity/userAssignedIdentities",
                    "Microsoft.KeyVault/vaults", "Microsoft.KeyVault/vaults/accessPolicies",
                    "Microsoft.EventGrid/systemTopics",
                ):
                    protected += 1
                    self.assertTrue(gated, f"Unprotected external-mode write: {kind} {resource['name']}")
                if kind == "Microsoft.Resources/deployments":
                    params = resource["properties"].get("parameters", {})
                    if resource["name"].endswith("-foundation"):
                        self.assertEqual(params["managePermissions"]["value"], "[parameters('managePermissions')]")
                    check(resource["properties"]["template"], gated)

        check(TEMPLATE)
        self.assertGreaterEqual(protected, 20)
        for side in ("primary", "secondary"):
            self.assertEqual(deployment(f"{side}-vault-access")["condition"], "[parameters('managePermissions')]")

    def test_all_module_inputs_and_resource_enumeration_are_transitively_runtime_free(self):
        checked = 0

        def check_template(template):
            nonlocal checked
            variables = template.get("variables", {}).copy()
            for variable in variables.pop("copy", []):
                variables[variable["name"]] = variable

            def check(value, seen=()):
                if isinstance(value, dict):
                    for item in value.values():
                        check(item, seen)
                elif isinstance(value, list):
                    for item in value:
                        check(item, seen)
                elif isinstance(value, str):
                    self.assertNotRegex(value, r"(?i)\b(references?|list\w*)\s*\(")
                    for name in re.findall(r"variables\('([^']+)'\)", value):
                        if name not in seen:
                            check(variables[name], (*seen, name))

            for resource in template.get("resources", []):
                checked += 1
                for field in ("name", "scope", "resourceGroup", "subscriptionId", "condition", "copy", "dependsOn"):
                    check(resource.get(field))
                if resource["type"] == "Microsoft.Resources/deployments":
                    check(resource["properties"].get("parameters"))
                    check_template(resource["properties"]["template"])

        check_template(TEMPLATE)
        self.assertGreater(checked, 100)

    def test_existing_vault_snapshot_and_static_service_contract(self):
        stamp = next(item for item in TEMPLATE["variables"]["copy"] if item["name"] == "stamps")["input"]
        for field, metadata in (("vaultUri", "vaultUri"), ("vaultUsesRbac", "usesRbac"), ("vaultTenantId", "tenantId")):
            self.assertIn(
                f"parameters('resolvedExistingVaults')[toLower(variables('stampInputs')[copyIndex('stamps')].vaultId)].{metadata}",
                stamp[field],
            )
        self.assertIn("environment().suffixes.keyvaultDns", stamp["vaultUri"])
        self.assertIn("Microsoft.Insights/components", stamp["runtime"]["insightsId"])
        self.assertNotIn("identityClientId", stamp["runtime"])
        self.assertNotIn("insightsConnectionString", stamp["runtime"])
        for field in ("resolvedExistingVaults", "resolvedVaultRoleAssignments"):
            self.assertEqual(TEMPLATE["parameters"][field]["defaultValue"], {})
        for side, index in (("primary", 0), ("secondary", 1)):
            worker = deployment(f"{side}-worker")["properties"]["parameters"]
            self.assertEqual(worker["foundation"]["value"], f"[variables('stamps')[{index}].runtime]")
            for name, field in (("sourceVaultId", "vaultId"), ("sourceVaultUri", "vaultUri")):
                self.assertEqual(worker[name]["value"], f"[variables('stamps')[{index}].{field}]")
            endpoints = deployment(f"{side}-endpoints")
            for stamp_side in ("primary", "secondary"):
                self.assertIn(f"[{module_id(stamp_side, 'foundation')}]", endpoints["dependsOn"])

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
                f"[if(variables('stamps')[{0 if side == 'primary' else 1}].vaultUsesRbac, 'rbac', 'accessPolicy')]",
            )
            self.assertEqual(
                main_outputs["identityPrincipalId"],
                f"[if(empty(parameters('{side}WorkerIdentityResourceId')), "
                f"{module_reference(side, 'worker-identity')}.outputs.principalId.value, "
                f"variables('stamps')[{0 if side == 'primary' else 1}].principalId)]",
            )

    def test_vault_grants_use_effective_model_tenant_and_correct_worker_principals(self):
        for side, other in (("primary", "secondary"), ("secondary", "primary")):
            parameters = deployment(f"{side}-vault-access")["properties"]["parameters"]
            index = 0 if side == "primary" else 1
            for parameter, field in (
                ("useRbac", "vaultUsesRbac"), ("tenantId", "vaultTenantId"),
                ("vaultName", "vaultName"), ("readerIdentityResourceId", "identityId"),
            ):
                self.assertEqual(
                    parameters[parameter]["value"],
                    f"[variables('stamps')[{index}].{field}]",
                )
            self.assertEqual(
                parameters["writerIdentityResourceId"]["value"],
                f"[variables('stamps')[{1 - index}].identityId]",
            )
            self.assertEqual(
                parameters["writerRoleId"],
                "[if(empty(parameters('replicaWriterRoleResourceId')), createObject('value', variables('replicaWriterRoleId')), "
                "createObject('value', parameters('replicaWriterRoleResourceId')))]",
            )
            self.assertEqual(parameters["existingRoleAssignmentNames"]["value"], "[parameters('resolvedVaultRoleAssignments')]")

    def test_access_policies_are_additive_and_exactly_secret_read_or_replica_write(self):
        policies = of_type("Microsoft.KeyVault/vaults/accessPolicies")
        self.assertEqual(len(policies), 2)
        for policy in policies:
            self.assertEqual(policy["condition"], "[and(parameters('isExistingVault'), not(parameters('useRbac')))]")
            self.assertEqual(policy["name"], "[format('{0}/{1}', parameters('vaultName'), 'add')]")
            self.assertEqual(policy["properties"]["accessPolicies"], [
                {
                    "tenantId": "[parameters('tenantId')]",
                    "objectId": f"[{selected_principal('readerIdentityResourceId', 'readerPrincipalId')}]",
                    "permissions": {"secrets": ["get", "list"]},
                },
                {
                    "tenantId": "[parameters('tenantId')]",
                    "objectId": f"[{selected_principal('writerIdentityResourceId', 'writerPrincipalId')}]",
                    "permissions": {"secrets": ["get", "list", "set"]},
                },
            ])

    def test_only_explicitly_reused_vaults_receive_additive_policy_updates(self):
        self.assertNotIn("isExistingVault", TEMPLATE["parameters"])
        for side in ("primary", "secondary"):
            access = deployment(f"{side}-vault-access")["properties"]
            self.assertEqual(
                access["parameters"]["isExistingVault"]["value"],
                f"[not(empty(parameters('{side}VaultResourceId')))]",
            )
            self.assertEqual(access["template"]["parameters"]["isExistingVault"]["type"], "bool")
            self.assertNotIn("defaultValue", access["template"]["parameters"]["isExistingVault"])
            policy = next(resource for resource in access["template"]["resources"]
                          if resource["type"] == "Microsoft.KeyVault/vaults/accessPolicies")
            self.assertEqual(policy["condition"], "[and(parameters('isExistingVault'), not(parameters('useRbac')))]")
            role = next(resource for resource in access["template"]["resources"]
                        if resource["type"] == "Microsoft.Authorization/roleAssignments")
            self.assertEqual(role["condition"], "[parameters('useRbac')]")

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
                properties["parameters"]["replicaWriterIdentityResourceId"]["value"],
                f"[variables('stamps')[{1 if side == 'primary' else 0}].identityId]",
            )
            managed_vault = foundation_module(side, "managed-vault")
            self.assertEqual(managed_vault["condition"],
                             "[and(parameters('managePermissions'), empty(parameters('existingVaultResourceId')))]")
            for parameter in ("workerIdentityResourceId", "replicaWriterIdentityResourceId",
                              "workerPrincipalId", "replicaWriterPrincipalId"):
                self.assertEqual(managed_vault["properties"]["parameters"][parameter]["value"],
                                 f"[parameters('{parameter}')]")
            nested = managed_vault["properties"]["template"]
            reader = (
                "createObject('tenantId', subscription().tenantId, 'objectId', "
                f"{selected_principal('workerIdentityResourceId', 'workerPrincipalId')}, "
                "'permissions', createObject('secrets', createArray('get', 'list')))"
            )
            writer = (
                "createObject('tenantId', subscription().tenantId, 'objectId', "
                f"{selected_principal('replicaWriterIdentityResourceId', 'replicaWriterPrincipalId')}, 'permissions', "
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
            "[or(variables('stamps')[0].vaultUsesRbac, variables('stamps')[1].vaultUsesRbac)]",
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
                    "identityId": "[parameters('readerIdentityResourceId')]",
                    "role": "[variables('readerRoleId')]",
                },
                {"identityId": "[parameters('writerIdentityResourceId')]", "role": "[parameters('writerRoleId')]"},
            ])
            self.assertEqual(
                access["variables"]["readerRoleId"],
                "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')]",
            )
            self.assertIn("parameters('existingRoleAssignmentNames')", assignments[0]["name"])
            self.assertIn(
                "guid(resourceId('Microsoft.KeyVault/vaults', parameters('vaultName')), "
                "variables('assignments')[copyIndex()].identityId, variables('assignments')[copyIndex()].role)",
                assignments[0]["name"],
            )
            self.assertEqual(assignments[0]["properties"]["principalType"], "ServicePrincipal")
            self.assertIn(identity_reference("readerIdentityResourceId"), assignments[0]["properties"]["principalId"])
            self.assertIn(identity_reference("writerIdentityResourceId"), assignments[0]["properties"]["principalId"])

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
            settings = {setting["name"]: setting["value"] for setting in props["siteConfig"]["appSettings"]}
            for name in ("AzureWebJobsStorage__clientId", "ServiceBusConnection__clientId",
                         "APPLICATIONINSIGHTS_AUTHENTICATION_STRING", "WORKER_CLIENT_ID"):
                self.assertIn("split(parameters('foundation').identityId, '/')[2]", settings[name])
                self.assertNotIn("subscription().subscriptionId", settings[name])

    def test_service_bus_receiver_uses_official_role_at_queue_scope(self):
        receivers = [
            assignment for assignment in of_type("Microsoft.Authorization/roleAssignments")
            if assignment["name"] == (
                "[guid(resourceId('Microsoft.ServiceBus/namespaces/queues', format('sb-{0}', parameters('prefix')), "
                f"'kv-events'), {identity_id('workerIdentityResourceId')}, 'receiver')]"
            )
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
        for side in ("primary", "secondary"):
            module = foundation_module(side, "managed-vault")
            self.assertEqual(module["condition"], "[and(parameters('managePermissions'), empty(parameters('existingVaultResourceId')))]")
            self.assertEqual(len([item for item in module["properties"]["template"]["resources"]
                                  if item["type"] == "Microsoft.KeyVault/vaults"]), 1)
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
                    self.assertEqual(
                        diagnostics["condition"],
                        "[or(and(parameters('managePermissions'), empty(parameters('existingVaultResourceId'))), "
                        "parameters('configureOwnedVaultDiagnostics'))]",
                    )
        for primary_id, secondary_id, expected_creates in [
            ("", "", 2), ("/existing-primary", "", 1),
            ("", "/existing-secondary", 1), ("/existing-primary", "/existing-secondary", 0),
        ]:
            creates = 0
            for foundation in foundations:
                selected_id = primary_id if foundation["name"] == "primary-foundation" else secondary_id
                for resource in foundation["properties"]["template"]["resources"]:
                    if resource["type"] != "Microsoft.Resources/deployments" or resource["name"] != "managed-vault":
                        continue
                    condition = resource["condition"]
                    self.assertEqual(condition, "[and(parameters('managePermissions'), empty(parameters('existingVaultResourceId')))]")
                    creates += not bool(selected_id)
            self.assertEqual(creates, expected_creates)
        for deployment in TEMPLATE["resources"]:
            if deployment["type"] == "Microsoft.Resources/deployments" and deployment["name"].endswith("-vault-access"):
                side = deployment["name"].split("-")[0]
                self.assertIn(f"split(parameters('{side}VaultResourceId'), '/')", deployment["resourceGroup"])

    def test_existing_topics_are_not_recreated_or_reconfigured(self):
        for side in ("primary", "secondary"):
            self.assertEqual(
                foundation_module(side, "managed-topic")["condition"],
                "[and(parameters('managePermissions'), empty(parameters('existingSystemTopicResourceId')))]",
            )
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


    def test_network_reuse_is_opt_in_and_the_default_still_creates_dedicated_vnets(self):
        for side in ("primary", "secondary"):
            mode = TEMPLATE["parameters"][f"{side}NetworkMode"]
            self.assertEqual(mode["defaultValue"], "new")
            self.assertEqual(mode["allowedValues"], ["new", "newSubnet", "existing"])
            for name in ("VnetName", "VnetResourceGroupName", "IntegrationSubnetName", "IntegrationSubnetPrefix", "PrivateEndpointSubnetName"):
                self.assertEqual(TEMPLATE["parameters"][f"{side}{name}"]["defaultValue"], "")
            self.assertEqual(TEMPLATE["parameters"][f"{side}EndpointsToCreate"]["defaultValue"], [])
            self.assertEqual(TEMPLATE["parameters"][f"{side}PrivateDnsZoneIds"]["defaultValue"], {})
            foundation = deployment(f"{side}-foundation")
            self.assertEqual(
                foundation["properties"]["parameters"]["createNetwork"]["value"],
                f"[equals(parameters('{side}NetworkMode'), 'new')]",
            )
            template = foundation["properties"]["template"]
            networks = [item for item in template["resources"] if item["type"] == "Microsoft.Network/virtualNetworks"]
            self.assertEqual([item.get("condition") for item in networks], ["[parameters('createNetwork')]"])
            self.assertEqual(
                template["variables"]["zoneList"],
                "[if(parameters('createNetwork'), items(parameters('zoneNames')), createArray())]",
            )

    def test_existing_vnets_are_referenced_without_being_redeployed(self):
        stamp = next(item for item in TEMPLATE["variables"]["copy"] if item["name"] == "stamps")["input"]
        local = lambda expression: expression.replace("variables('stampInputs')[copyIndex('stamps')]", "stamp")
        for reference in (stamp["endpointSubnetId"], stamp["runtime"]["integrationSubnetId"], stamp["runtime"]["endpointSubnetId"]):
            self.assertIn("stamp.vnetGroupName", local(reference))
            self.assertIn("'Microsoft.Network/virtualNetworks/subnets', stamp.vnetName", local(reference))
        self.assertIn("equals(variables('stampInputs')[copyIndex('stamps')].networkMode, 'new')", stamp["zoneIds"])
        self.assertIn("suppliedZoneIds", stamp["zoneIds"])
        self.assertIn("tryGet(variables('stampInputs')[copyIndex('stamps')].suppliedZoneIds, 'app')", stamp["runtime"]["appDnsZoneId"])
        # Reused VNets must never be redeclared; only the new-mode foundation owns a virtualNetworks resource.
        for resource in resources(TEMPLATE):
            if resource["type"] == "Microsoft.Network/virtualNetworks":
                self.assertEqual(resource["condition"], "[parameters('createNetwork')]")

    def test_new_subnet_mode_adds_only_the_delegated_integration_subnet_in_the_vnet_group(self):
        for side in ("primary", "secondary"):
            module = deployment(f"{side}-integration-subnet")
            self.assertEqual(module["condition"], f"[equals(parameters('{side}NetworkMode'), 'newSubnet')]")
            self.assertEqual(module["resourceGroup"], f"[parameters('{side}VnetResourceGroupName')]")
            template = module["properties"]["template"]
            subnets = [item for item in template["resources"] if item["type"] == "Microsoft.Network/virtualNetworks/subnets"]
            self.assertEqual(len(subnets), 1)
            self.assertEqual(
                subnets[0]["properties"]["delegations"][0]["properties"]["serviceName"], "Microsoft.App/environments",
            )
            self.assertFalse([item for item in template["resources"] if item["type"] == "Microsoft.Network/virtualNetworks"])
            app = deployment(f"{side}-worker")
            self.assertIn(f"{side}-integration-subnet", json.dumps(app["dependsOn"]))

    def test_network_inputs_are_validated_before_any_resource_changes(self):
        self.assertEqual(
            TEMPLATE["variables"]["validatedTags"],
            "[if(empty(variables('inputErrors')), parameters('tags'), fail(join(variables('inputErrors'), ' ')))]",
        )
        groups = of_type("Microsoft.Resources/resourceGroups")
        self.assertEqual(len(groups), 2)
        for group in groups:
            self.assertEqual(group["tags"], "[variables('validatedTags')]")
        errors = json.dumps(TEMPLATE["variables"]["inputErrors"])
        for message in ("NetworkMode is {1}, so set", "IntegrationSubnetName", "IntegrationSubnetPrefix",
                        "PrivateEndpointSubnetName", "EndpointsToCreate accepts only", "PrivateDnsZoneIds must map"):
            self.assertIn(message, errors)

    def test_vault_endpoints_are_optional_only_for_reused_vaults_in_reused_networks(self):
        owned = TEMPLATE["variables"]["primaryVaultIsSolutionOwned"]
        self.assertIn("empty(parameters('primaryVaultResourceId'))", owned)
        self.assertIn("'vault'", owned)
        stamp_inputs = TEMPLATE["variables"]["stampInputs"]
        for index, side in enumerate(("primary", "secondary")):
            local = stamp_inputs[index]["createLocalVaultEndpoint"]
            remote = stamp_inputs[index]["createRemoteVaultEndpoint"]
            for expression, owner, role in (
                (local, side, f"{side}Vault"),
                (remote, "secondary" if side == "primary" else "primary", f"{'secondary' if side == 'primary' else 'primary'}Vault"),
            ):
                self.assertIn(f"equals(parameters('{side}NetworkMode'), 'new')", expression)
                self.assertIn(f"variables('{owner}VaultIsSolutionOwned')", expression)
                self.assertIn(f"contains(parameters('{side}EndpointsToCreate'), '{role}')", expression)
        endpoints = {side: deployment(f"{side}-endpoints")["properties"]["parameters"] for side in ("primary", "secondary")}
        self.assertEqual(endpoints["primary"]["createLocalVaultEndpoint"]["value"], "[variables('stamps')[0].createLocalVaultEndpoint]")
        self.assertEqual(endpoints["secondary"]["createRemoteVaultEndpoint"]["value"], "[variables('stamps')[1].createRemoteVaultEndpoint]")

    def test_private_endpoints_tolerate_policy_managed_dns(self):
        zone_groups = of_type("Microsoft.Network/privateEndpoints/privateDnsZoneGroups")
        self.assertTrue(zone_groups)
        for group in zone_groups:
            self.assertEqual(group["condition"], "[not(empty(variables('zoneIds')))]")


if __name__ == "__main__":
    unittest.main()
