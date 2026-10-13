# Troubleshooting

| Symptom | Check | Do not do |
| --- | --- | --- |
| Function host cannot start | Host Blob Data Owner, client ID, local Blob/Table DNS, RBAC propagation | Add storage keys or public access |
| Package publication fails | Publisher private routing, app/SCM DNS, supported Core Tools, package-feed/build connectivity | Enable public SCM or Basic auth |
| Service Bus listener denied | Local Data Receiver scope, identity client ID, namespace DNS, role propagation | Grant sender/owner rights to both namespaces |
| Event Grid cannot deliver | System identity, namespace Data Sender, trusted-service bypass, muted subject filter | Assume it delivers through the namespace endpoint |
| Secret get/set denied | Source Secrets User, destination custom Replica Writer, source status, both local vault DNS records | Default an unknown source to primary |
| Secret tag limit | At most 12 user tags plus 3 reserved tags | Drop user tags silently |
| Ambiguous unreadable version | Multiple source versions share creation time; inspect latest state manually | Guess a newest version GUID |
| Old state temporarily visible | Source changed during processing; inspect next event/reconciliation | Claim atomic cross-vault consistency |
| Metadata-only update delayed | Five-minute reconciliation and heartbeat | Claim all changes meet the event latency target |
| Replica was manually edited | Restrict replica writers and inspect lineage | Trust tags to detect arbitrary value tampering |
| Source missing | Deleted/missing secret; review replica revocation explicitly | Automatically purge destination |
| Lease contention | Another invocation is working; check retries/DLQ and reconciliation | Remove a live lock blindly |
| Telemetry missing | AMPLS membership, five DNS-zone registrations, Metrics Publisher, Entra app setting | Reenable public ingestion/query |
| Portal cannot read secret/blob | Browser/client machine private connectivity plus data-plane role | Treat Contributor as a secret-read role |
| Alerts without email | Populate and test the approved action-group receiver | Treat a created alert as delivered notification |

SDK HTTP logging is disabled. Application errors report the exception type,
not service response bodies or secret values. Keep diagnostic artifacts value-free.

A complete region outage can block source reads and event emission. A private
endpoint in the healthy region does not resurrect the failed vault. Use the
promotion procedure and report the actual recovery boundary.
