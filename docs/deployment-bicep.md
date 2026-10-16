# Bicep deployment guide

**Illustration only; not intended for production use.** This guide and its sample
code are subject to the [sample code disclaimer](../DISCLAIMER.md).

This is a deployment procedure, not evidence that a live deployment has passed.
Do not run deployment or live tests until the subscription owner authorizes them.

## 1. Prerequisites and preflight

Use Azure CLI **2.75.0 or newer** with Bicep, PowerShell 7, Python, and GA Azure
Functions Core Tools v4. The CLI minimum exposes what-if coverage diagnostics;
the runner rejects older versions rather than silently losing that safety check.
Publish from a machine with private connectivity and DNS to the target app and
storage. A VPN-connected workstation or an approved private runner is suitable.
Default public Cloud Shell and ordinary public CI runners are not substitutes.

Select a subscription and check:

- Allowed locations and resource types; storage/key/public-network policies.
- Flex Consumption support and regional memory quota in East US/Central US.
- Premium Service Bus quota/capacity; ZRS availability.
- Provider registrations: Microsoft.Network, Microsoft.Storage, Microsoft.Web,
  Microsoft.App, Microsoft.KeyVault, Microsoft.EventGrid, Microsoft.ServiceBus,
  Microsoft.ManagedIdentity, Microsoft.Insights, Microsoft.OperationalInsights.
- Permission to deploy subscription/resource-group resources. The default
  `managePermissions = true` mode additionally requires permission to assign roles.
  Contributor alone does not grant role-assignment write permissions. RBAC
  vaults also require custom-role-definition write permission; policy-based
  vaults require `Microsoft.KeyVault/vaults/accessPolicies/write` at their scopes.
  Storage, Service Bus, Event Grid delivery, and monitoring still use RBAC even
  when both vaults use access policies.
- For Contributor-only operators, use the [external-permissions procedure](deployment-contributor.md)
  and `infra\parameters\contributor.example.bicepparam`. Administrators prepare
  identities/resources/grants separately; `managePermissions = false` omits all
  role definitions, role assignments, and vault policy updates.
- Nonoverlapping CIDRs with the deployment network.
- Global name availability for vault, storage, namespace, and app names.
- Operator private access to both monitoring DNS views. Do not merge two AMPLS
  DNS views into a common resolver without redesign.

Resource names use a short workload prefix and deterministic subscription suffix.
Use only lowercase letters/numbers for `workload`; decorators enforce length,
and ARM validation enforces service-specific character rules.

Role assignments may take up to ten minutes to propagate. A successful ARM
deployment is not evidence that every data-plane permission is ready.
Verify policy grants and actual worker access too; read-only compatibility
preflight does not test managed-identity secret reads or writes.

## 2. Configure initial parameters

For existing vaults, follow [existing-vault integration](existing-vaults.md) and
start with `infra\parameters\existing-vaults.example.bicepparam`. The deployment
script performs read-only compatibility and topic-reuse preflight; Python must
be available. Leave vault IDs empty to retain new-vault behavior.

Copy the appropriate example to `infra\parameters\deployment.local.bicepparam`
(Git-ignored) and edit that file only. Do not change Bicep files or supply worker
principal/client IDs. By default the template creates both worker identities.
Set either `primaryWorkerIdentityResourceId` or `secondaryWorkerIdentityResourceId`
to reuse a same-subscription, same-tenant user-assigned identity, including one in
another resource group. The runner discovers its principal/client IDs read-only.
An empty ID still creates that worker when `managePermissions = true`.
Both regional workers must use distinct identities. Reuse alone does not remove
the caller's IAM-write requirements; use `managePermissions = false` and its
administrator prerequisites for that.
Set a real `alertEmail` before operational use. Do not commit personal recipients
if your organization treats them as private information.

Keep:

```bicep
param enableWorker = false
param enableEventRouting = false
param authoritativeRegion = 'primary'
```

These settings fence both triggers and mute both subscriptions using a nonmatching
subject filter. The resources are present but do not process vault changes.

### Vault authorization

New solution-managed vaults default to access policies, explicitly setting
`enableRbacAuthorization = false` even with API `2026-02-01`. Choose independently:

```bicep
param primaryVaultAuthorizationModel = 'accessPolicy'
param secondaryVaultAuthorizationModel = 'accessPolicy'
```

Use `'rbac'` for either or both parameters to opt into RBAC. When a vault resource
ID is supplied, its actual authorization model is detected and preserved instead;
the corresponding model parameter does not change it. False, legacy null, or an
omitted `enableRbacAuthorization` means access policies. Mixed pairs are supported.
Deployment outputs report each effective model and worker principal ID.

Access policies grant the local worker secret **Get, List** and the opposite
worker **Get, List, Set** for reverse-direction recovery. Set also permits secret
attribute updates, including disabling a replica. There is no policy permission
named Update. No key, certificate, delete, purge, backup, or restore permission is
granted. Use managed-identity **principal/object IDs**, not client IDs, in policies.
The same identity-authenticated worker code serves both models.

With `managePermissions = true`, a reused policy-based vault's child
`accessPolicies/add` request adds both
worker entries without replacing the full list or unrelated principals' entries.
The managed identities and vault must share the subscription's Entra tenant;
confirm existing policy capacity (maximum 1,024 entries) and effective permissions.

For a solution-managed vault, the template owns its complete policy list. Both
identities are available first so vault creation/redeployment includes both worker
grants rather than clearing the list and restoring them later. Managed vaults do
not also receive a redundant child `accessPolicies/add` deployment. Extra policies
added outside this template are not preserved on a managed-vault redeployment.
Use [existing-vault reference mode](existing-vaults.md) for shared vaults or to
retain externally managed application/operator policies. Assign any temporary
test permissions after the final infrastructure update.

**Upgrade warning:** earlier versions created RBAC-managed vaults. Before
redeploying one of those environments, explicitly set both model parameters to
`'rbac'` (or the appropriate model for each side) in its actual parameter file.
The new default is not a migration instruction. Preservation by detection applies
to vaults selected by resource ID, not to vaults still managed by the template.
Changing a managed vault's parameter changes its authorization model and can
interrupt clients. Plan any migration separately, fence writes, and review all
grants; incremental deployment does not automatically delete old policy/role
resources when their conditions become false.

Microsoft supports both models but recommends RBAC: principals with vault-write
control, such as Contributor/Key Vault Contributor, can grant themselves data
access on policy-based vaults. Review that privilege boundary with the customer.
In managed-permissions mode the custom replica role is defined only when at least
one selected vault uses RBAC; vault assignments are made only on RBAC vaults.
External-permissions mode leaves every grant to administrators.

## 3. Validate, preview, and deploy infrastructure

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>'
```

For a local parameter file, use:

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
  -ParametersFile .\infra\parameters\deployment.local.bicepparam
```

The runner compiles this file and performs read-only discovery of reused-vault
authorization models, tenant IDs, vault URIs, and explicitly selected worker
identity metadata. When either vault uses RBAC and `managePermissions = true`,
it also inventories existing solution-worker identities and their vault role
assignments. Previously deployed assignment names are retained to avoid
`RoleAssignmentExists`; new assignments use deterministic vault/identity ARM
IDs and role IDs, not runtime principal IDs.

Discovery populates internal `resolvedExistingVaults`, `resolvedWorkerIdentities`,
and `resolvedVaultRoleAssignments` parameters in a temporary JSON file. **Do not add
these parameters to your `.bicepparam` file.** The runner refreshes them on each
invocation, leaves the user file untouched, and removes temporary files on exit.
ARM validation, what-if, and optional create all use that same prepared snapshot.
Keep the vault authorization/configuration stable during the run. Direct existing-
vault deployment without discovery is not the supported workflow.

The preview is full-payload JSON, including property changes, rather than the
CLI's colored summary. Review every create/modify/delete and confirm no unrelated
resource is changed. Any diagnostics, ignored/unsupported/unevaluated resources,
additional potential/paginated results, malformed output, or failed command
blocks deployment, **even with `-Apply`**. There is no incomplete-preview override.
Unknown runtime property differences may still appear; complete module expansion
does not mean that every property's final value is predictable.

This performs validation and preview only. Then, if approved:

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
  -ParametersFile .\infra\parameters\deployment.local.bicepparam -Apply
```

`-Apply` reruns discovery, validation, and the coverage gate before creation;
review a fresh preview whenever parameters or existing configuration change.
Managed-vault policies do not require a separate identity-bootstrap deployment
or user template changes. First-time integration with an existing policy-based
vault can still be blocked if Azure cannot predict its additive policy update
while newly created worker principal IDs are unresolved. Reusing precreated
workers supplies known principal IDs; external-permissions mode omits that policy
update altogether. Neither option weakens the coverage gate or guarantees
noise-free Azure analysis.

Do not reuse names to "adopt" preexisting vaults. Supply their full resource IDs
through the explicit existing-vault parameters instead. Existing resources are
referenced without redeploying their properties.

If this deployment partially fails, preserve the subscription, workload name,
parameter file, deployment name/location, and authority settings. Review a new
what-if and rerun against the same resources to repair the failed operations.
Do not delete resource groups or purge vaults as a retry step. ARM validation
and Bicep compilation do not catch every provider-side requirement.

Inspect outputs from subscription deployment `mrkv-infrastructure`:

```powershell
az deployment sub show --subscription '<subscription-id>' `
  --name mrkv-infrastructure --query properties.outputs --output json
```

Outputs contain non-secret resource names and vault URIs. Keep the deployment
location stable for the same deployment name.

## 4. Verify foundation and publish code

Check every endpoint is Approved. From a connected deployment machine, verify
both vaults and the app/SCM resolve to reachable private addresses in the local
VNet. Check Blob, Queue, Table, and Service Bus resolution too.

Use the returned app and resource group names:

```powershell
.\scripts\Publish-Functions.ps1 -SubscriptionId '<subscription-id>' `
  -ResourceGroup '<primary-rg>' -AppName '<primary-app>'
.\scripts\Publish-Functions.ps1 -SubscriptionId '<subscription-id>' `
  -ResourceGroup '<secondary-rg>' -AppName '<secondary-app>'
```

Core Tools performs a Python remote build with the host-compatible Linux
dependencies. This does not use classic EP1 run-from-package app settings.
The publisher must reach the private deployment endpoint; its build path also
needs required package feeds and extension-bundle endpoints. Resolve network
restrictions rather than enabling public access or Basic publishing credentials.

Verify discovered functions are exactly Replicate and Reconcile in both apps.
Check host startup/identity errors in Application Insights through its private
network. Fix permission propagation or network issues before activation.

## 5. Activate and bootstrap

Once code and telemetry work, set:

```bicep
param enableWorker = true
param enableEventRouting = true
param authoritativeRegion = 'primary'
```

Review another what-if, then explicitly apply. Only the primary worker and route
become active. The next timer run performs initial synchronization within the
five-minute schedule; do not consider the secondary ready until a successful
`reconciliation_complete` record and secret inventory checks.

Run initial synchronization while restricting primary writes, or repeat until
both source changes and the replica have converged. Before switching clients,
validate actual secret values and applicable properties without writing values
to terminal logs or report artifacts.

## 6. Acceptance checks

From a privately connected machine with explicitly assigned test permissions:

```powershell
.\scripts\Test-Replication.ps1 `
  -SourceVaultUri '<primary-vault-uri>' `
  -DestinationVaultUri '<secondary-vault-uri>' -CreateSyntheticSecret
```

This enforces the 60-second value/metadata/lineage threshold and retains a uniquely
named synthetic secret. It does not print its value or automatically delete it.
The test identity needs write access on the source and read access on the replica;
that identity is distinct from worker and deployment identities.

Also verify:

| Check | Expected result |
| --- | --- |
| Public source/replica secret read | Denied from an unauthorized external network |
| Public runtime Blob read | Denied even with valid identity |
| Private app publication | Successful without reopening public network access |
| Duplicate notification | No additional destination version |
| Older event after rotation | Current value remains authoritative |
| Metadata-only change | Converges by reconciliation, not necessarily within 60 seconds |
| Disable source | Existing replica becomes disabled |
| Destination-only secret | Warning, not deletion |
| Worker stopped or destination unavailable | Failures/backlog visible; recover/reconcile after restoration |
| Event Grid delivery denied | Event Grid failure alert/dead-letter path, not just Service Bus DLQ |
| Private telemetry/query | Functions logs ingest; external query access denied |
| Notifications | Action group test reaches approved operator |

Perform the controlled recovery exercise in [operations](operations.md).
Only live evidence can establish quota/policy compatibility, Portal parity,
the latency target, or the 30-minute manual recovery target.

## 7. Updating and rollback

Publish the same tested source revision to both workers. Store build artifacts
and dependency revisions in your approved release system. Roll back code to a
known-good package, not vault contents or infrastructure blindly.

Always preserve the current `authoritativeRegion`. Applying the example's
default primary settings during a secondary-authority incident is unsafe.
No workflow, remote runner, GitHub repository, commit, or push is created implicitly.
See [validation and scanner dispositions](validation.md) before promoting this
demo beyond its tested local scope.
