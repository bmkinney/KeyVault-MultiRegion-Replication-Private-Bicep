# Precreated identities and Contributor-only deployment

**Illustration only; not intended for production use.** This guide is subject to
the [sample code disclaimer](../DISCLAIMER.md).

Use this workflow when deployment operators have Contributor but cannot administer
role assignments or custom roles. **Reusing a managed identity does not let the
operator deploy with that identity's permissions.** The operator authenticates to
ARM as themselves; each Function uses its selected identity at runtime.

## Choose permission ownership

If you want a supplied identity to **create workers and assign grants**,
use [delegated provisioning](deployment-delegated.md) instead. This page describes
the separate workflow where administrators have already prepared runtime grants.

| Parameters | Behavior |
| --- | --- |
| `managePermissions = true` (default), worker IDs empty | Create workers and provision the existing template-managed grants |
| `managePermissions = true`, either/both worker IDs supplied | Reference those workers; create only missing workers; still provision grants and require IAM-write permissions |
| `managePermissions = false`, both worker IDs supplied | Reference precreated workers, vaults, and system topics; administrators own all grants; no IAM writes |
| `managePermissions = true`, provisioning identity supplied | [Keyless delegated jobs](deployment-delegated.md) create missing workers and actively apply grants; operator remains Contributor |

External-permissions mode also requires both vault and both system-topic IDs.
There is no automatic identity creation, topic system-identity enablement,
custom-role definition, role assignment, or vault policy update in this mode.
Existing vault/topic parent resources and diagnostics are not redeployed.
Other solution infrastructure, private endpoints, Function identity attachments,
alerts, and replication event subscriptions are still deployed/updated.

Operators still need ordinary subscription/RG deployment permissions and writes
at the actual resource scopes, including the reused topic's event subscriptions.
They need managed-identity read and `Microsoft.ManagedIdentity/userAssignedIdentities/assign/action`
at each selected identity scope. Standard Contributor covering those scopes
normally already supplies these actions; an additional Managed Identity Operator
assignment is not inherently required. Custom roles, deny assignments, policy,
resource locks, provider registration, and Private Link approvals can still block
deployment. This mode does not bypass any of them.

## Administrator preparation

Prepare two distinct user-assigned identities, two compatible private vaults,
and each vault's existing Event Grid system topic with its **system-assigned**
identity enabled. All must share the deployment tenant. Worker identities may be
in other subscriptions; vaults and topics must be in the deployment subscription.
Resource groups may differ. Grant the operator identity read/assign at the actual
identity scopes, including other subscriptions; deployment-subscription Contributor
alone does not cover them. Vault and topic regions must match their configured regional locations.
Use [existing-vault requirements](existing-vaults.md) for the compatibility checks.
Record ARM IDs, not principal/client IDs, for the operator's parameter file.

The Event Grid identities are separate from the two worker identities. They
deliver messages and dead letters; attaching a worker identity to a Function
does not grant Event Grid permission to deliver.

Provision these grants for **each region** before activation:

| Principal | Minimum intended scope | Grant |
| --- | --- | --- |
| Local worker | Local runtime storage account | Storage Blob Data Owner; Storage Table Data Contributor |
| Local worker | Local `kv-events` queue | Azure Service Bus Data Receiver |
| Local worker | Local Application Insights component | Monitoring Metrics Publisher |
| Local topic's system identity | Local Service Bus namespace | Azure Service Bus Data Sender |
| Local topic's system identity | Dedicated local dead-letter storage account | Storage Blob Data Contributor |
| Local worker | Local source vault | Secrets Get/List policy, or Key Vault Secrets User |
| Opposite worker | Local destination vault | Secrets Get/List/Set policy, or the restricted replica-writer role below |

Assign both vault directions in advance so promotion does not require emergency
IAM changes. Each vault independently keeps its current authorization model.
For an RBAC vault, administrators provide a custom replica-writer role containing
only these data actions and assign it to the opposite worker at that vault:

```text
Microsoft.KeyVault/vaults/secrets/readMetadata/action
Microsoft.KeyVault/vaults/secrets/getSecret/action
Microsoft.KeyVault/vaults/secrets/setSecret/action
Microsoft.KeyVault/vaults/secrets/update/action
```

No keys, certificates, delete, purge, backup, or restore grants are required.
Use principal/object IDs for administrator grants, not client IDs. In external
mode the deployment does not require a specific custom-role name/GUID because
it neither creates nor assigns that role.

### Prepare scopes that do not exist yet

An administrator cannot grant at a storage/queue/Insights scope that has not been
created. Choose an explicit preparation approach:

- **Resource-scoped grants (preferred):** an authorized administrator first
  provisions the solution resources and grants using the same workload,
  subscription, regions, and identity IDs. This can be a reviewed initial
  managed-permissions deployment, with both activation flags false. The operator
  then supplies explicit vault/topic/worker IDs and deploys with
  `managePermissions = false`. Keep the workload/subscription unchanged so
  deterministic infrastructure names remain the same.
- **Inherited grants (broader scope):** administrators first create the two exact
  solution resource groups from the compiled template/preview and grant the
  table's non-vault roles to the corresponding principals at each regional RG.
  Subsequent storage/queue/Insights resources inherit them. This expands access
  to other matching resources in those RGs: for example, a topic's Blob Data
  Contributor can then reach runtime blobs too. Approve that tradeoff explicitly;
  do not grant subscription-wide roles as an undocumented shortcut. Vault grants
  remain at the selected existing vaults. Identities/topics must exist first.

A reviewed administrator deployment still uses the complete-preview gate; it
does not authorize bypassing an Unsupported result. Administrator-precreated
solution infrastructure remains template-owned and will be PUT/updated by later
operator deployments; only explicitly referenced identities/vaults/topics are
read-only references.

Under incremental ARM deployment, turning `managePermissions` off does **not**
remove prior grants. It transfers management responsibility to administrators.
Do not remove existing permissions until replacement access is verified.

## Operator configuration

Copy `infra\parameters\contributor.example.bicepparam` to the Git-ignored
`infra\parameters\deployment.local.bicepparam`. Replace the six placeholder ARM
IDs, choose matching locations, and configure the secret selection and alerts.
The essential switch is:

```bicep
param managePermissions = false
```

Keep `enableWorker = false` and `enableEventRouting = false` initially. Do not
edit templates or supply `resolvedWorkerIdentities`/other internal snapshots.

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
  -ParametersFile .\infra\parameters\deployment.local.bicepparam
```

Preflight fails before Azure queries when external-mode prerequisite IDs are
missing. It then reads vault/topic/worker metadata, validates same-tenant
identities, and prepares a fresh temporary snapshot. External mode does not
inventory role assignments or list subscription worker identities and never uses
Graph. It does **not** prove effective inherited/group/conditional data-plane
access, grant propagation, endpoint approval, or network reachability.

Review full what-if output. In external mode there must be no active role
definitions/assignments, worker identity creations, vault policy updates, or
vault/topic parent writes. Incomplete coverage still blocks apply. Once separately
authorized, rerun with `-Apply`; then publish, verify private/runtime access with
the intended identities, and follow the normal explicit activation procedure.
Missing administrator grants require administrator remediation, not extra roles
for the operator or a template workaround.

## Microsoft references

- [Contributor actions and IAM exclusions](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/privileged#contributor)
- [Managed Identity Operator read/assign actions](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/identity#managed-identity-operator)
- [RBAC scope and inheritance](https://learn.microsoft.com/en-us/azure/role-based-access-control/scope-overview)
- [Functions identity connections and runtime roles](https://learn.microsoft.com/en-us/azure/azure-functions/manage-connections?pivots=functions-auth-identity&tabs=host)
- [Event Grid delivery identities](https://learn.microsoft.com/en-us/azure/event-grid/managed-service-identity)
