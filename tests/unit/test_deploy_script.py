"""Exercise the real deployment runner with local command doubles; never contacts Azure."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESOURCE_ID = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/sample"
CHANGE = {"resourceId": RESOURCE_ID, "changeType": "Create", "after": {"properties": {"marker": "FULL_PAYLOAD"}}}

WRAPPER = r"""
$ErrorActionPreference = 'Stop'
function Record-Call($Tool, $Arguments) {
    $record = @{ tool = $Tool; arguments = @($Arguments) }
    $index = [array]::IndexOf($Arguments, '--parameters')
    if ($index -ge 0 -and (Test-Path -LiteralPath $Arguments[$index + 1])) {
        $record['parameters'] = Get-Content -LiteralPath $Arguments[$index + 1] -Raw | ConvertFrom-Json
    }
    Add-Content -LiteralPath $env:TEST_LOG -Value ($record | ConvertTo-Json -Depth 30 -Compress)
}
function az {
    Record-Call 'az' $args
    $global:LASTEXITCODE = 0
    $stage = if ($args[0] -eq 'version') { 'version' } elseif ($args[0] -eq 'bicep') { 'compile' } else { $args[2] }
    if ($stage -eq $env:TEST_FAILURE) { $global:LASTEXITCODE = 1; return }
    switch ($stage) {
        'version' { return ('{"azure-cli":"' + $env:TEST_CLI_VERSION + '"}') }
        'compile' {
            $index = [array]::IndexOf($args, '--outfile')
            Copy-Item -LiteralPath $env:TEST_PARAMETERS -Destination $args[$index + 1]
        }
        'what-if' { Get-Content -LiteralPath $env:TEST_PREVIEW -Raw }
        'create' { Set-Content -LiteralPath $env:TEST_CREATED -Value 'created' }
    }
}
function python {
    Record-Call 'python' $args
    $global:LASTEXITCODE = 0
    if ($args[0] -like '*deploy_delegated.py') {
        if ($env:TEST_FAILURE -eq 'delegated') { $global:LASTEXITCODE = 1 }
        return
    }
    if ($env:TEST_FAILURE -eq 'preflight') { $global:LASTEXITCODE = 1; return }
    $index = [array]::IndexOf($args, '--output-parameters')
    if ($index -lt 0) { throw 'Runner did not request prepared parameters' }
    $prepared = Get-Content -LiteralPath $env:TEST_PARAMETERS -Raw | ConvertFrom-Json
    $prepared.parameters | Add-Member -NotePropertyName resolvedExistingVaults -NotePropertyValue @{ value = @{} }
    $prepared.parameters | Add-Member -NotePropertyName resolvedVaultRoleAssignments -NotePropertyValue @{ value = @{} }
    $prepared.parameters | Add-Member -NotePropertyName resolvedWorkerIdentities -NotePropertyValue @{ value = @{} }
    $prepared | ConvertTo-Json -Depth 30 | Set-Content -LiteralPath $args[$index + 1]
}
try {
    & $env:TEST_SCRIPT -SubscriptionId '00000000-0000-0000-0000-000000000000' `
        -ParametersFile $env:TEST_USER_PARAMETERS -Apply:($env:TEST_APPLY -eq '1')
} catch {
    Write-Output $_.Exception.Message
    exit 1
}
"""


@unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 is required for deployment runner tests")
class DeploymentRunnerTests(unittest.TestCase):
    def run_preview(self, preview, *, apply=True, failure="", version="2.91.0", delegated=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp = root / "temporary"
            temp.mkdir()
            wrapper, parameters, result = root / "wrapper.ps1", root / "parameters.json", root / "preview.json"
            log, created = root / "calls.jsonl", root / "created"
            wrapper.write_text(WRAPPER, encoding="utf-8")
            values = {"workload": {"value": "mrkv"}}
            if delegated:
                values["provisioningIdentityResourceId"] = {"value": "synthetic-provisioner"}
            parameters.write_text(json.dumps({"parameters": values}), encoding="utf-8")
            result.write_text(preview if isinstance(preview, str) else json.dumps(preview), encoding="utf-8")
            env = {
                **os.environ, "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp),
                "TEST_SCRIPT": str(ROOT / "scripts" / "Deploy.ps1"),
                "TEST_USER_PARAMETERS": str(ROOT / "infra" / "parameters" / "example.bicepparam"),
                "TEST_PARAMETERS": str(parameters), "TEST_PREVIEW": str(result),
                "TEST_LOG": str(log), "TEST_CREATED": str(created),
                "TEST_FAILURE": failure, "TEST_CLI_VERSION": version, "TEST_APPLY": "1" if apply else "0",
            }
            completed = subprocess.run(
                [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
                env=env, capture_output=True, text=True, timeout=60,
            )
            calls = [json.loads(line) for line in log.read_text(encoding="utf-8-sig").splitlines()] if log.exists() else []
            self.assertEqual(list(temp.iterdir()), [], "Runner left temporary parameter/what-if files behind")
            return completed, calls, created.exists()

    def test_clean_cli_and_rest_previews_reuse_prepared_parameters(self):
        for nested in (False, True):
            for apply in (False, True):
                with self.subTest(nested=nested, apply=apply):
                    payload = {"changes": [CHANGE], "diagnostics": []}
                    preview = {"status": "Succeeded", **({"properties": payload} if nested else payload)}
                    result, calls, created = self.run_preview(preview, apply=apply)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(created, apply)
                    self.assertIn("FULL_PAYLOAD", result.stdout)
                    commands = [call for call in calls if call["tool"] == "az" and call["arguments"][:2] == ["deployment", "sub"]]
                    expected = ["validate", "what-if", "create"] if apply else ["validate", "what-if"]
                    self.assertEqual([call["arguments"][2] for call in commands], expected)
                    parameter_paths = [call["arguments"][call["arguments"].index("--parameters") + 1] for call in commands]
                    self.assertEqual(len(set(parameter_paths)), 1)
                    for call in commands:
                        self.assertIn("resolvedExistingVaults", call["parameters"]["parameters"])
                        self.assertIn("resolvedVaultRoleAssignments", call["parameters"]["parameters"])
                        self.assertIn("resolvedWorkerIdentities", call["parameters"]["parameters"])
                    self.assertIn("--no-pretty-print", commands[1]["arguments"])
                    self.assertEqual(
                        commands[1]["arguments"][commands[1]["arguments"].index("--result-format") + 1],
                        "FullResourcePayloads",
                    )

    def test_clean_empty_preview_is_valid(self):
        result, _, created = self.run_preview({"status": "Succeeded", "changes": []}, apply=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(created)

    def test_supplied_provisioner_routes_only_to_delegated_orchestration(self):
        for apply in (False, True):
            result, calls, created = self.run_preview({}, apply=apply, delegated=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(created)
            python = [call for call in calls if call["tool"] == "python"]
            self.assertEqual(len(python), 1)
            self.assertTrue(python[0]["arguments"][0].endswith("deploy_delegated.py"))
            self.assertEqual("--apply" in python[0]["arguments"], apply)
            self.assertFalse(any(call["arguments"][:1] == ["deployment"] for call in calls))
        result, _, _ = self.run_preview({}, delegated=True, failure="delegated")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("partially completed", result.stdout)

    def test_incomplete_and_invalid_previews_never_create(self):
        valid = {"status": "Succeeded", "changes": [CHANGE]}
        previews = [
            {**valid, "diagnostics": [{"code": "NestedDeploymentShortCircuited"}]},
            {**valid, "diagnostics": [{"code": "FutureCoverageWarning"}]},
            {**valid, "error": {"code": "DeploymentFailed"}},
            {**valid, "status": "Failed"},
            {**valid, "status": "Running"},
            {"status": "Succeeded"},
            {"changes": []},
            {**valid, "changes": {}},
            {**valid, "changes": [{"changeType": "Create"}]},
            {**valid, "changes": [{**CHANGE, "unsupportedReason": "not analyzed"}]},
            {**valid, "potentialChanges": [CHANGE]},
            {**valid, "nextLink": "https://example.invalid/next"},
            {"status": "Succeeded", "properties": {"changes": [CHANGE], "diagnostics": [{"code": "Skipped"}]}},
            {"status": "Succeeded", "properties": {"changes": [CHANGE], "error": {"code": "Failed"}}},
            "invalid JSON",
            "null",
        ]
        previews.extend({**valid, "changes": [{**CHANGE, "changeType": kind}]} for kind in ("Ignore", "Unsupported", "Deploy", "FutureType"))
        for preview in previews:
            with self.subTest(preview=preview):
                result, _, created = self.run_preview(preview)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(created)

    def test_command_failures_and_old_cli_never_create(self):
        for failure in ("version", "compile", "preflight", "validate", "what-if"):
            with self.subTest(failure=failure):
                result, _, created = self.run_preview({"status": "Succeeded", "changes": []}, failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(created)
        result, calls, created = self.run_preview({"status": "Succeeded", "changes": []}, version="2.74.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("2.75.0", result.stdout)
        self.assertFalse(created)
