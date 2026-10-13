# Bicep deployment guide

**Illustration only; not intended for production use.** This guide and its sample
code are subject to the [sample code disclaimer](../DISCLAIMER.md).

This is a deployment procedure, not evidence that a live deployment has passed.
Do not run deployment or live tests until the subscription owner authorizes them.

## 1. Prerequisites and preflight

Use Azure CLI with Bicep, PowerShell 7, Python, and GA Azure Functions Core Tools v4.
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
- Permission to deploy subscription/resource-group resources and assign roles.
  Contributor alone does not grant role-assignment or custom-role-definition
  write permissions.
- Nonoverlapping CIDRs with the deployment network.
- Global name availability for vault, storage, namespace, and app names.
- Operator private access to both monitoring DNS views. Do not merge two AMPLS
  DNS views into a common resolver without redesign.

Resource names use a short workload prefix and deterministic subscription suffix.
Use only lowercase letters/numbers for `workload`; decorators enforce length,
and ARM validation enforces service-specific character rules.

Role assignments may take up to ten minutes to propagate. A successful ARM
deployment is not evidence that every data-plane permission is ready.

## 2. Configure initial parameters

For existing vaults, follow [existing-vault integration](existing-vaults.md) and
start with `infra\parameters\existing-vaults.example.bicepparam`. The deployment
script performs read-only compatibility and topic-reuse preflight; Python must
be available. Leave vault IDs empty to retain new-vault behavior.

Edit `infra\parameters\example.bicepparam` or create your own local parameter file.
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

## 3. Validate, preview, and deploy infrastructure

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>'
```

This performs ARM validation and what-if only. Review every create/modify/delete
and confirm no unrelated resource is changed. Then, if approved:

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' -Apply
```

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
