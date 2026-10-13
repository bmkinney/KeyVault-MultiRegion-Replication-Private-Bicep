# Operations and recovery

**Illustration only; not intended for production use.** These sample procedures
are subject to the [sample code disclaimer](../DISCLAIMER.md).

## Health and objectives

Normal authority is East US; Central US is the replica. Keep all application
writers pointed at the authoritative vault and restrict independent writes to
the replica. There is no automatic application endpoint redirection.

Demo targets: 100 secrets, 10 updates/minute, event-driven convergence within
60 seconds under healthy conditions, five-minute reconciliation, manually
controlled recovery within 30 minutes. These are acceptance targets, not Azure
guarantees. Define outage RPO from the last confirmed replicated state, not
from an assumed 60-second maximum.

Use the private workspace query:

```kusto
AppTraces
| where TimeGenerated > ago(1h)
| where Message startswith "MRKV "
| extend report = parse_json(substring(Message, 5))
| project TimeGenerated, AppRoleName, report
| order by TimeGenerated desc
```

Watch failures, source_missing, destination_only, source_inactive, near_expiry,
expired, and lagSeconds. Source-inactive records warrant inventory review
when no destination exists. A successful reconciliation reports failures zero.

Alert thresholds:

| Signal | Threshold |
| --- | --- |
| Service Bus DLQ | Any message |
| Event Grid delivery failures | Any in five minutes |
| Worker failure/expiry/drift | Any structured warning selected by the log rule |
| Replication lag | Reported completed-copy lag > 60 seconds |
| Active reconciliation heartbeat | No successful completion in 15 minutes |

Lag is reported on actual copies, not unchanged reconciliation reads. Absence
of lag records alone does not prove health; heartbeat and synthetic checks are
required. Action groups without recipients do not deliver notifications.

## Promotion to Central US

Preserve the same name selection in both directions. Destination collision
protection stays enabled: a recognized replica/original-source lineage permits
normal recovery, but independently modified or unmanaged destination secrets
require a separate conflict review. Do not turn on broad unmanaged overwrite
to silence a recovery failure. See [existing-vault safeguards](existing-vaults.md).

1. Declare the incident, obtain promotion approval, and record last confirmed
   synchronization time and estimated unreplicated changes.
2. Fence application writers to East US. Revoke or suspend their write permission
   and stop their write traffic through your application's approved procedure.
3. Fence the East US replication worker: set REPLICATION_ENABLED false and both
   AzureWebJobs.Replicate.Disabled/AzureWebJobs.Reconcile.Disabled true; stop
   the app and mute its event subscription. Wait for in-flight invocations to end.
4. Verify fencing. An unreachable management plane is not evidence a worker
   or writer has stopped. If fencing cannot be verified, explicitly record
   split-brain risk and do not describe this as a safe automatic promotion.
5. Validate Central US secrets are suitable for application use. Change application
   configuration to the secondary URI. Grant approved writer identities access
   only to Central US; retain an incident inventory of writes/revocations.
6. If East US is still unavailable, keep reverse replication fenced. Central US
   applications can operate without a worker trying to write into a failed vault.
   Record degradation and loss of cross-region protection.
7. When East US is reachable, keep its application writers and worker fenced.
   Set desired-state `authoritativeRegion = 'secondary'`, enableWorker true,
   enableEventRouting true. Review what-if and apply only with recovery approval.
   Alternatively configure the same flags and subscription filters in the Portal.
8. Verify reverse reconciliation and current inventory before claiming redundancy
   restored. Old notifications synchronize current authority, not old values.
   Source deletions are still manual; reconcile destination-only secrets deliberately.

Changing a deployment parameter is not a distributed fence. Operational checks
and IAM changes are mandatory. Do not apply a deployment that depends on the
failed region during the outage and expect it to perform regional promotion.

## Failback to East US

Freeze Central US application writes; drain and stop its replication worker.
Verify fencing and successful reverse reconciliation. Review any manual revocation
actions and destination-only objects. Change authority to primary, enable only
primary triggers/route, and redirect applications/writers after inventory checks.
Complete a forward reconciliation and a synthetic check. Measure the elapsed
recovery time against the 30-minute target.

Never leave both workers enabled. Never resolve conflicts by comparing destination
Key Vault version GUIDs: version IDs are not a shared sequence.

## Revocation, expiry, and deletion

For urgent revocation, an operator must disable the secret in **both** vaults
and update dependent applications. Do not wait five minutes for reconciliation.
Disabled/expired/future source secrets cause an existing destination to be disabled.
Values of unreadable source versions cannot be newly replicated.

Deletion and purge do not propagate. Reconciliation reports destination-only
objects. Review ownership and recovery needs before manually disabling/deleting
them. Do not grant purge permissions to the worker. Purge protection persists
for the 90-day retention period and cannot be turned off to speed up cleanup.

## Dead-letter replay

There are two independent stores:

- Service Bus `kv-events/$DeadLetterQueue`: message processing failures/expiry.
- Blob container `eventgrid-deadletter`: events that failed before queue delivery.

Inspect through a private client using an approved identity. Determine source
authority, validate event contents, and fix underlying network/RBAC/code failures.
Replay through the corresponding authorized source queue only after explicit
approval. Historical notifications cause a current-state synchronization.

Do not automatically purge a DLQ or assume zero active messages means all
secrets are current. Reconciliation and inventory comparison provide that evidence.

## Cleanup and recovery

No cleanup script automatically deletes resources or secrets. For an approved
teardown, stop workers, fence writers, preserve required backup/recovery records,
disconnect workspaces/Insights from AMPLS before deleting monitoring resources,
and delete only the explicitly identified deployment resource groups.

When reusing vaults or system topics, exclude their resource groups from cleanup.
Even an `existing` Bicep reference cannot protect a vault if an operator deletes
its containing group. Remove only approved integration resources in shared groups.

Soft-deleted vault names remain reserved. Recovering a vault does not restore
its RBAC assignments/Event Grid integrations; restore those through reviewed
IaC or the Portal guide before reactivating.

Native encrypted Key Vault object backups require the same subscription/geography.
The example regions meet that geography condition, but backup/restore is not
implemented as ongoing replication and does not replace this runbook.
