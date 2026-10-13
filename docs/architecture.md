# Architecture

**Illustration only; not intended for production use.** This sample architecture
is subject to the [sample code disclaimer](../DISCLAIMER.md).

## Independent regional stamps

| Resource | East US | Central US |
| --- | --- | --- |
| VNet | `10.35.0.0/16` | `10.36.0.0/16` |
| Functions integration | `10.35.1.0/24` | `10.36.1.0/24` |
| Private endpoints | `10.35.0.0/24` | `10.36.0.0/24` |
| Local vault role | Authoritative by default | Replica by default |
| Worker | Enabled only after activation | Fenced by default |
| Storage | Local StorageV2 ZRS | Local StorageV2 ZRS |
| Messaging | Local Service Bus Premium | Local Service Bus Premium |
| Monitoring | Local workspace/Insights/AMPLS | Local workspace/Insights/AMPLS |

Each stamp has private endpoints for both vaults, runtime Blob/Queue/Table,
local Service Bus, the Function App, dead-letter Blob, and Azure Monitor.
There are nine endpoints per stamp, 18 total. AMPLS allocates several IP addresses
within its endpoint; subnet sizing must accommodate them.

Private endpoints live in the VNet's region even when the target vault is remote.
No regional peering is necessary for the replication data path. A local endpoint
to a remote vault does not make that remote service available during its outage.

## Paths and trust boundaries

| Path | Networking | Identity |
| --- | --- | --- |
| Vault publishes version/expiry event | Azure platform integration | Azure service |
| Event Grid to Service Bus | Public endpoint IP space on Microsoft backbone; trusted-service bypass | System-assigned system-topic identity |
| Event Grid dead-letter write | Trusted-service Storage exception, not Private Link | Same system-topic identity |
| Worker receives messages | Local Service Bus private endpoint | Regional user-assigned worker identity |
| Worker reads/writes secrets | Local private endpoints targeting both vaults | Regional user-assigned worker identity |
| Host/package/coordination/diagnostics storage | Local service-specific private endpoints | Regional user-assigned worker identity |
| Worker telemetry | Local AMPLS endpoint | Monitoring Metrics Publisher and Entra ingestion |
| Infrastructure management | ARM/Entra public management endpoints | Operator deployment identity |
| Code publication | App private deployment endpoint | Authorized publishing identity |

Private DNS and authorization are independent. Successful DNS resolution does
not demonstrate permission; an assigned role does not establish network access.
Private endpoints do not disable public endpoints automatically.

## Resource and permission details

Key Vault has Standard SKU, RBAC, public network access disabled, no trusted
service bypass, soft delete, and 90-day purge protection. VM deployment, disk
encryption, and ARM secret retrieval switches are off because this workload
does not need them.

Worker identity permissions:

- Secrets User on its local source vault.
- Custom Secret Replica Writer on the opposite destination vault (metadata,
  get, set, and attribute update only; no delete/purge/backup/restore).
- Data Receiver on the local queue, not both namespaces.
- Blob Data Owner on runtime storage (host, deployment, coordination).
- Table Data Contributor on runtime storage for host diagnostic events.
- Monitoring Metrics Publisher on local Application Insights.

No queue storage binding uses the runtime Queue endpoint today. The account
supports it and the private endpoint is provisioned for host compatibility;
no speculative Queue Data Contributor role is assigned.

Event Grid identity permissions:

- Service Bus Data Sender on its local namespace.
- Blob Data Contributor on dedicated dead-letter storage.
- No Key Vault reader/secret permission.

Runtime storage has no trusted-service bypass. Separate dead-letter storage
has the documented AzureServices bypass and managed-identity access. Neither
allows shared keys or anonymous blob access.

## Current-state convergence

Events contain identifiers, not secret values. The worker validates the configured
source ARM ID, vault name, object type, secret ID/name/version, schema, and event
timestamp before processing any member of the message.

An event is a notification to synchronize **current authority**, not a command
to replay an old version. Per-secret Blob leases serialize local replicas across
instances and between event processing and reconciliation. Leases renew while
work runs and expire after a crash. They do not form an atomic transaction with
Key Vault and are not a global leader-election mechanism.

Destination tags record source ARM ID, source version, and a metadata fingerprint.
On retry after a destination write, the marker check avoids an extra version even
if recording coordination state failed. Blob state contains version IDs only.
Secret values are never logged or serialized to coordination storage.

New destination versions use local Key Vault version IDs. Versioned source
secret URLs are not valid on the replica. Applications should use the configured
vault URI and an appropriate version-independent secret reference.

The worker copies value, content type, not-before/expiry dates, enabled state,
and user tags for readable current secrets. For disabled, expired, or future
secrets, it disables an existing destination rather than publishing unavailable
material. If no replica exists, it reports `source_inactive`; the value cannot
be backfilled until it is readable. Source deletion is reported, not propagated.
Reconciliation reports destination-only secrets for manual review.

The system does not compare or overwrite every manually edited replica value:
lineage assumes exclusive writes to the destination. Restrict writers accordingly.
Source lineage from a previous promotion is stripped before assigning new lineage.
Ordinary editors must not use `mrkv_` tags.

## Failure behavior

| Failure | Behavior |
| --- | --- |
| Invalid or foreign event | Raise; Service Bus retries then DLQ |
| Temporary Key Vault/Storage failure | SDK retries, then message retry/DLQ |
| Concurrent secret processing | Lease contention raises; retries and reconciliation recover |
| Lease loss | Stop before subsequent writes; report failure |
| Crash after destination write | Destination lineage recognizes completed write on retry |
| Source changes during a copy | Next event/reconciliation converges; no atomic source snapshot |
| Expiration or near-expiry event | Structured warning and scheduled alert; no rotation |
| Event Grid cannot reach queue | Delivery retry/dead-letter policy; delivery-failure alert |
| Destination-only object | Warning; never delete/purge automatically |
| Source region fails before replication | Latest unreplicated state may be unavailable |

Event Grid retry retention is up to 24 hours/30 attempts; the queue has 14-day
TTL and maximum delivery count 10. Service Bus DLQ is distinct from Event Grid
Blob dead-letter storage. Replaying either requires reviewing current authority.

## Cost and production boundaries

Two Premium Service Bus namespaces dominate fixed cost. Always-ready Flex
instances, 18 endpoints, ZRS storage, ingestion, alerts, and private deployment
connectivity also cost money. No June-2024 upstream price estimates are reused.
Use current regional Azure pricing before deployment.

Replication is not backup: it can copy erroneous values. Independent recovery,
application failover logic, operator access control, and incident-response
procedures remain necessary. Keys, certificates, full vault configuration,
historical versions, and cross-tenant deployment are outside the implementation.
