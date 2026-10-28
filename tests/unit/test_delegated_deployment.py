"""Exercise every delegated stage with a local ARM/ACI double, never Azure."""

import base64
import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from deploy_delegated import (
    DelegatedDeployment, WRITER_ACTIONS, arm_parameters, completed_job, encode_payload, result_from_logs,
)
from preview_validation import validate_preview
from provisioning_task import run_task

SUB = "00000000-0000-0000-0000-000000000000"
BASE = f"/subscriptions/{SUB}"
PROVISIONER = f"{BASE}/resourceGroups/platform/providers/Microsoft.ManagedIdentity/userAssignedIdentities/provisioner"
ROLE = f"{BASE}/providers/Microsoft.Authorization/roleDefinitions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def identity(resource_id, number, tags=None):
    principal = f"{number:08d}-1111-1111-1111-111111111111"
    return {"id": resource_id, "properties": {"tenantId": SUB, "clientId": principal, "principalId": principal},
            "tags": tags or {}}


class AzureDouble:
    def __init__(self, new=False):
        self.resources = {PROVISIONER: identity(PROVISIONER, 1)}
        self.calls, self.jobs, self.privileged, self.contributor = [], {}, [], []
        self.groups_exist, self.infrastructure_exists = not new, not new
        self.values = {"provisioningIdentityResourceId": PROVISIONER, "provisioningResourceGroupName": "execution",
                       "secretNames": ["selected"], "provisioningTimeoutSeconds": 120}
        self.bad_preview = None
        self.running = False
        self.now = 0
        if not new:
            for number, side in enumerate(("primary", "secondary"), 2):
                base = f"{BASE}/resourceGroups/existing-{side}/providers"
                worker = f"{base}/Microsoft.ManagedIdentity/userAssignedIdentities/{side}-worker"
                vault = f"{base}/Microsoft.KeyVault/vaults/{side}"
                topic = f"{base}/Microsoft.EventGrid/systemTopics/{side}"
                location = "eastus" if side == "primary" else "centralus"
                self.resources[worker] = identity(worker, number)
                self.add_vault(vault, location, {})
                self.add_topic(topic, vault, location, {})
                for key, value in (("WorkerIdentity", worker), ("Vault", vault), ("SystemTopic", topic)):
                    self.values[f"{side}{key}ResourceId"] = value

    def add_vault(self, resource_id, location, tags, rbac=False):
        self.resources[resource_id] = {"id": resource_id, "location": location, "tags": tags, "properties": {
            "tenantId": SUB, "vaultUri": f"https://{resource_id.split('/')[-1]}.vault.azure.net/",
            "enableRbacAuthorization": rbac, "enablePurgeProtection": True, "publicNetworkAccess": "Disabled",
        }}

    def add_topic(self, resource_id, vault, location, tags):
        self.resources[resource_id] = {
            "id": resource_id, "source": vault, "location": location, "tags": tags,
            "identity": {"principalId": "99999999-1111-1111-1111-111111111111"},
        }

    def query(self, *args):
        self.calls.append(args)
        option = lambda key: args[args.index(key) + 1]
        if args[0] == "version":
            return {"azure-cli": "2.91.0"}
        if args[:2] == ("bicep", "build"):
            return {"stage": Path(option("--file")).name}
        if args[:2] == ("group", "show"):
            return {"id": f"{BASE}/resourceGroups/execution"}
        if args[:2] == ("account", "show"):
            return {"tenantId": SUB}
        if args[:2] == ("resource", "show"):
            resource_id = option("--ids")
            if resource_id not in self.resources:
                raise RuntimeError("(ResourceNotFound) synthetic missing resource")
            return copy.deepcopy(self.resources[resource_id])
        if args[:3] == ("eventgrid", "system-topic", "list"):
            return [copy.deepcopy(resource) for resource in self.resources.values() if "source" in resource]
        if args[:4] == ("eventgrid", "system-topic", "event-subscription", "list"):
            return []
        if args[:3] == ("role", "assignment", "list"):
            return []
        if args[:3] == ("role", "definition", "list"):
            return [{"id": ROLE, "roleType": "CustomRole", "assignableScopes": [BASE],
                     "permissions": [{"actions": [], "dataActions": sorted(WRITER_ACTIONS)}]}]
        if args[0] == "deployment":
            if args[2] == "validate":
                return {}
            if args[2] == "show":
                group, name = option("--resource-group"), option("--name")
                return {"id": f"{BASE}/resourceGroups/{group}/providers/Microsoft.Resources/deployments/{name}",
                        "properties": {"provisioningState": "Succeeded"}}
            if args[2] == "what-if":
                source = json.loads(Path(option("--template-file")).read_text())["stage"]
                if source == self.bad_preview:
                    return {"status": "Succeeded", "changes": [], "diagnostics": [{"code": "Skipped"}]}
                if source == "provisioning-layout.bicep":
                    changes = [{"resourceId": f"{BASE}/resourceGroups/rg-mrkv-{side}-abcdefgh",
                                "changeType": "NoChange" if self.groups_exist else "Create"} for side in ("p", "s")]
                else:
                    kind = "NoChange" if source == "main.bicep" and self.infrastructure_exists else "Create"
                    changes = [{"resourceId": f"{BASE}/resourceGroups/sample/providers/Synthetic/resources/item",
                                "changeType": kind}]
                return {"status": "Succeeded", "changes": changes}
        if args[0] == "rest":
            body = json.loads(Path(option("--body").removeprefix("@")).read_text())
            deployment_id = option("--url").split("?")[0].removeprefix("https://management.azure.com")
            values = {key: item["value"] for key, item in body["properties"]["parameters"].items()}
            source = body["properties"]["template"]["stage"]
            self.contributor.append((source, values))
            if source == "provisioning-layout.bicep":
                self.groups_exist = True
            elif source == "main.bicep":
                self.infrastructure_exists = True
            elif source == "provisioning-job.bicep":
                payload = json.loads(zlib.decompress(base64.b64decode(values["payload"])))
                child_values = {key: item["value"] for key, item in payload["parameters"]["parameters"].items()}
                child_source = payload["template"]["stage"]
                self.privileged.append((child_source, child_values))
                group = payload["deployment"]["resourceGroup"]
                prefix = child_values["prefix"]
                base = f"{BASE}/resourceGroups/{group}/providers"
                if child_source == "worker-identity.bicep":
                    worker = f"{base}/Microsoft.ManagedIdentity/userAssignedIdentities/id-{prefix}-worker"
                    self.resources[worker] = identity(worker, len(self.resources) + 2, child_values["tags"])
                elif child_source == "provision-vault-topic.bicep":
                    vault = child_values["vaultResourceId"]
                    if child_values["createVault"]:
                        self.add_vault(vault, child_values["location"], child_values["tags"],
                                       child_values["vaultAuthorizationModel"] == "rbac")
                    if child_values["createTopic"]:
                        topic = f"{base}/Microsoft.EventGrid/systemTopics/evgt-{prefix}"
                        self.add_topic(topic, vault, child_values["location"], child_values["tags"])
                child_id = f"{BASE}/resourceGroups/{group}/providers/Microsoft.Resources/deployments/{payload['deployment']['name']}"
                resource = {"tags": {"execution": values["runId"], "payload": values["payloadDigest"]},
                            "containers": [{"name": "provision", "instanceView": {"restartCount": 0, "currentState": {
                                "state": "Running" if self.running else "Terminated", "exitCode": 0,
                            }}}]}
                record = {"runId": values["runId"], "digest": values["payloadDigest"],
                          "deploymentId": child_id, "status": "Succeeded"}
                self.jobs["aci-mrkv-" + values["runId"][:12]] = (resource, "MRKV_RESULT " + json.dumps(record))
            else:
                raise AssertionError(source)
            return {"id": deployment_id, "properties": {"provisioningState": "Succeeded"}}
        if args[:2] == ("container", "show"):
            return copy.deepcopy(self.jobs[option("--name")][0])
        raise AssertionError(f"Unexpected Azure command: {args}")

    def text(self, *args):
        self.calls.append(args)
        if args[:2] == ("container", "logs"):
            return self.jobs[args[args.index("--name") + 1]][1]
        if args[:2] == ("container", "stop"):
            return ""
        raise AssertionError(args)

    def sleep(self, seconds):
        self.now += seconds

    def run(self, apply=False):
        output = StringIO()
        deployment = DelegatedDeployment(
            arm_parameters(self.values), SUB, "eastus", apply,
            query=self.query, stop=self.text, emit=lambda value: print(value, file=output),
            clock=lambda: self.now, sleep=self.sleep,
        )
        with redirect_stdout(output):
            deployment.run()
        return deployment, output.getvalue()


class DelegatedTests(unittest.TestCase):
    def test_deployment_previews_use_configured_timeout_not_metadata_query_timeout(self):
        deployment = DelegatedDeployment(arm_parameters({"provisioningTimeoutSeconds": 900}), SUB, "eastus")
        with patch("deploy_delegated.azure_json", return_value={}) as cli:
            deployment.query("deployment", "sub", "what-if")
            cli.assert_called_once_with("deployment", "sub", "what-if", timeout=900)
            cli.reset_mock()
            deployment.query("version")
            cli.assert_called_once_with("version", timeout=120)

    def test_existing_resources_are_referenced_and_only_aci_performs_iam(self):
        fake = AzureDouble()
        original = copy.deepcopy(fake.values)
        fake.run(apply=True)
        self.assertEqual(fake.values, original)
        self.assertEqual([stage for stage, _ in fake.privileged], ["provision-access.bicep"] * 2)
        for stage, values in fake.contributor:
            self.assertIn(stage, ("provisioning-layout.bicep", "main.bicep", "provisioning-job.bicep"))
            if stage == "main.bicep":
                self.assertFalse(values["managePermissions"])
                self.assertEqual(values["provisioningIdentityResourceId"], "")
                self.assertFalse(values["enableWorker"])
        self.assertTrue(all(call[0] != "identity" for call in fake.calls))

    def test_cross_subscription_provisioner_and_workers_preserve_deployment_scopes(self):
        for selected in (("provisioning",), ("primaryWorker", "secondaryWorker"),
                         ("provisioning", "primaryWorker", "secondaryWorker")):
            with self.subTest(selected=selected):
                fake = AzureDouble()
                external_ids = []
                for index, name in enumerate(selected, 2):
                    key = name + "IdentityResourceId"
                    original = fake.values[key]
                    external_subscription = f"{index}" * 8 + "-2222-2222-2222-222222222222"
                    external = original.replace(SUB, external_subscription)
                    fake.resources[external] = {**fake.resources.pop(original), "id": external}
                    fake.values[key] = external
                    external_ids.append(external)
                fake.run(apply=True)
                reads = [call for call in fake.calls if call[:2] == ("resource", "show") and call[3] in external_ids]
                self.assertEqual({call[3] for call in reads}, set(external_ids))
                for call in reads:
                    self.assertEqual(call[call.index("--subscription") + 1], call[3].split("/")[2])
                for call in fake.calls:
                    if call[:2] == ("group", "show") or call[0] == "deployment":
                        self.assertEqual(call[call.index("--subscription") + 1], SUB)
                core = next(values for stage, values in fake.contributor if stage == "main.bicep")
                for side in ("primary", "secondary"):
                    key = side + "WorkerIdentityResourceId"
                    self.assertEqual(core[key], fake.values[key])
                for stage, values in fake.contributor:
                    if stage == "provisioning-job.bicep":
                        self.assertEqual(values["provisioningIdentityResourceId"], fake.values["provisioningIdentityResourceId"])

    def test_cross_tenant_provisioner_is_rejected_before_deployment(self):
        fake = AzureDouble()
        fake.resources[PROVISIONER]["properties"]["tenantId"] = "22222222-2222-2222-2222-222222222222"
        with self.assertRaisesRegex(ValueError, "different tenant"):
            fake.run(apply=True)
        self.assertEqual(fake.contributor, [])

    def test_new_resources_bootstrap_before_core_and_grants(self):
        fake = AzureDouble(new=True)
        fake.run(apply=True)
        self.assertEqual([stage for stage, _ in fake.privileged], [
            "worker-identity.bicep", "worker-identity.bicep",
            "provision-vault-topic.bicep", "provision-vault-topic.bicep",
            "provision-access.bicep", "provision-access.bicep",
        ])
        core = next(values for stage, values in fake.contributor if stage == "main.bicep")
        self.assertTrue(core["replicateAllSecrets"])
        self.assertTrue(core["resolvedOwnedFoundationDiagnostics"]["primary"]["vault"])
        for _, grant in fake.privileged[-2:]:
            self.assertFalse(grant["isExistingVault"])
        self.assertNotIn(PROVISIONER, (core["primaryWorkerIdentityResourceId"], core["secondaryWorkerIdentityResourceId"]))

    def test_preview_never_creates_compute_or_changes_resources(self):
        for new in (False, True):
            fake = AzureDouble(new=new)
            before = copy.deepcopy(fake.resources)
            _, output = fake.run()
            self.assertEqual(fake.resources, before)
            self.assertEqual(fake.contributor, [])
            self.assertEqual(fake.privileged, [])
            self.assertNotIn(("container", "stop"), [call[:2] for call in fake.calls])
            self.assertIn("Preview only", output)
            if new:
                self.assertIn("DEFERRED", output)

    def test_missing_worker_preview_defers_later_stages(self):
        fake = AzureDouble(new=True)
        fake.groups_exist = True
        _, output = fake.run()
        self.assertIn("identity bootstrap reviewed", output)
        self.assertIn("DEFERRED", output)
        self.assertEqual(fake.jobs, {})

    def test_incomplete_privileged_preview_blocks_container_launch(self):
        fake = AzureDouble()
        fake.bad_preview = "provision-access.bicep"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            fake.run(apply=True)
        self.assertEqual(fake.privileged, [])

    def test_mixed_vault_models_use_predefined_role_and_exact_scope_inventory(self):
        fake = AzureDouble()
        vault = fake.values["primaryVaultResourceId"]
        fake.resources[vault]["properties"]["enableRbacAuthorization"] = True
        with self.assertRaisesRegex(ValueError, "predefined"):
            fake.run(apply=True)
        self.assertEqual(fake.contributor, [])
        fake.values["replicaWriterRoleResourceId"] = ROLE
        fake.run(apply=True)
        calls = [call for call in fake.calls if call[:3] == ("role", "assignment", "list")]
        self.assertTrue(calls)
        self.assertTrue(all("--scope" in call and "--all" not in call for call in calls))
        self.assertEqual({values["useRbac"] for _, values in fake.privileged}, {True, False})

    def test_rejects_conflicting_modes_worker_provisioner_reuse_and_unowned_names(self):
        for changes in (
            {"managePermissions": False}, {"primaryWorkerIdentityResourceId": PROVISIONER},
            {"provisioningResourceGroupName": ""}, {"provisioningTimeoutSeconds": 0},
        ):
            fake = AzureDouble()
            fake.values.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fake.run(apply=True)
            self.assertEqual(fake.contributor, [])
        fake = AzureDouble(new=True)
        resource_id = f"{BASE}/resourceGroups/rg-mrkv-p-abcdefgh/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-mrkv-p-abcdefgh-worker"
        fake.resources[resource_id] = identity(resource_id, 2)
        with self.assertRaisesRegex(ValueError, "Refusing to adopt"):
            fake.run(apply=True)
        self.assertEqual(fake.contributor, [])

    def test_job_timeout_stops_exact_execution_and_reports_partial_work(self):
        fake = AzureDouble()
        fake.running = True
        with self.assertRaisesRegex(TimeoutError, "completion"):
            fake.run(apply=True)
        stops = [call for call in fake.calls if call[:2] == ("container", "stop")]
        self.assertEqual(len(stops), 1)
        self.assertTrue(stops[0][stops[0].index("--name") + 1].startswith("aci-mrkv-"))
        self.assertEqual(stops[0][stops[0].index("--resource-group") + 1], "execution")

    def test_activation_follows_successful_authorization(self):
        fake = AzureDouble()
        fake.values["enableWorker"] = True
        fake.values["enableEventRouting"] = True
        fake.run(apply=True)
        cores = [values for stage, values in fake.contributor if stage == "main.bicep"]
        self.assertFalse(cores[0]["enableWorker"])
        self.assertTrue(cores[1]["enableWorker"])

    def test_parent_and_infrastructure_preview_deferral_is_read_only(self):
        fake = AzureDouble(new=True)
        fake.run(apply=True)
        # Retain the owned worker resources, then preview the missing parents.
        fake.resources = {key: value for key, value in fake.resources.items()
                          if "/userAssignedIdentities/" in key}
        fake.values = {key: value for key, value in fake.values.items() if not key.endswith("ResourceId") or key == "provisioningIdentityResourceId"}
        fake.contributor.clear()
        _, output = fake.run()
        self.assertIn("parent bootstrap reviewed", output)
        self.assertEqual(fake.contributor, [])
        existing = AzureDouble()
        existing.infrastructure_exists = False
        _, output = existing.run()
        self.assertIn("IAM and activation previews are DEFERRED", output)
        self.assertEqual(existing.jobs, {})

    def test_owned_bootstrap_reruns_preserve_identity_ids(self):
        fake = AzureDouble(new=True)
        fake.run(apply=True)
        workers = {key: value["properties"]["principalId"] for key, value in fake.resources.items()
                   if "/userAssignedIdentities/" in key}
        fake.contributor.clear()
        fake.privileged.clear()
        fake.run(apply=True)
        self.assertNotIn("worker-identity.bicep", [stage for stage, _ in fake.privileged])
        self.assertEqual(workers, {key: value["properties"]["principalId"] for key, value in fake.resources.items()
                                  if "/userAssignedIdentities/" in key})

    def test_read_denial_and_foreign_tenant_never_become_bootstrap_absence(self):
        fake = AzureDouble(new=True)
        original = fake.query
        def denied(*arguments):
            if arguments[:2] == ("resource", "show") and arguments[arguments.index("--ids") + 1] != PROVISIONER:
                raise RuntimeError("(AuthorizationFailed) denied")
            return original(*arguments)
        fake.query = denied
        with self.assertRaisesRegex(RuntimeError, "AuthorizationFailed"):
            fake.run(apply=True)
        self.assertEqual(fake.contributor, [])
        fake = AzureDouble()
        fake.resources[PROVISIONER]["properties"]["tenantId"] = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        with self.assertRaisesRegex(ValueError, "tenant"):
            fake.run(apply=True)
        self.assertEqual(fake.contributor, [])

    def test_rejects_overbroad_or_unassignable_replica_role(self):
        for mutation in (
            {"dataActions": ["*"]}, {"actions": ["*"]}, {"notDataActions": ["excluded"]},
            {"dataActions": sorted(WRITER_ACTIONS - {"microsoft.keyvault/vaults/secrets/update/action"})},
            {"assignableScopes": [BASE + "/resourceGroups/unrelated"]},
        ):
            with self.subTest(mutation=mutation):
                fake = AzureDouble()
                fake.resources[fake.values["primaryVaultResourceId"]]["properties"]["enableRbacAuthorization"] = True
                fake.values["replicaWriterRoleResourceId"] = ROLE
                original = fake.query
                def role(*arguments):
                    result = original(*arguments)
                    if arguments[:3] == ("role", "definition", "list"):
                        if "assignableScopes" in mutation:
                            result[0].update(mutation)
                        else:
                            result[0]["permissions"][0].update(mutation)
                    return result
                fake.query = role
                with self.assertRaises(ValueError):
                    fake.run(apply=True)
                self.assertEqual(fake.contributor, [])

    def test_cli_timeout_stops_job_and_preserves_original_error_if_stop_fails(self):
        fake = AzureDouble()
        original_query, original_text = fake.query, fake.text
        def query(*arguments):
            if arguments[:2] == ("container", "show"):
                raise subprocess.TimeoutExpired(["az", "container", "show"], 120)
            return original_query(*arguments)
        def text(*arguments):
            if arguments[:2] == ("container", "stop"):
                raise RuntimeError("synthetic stop denial")
            return original_text(*arguments)
        fake.query, fake.text = query, text
        with self.assertRaises(subprocess.TimeoutExpired) as error:
            fake.run(apply=True)
        self.assertIn("synthetic stop denial", str(error.exception.__notes__))

    def test_failed_bootstrap_never_deploys_infrastructure_or_activates(self):
        fake = AzureDouble(new=True)
        fake.bad_preview = "provision-vault-topic.bicep"
        fake.values["enableWorker"] = True
        with self.assertRaisesRegex(ValueError, "incomplete"):
            fake.run(apply=True)
        self.assertNotIn("main.bicep", [stage for stage, _ in fake.contributor])

    def test_failed_child_arm_confirmation_stops_job_without_activation(self):
        fake = AzureDouble()
        fake.values["enableWorker"] = True
        original = fake.query
        def query(*arguments):
            result = original(*arguments)
            if arguments[:3] == ("deployment", "group", "show"):
                result["properties"]["provisioningState"] = "Failed"
            return result
        fake.query = query
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            fake.run(apply=True)
        cores = [value for stage, value in fake.contributor if stage == "main.bicep"]
        self.assertEqual(len(cores), 1)
        self.assertFalse(cores[0]["enableWorker"])
        self.assertTrue(any(call[:2] == ("container", "stop") for call in fake.calls))

    def test_parent_privileged_previews_only_use_provider_no_rbac(self):
        fake = AzureDouble()
        fake.run()
        for command in fake.calls:
            if command[0] == "deployment" and command[2] in ("validate", "what-if"):
                privileged = command[command.index("--name") + 1].startswith("mrkv-preview-")
                self.assertEqual("--validation-level" in command, privileged)
                if privileged:
                    self.assertEqual(command[command.index("--validation-level") + 1], "ProviderNoRbac")

    def test_arm_failed_canceled_or_unfinished_deployments_are_not_confirmed(self):
        expected_id = f"{BASE}/providers/Microsoft.Resources/deployments/test"
        for state in ("Failed", "Canceled", "Running", "Succeeded"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                now = [0]
                result = {"id": "wrong-deployment" if state == "Succeeded" else expected_id,
                          "properties": {"provisioningState": state}}
                deployment = DelegatedDeployment(
                    arm_parameters({}), SUB, "eastus", query=Mock(return_value=result),
                    clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
                )
                deployment.directory, deployment.timeout = Path(directory), 120
                template, parameters = deployment.files({"resources": []}, arm_parameters({}))
                common = ("deployment", "sub", "--subscription", SUB, "--location", "eastus",
                          "--name", "test", "--template-file", template, "--parameters", parameters)
                expected = TimeoutError if state == "Running" else ValueError if state == "Succeeded" else RuntimeError
                with self.assertRaises(expected):
                    deployment.create(common, "test-stage")
                self.assertEqual(deployment.completed, [])

    def test_task_validates_and_previews_before_create(self):
        calls = []
        def query(*args):
            calls.append(args)
            if args[2] == "what-if":
                return {"status": "Succeeded", "changes": []}
            return {"id": "deployment", "properties": {"provisioningState": "Succeeded"}}
        payload = {"version": 1, "action": "apply", "subscription": SUB, "deployment": {
            "name": "job", "resourceGroup": "scope", "templateFile": "template.json", "parametersFile": "params.json",
        }}
        self.assertEqual(run_task(payload, query, lambda _: None), "deployment")
        self.assertEqual([call[2] for call in calls], ["validate", "what-if", "create"])
        for call in calls[:2]:
            self.assertEqual(call[call.index("--validation-level") + 1], "Provider")
        failed = Mock(side_effect=[{}, {"status": "Succeeded", "changes": [], "diagnostics": [1]}])
        with self.assertRaises(ValueError):
            run_task(payload, failed, lambda _: None)
        self.assertEqual(failed.call_count, 2)

    def test_completion_requires_identity_digest_exit_and_arm_correlation(self):
        resource = {"tags": {"execution": "run", "payload": "hash"}, "containers": [
            {"name": "provision", "instanceView": {"restartCount": 0, "currentState": {"state": "Terminated", "exitCode": 0}}}
        ]}
        self.assertTrue(completed_job(resource, "run", "hash"))
        missing = copy.deepcopy(resource)
        del missing["containers"][0]["instanceView"]["restartCount"]
        with self.assertRaisesRegex(ValueError, "restart count"):
            completed_job(missing, "run", "hash")
        for malformed in (None, {}, {"tags": None}, {**resource, "containers": None},
                          {**resource, "properties": None}):
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                completed_job(malformed, "run", "hash")
        for patch in ({"exitCode": 1}, {"state": "Running"}):
            changed = copy.deepcopy(resource)
            changed["containers"][0]["instanceView"]["currentState"].update(patch)
            if patch.get("exitCode"):
                with self.assertRaises(RuntimeError):
                    completed_job(changed, "run", "hash")
            else:
                self.assertFalse(completed_job(changed, "run", "hash"))
        resource["containers"][0]["instanceView"]["restartCount"] = 1
        with self.assertRaisesRegex(RuntimeError, "restarted"):
            completed_job(resource, "run", "hash")
        for logs in ("", "MRKV_FAILURE {}", 'MRKV_RESULT {"status":"Succeeded"}'):
            with self.assertRaises(RuntimeError):
                result_from_logs(logs, "run", "hash", "deployment")
        record = {"runId": "run", "digest": "hash", "deploymentId": "deployment", "status": "Succeeded"}
        result_from_logs("MRKV_RESULT " + json.dumps(record), "run", "hash", "deployment")
        for mutation in ({"runId": "wrong"}, {"digest": "wrong"}, {"deploymentId": "wrong"},
                         {"status": "Failed"}, {"unexpected": "field"}):
            with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                result_from_logs("MRKV_RESULT " + json.dumps({**record, **mutation}), "run", "hash", "deployment")
        with self.assertRaises(RuntimeError):
            result_from_logs(("MRKV_RESULT " + json.dumps(record) + "\n") * 2, "run", "hash", "deployment")

    def test_payload_integrity_bounds_and_preview_envelopes(self):
        payload = {"value": "ordinary parameters"}
        encoded, digest = encode_payload(payload)
        self.assertEqual(json.loads(zlib.decompress(base64.b64decode(encoded))), payload)
        self.assertEqual(len(digest), 64)
        with self.assertRaisesRegex(ValueError, "bound"):
            encode_payload({"data": random.Random(0).randbytes(60000).hex()})
        preview = {"status": "Succeeded", "properties": {"changes": []}}
        self.assertEqual(validate_preview(preview), [])
        for invalid in (None, {"status": "Failed"}, {"status": "Succeeded", "changes": {}, "diagnostics": []},
                        {"status": "Succeeded", "changes": [{"resourceId": "id", "changeType": "Delete"}]},
                        {"status": "Succeeded", "changes": [], "nextLink": "next"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_preview(invalid)
