# Azure Portal deployment guide (without Bicep)

**Illustration only; not intended for production use.** This guide and its sample
code are subject to the [sample code disclaimer](../DISCLAIMER.md).

This guide creates the foundation resources manually. It includes a limited,
Portal-uploadable ARM fallback for creating keyless Functions when the wizard
cannot express the required settings. Portal screens change; the referenced Microsoft procedures describe
the supported controls. Screenshots and exact tenant-wizard behavior must be
verified during the first live walkthrough; they are not fabricated here.

**No Bicep does not mean no code publication tool.** Python code must still be
published from a supported tool on a privately connected machine. If a Portal
wizard cannot create a policy-compliant identity/storage combination, use the
explicit Portal ARM fallback below rather than permitting public storage or keys.

## 1. Prepare a naming worksheet

Choose a lowercase token and fill in these names for both regions. Globally scoped
service names must be unique. Use short names to fit vault (3-24 characters),
storage (3-24 lowercase letters/numbers), app (2-60), and namespace (6-50) rules.

| Item | Primary | Secondary |
| --- | --- | --- |
| Region | East US | Central US |
| Resource group | `rg-<prefix>-p` | `rg-<prefix>-s` |
| VNet | `vnet-<prefix>-p` | `vnet-<prefix>-s` |
| Vault | `kv-<prefix>-p` | `kv-<prefix>-s` |
| Worker identity | `id-<prefix>-p-worker` | `id-<prefix>-s-worker` |
| Runtime storage | `st<prefix>prun` | `st<prefix>srun` |
| Dead-letter storage | `st<prefix>pdl` | `st<prefix>sdl` |
| Service Bus | `sb-<prefix>-p` | `sb-<prefix>-s` |
| Function App | `func-<prefix>-p` | `func-<prefix>-s` |
| System topic | `evgt-<prefix>-p` | `evgt-<prefix>-s` |
| Workspace/Insights/AMPLS | `log/appi/ampls-<prefix>-p` | `log/appi/ampls-<prefix>-s` |

Complete the subscription/permission/quota/connectivity preflight in
[the Bicep guide](deployment-bicep.md#1-prerequisites-and-preflight); it applies
equally to manual deployment.
For Contributor-only operators, an administrator performs identity creation and
all IAM/policy-grant steps below. Use the
[administrator/operator permission matrix](deployment-contributor.md) and reuse
the prepared identities/vaults/topics; Contributor does not administer RBAC.
The alternative [delegated provisioning workflow](deployment-delegated.md)
launches keyless ACI jobs using a precreated privileged identity. It uses the
documented Bicep/PowerShell orchestrator for staged previews and completion
checks; uploading the Function-only Portal fallback does not perform that handoff.
For this manual Portal path, administrator grant steps remain explicit.

## 2. Create resource groups, identities, and networks

In Resource groups, create one group in each location. In Managed Identities,
create a user-assigned identity in each regional group, or select two distinct
precreated identities in the same subscription/tenant. Their names/groups need
not match the worksheet. A Contributor-only operator reuses the administrator's
identities rather than creating them. Record each resource ID,
principal ID, and client ID; do not confuse these three identifiers.
Use the actual existing identity ARM ID when attaching it to the Function or
supplying the Portal ARM fallback. Identity attachment still requires read and
assign/action at its scope, normally included in Contributor at that scope.

Create VNets and subnets:

| Setting | Primary | Secondary |
| --- | --- | --- |
| Address space | `10.35.0.0/16` | `10.36.0.0/16` |
| `integration` | `10.35.1.0/24` | `10.36.1.0/24` |
| `endpoints` | `10.35.0.0/24` | `10.36.0.0/24` |

Delegate integration to **Microsoft.App/environments**, not
Microsoft.Web/serverFarms. Leave endpoints undelegated. Do not place endpoints
in the integration subnet. Match any endpoint network-policy/NSG behavior to
your approved network design.

Create separate instances of these Private DNS zones in each regional group.
Link each instance only to its local VNet with auto-registration disabled:

- `privatelink.vaultcore.azure.net`
- `privatelink.servicebus.windows.net`
- `privatelink.blob.core.windows.net`
- `privatelink.queue.core.windows.net`
- `privatelink.table.core.windows.net`
- `privatelink.azurewebsites.net`
- `privatelink.monitor.azure.com`
- `privatelink.oms.opinsights.azure.com`
- `privatelink.ods.opinsights.azure.com`
- `privatelink.agentsvc.azure-automation.net`

These separate views must not share unreachable endpoint records. Custom DNS
needs forwarding to the appropriate regional view. Do not simply connect both
sets of AMPLS zones to the same resolver.

## 3. Create vaults

To reuse one or both vaults, follow [existing-vault integration](existing-vaults.md)
instead of creating those resources. Preserve their configuration and diagnostic
settings, and use their actual IDs/URIs for endpoints, IAM, events, and workers.

Create one Standard vault per region. On access configuration explicitly choose
**Vault access policy** for this sample's default, or **Azure role-based access
control** for either vault if required. Do not rely on the wizard/API default,
and do not switch an existing vault's model as part of integration. Mixed pairs
are supported. Microsoft recommends RBAC because vault-write principals can
grant themselves data access under the access-policy model.
Enable purge protection and 90-day soft-delete
retention. Do not enable VM deployment, disk encryption, or ARM template
secret access. Disable public network access and trusted-service bypass.

Create four vault endpoints using Private Link Center:

| Endpoint VNet | Target |
| --- | --- |
| Primary | Primary vault, subresource `vault` |
| Primary | Secondary vault, subresource `vault` |
| Secondary | Secondary vault, subresource `vault` |
| Secondary | Primary vault, subresource `vault` |

Choose the endpoint region to match the VNet, not the target vault.
For DNS integration, explicitly choose the vault zone in that endpoint's regional
group. Confirm one private address for each vault in each local DNS view.

## 4. Create runtime and dead-letter storage

Create two StorageV2 Standard ZRS accounts per region. Require HTTPS and TLS 1.2;
disable shared-key access and anonymous Blob access. Disable public network access.
Runtime storage has no trusted-service bypass. Dead-letter storage permits
**Allow Azure services on the trusted services list** for Event Grid; RBAC is
still required.

Create containers through the management-plane Containers experience:

| Account | Containers |
| --- | --- |
| Runtime | `deployment`, `coordination` |
| Dead-letter | `eventgrid-deadletter` |

Set private container access and seven-day blob/container soft delete. Do not
enable account-wide lifecycle expiry on host/coordination containers.

Create local runtime private endpoints for Blob, Queue, and Table; create a
local dead-letter Blob endpoint. Use the matching regional DNS zone. Data-plane
Blob browsing requires private connectivity even when management-plane container
creation succeeds.

In each runtime account IAM, assign its worker identity **Storage Blob Data Owner**
and **Storage Table Data Contributor**. No file share or storage key is needed.

## 5. Create Service Bus

Create a Premium namespace per region with one messaging unit, one messaging
partition, zone redundancy, minimum TLS 1.2, and public network access Disabled.
Disable local/SAS authentication. Enable trusted Microsoft services to bypass
the firewall; this exception is necessary for Event Grid delivery.

Create `kv-events`: no sessions, no entity partitioning, one-minute lock,
14-day TTL, maximum delivery count 10, dead-letter expired messages enabled.
Do not rely on queue duplicate detection for application idempotency.

Create a local namespace private endpoint (`namespace`) and integrate with the
local Service Bus DNS zone.
Assign the local worker **Azure Service Bus Data Receiver** on the queue.

## 6. Create private monitoring

Create a regional Log Analytics PerGB2018 workspace (30-day retention) and
workspace-based Application Insights resource. Disable local authentication
and public ingestion/query access on both resources where applicable.

Create an Azure Monitor Private Link Scope per isolated regional DNS view,
with Ingestion and Query access modes **Private Only**. Add both the workspace
and Application Insights to it. Create a local `azuremonitor` endpoint and
select the four monitoring zones plus the local Blob zone.

Assign the worker **Monitoring Metrics Publisher** on Application Insights.
Inspect existing monitoring dependencies before changing shared DNS endpoints.
In connected enterprise networks reuse an approved AMPLS design instead of
introducing conflicting scopes.

## 7. Create Functions without key/public-access workarounds

Create a Linux Flex Consumption Function App in each region using Python 3.12,
2,048 MB instances, maximum 40 on-demand instances, and its local runtime storage.
Enable VNet integration with the dedicated integration subnet and disable
public access. Add a local Function App private endpoint (`sites`), with DNS
integration for the app and SCM names.

Assign the regional user-assigned identity under Identity. Under Deployment
settings select local runtime storage, `deployment`, and **User assigned identity**.
Configure one always-ready instance each for `function:Replicate` and
`function:Reconcile`. Disable FTP and SCM Basic authentication; require TLS 1.2.

**Wizard limitation:** Microsoft documents that custom deployment storage cannot
currently be selected during Portal app creation. CLI `functionapp create` can
select deployment identity but may still initialize host-storage connection
strings; it is not a reliable workaround for keyless-on-create policy.

For such tenants, select **Deploy a custom template** in the Portal, then **Build
your own template in the editor**, **Load file**, and upload
`docs\portal\function-app.json`. This is a precompiled ARM template for only the
Functions portion; the user does not need Bicep or the Bicep compiler. It creates
the plan, identity-configured app, app private endpoint, diagnostics, and worker
alerts against the foundation you created manually. It is generated from this
repository's original Function App module, not exported from a deployed resource.

Use the existing regional resource group. Set `prefix`, `location`, source/destination
vault settings (including `destinationVaultId`), `enabled = false`, and `tags`.
Set `secretNames`/`secretPrefix`, or explicitly set `replicateAllSecrets = true`
for the complete new-vault demo. Keep `allowUnmanagedOverwrite = false`.
In **Edit parameters**, populate
the `foundation` object with the following existing values:

```json
{
  "identityId": "<regional-worker-identity-ARM-ID>",
  "storageName": "<runtime-storage-name>",
  "storageBlobUri": "https://<runtime-storage-name>.blob.core.windows.net/",
  "busNamespace": "<regional-namespace>.servicebus.windows.net",
  "integrationSubnetId": "<regional-vnet-ARM-ID>/subnets/integration",
  "endpointSubnetId": "<regional-vnet-ARM-ID>/subnets/endpoints",
  "appDnsZoneId": "<regional-private-DNS-zone-ARM-ID-for-privatelink.azurewebsites.net>",
  "workspaceId": "<regional-workspace-ARM-ID>",
  "insightsId": "<regional-Application-Insights-ARM-ID>",
  "actionGroupId": "<approved-action-group-ARM-ID>"
}
```

The fallback resolves the identity client ID and Application Insights connection
string internally from these ARM IDs; do not supply those runtime values in
the `foundation` object. The manually configured app-settings table below still
uses the actual client ID/connection string when not using this fallback.

Create the action group from section 10 before using this fallback. Assign the
vault permissions from section 8 before starting the app. Review the resource changes
and submit only with deployment approval. Repeat for the other region with its
local foundation and reversed source/destination. Do not also create the same
app manually. This fallback starts with public app access disabled and all
replication triggers fenced.

In Configuration/Environment variables, configure:

| Setting | Primary | Secondary |
| --- | --- | --- |
| `AzureWebJobsStorage__accountName` | Local runtime account | Local runtime account |
| `AzureWebJobsStorage__credential` | `managedidentity` | `managedidentity` |
| `AzureWebJobsStorage__clientId` | Local identity client ID | Local identity client ID |
| `ServiceBusConnection__fullyQualifiedNamespace` | Local namespace FQDN | Local namespace FQDN |
| `ServiceBusConnection__credential` | `managedidentity` | `managedidentity` |
| `ServiceBusConnection__clientId` | Local identity client ID | Local identity client ID |
| `WORKER_CLIENT_ID` | Local identity client ID | Local identity client ID |
| `SOURCE_VAULT_ID` | Primary vault ARM ID | Secondary vault ARM ID |
| `SOURCE_VAULT_URI` | Primary URI | Secondary URI |
| `DESTINATION_VAULT_URI` | Secondary URI | Primary URI |
| `DESTINATION_VAULT_ID` | Secondary vault ARM ID | Primary vault ARM ID |
| `SECRET_NAMES` | JSON allowlist, e.g. `["application-password"]` | Same selection |
| `SECRET_PREFIX` | Optional name prefix (empty selects nothing) | Same selection |
| `REPLICATE_ALL_SECRETS` | `false`; explicitly use `true` only to select all secrets | Same selection |
| `ALLOW_UNMANAGED_OVERWRITE` | `false` | `false` |
| `COORDINATION_BLOB_URI` | Local runtime Blob HTTPS URI | Local runtime Blob HTTPS URI |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Local Insights connection string | Local Insights connection string |
| `APPLICATIONINSIGHTS_AUTHENTICATION_STRING` | `ClientId=<local-client-id>;Authorization=AAD` | Same format |
| `REPLICATION_ENABLED` | `false` | `false` |
| `AzureWebJobs.Replicate.Disabled` | `true` | `true` |
| `AzureWebJobs.Reconcile.Disabled` | `true` | `true` |
| `RECONCILIATION_SCHEDULE` | `0 */5 * * * *` | Same |

Remove legacy host/deployment connection strings after identity configuration
works; they are not used by this implementation. Do not add
WEBSITE_RUN_FROM_PACKAGE, Azure Files settings, or EP1 runtime app settings.

## 8. Assign vault permissions and publish code

Follow the branch matching **each vault's current access configuration**.
Managed identities use the same worker code with either model. The reverse
worker's write grant is preassigned for recovery, not permission to enable both
workers simultaneously.

### Vault access policy (default)

On each vault's **Access policies** page, choose **Create** and select only these
**Secret permissions**. Select the user-assigned managed identity by name and
verify its principal/object ID against the naming worksheet, then save the policy.
Do not use its client ID as an object ID or select application permissions
for a different principal.

| Principal | Vault | Secret permissions |
| --- | --- | --- |
| Primary worker | Primary vault | Get, List |
| Primary worker | Secondary vault | Get, List, Set |
| Secondary worker | Secondary vault | Get, List |
| Secondary worker | Primary vault | Get, List, Set |

Set covers both creating secret versions and updating attributes (including
disable). There is no separate Update policy permission. Leave key/certificate,
delete, purge, backup, and restore permissions unselected. Add/update only these
worker entries; preserve unrelated existing policies. Both identities must be
in the vault's tenant; policy capacity is 1,024 entries. The operator needs
`Microsoft.KeyVault/vaults/accessPolicies/write`; a Key Vault RBAC data role is
not a substitute on a policy-based vault.

### Azure RBAC (alternative)

If either vault uses RBAC, in subscription IAM create a custom role named
`<prefix> Secret Replica Writer`.
Under its permissions JSON use empty Actions/NotActions/NotDataActions and these
DataActions only:

```json
[
  "Microsoft.KeyVault/vaults/secrets/readMetadata/action",
  "Microsoft.KeyVault/vaults/secrets/getSecret/action",
  "Microsoft.KeyVault/vaults/secrets/setSecret/action",
  "Microsoft.KeyVault/vaults/secrets/update/action"
]
```

Set AssignableScopes to this subscription and assign the role only at each
destination vault. This requires custom-role-definition write permission and
does not grant subscription-wide secret access merely by defining the role.

| Principal | Scope | Role |
| --- | --- | --- |
| Primary worker | Primary vault | Key Vault Secrets User |
| Primary worker | Secondary vault | Custom Secret Replica Writer |
| Secondary worker | Secondary vault | Key Vault Secrets User |
| Secondary worker | Primary vault | Custom Secret Replica Writer |

Apply only the rows whose target vault uses RBAC. For example, a policy-based
primary and RBAC secondary need policies on primary and role assignments on
secondary. Do not change the vault model or grant both kinds of access to
work around a denial.

Wait for RBAC propagation where applicable and verify actual worker secret access
under each selected model. Storage, Service Bus, Event Grid delivery, and
monitoring retain their RBAC assignments regardless of vault configuration.
Publish original code using `scripts\Publish-Functions.ps1` or the supported
VS Code/Core Tools procedure from a privately connected machine. This step
does not require Bicep. Verify both functions are discovered.

## 9. Create Event Grid system topics and subscriptions

If a selected vault already has a system topic, reuse it rather than creating
another, and use a distinct replication subscription name. Preserve its existing
identity configuration/subscriptions; system-assigned delivery identity must
already be enabled. See [existing-topic handling](existing-vaults.md#existing-event-grid-topics).

Create a system topic for each vault, in that vault's region and subscription,
type **Microsoft.KeyVault.vaults**. Enable a system-assigned managed identity.
Assign it **Azure Service Bus Data Sender** on its local namespace and
**Storage Blob Data Contributor** on its dedicated dead-letter account.
Do not assign it a Key Vault role.

Create `secret-events` under each system topic:

| Control | Value |
| --- | --- |
| Schema | Event Grid schema (not CloudEvents) |
| Labels | `mrkv-replication` |
| Event types | SecretNewVersionCreated, SecretNearExpiry, SecretExpired |
| Destination | Local Service Bus queue `kv-events` |
| Delivery identity | System assigned |
| Retry | Maximum attempts 30; TTL 1440 minutes |
| Dead-letter | Local dead-letter account, `eventgrid-deadletter`, system-assigned identity |
| Initial subject prefix | `__mrkv_inactive__/` |

The prefix intentionally cannot match a valid secret name and mutes the route.
Do not enable both directions simultaneously.

## 10. Configure diagnostics and alerts

Send vault AuditEvent/allLogs, Service Bus allLogs, Event Grid system-topic allLogs, runtime/dead-letter Blob
logs, and Functions FunctionAppLogs to the local workspace. Enable their supported
metrics (AllMetrics for vault/bus/app, Transaction for Blob).

Create an action group with the approved recipient and test delivery.
Create alerts equivalent to Bicep:

- Service Bus `DeadletteredMessages` Maximum > 0, five-minute window, one-minute evaluation.
- Event Grid `DeliveryAttemptFailCount` Total > 0, same window/evaluation.
- Scheduled log alert on AppTraces: MRKV failure, source_missing, destination_only,
  expired, near_expiry, or lagSeconds > 60.
- Active-worker reconciliation heartbeat absent for 15 minutes.

See [operations](operations.md) for the structured-log query and thresholds.

## 11. Activate, bootstrap, and verify parity

Set only the primary app REPLICATION_ENABLED true and both `.Disabled` settings
false. Remove only the primary subscription's inactive subject prefix. Leave
the secondary fenced and its subscription muted.

Wait for a successful reconciliation, then perform the shared acceptance
checklist in the Bicep guide and the controlled recovery exercise. Confirm all
18 endpoints, IAM scopes, app settings, private queries, and alerts match.

No screenshot or "Deployment succeeded" notification alone demonstrates parity.
Record actual runtime evidence without secret values. Do not grant broad data
roles to all operators or reopen public access to make Portal browsing work.
