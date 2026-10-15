# Integrating existing Key Vaults

**Illustration only; not intended for production use.** See [DISCLAIMER.md](../DISCLAIMER.md).

Each side independently supports a new or existing vault. New/new, existing/new,
new/existing, and existing/existing use the same replication topology. Existing
vaults and any reused system topics must be in the deployment subscription;
their resource groups may differ from the new infrastructure groups.

## What is preserved and what is added

| Surface | Existing-vault behavior |
| --- | --- |
| Vault resource/configuration | Bicep `existing` reference; no vault PUT, recreation, network/security toggle, or tag changes |
| Existing system topic | Reused by explicit ARM ID; no identity/configuration/diagnostic changes |
| Authorization | Detect and preserve each vault's RBAC or access-policy model; add matching worker grants at that vault |
| Networking | Add endpoint connections from the two solution VNets; leave previous connections/DNS views untouched |
| Events | Add a dedicated replication subscription to the selected/new system topic |
| Vault diagnostics | Leave existing settings unchanged; use your existing audit destinations |
| Secret contents | Only selected current-state secrets; unmanaged destination collisions rejected unless approved |

`existing` prevents redeployment, **not deletion by an operator**. If a reused vault
is in a solution-owned resource group, deleting that group still deletes the vault.
Never include existing vault/topic groups in solution cleanup. In a shared group,
remove only explicitly identified solution assignments/subscriptions/endpoints.

## Compatibility gates

The read-only `scripts\check_vault_integration.py` preflight runs automatically
from `scripts\Deploy.ps1`, before ARM validation/what-if/deployment. It requires:

- Full same-subscription vault/system-topic ARM IDs and two distinct vaults.
- Each existing vault in its configured `primaryLocation`/`secondaryLocation`.
- Either RBAC or vault access policies, plus soft delete and purge protection.
  True `enableRbacAuthorization` means RBAC; false, legacy null, or an omitted
  property means access policies. Invalid nonboolean values are rejected.
- Public vault access already Disabled. The sample does not disable access that
  other clients still need; migrate those clients separately or do not integrate.
- Explicit name allowlist, prefix, or whole-vault opt-in when either vault exists.
- Reuse of an existing source system topic, with its system-assigned identity
  already enabled. The preflight never enables an identity automatically.

The account running preflight must read both vault resources and subscription
system topics; failure to inventory is a blocking error, not permission to
create a duplicate. It does not retrieve secret values. Endpoint approvals,
tenant policies, existing network/DNS effects, and data-plane permissions still
require the deployment operator's review.

Use the supplied deployment script rather than bypassing these checks with a raw
ARM command. A direct deployment can bypass Python validation; the worker still
fails closed on an empty secret selection, but that does not replace preflight.

## Authorization and permissions

Access policies are supported; **RBAC is not a Key Vault integration requirement**.
Each reused vault retains its current model, independently of the other vault.
For example, the source may use access policies while the replica uses RBAC.
The `primaryVaultAuthorizationModel`/`secondaryVaultAuthorizationModel` parameters
default to `'accessPolicy'` but apply only when that side's vault ID is empty.
There is no automatic authorization migration for a reused vault.

| Vault model | Local source-reader worker | Opposite-region replica-writer worker |
| --- | --- | --- |
| Access policy | Secrets Get, List | Secrets Get, List, Set |
| RBAC | Key Vault Secrets User | Custom Secret Replica Writer: readMetadata, getSecret, setSecret, update |

Both grants are preassigned per vault so the fenced standby can reverse direction
after promotion. Set includes secret attribute updates; policy permissions have
no separate Update entry. No key/certificate or delete/purge/backup/restore rights
are added. Filtering is application logic: grants allow access across the vault,
not just selected names. Use dedicated vaults if IAM isolation is required.

Policy-based integration uses one `vaults/accessPolicies` **add** operation per
vault containing both worker principal IDs and the vault's tenant ID. It does
not use replace/remove, redeploy the vault, or replace other principals' policies.
The worker identities must be in the same tenant; confirm available policy
capacity (1,024 entries) and verify existing/effective grants separately.
RBAC integration adds only vault-scoped role assignments.

The deployer needs policy-write permission for policy-based vaults and
role-assignment/custom-role-definition write permissions for RBAC vaults.
Storage, Service Bus, Event Grid, and monitoring still require RBAC. Preflight
reads configuration and topics only; it cannot establish worker data access or
permission propagation. Verify actual access before activation.

Microsoft supports both models but recommends RBAC for separation of duties:
vault-write control lets principals grant themselves data permissions under
access policies. Accepting the customer policy model does not remove that risk.
See [official authorization references](references.md) and
[managed-vault upgrade/ownership rules](deployment-bicep.md#vault-authorization).

## Bicep configuration

Copy `infra\parameters\existing-vaults.example.bicepparam` to an ignored
`infra\parameters\deployment.local.bicepparam`. Replace the placeholder IDs.
An empty vault ID means **create new**, not infer a vault by its name.

```bicep
param primaryVaultResourceId = '/subscriptions/<subscription-id>/resourceGroups/<source-rg>/providers/Microsoft.KeyVault/vaults/<source-name>'
param secondaryVaultResourceId = '/subscriptions/<subscription-id>/resourceGroups/<replica-rg>/providers/Microsoft.KeyVault/vaults/<replica-name>'

param primarySystemTopicResourceId = '/subscriptions/<subscription-id>/resourceGroups/<topic-rg>/providers/Microsoft.EventGrid/systemTopics/<existing-source-topic>'
param secondarySystemTopicResourceId = ''

param secretNames = ['application-password', 'service-token']
param secretPrefix = 'demo-'
param replicateAllSecrets = false
param allowUnmanagedOverwrite = false
param enableWorker = false
param enableEventRouting = false
```

Names and prefix form a **case-insensitive union**. The prefix is not a glob or
regular expression. `replicateAllSecrets = true` explicitly selects everything;
it defaults true only when both vault IDs are empty. Use the same selection
in both directions during promotion/failback. Filtering is enforced in the worker
for both event processing and reconciliation; events for unselected secrets
may reach the queue, but their values are never read or copied by the worker.
Source/destination metadata enumeration for reconciliation still requires vault
read/list permissions. The filter is not an IAM boundary.

Keep `allowUnmanagedOverwrite = false`. A destination secret without lineage
belonging to this source is an unmanaged collision, even if its value matches.
The worker raises a visible error without setting or disabling it; retries can
reach the DLQ. Approval through `true` applies to **all selected names**, including
disabling existing destination secrets when the source is inactive. Narrow the
selection before granting approval and restore false after migration.

The collision check is not an atomic compare-and-set with Key Vault. Restrict
other writers to the replica and fence them during bootstrap; otherwise a writer
can race the check. Lineage is application metadata, not a security boundary.

Preview:

```powershell
.\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
  -ParametersFile .\infra\parameters\deployment.local.bicepparam
```

Expected what-if: new integration resources/assignments, **no existing-vault
configuration change or delete**, and no replacement of a reused system topic.
Apply only with separate authorization, publish both workers, then activate
the authoritative worker/route and verify reconciliation.

## Existing Event Grid topics

Azure permits one system topic per source. Set the existing topic's complete
ARM ID when present, regardless of its resource group. The new replication
subscription uses `<workload>-<token>-secret-events`, rather than overwriting
its existing subscriptions. Existing subscriptions keep their destinations and
filters. Names in this reserved pattern require the solution's ownership label
and matching queue destination to pass preflight.

When no topic exists, the solution creates one in its regional infrastructure
group, in the same subscription/location as the vault. A subsequent retry
recognizes that solution-owned topic. Do not switch between created/reused topic
configurations casually; this changes the subscription name and can leave an
older subscription behind under incremental deployment. Keep desired-state
IDs/names stable; explicitly review and retire an obsolete route.

## Portal deployment

Use [the Portal guide](deployment-portal.md) with these substitutions:

1. At the vault creation step, select the existing resources instead of creating
   them; confirm all compatibility gates without changing their properties.
2. Create the same four vault endpoints, choosing the existing vault IDs. Existing
   approvals and application network paths remain intact.
3. Add worker access policies or IAM role assignments according to each vault's
   current model, on the actual vault resources rather than solution groups.
4. Reuse an existing system topic with a system-assigned identity rather than
   creating another. Add a dedicated, uniquely named subscription with the
   `mrkv-replication` label; leave other subscriptions unchanged.
5. Set the worker's source/destination IDs and URIs to the selected resources.
   Set `SECRET_NAMES` as a JSON array, `SECRET_PREFIX`, `REPLICATE_ALL_SECRETS`
   and `ALLOW_UNMANAGED_OVERWRITE` identically in both workers.
6. Do not replace the existing vault/topic diagnostic settings. Ensure your
   existing audit logs remain accessible to authorized operators.

The Portal Functions ARM fallback accepts the same selection/collision
parameters plus the selected destination vault ID. Run the read-only Python
preflight against compiled parameters when using Bicep; Portal operators must
perform its equivalent inventory checks manually.

## Recovery and lifecycle

Promotion is still single-writer and manually fenced. A promoted replica can
recognize its original destination through source lineage and the original
destination version/fingerprint. If that destination has independently changed,
the worker rejects it rather than guessing conflict authority. Review the
conflict before explicitly approving an overwrite.

Switching a previously created vault to an existing reference does not delete
the old vault under incremental deployment. Inventory both and perform any
retirement as a separate approved operation; never purge a vault to make a
configuration switch work.
