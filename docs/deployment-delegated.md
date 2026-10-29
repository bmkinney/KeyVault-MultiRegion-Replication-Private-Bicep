# Contributor-triggered delegated provisioning

**Illustration only; not intended for production use.** This guide is subject to
the [sample code disclaimer](../DISCLAIMER.md). No live Azure/ACI deployment has
been qualified by the local tests.

This mode matches a different requirement from
[external-permissions mode](deployment-contributor.md): an administrator prepares
one **provisioning user-assigned managed identity**, and a Contributor operator
supplies its ARM ID. The provisioning identity creates the regional worker
identities and applies their runtime grants. The operator is not assigned an IAM
administrator role and does not impersonate the identity from their workstation.

## Parameter-only configuration

Copy `infra\parameters\delegated.example.bicepparam` to the Git-ignored
`infra\parameters\deployment.local.bicepparam`. Configure:

| Parameter | Meaning |
| --- | --- |
| `provisioningIdentityResourceId` | Precreated, administrator-authorized UAMI in any subscription in the deployment tenant |
| `provisioningResourceGroupName` | Optional override. Omit it: the temporary provisioning container runs in the primary solution group the tool creates. If set, it must be an existing RG **in the deployment subscription** (never the identity's subscription) |
| `provisioningLocation` | Optional ACI execution region; defaults to `primaryLocation`. Management-plane connectivity required |
| `provisioningTimeoutSeconds` | Per-deployment/job deadline, 120-7,200 seconds; default 1,800 |
| `managePermissions` | Keep `true`: grants are actively provisioned, but by the provisioning identity |
| Worker/vault/topic IDs | Leave empty to create under the provisioning identity, or supply existing IDs to reference |
| `replicaWriterRoleResourceId` | Administrator-predefined restricted custom role; required if either selected vault uses RBAC |

New vaults still default to access policies. Reused vaults preserve their detected
model; mixed pairs remain supported. Explicitly select secrets when reusing a
vault, and leave both activation flags false until code and runtime access have
been verified. No principal/client IDs or internal discovery fields belong in
the user parameter file.

Provisioning and existing worker identities may each be in a different subscription,
provided they share the deployment tenant. Supply their full ARM IDs; preflight
reads each identity in its own subscription and rejects cross-tenant identities.
Vaults, system topics, the execution RG, and solution deployments remain in the
deployment subscription. `-SubscriptionId` still selects that target subscription,
not the identity subscription.

Use the supported orchestrator, not a raw deployment of `main.bicep`:

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
  -ParametersFile .\infra\parameters\deployment.local.bicepparam
```

Delegated mode needs Azure CLI **2.76.0+** to preview privileged templates with
`ProviderNoRbac`. This previews resource configuration with the operator's read
permissions; it does not grant write permissions or prove the provisioning
identity's access. Inside each job, validation and what-if run again using the
provisioning identity with full permission checking before apply.

## Execution and preview stages

| Stage | Executing principal | Behavior |
| --- | --- | --- |
| Layout | Contributor | Preview/apply the same deterministic solution resource groups |
| Worker bootstrap | Provisioning UAMI | Create missing regional worker UAMIs |
| Parent bootstrap | Provisioning UAMI | Create solution-managed vaults with complete worker policies, and missing system topics with their separate system identities |
| Ordinary infrastructure | Contributor | Deploy private networking, storage, messaging, Functions, diagnostics, alerts, and event subscriptions; no IAM writes; both workers/routes fenced |
| Runtime authorization | Provisioning UAMI | Apply exact service role assignments and per-vault RBAC or additive existing-vault policies |
| Event routing | Contributor | Create the fenced event subscriptions only after the grants exist, because Event Grid validates delivery permission when a subscription is created |
| Requested activation | Contributor | Only after all authorization stages complete; honors the user's explicit activation flags |

Each privileged stage uses `infra\modules\provisioning-job.bicep`. The container
receives a content-digested, compressed copy of locally compiled ARM templates
and parameters. It does not download repository code or scripts from a mutable
URL. The fixed Python entrypoint uses the supplied identity to validate, preview,
and incrementally deploy that stage's template.

**Preview-only creates nothing**, including no container or worker identity.
If groups, workers, or other prerequisite resources do not exist, later
principal/scope-dependent previews are explicitly marked **DEFERRED**, not
reported as complete. Bootstrap has its own fully evaluable preview. An
authorized run then advances one stage at a time, obtaining real metadata and a
fresh complete preview before each stage's mutations:

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
  -ParametersFile .\infra\parameters\deployment.local.bicepparam -Apply
```

`-Apply` authorizes the visible staged workflow, including bootstrap before later
previews can finish. It does not bypass incomplete what-if: diagnostics,
Unsupported/Ignore/unknown changes, additional results, malformed output, and
deletes block the affected stage. Earlier completed stages remain if a later
stage fails; no automatic rollback or grant deletion is attempted. Review stage
results and rerun with the same subscription/workload/parameters to repair.

Successful ACI resource creation is not task success. The runner requires a
matching execution ID/digest, a terminated process with exit code zero, exactly
one matching completion record, and an independently confirmed successful ARM
deployment. Restarts and deadlines are errors; stop requests target only that
execution. Stopping a container does not cancel ARM operations already submitted.
Inspect those operations before retrying. ARM names are deterministic so retries
do not intentionally create new identities or grants.

For a failed run, retain the printed execution/deployment names and inspect
`az container logs` in the execution group (the primary solution group unless
overridden) and `az deployment operation group list` in the failed stage's target
RG. After a stage is confirmed successful the terminated container is deleted; a
failed stage's container is kept for log inspection. A stopped
execution is not evidence that its previously submitted ARM operations stopped.

Provision/reprovision deliberately fences workers/routes before updating grants.
A run requesting activation re-enables the selected authority only after every
authorization stage succeeds. This can interrupt an active sample while it is
being reprovisioned; it is not a zero-downtime upgrade workflow. ARM success does
not prove grant propagation, private connectivity, or application health.

## Administrator-scoped prerequisites

Precreate the provisioning identity; no execution RG is needed. For tight scoping,
precreate the two deterministic solution RGs shown in the layout preview and
grant the provisioning identity authority there. It cannot receive a grant at
a nonexistent scope. Broader inherited authority requires explicit administrator
approval; the deployment never grants itself privileges or elevates the operator.

| Principal | Required authority |
| --- | --- |
| Operator | Contributor/resource deployment and read at solution and referenced resource scopes (and the execution RG only if overridden); identity read/assign at the provisioning and runtime UAMI scopes; topic inventory and vault grant reads used by preflight |
| Provisioning UAMI | `Microsoft.Resources/deployments/*` at the solution RGs and selected existing vault RGs where nested access modules run, and where a new system topic must be created (Event Grid requires it in the vault's RG, with Event Grid write there); worker identity read/write in solution RGs and read at reused worker identity scopes, including other subscriptions; vault read/write for newly managed vaults and topic read/write for newly managed topics |
| Provisioning UAMI | Role assignment read/write at the solution RGs (inherited by their eventual service resources) and selected RBAC vaults; restrict assignable roles/principal types with administrator conditions where supported |
| Provisioning UAMI | Access-policy write at selected policy-based existing vaults; no key/secret-value access is needed by provisioning code |

Standard Contributor at an identity scope normally includes its read/assign
actions; a separate Managed Identity Operator assignment is not inherently
required. Actual deny assignments, custom roles, locks, policy, provider
registration, ACI quota, and location restrictions still apply. Register
Microsoft.ContainerInstance in addition to the normal solution providers.

## Identities in another subscription: checklist and readiness report

The identity ARM IDs carry their own subscription, so no extra parameter is needed.
Complete these in order before the first run:

1. **Same tenant.** Every identity's subscription must be in the deployment subscription's Microsoft Entra tenant.
2. **Operator access.** The signed-in operator account (`az login`) must be able to read the identity subscription, and needs Managed Identity Operator (read + `Microsoft.ManagedIdentity/userAssignedIdentities/*/assign/action`) on the provisioning identity and on each reused worker identity. Contributor in the deployment subscription does not cover these.
3. **Deployment subscription.** The operator needs Contributor-level rights. Register `Microsoft.ContainerInstance`, `Microsoft.ManagedIdentity`, and `Microsoft.App` (required for the Flex Consumption subnet delegation) there, along with the solution's usual providers. The readiness report checks all of them.
4. **Provisioning identity grants.** An administrator grants the provisioning identity the roles in the table above at the deployment subscription, or at both solution groups after pre-creating them. The identity's own subscription is irrelevant to this step.
5. **Run the preview** (no `-Apply`). Fix everything the readiness report lists, then rerun.

Every run starts with a read-only readiness report. It checks all of the above
together, prints the exact grants an administrator must create (with the
provisioning identity's object ID), and stops before any change if anything blocks.
Warnings mean a check could not be completed, usually missing read access; verify
those manually.

| Error | Meaning | Fix |
| --- | --- | --- |
| `Readiness check failed ... Cannot read subscription` | The operator account cannot see the identity's subscription | `az login` with the right account, or an administrator grants Reader there |
| `Operator lacks ... assign/action` or `LinkedAuthorizationFailed` | The operator cannot attach the identity to the container | Assign Managed Identity Operator on that identity |
| `Resource provider ... NotRegistered` or `MissingSubscriptionRegistration` | Provider not registered in the deployment subscription | `az provider register --namespace <name> --subscription <deployment subscription>` |
| `no role assignment in subscription` | The provisioning identity has no grants there | Administrator grants it the table roles at the subscription or the solution groups |
| `provisioningResourceGroupName ... does not exist` | The override points at a group in the deployment subscription that is missing | Remove the parameter, or create the group there. Do not use the identity's resource group name |
| `cross-tenant identities are not supported` | The identity's subscription is in another tenant | Use an identity from the deployment tenant |

Contributor in the deployment subscription does not grant identity read/assign
in another subscription. Administrators must grant the operator those actions
at each reused provisioning/runtime identity scope separately. Keeping an
identity in another subscription does not grant it access to deployment resources;
its required target-scope permissions in the table above must still be provisioned.

For RBAC vaults, administrators predefine a custom role with **only** the four
data actions in the [runtime grant matrix](deployment-contributor.md#administrator-preparation),
no management actions, and assignable scopes covering the selected vaults.
Delegated deployment validates that definition and never creates custom roles.
Role Based Access Control Administrator alone does not create role definitions.
Both vault models still need RBAC role-assignment authority for storage, bus, and
telemetry. The runtime grant matrix is unchanged.

**Delegation is a trust decision:** an operator who can attach this identity to
code they control can exercise its effective permissions. Application-side
template checks do not prevent such an operator modifying the execution code.
If operators must only trigger an approved operation, use an administrator-owned
runner outside their write/identity-assignment scopes instead. Never give this
identity subscription Owner as a convenience or attach it to replication Functions.

## Ownership, networking, and cost

New bootstrap identities/vaults/topics carry a deterministic provisioning-owner
tag. A rerun with empty IDs may update only resources with that ownership tag;
an unrelated existing name blocks deployment. To integrate a prior direct-mode
deployment or shared resource, supply explicit IDs rather than silently adopting
its names. Explicitly reused parents/policies/diagnostics retain the documented
existing-resource rules. Bootstrap-owned vaults retain complete inline worker
policies on bootstrap updates; ordinary infrastructure references those parents
without clearing policies. Owned vault/topic workspace diagnostics are wired
after the regional workspaces exist.

ACI has no exposed inbound address/ports or storage volumes in this sample.
It calls ARM/Entra and pulls a digest-pinned Microsoft Azure CLI image; it does
not access private vault data or run private acceptance tests. Management-plane
egress is required. This runner targets Azure public cloud's ARM endpoint;
sovereign-cloud execution is not implemented. A tenant requiring all execution inside an approved VNet
needs a separately designed execution-network path; do not weaken that policy.

No staging storage account, Azure Files share, account key, client secret, or
credential export is used. Each execution requests 1 vCPU and 1.5 GiB and has a
`Never` restart policy. Azure can still retry nonzero failures; tasks must remain
idempotent and the runner rejects observed restarts. Completed container groups
are retained for diagnostics; compute stops on successful termination. Review
and remove retained execution resources separately according to local policy.

## Official references

- [ACI managed identities](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-managed-identity)
- [Run-to-completion and nonzero restart behavior](https://learn.microsoft.com/en-us/azure/container-instances/container-instances-restart-policy)
- [What-if permission/validation levels](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-what-if)
- [Identity assignment trust boundaries](https://learn.microsoft.com/en-us/entra/identity/managed-identities-azure-resources/managed-identity-best-practice-recommendations)
- [Why deploymentScripts is not this keyless mechanism](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deployment-script-template#use-existing-storage-account)
