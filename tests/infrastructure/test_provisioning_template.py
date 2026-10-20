"""Assert delegated stage structure and execute the actual compiled ACI command locally."""

import base64
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

DIRECTORY = Path(sys.argv.pop(1))
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from deploy_delegated import encode_payload


def template(name):
    return json.loads((DIRECTORY / (name + ".json")).read_text(encoding="utf-8"))


def materialize_entrypoint(document):
    variables = document["variables"]
    validator = re.fullmatch(r"\[base64\(variables\('([^']+)'\)\)\]", variables["validator"])
    if validator is None:
        raise AssertionError("Unexpected embedded validator expression")
    encoded = base64.b64encode(variables[validator[1]].encode()).decode()
    entrypoint = re.fullmatch(
        r"\[format\('((?:[^']|'')*)', variables\('validator'\), variables\('([^']+)'\)\)\]",
        variables["entrypoint"],
    )
    if entrypoint is None:
        raise AssertionError("Entrypoint must interpolate actual source, not literal Bicep placeholders")
    return entrypoint[1].replace("''", "'").format(encoded, variables[entrypoint[2]])


class ProvisioningTemplateTests(unittest.TestCase):
    def test_container_is_keyless_pinned_bounded_and_uses_only_selected_identity(self):
        document = template("modules-provisioning-job")
        job, = document["resources"]
        self.assertEqual(job["type"], "Microsoft.ContainerInstance/containerGroups")
        self.assertEqual(job["identity"], {
            "type": "UserAssigned",
            "userAssignedIdentities": {"[format('{0}', parameters('provisioningIdentityResourceId'))]": {}},
        })
        self.assertEqual(document["parameters"]["payload"]["type"], "securestring")
        self.assertEqual(document["parameters"]["payload"]["maxLength"], 48000)
        props = job["properties"]
        self.assertEqual(props["restartPolicy"], "Never")
        self.assertNotIn("ipAddress", props)
        self.assertNotIn("volumes", props)
        container, = props["containers"]
        config = container["properties"]
        self.assertRegex(config["image"], r"^mcr\.microsoft\.com/azure-cli@sha256:[a-f0-9]{64}$")
        self.assertEqual(config["command"], ["python3", "-c", "[variables('entrypoint')]"])
        self.assertNotIn("ports", config)
        self.assertNotIn("volumeMounts", config)
        env = {value["name"]: value for value in config["environmentVariables"]}
        self.assertEqual(env["MRKV_PAYLOAD"]["secureValue"], "[parameters('payload')]")
        self.assertNotIn("value", env["MRKV_PAYLOAD"])

    def test_actual_compiled_python_logs_in_previews_applies_and_correlates_success(self):
        code = materialize_entrypoint(template("modules-provisioning-job"))
        self.assertNotIn("${", code)
        compile(code, "<compiled ACI entrypoint>", "exec")
        payload = {
            "version": 1, "action": "apply", "runId": "a" * 32,
            "subscription": "00000000-0000-0000-0000-000000000000",
            "clientId": "11111111-1111-1111-1111-111111111111",
            "timeoutSeconds": 120,
            "deployment": {"resourceGroup": "sample", "name": "test"},
            "template": {"resources": []}, "parameters": {"parameters": {}},
        }
        encoded, digest = encode_payload(payload)
        calls = []

        def az(arguments, **options):
            calls.append(arguments)
            self.assertEqual(arguments[0], "az")
            self.assertGreater(options["timeout"], 0)
            if arguments[1] == "login":
                self.assertEqual(arguments[2:6], ["--identity", "--client-id", payload["clientId"], "--output"])
                result = []
            elif arguments[3] == "what-if":
                self.assertEqual(arguments[arguments.index("--validation-level") + 1], "Provider")
                result = {"status": "Succeeded", "changes": []}
            elif arguments[3] == "create":
                path = arguments[arguments.index("--template-file") + 1]
                self.assertEqual(json.loads(Path(path).read_text()), payload["template"])
                result = {"id": "expected-deployment", "properties": {"provisioningState": "Succeeded"}}
            else:
                self.assertEqual(arguments[3], "validate")
                result = {}
            return subprocess.CompletedProcess(arguments, 0, json.dumps(result), "")

        output = StringIO()
        original_path = sys.path[:]
        with tempfile.TemporaryDirectory() as directory:
            loader_directory = Path(directory) / "loader"
            loader_directory.mkdir()
            try:
                with patch("tempfile.mkdtemp", return_value=str(loader_directory)), \
                        patch("subprocess.run", side_effect=az), \
                        patch.dict(os.environ, MRKV_PAYLOAD=encoded, MRKV_DIGEST=digest, MRKV_RUN_ID=payload["runId"]), \
                        contextlib.redirect_stdout(output):
                    # TemporaryDirectory also uses mkdtemp; isolate it from the loader stub.
                    with patch("tempfile.TemporaryDirectory") as task_directory:
                        task_directory.return_value.__enter__.return_value = directory
                        exec(code, {"__name__": "__main__"})
                self.assertEqual(
                    (loader_directory / "preview_validation.py").read_text(),
                    (ROOT / "scripts" / "preview_validation.py").read_text(),
                )
            finally:
                sys.path[:] = original_path
        self.assertEqual([call[1] if call[1] == "login" else call[3] for call in calls],
                         ["login", "validate", "what-if", "create"])
        marker = next(line for line in output.getvalue().splitlines() if line.startswith("MRKV_RESULT "))
        self.assertEqual(json.loads(marker[12:]), {
            "runId": payload["runId"], "digest": digest,
            "deploymentId": "expected-deployment", "status": "Succeeded",
        })

    def test_actual_compiled_entrypoint_rejects_tampering_before_login(self):
        code = materialize_entrypoint(template("modules-provisioning-job"))
        payload, _ = encode_payload({"runId": "a" * 32})
        original_path = sys.path[:]
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch("tempfile.mkdtemp", return_value=directory), patch("subprocess.run") as az, \
                        patch.dict(os.environ, MRKV_PAYLOAD=payload, MRKV_DIGEST="wrong", MRKV_RUN_ID="a" * 32), \
                        contextlib.redirect_stderr(StringIO()), self.assertRaises(SystemExit) as result:
                    exec(code, {"__name__": "__main__"})
                self.assertEqual(result.exception.code, 1)
                az.assert_not_called()
            finally:
                sys.path[:] = original_path

    def test_bootstrap_is_conditional_and_reuses_the_same_managed_parent_definitions(self):
        main = template("main")
        foundation = next(resource for resource in main["resources"] if resource["name"] == "primary-foundation")
        children = foundation["properties"]["template"]["resources"]
        bootstrap = template("provision-vault-topic")["resources"]
        for name, flag in (("managed-vault", "createVault"), ("managed-topic", "createTopic")):
            normal = next(resource for resource in children if resource["name"] == name)
            delegated = next(resource for resource in bootstrap if resource["name"] == name)
            self.assertEqual(delegated["condition"], f"[parameters('{flag}')]")
            self.assertEqual(normal["properties"]["template"], delegated["properties"]["template"])
        alert = next(resource for resource in children if resource["name"] == "[format('alert-{0}-event-delivery', parameters('prefix'))]")
        self.assertIn("[resourceId('Microsoft.Resources/deployments', 'managed-topic')]", alert["dependsOn"])

    def test_access_stage_has_no_worker_creation_or_custom_role_definition(self):
        resources = template("provision-access")["resources"]
        self.assertEqual({resource["name"] for resource in resources}, {"service-access", "vault-access"})
        for module in resources:
            for resource in module["properties"]["template"]["resources"]:
                self.assertIn(resource["type"], {
                    "Microsoft.Authorization/roleAssignments", "Microsoft.KeyVault/vaults/accessPolicies",
                })
        vault = next(resource for resource in resources if resource["name"] == "vault-access")
        self.assertIn("parameters('vaultResourceId')", vault["resourceGroup"])
        self.assertEqual(vault["properties"]["parameters"]["isExistingVault"]["value"], "[parameters('isExistingVault')]")


if __name__ == "__main__":
    unittest.main()
