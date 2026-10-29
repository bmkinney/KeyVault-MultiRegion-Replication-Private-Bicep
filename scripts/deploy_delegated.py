"""Staged Contributor/managed-identity deployment. Preview never launches compute."""

import argparse
import base64
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path
from uuid import uuid4

from check_vault_integration import (
    azure_json, azure_text, existing_role_assignment_names, preflight, preflight_workers,
    preflight_workspaces, prepare_parameters, validate_id,
)
from delegated_readiness import ReadinessError, check, grant_guide, provider_namespaces
from preview_validation import validate_preview

ROOT = Path(__file__).resolve().parents[1]
WRITER_ACTIONS = {
    "microsoft.keyvault/vaults/secrets/readmetadata/action",
    "microsoft.keyvault/vaults/secrets/getsecret/action",
    "microsoft.keyvault/vaults/secrets/setsecret/action",
    "microsoft.keyvault/vaults/secrets/update/action",
}


def arm_parameters(values):
    return {"parameters": {key: {"value": value} for key, value in values.items()}}


def encode_payload(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    encoded = base64.b64encode(zlib.compress(raw)).decode()
    if len(encoded) > 48000:
        raise ValueError("Provisioning payload exceeds the supported 48,000-character bound")
    return encoded, hashlib.sha256(raw).hexdigest()


def completed_job(resource, run_id, digest):
    tags = resource.get("tags") if isinstance(resource, dict) else None
    if not isinstance(tags, dict) or tags.get("execution") != run_id or tags.get("payload") != digest:
        raise ValueError("ACI execution does not match the submitted request")
    props = resource.get("properties", resource)
    if not isinstance(props, dict):
        raise ValueError("ACI returned an invalid resource envelope")
    if props.get("provisioningState") in ("Failed", "Canceled"):
        raise RuntimeError("ACI resource provisioning failed")
    containers = props.get("containers", [])
    if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict) or containers[0].get("name") != "provision":
        raise ValueError("ACI returned an unexpected container set")
    view = containers[0].get("instanceView", {})
    if not isinstance(view, dict):
        raise ValueError("ACI returned an invalid container instance view")
    restarts = view.get("restartCount")
    if restarts is not None and (type(restarts) is not int or restarts != 0):
        raise RuntimeError("Provisioning container restarted; inspect the partial deployment")
    state = view.get("currentState", {})
    if not isinstance(state, dict):
        raise ValueError("ACI returned an invalid process state")
    if state.get("state") != "Terminated":
        return False
    if restarts is None:
        raise ValueError("ACI did not confirm the completed process restart count")
    if type(state.get("exitCode")) is not int or state["exitCode"] != 0:
        raise RuntimeError("Provisioning container exited unsuccessfully")
    return True


def result_from_logs(logs, run_id, digest, deployment_id):
    records = []
    for line in logs.splitlines():
        if line.startswith("MRKV_FAILURE "):
            raise RuntimeError("Provisioning task reported failure")
        if line.startswith("MRKV_RESULT "):
            records.append(json.loads(line.removeprefix("MRKV_RESULT ")))
    if len(records) != 1 or not isinstance(records[0], dict):
        raise RuntimeError("Missing or mismatched provisioning completion result")
    record = records[0]
    if (
        set(record) != {"runId", "digest", "deploymentId", "status"} or record["runId"] != run_id
        or record["digest"] != digest or record["status"] != "Succeeded"
        or not isinstance(record["deploymentId"], str) or record["deploymentId"].lower() != deployment_id.lower()
    ):
        raise RuntimeError("Missing or mismatched provisioning completion result")


class DelegatedDeployment:
    def __init__(self, document, subscription, location, apply=False, *, query=None, stop=azure_text,
                 emit=print, clock=time.monotonic, sleep=time.sleep):
        self.document = copy.deepcopy(document)
        self.values = {key: item["value"] for key, item in document["parameters"].items()}
        self.subscription, self.location, self.apply = subscription, location, apply
        self.query, self.stop, self.emit, self.clock, self.sleep = query or self.azure_query, stop, emit, clock, sleep
        self.timeout = self.values.get("provisioningTimeoutSeconds", 1800)
        self.completed = []
        self.templates = {}
        self.directory = None

    def azure_query(self, *arguments):
        long_preview = arguments[0] == "deployment" and arguments[2] in ("validate", "what-if")
        return azure_json(*arguments, timeout=self.timeout if long_preview else 120)

    def compile(self, relative):
        if relative not in self.templates:
            self.templates[relative] = self.query(
                "bicep", "build", "--file", str(ROOT / "infra" / Path(relative)), "--stdout",
            )
        return self.templates[relative]

    def files(self, template, parameters):
        token = uuid4().hex
        files = [self.directory / f"{token}-{suffix}.json" for suffix in ("template", "parameters")]
        for path, value in zip(files, (template, parameters)):
            path.write_text(json.dumps(value), encoding="utf-8")
        return str(files[0]), str(files[1])

    def preview(self, template, parameters, name, group=None, privileged=False):
        template_file, parameters_file = self.files(template, parameters)
        common = (
            "deployment", "group" if group else "sub", "--subscription", self.subscription,
            *(("--resource-group", group) if group else ("--location", self.location)),
            "--name", name, "--template-file", template_file, "--parameters", parameters_file,
        )
        level = ("--validation-level", "ProviderNoRbac") if privileged else ()
        self.emit(f"Stage preview: {name} ({'provisioning identity' if privileged else 'Contributor'} applies)")
        self.query(*common[:2], "validate", *common[2:], *level)
        preview = self.query(
            *common[:2], "what-if", *common[2:], *level,
            "--no-pretty-print", "--result-format", "FullResourcePayloads",
        )
        self.emit(json.dumps(preview, indent=2))
        changes = validate_preview(preview)
        return changes, common

    def create(self, common, stage):
        options = dict(zip(common[2::2], common[3::2]))
        scope = f"/subscriptions/{self.subscription}"
        if common[1] == "group":
            scope += "/resourceGroups/" + options["--resource-group"]
        deployment_id = scope + "/providers/Microsoft.Resources/deployments/" + options["--name"]
        body = {"properties": {
            "mode": "Incremental",
            "template": json.loads(Path(options["--template-file"]).read_text(encoding="utf-8")),
            "parameters": json.loads(Path(options["--parameters"]).read_text(encoding="utf-8"))["parameters"],
        }}
        if common[1] == "sub":
            body["location"] = self.location
        body_file = self.directory / f"{uuid4().hex}-deployment.json"
        body_file.write_text(json.dumps(body), encoding="utf-8")
        deadline = self.clock() + self.timeout
        result = self.query(
            "rest", "--method", "put", "--url",
            f"https://management.azure.com{deployment_id}?api-version=2025-04-01",
            "--body", "@" + str(body_file), "--subscription", self.subscription,
        )
        while self.clock() < deadline:
            properties = result.get("properties") if isinstance(result, dict) else None
            if not isinstance(properties, dict):
                raise ValueError(f"{stage}: ARM returned an invalid deployment envelope")
            state = properties.get("provisioningState")
            if state == "Succeeded":
                result_id = result.get("id")
                if not isinstance(result_id, str) or result_id.lower() != deployment_id.lower():
                    raise ValueError("ARM confirmed a different deployment")
                self.completed.append(stage)
                return
            if state in ("Failed", "Canceled"):
                raise RuntimeError(f"{stage}: ARM deployment {state}; inspect deployment operations")
            self.sleep(min(5, max(0, deadline - self.clock())))
            result = self.query(
                *common[:2], "show", "--name", options["--name"], "--subscription", self.subscription,
                *(("--resource-group", options["--resource-group"]) if common[1] == "group" else ()),
            )
        raise TimeoutError(f"{stage}: ARM deployment deadline exceeded; submitted operations can still be running")

    def owned_or_missing(self, resource_id):
        try:
            resource = self.query("resource", "show", "--ids", resource_id, "--subscription", self.subscription)
        except RuntimeError as error:
            if re.search(r"\((?:ResourceNotFound|ResourceGroupNotFound)\)", str(error)):
                return None
            raise
        if resource.get("id", "").lower() != resource_id.lower():
            raise ValueError("Azure returned a different bootstrap resource")
        if resource.get("tags", {}).get("mrkv-provisioning-owner") != self.owner:
            raise ValueError(f"Refusing to adopt an existing bootstrap name; supply its explicit ARM ID: {resource_id}")
        return resource

    def job(self, template, parameters, group, stage):
        run_id = uuid4().hex
        deployment_name = f"mrkv-{stage}-{run_id[:12]}"
        payload = {
            "version": 1, "action": "apply", "runId": run_id, "subscription": self.subscription,
            "clientId": self.provisioner["clientId"], "timeoutSeconds": self.timeout,
            "deployment": {"resourceGroup": group, "name": deployment_name},
            "template": template, "parameters": parameters,
        }
        encoded, digest = encode_payload(payload)
        job_parameters = arm_parameters({
            "location": self.values.get("provisioningLocation", self.values.get("primaryLocation", "eastus")),
            "provisioningIdentityResourceId": self.provisioner_id,
            "runId": run_id, "payloadDigest": digest, "payload": encoded,
        })
        _, common = self.preview(
            self.compile("modules/provisioning-job.bicep"), job_parameters,
            f"mrkv-execution-{run_id[:12]}", self.execution_group,
        )
        if not self.apply:
            raise RuntimeError("Internal error: preview attempted to launch privileged compute")
        container = f"aci-mrkv-{run_id[:12]}"
        expected_id = (
            f"/subscriptions/{self.subscription}/resourceGroups/{group}"
            f"/providers/Microsoft.Resources/deployments/{deployment_name}"
        )
        deadline = self.clock() + self.timeout
        try:
            self.create(common, f"{stage}:container-created")
            while self.clock() < deadline:
                resource = self.query(
                    "container", "show", "--name", container, "--resource-group", self.execution_group,
                    "--subscription", self.subscription,
                )
                if completed_job(resource, run_id, digest):
                    logs = self.stop(
                        "container", "logs", "--name", container, "--resource-group", self.execution_group,
                        "--subscription", self.subscription,
                    )
                    self.emit(logs)
                    result_from_logs(logs, run_id, digest, expected_id)
                    result = self.query(
                        "deployment", "group", "show", "--name", deployment_name, "--resource-group", group,
                        "--subscription", self.subscription,
                    )
                    result_id = result.get("id") if isinstance(result, dict) else None
                    properties = result.get("properties") if isinstance(result, dict) else None
                    if (
                        not isinstance(result_id, str) or result_id.lower() != expected_id.lower()
                        or not isinstance(properties, dict) or properties.get("provisioningState") != "Succeeded"
                    ):
                        raise RuntimeError("Provisioning result was not confirmed by ARM")
                    self.completed.append(stage)
                    self.delete_container(container)
                    return
                self.sleep(min(5, max(0, deadline - self.clock())))
            raise TimeoutError(f"{stage}: provisioning completion deadline exceeded")
        except (RuntimeError, ValueError, TimeoutError, subprocess.TimeoutExpired, OSError, KeyboardInterrupt) as error:
            try:
                self.stop(
                    "container", "stop", "--name", container, "--resource-group", self.execution_group,
                    "--subscription", self.subscription,
                )
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as stop_error:
                self.emit(f"Execution stop also failed: {stop_error}")
                error.add_note(str(stop_error))
            else:
                self.emit(f"{stage}: execution stop requested.")
            self.emit("Submitted ARM operations can still be running; inspect before retrying.")
            raise

    def delete_container(self, container):
        try:
            self.stop(
                "container", "delete", "--name", container, "--resource-group", self.execution_group,
                "--subscription", self.subscription, "--yes",
            )
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as error:
            self.emit(f"Warning: could not delete completed container {container}: {error}")

    def privileged(self, source, values, group, stage):
        template, parameters = self.compile(source), arm_parameters(values)
        changes, _ = self.preview(template, parameters, f"mrkv-preview-{stage}", group, True)
        changed = any(item["changeType"] != "NoChange" for item in changes)
        if self.apply and changed:
            self.job(template, parameters, group, stage)
        return changed

    def role(self, vaults):
        role_id = self.values.get("replicaWriterRoleResourceId", "")
        if not any(vault["usesRbac"] for vault in vaults.values()):
            return ""
        match = re.fullmatch(
            rf"/subscriptions/{re.escape(self.subscription)}/providers/Microsoft.Authorization/roleDefinitions/([a-fA-F0-9-]{{36}})",
            role_id, re.IGNORECASE,
        )
        if not match:
            raise ValueError("Delegated RBAC requires an administrator-predefined replicaWriterRoleResourceId")
        roles = self.query("role", "definition", "list", "--name", match[1], "--subscription", self.subscription)
        if len(roles) != 1 or roles[0].get("id", "").lower() != role_id.lower() or roles[0].get("roleType") != "CustomRole":
            raise ValueError("Replica writer role is not the selected custom role")
        permissions = roles[0].get("permissions", [])
        if (
            len(permissions) != 1 or permissions[0].get("actions") or permissions[0].get("notActions")
            or permissions[0].get("notDataActions")
            or {item.lower() for item in permissions[0].get("dataActions", [])} != WRITER_ACTIONS
        ):
            raise ValueError("Replica writer role must contain exactly the four non-destructive secret data actions")
        for vault_id, metadata in vaults.items():
            if metadata["usesRbac"] and not any(
                vault_id.lower().startswith(scope.rstrip("/").lower() + "/") or vault_id.lower() == scope.lower()
                for scope in roles[0].get("assignableScopes", [])
            ):
                raise ValueError("Replica writer role is not assignable at a selected vault")
        return role_id

    def run(self):
        with tempfile.TemporaryDirectory(prefix="mrkv-") as directory:
            self.directory = Path(directory)
            try:
                self._run()
            except (RuntimeError, ValueError, KeyError, TimeoutError, subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
                self.emit("Not complete. Confirmed stages: " + (", ".join(self.completed) or "none"))
                raise

    def _run(self):
        values = self.values
        version = self.query("version")["azure-cli"]
        if tuple(int(part) for part in version.split(".")[:3]) < (2, 76, 0):
            raise ValueError("Delegated previews require Azure CLI 2.76.0 or newer (ProviderNoRbac)")
        if values.get("managePermissions", True) is not True:
            raise ValueError("Provisioning identity cannot be combined with managePermissions = false in the user file")
        self.provisioner_id = values["provisioningIdentityResourceId"]
        validate_id(self.provisioner_id, self.subscription, "Microsoft.ManagedIdentity/userAssignedIdentities")
        # Optional override; empty selects the primary solution group once the layout stage resolves it.
        self.execution_group = values.get("provisioningResourceGroupName", "")
        if self.execution_group and (
            not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,89}", self.execution_group) or self.execution_group.endswith(".")
        ):
            raise ValueError("provisioningResourceGroupName must use ASCII letters/digits/._- or be omitted")
        if type(self.timeout) is not int or not 120 <= self.timeout <= 7200:
            raise ValueError("provisioningTimeoutSeconds must be between 120 and 7200")
        worker_ids = [values.get(f"{side}WorkerIdentityResourceId", "") for side in ("primary", "secondary")]
        for worker_id in worker_ids:
            if worker_id:
                validate_id(worker_id, self.subscription, "Microsoft.ManagedIdentity/userAssignedIdentities")
        workspace_ids = sorted({
            item for item in (values.get(f"{side}LogAnalyticsWorkspaceResourceId", "") for side in ("primary", "secondary")) if item
        }, key=str.lower)
        for workspace_id in workspace_ids:
            validate_id(workspace_id, self.subscription, "Microsoft.OperationalInsights/workspaces")
        blockers, warnings, principal = check(
            subscription=self.subscription, provisioner_id=self.provisioner_id, worker_ids=worker_ids,
            workspace_ids=workspace_ids,
            override_group=self.execution_group,
            namespaces=provider_namespaces(self.compile("main.bicep"), self.compile("modules/provisioning-job.bicep")),
            query=self.query,
        )
        for line in grant_guide(self.subscription, self.provisioner_id, principal, worker_ids, workspace_ids):
            self.emit(line)
        for warning in warnings:
            self.emit("Readiness warning: " + warning)
        if blockers:
            raise ReadinessError("Readiness check failed; nothing was changed:\n" + "\n".join("  - " + item for item in blockers))
        workload = values.get("workload", "mrkv")
        if not re.fullmatch(r"[a-z0-9]{3,8}", workload):
            raise ValueError("workload must be 3-8 lowercase letters/digits")
        original = self.document["parameters"]
        vaults = preflight(original, self.subscription, self.query)
        preflight_workers(original, self.subscription, vaults, self.query)
        preflight_workspaces(original, self.subscription, self.query)
        provisioners = preflight_workers(
            arm_parameters({"primaryWorkerIdentityResourceId": self.provisioner_id})["parameters"],
            self.subscription, vaults, self.query,
        )
        self.provisioner = provisioners[self.provisioner_id.lower()]
        # Preserve the new-vault whole-vault default before substituting references.
        values.setdefault("replicateAllSecrets", not any(values.get(f"{side}VaultResourceId", "") for side in ("primary", "secondary")))
        tags = values.get("tags", {"workload": workload, "purpose": "private-secret-replication-demo"})
        self.owner = hashlib.sha256(f"{self.subscription.lower()}|{workload}".encode()).hexdigest()[:32]
        owned_tags = {**tags, "mrkv-provisioning-owner": self.owner}
        layout_values = {
            "workload": workload, "primaryLocation": values.get("primaryLocation", "eastus"),
            "secondaryLocation": values.get("secondaryLocation", "centralus"), "tags": tags,
        }
        layout, common = self.preview(
            self.compile("provisioning-layout.bicep"), arm_parameters(layout_values), "mrkv-layout",
        )
        groups = {}
        for change in layout:
            resource_id = change["resourceId"]
            match = re.fullmatch(
                rf"/subscriptions/{re.escape(self.subscription)}/resourceGroups/rg-{workload}-([ps])-([a-z0-9]{{8}})",
                resource_id, re.IGNORECASE,
            )
            if not match and change["changeType"] == "Ignore" and re.match(
                rf"/subscriptions/{re.escape(self.subscription)}/resourceGroups/rg-{workload}-[ps]-[a-z0-9]{{8}}/",
                resource_id, re.IGNORECASE,
            ):
                continue  # Existing resources inside the solution groups, which the layout never manages.
            if not match or match[1] in groups:
                raise ValueError("Layout preview must contain exactly the two deterministic solution groups")
            groups[match[1]] = (resource_id.split("/")[-1], match[2])
        if set(groups) != {"p", "s"} or groups["p"][1] != groups["s"][1]:
            raise ValueError("Could not resolve solution groups from the complete layout preview")
        self.execution_group = self.execution_group or groups["p"][0]
        stamps = []
        for side, short in (("primary", "p"), ("secondary", "s")):
            group, token = groups[short]
            prefix = group.removeprefix("rg-")
            stamp = {
                "side": side, "group": group, "prefix": prefix, "storageToken": f"{workload}{short}{token}",
                "location": layout_values[side + "Location"],
            }
            for suffix, kind, name in (
                ("WorkerIdentity", "Microsoft.ManagedIdentity/userAssignedIdentities", f"id-{prefix}-worker"),
                ("Vault", "Microsoft.KeyVault/vaults", f"kv-{prefix}"),
                ("SystemTopic", "Microsoft.EventGrid/systemTopics", f"evgt-{prefix}"),
            ):
                parameter = f"{side}{suffix}ResourceId"
                # Event Grid requires a system topic to share its source vault's resource group.
                owner_group = stamp["Vault"].split("/")[4] if suffix == "SystemTopic" else group
                stamp[suffix] = values.get(parameter) or (
                    f"/subscriptions/{self.subscription}/resourceGroups/{owner_group}/providers/{kind}/{name}"
                )
                stamp["owned" + suffix] = not bool(values.get(parameter))
                if stamp["owned" + suffix]:
                    stamp["existing" + suffix] = self.owned_or_missing(stamp[suffix])
            if stamp["WorkerIdentity"].lower() == self.provisioner_id.lower():
                raise ValueError("Provisioning identity must not be used as a runtime worker")
            stamps.append(stamp)
        if stamps[0]["WorkerIdentity"].lower() == stamps[1]["WorkerIdentity"].lower():
            raise ValueError("Regional workers must use different identities")
        projected_vaults = {
            stamp["Vault"].lower(): vaults.get(stamp["Vault"].lower(), {
                "usesRbac": values.get(stamp["side"] + "VaultAuthorizationModel", "accessPolicy") == "rbac",
            }) for stamp in stamps
        }
        writer_role = self.role(projected_vaults)
        if not self.apply and any(item["changeType"] == "Create" for item in layout):
            self.emit("Preview only: solution-group bootstrap reviewed. Worker/parent, infrastructure, and IAM previews are DEFERRED until an explicitly authorized -Apply creates the groups. Nothing was created.")
            return
        if self.apply:
            self.create(common, "layout")
        deferred = False
        for stamp in stamps:
            if stamp["ownedWorkerIdentity"] and stamp["existingWorkerIdentity"] is None:
                self.privileged("modules/worker-identity.bicep", {
                    "prefix": stamp["prefix"], "location": stamp["location"], "tags": owned_tags,
                }, stamp["group"], stamp["side"] + "-worker")
                deferred |= not self.apply
        if deferred:
            self.emit("Preview only: identity bootstrap reviewed. Vault/topic, infrastructure, and IAM previews are DEFERRED until an explicitly authorized -Apply creates the workers. Nothing was created.")
            return
        references = {
            **values, "managePermissions": False, "provisioningIdentityResourceId": "",
            **{stamp["side"] + "WorkerIdentityResourceId": stamp["WorkerIdentity"] for stamp in stamps},
        }
        workers = preflight_workers(arm_parameters(references)["parameters"], self.subscription, vaults, self.query)
        if any(worker["principalId"].lower() == self.provisioner["principalId"].lower() for worker in workers.values()):
            raise ValueError("Provisioning principal must not be used as a runtime worker")
        for index, stamp in enumerate(stamps):
            other = stamps[1 - index]
            if stamp["ownedVault"] or stamp["ownedSystemTopic"]:
                changed = self.privileged("provision-vault-topic.bicep", {
                    "prefix": stamp["prefix"], "location": stamp["location"], "tags": owned_tags,
                    "vaultResourceId": stamp["Vault"],
                    "createVault": stamp["ownedVault"], "createTopic": stamp["ownedSystemTopic"],
                    "vaultAuthorizationModel": values.get(stamp["side"] + "VaultAuthorizationModel", "accessPolicy"),
                    "workerIdentityResourceId": stamp["WorkerIdentity"],
                    "replicaWriterIdentityResourceId": other["WorkerIdentity"],
                    "workerPrincipalId": workers[stamp["WorkerIdentity"].lower()]["principalId"],
                    "replicaWriterPrincipalId": workers[other["WorkerIdentity"].lower()]["principalId"],
                }, stamp["group"] if stamp["ownedVault"] else stamp["Vault"].split("/")[4], stamp["side"] + "-parents")
                deferred |= not self.apply and changed
        if deferred:
            self.emit("Preview only: parent bootstrap reviewed. Infrastructure and IAM previews are DEFERRED until authorized bootstrap completes. Nothing was created.")
            return
        for stamp in stamps:
            for suffix in ("Vault", "SystemTopic"):
                references[stamp["side"] + suffix + "ResourceId"] = stamp[suffix]
        core = prepare_parameters(arm_parameters(references), self.subscription, self.query)
        if any(
            core["parameters"]["resolvedExistingVaults"]["value"][vault_id]["usesRbac"] != metadata["usesRbac"]
            for vault_id, metadata in projected_vaults.items()
        ):
            raise ValueError("Vault authorization changed during delegated preparation; review a fresh preview")
        core["parameters"]["resolvedOwnedFoundationDiagnostics"] = {"value": {
            stamp["side"]: {"vault": stamp["ownedVault"], "topic": stamp["ownedSystemTopic"] and stamp["ownedVault"]} for stamp in stamps
        }}
        for flag in ("enableWorker", "enableEventRouting"):
            core["parameters"][flag] = {"value": False}
        # Event Grid validates delivery grants when a subscription is created, so routes wait for the access stages.
        core["parameters"]["deployEventRouting"] = {"value": False}
        infrastructure_changes, common = self.preview(self.compile("main.bicep"), core, "mrkv-infrastructure")
        if self.apply:
            self.create(common, "infrastructure-fenced")
        elif any(item["changeType"] != "NoChange" for item in infrastructure_changes):
            self.emit("Preview only: infrastructure reviewed. IAM and activation previews are DEFERRED until authorized infrastructure deployment completes. Nothing was created.")
            return
        snapshot = core["parameters"]["resolvedExistingVaults"]["value"]
        names = existing_role_assignment_names(workload, self.subscription, self.query, workers,
                                               [stamp["Vault"] for stamp in stamps]) if writer_role else {}
        for index, stamp in enumerate(stamps):
            other = stamps[1 - index]
            topic = self.query("resource", "show", "--ids", stamp["SystemTopic"], "--subscription", self.subscription)
            principal = topic.get("identity", {}).get("principalId")
            if topic.get("id", "").lower() != stamp["SystemTopic"].lower() or not principal:
                raise ValueError("Missing selected system-topic principal")
            metadata = snapshot[stamp["Vault"].lower()]
            self.privileged("provision-access.bicep", {
                "prefix": stamp["prefix"], "storageToken": stamp["storageToken"],
                "workerIdentityResourceId": stamp["WorkerIdentity"],
                "workerPrincipalId": workers[stamp["WorkerIdentity"].lower()]["principalId"],
                "systemTopicResourceId": stamp["SystemTopic"], "topicPrincipalId": principal,
                "vaultResourceId": stamp["Vault"], "isExistingVault": not stamp["ownedVault"],
                "useRbac": metadata["usesRbac"], "tenantId": metadata["tenantId"],
                "writerIdentityResourceId": other["WorkerIdentity"],
                "writerPrincipalId": workers[other["WorkerIdentity"].lower()]["principalId"],
                "writerRoleId": writer_role, "existingRoleAssignmentNames": names,
            }, stamp["group"], stamp["side"] + "-access")
        core["parameters"]["deployEventRouting"] = {"value": True}
        _, common = self.preview(self.compile("main.bicep"), core, "mrkv-routing")
        if self.apply:
            self.create(common, "routing")
        if values.get("enableWorker") or values.get("enableEventRouting"):
            for flag in ("enableWorker", "enableEventRouting"):
                core["parameters"][flag] = {"value": values.get(flag, False)}
            _, common = self.preview(self.compile("main.bicep"), core, "mrkv-activation")
            if self.apply:
                self.create(common, "activation")
        self.emit("Delegated ARM stages completed. Runtime grant propagation/private access remain to be verified."
                  if self.apply else "Preview only: all currently evaluable stages reviewed; no resources were created.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parameters", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--location", default="eastus")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    document = json.loads(Path(args.parameters).read_text(encoding="utf-8-sig"))
    try:
        DelegatedDeployment(document, args.subscription, args.location, args.apply).run()
    except ReadinessError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
