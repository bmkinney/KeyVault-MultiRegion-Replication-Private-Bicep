# Troubleshooting

| Symptom | Check | Do not do |
| --- | --- | --- |
| Function host cannot start | Host Blob Data Owner, client ID, local Blob/Table DNS, RBAC propagation | Add storage keys or public access |
| Package publication fails | Publisher private routing, app/SCM DNS, supported Core Tools, package-feed/build connectivity | Enable public SCM or Basic auth |
| Service Bus listener denied | Local Data Receiver scope, identity client ID, namespace DNS, role propagation | Grant sender/owner rights to both namespaces |
| Event Grid cannot deliver | System identity, namespace Data Sender, trusted-service bypass, muted subject filter | Assume it delivers through the namespace endpoint |
| Secret get/set denied | Actual vault model; policy Get/List on source and Get/List/Set on destination, or RBAC Secrets User/custom Replica Writer; worker principal ID, propagation, source status, both local vault DNS records | Switch authorization models, grant broad rights, or default an unknown source to primary |
| Secret tag limit | At most 12 user tags plus 3 reserved tags | Drop user tags silently |
| Ambiguous unreadable version | Multiple source versions share creation time; inspect latest state manually | Guess a newest version GUID |
| Old state temporarily visible | Source changed during processing; inspect next event/reconciliation | Claim atomic cross-vault consistency |
| Metadata-only update delayed | Five-minute reconciliation and heartbeat | Claim all changes meet the event latency target |
| Replica was manually edited | Restrict replica writers and inspect lineage | Trust tags to detect arbitrary value tampering |
| Existing-vault preflight fails | Same subscription, matching region, valid RBAC/access-policy model, private access, purge protection, source-topic reuse | Silently change authorization/network settings or create a duplicate topic |
| What-if reports short-circuited/skipped resources | Latest templates and runner, Azure CLI >=2.75.0, automatic discovery permissions, detailed diagnostic code | Apply an incomplete preview or manually wire generated principal IDs into templates |
| Extra policies disappear after managed-vault redeploy | Template owns the complete managed-vault policy list; use existing-vault reference mode to preserve externally managed policies | Claim additive integration preserves policies on a vault still managed by a full PUT |
| Earlier managed RBAC vault would change model | Explicitly pin the corresponding model parameters to `rbac` before redeploying with new defaults | Treat a default change as an approved authorization migration |
| No names/prefix selected on a reused vault | Supply allowlist/prefix or explicitly opt into all secrets | Treat an empty prefix as permission to copy everything |
| Unmanaged destination name collision | Review ownership, narrow selection, explicitly approve replacement only if intended | Automatically overwrite or disable an unrelated secret |
| Source missing | Deleted/missing secret; review replica revocation explicitly | Automatically purge destination |
| Lease contention | Another invocation is working; check retries/DLQ and reconciliation | Remove a live lock blindly |
| Telemetry missing | AMPLS membership, five DNS-zone registrations, Metrics Publisher, Entra app setting | Reenable public ingestion/query |
| Portal cannot read secret/blob | Browser/client machine private connectivity plus matching vault policy/RBAC grant; Storage still uses RBAC | Treat control-plane access as proof of data-plane readiness |
| Alerts without email | Populate and test the approved action-group receiver | Treat a created alert as delivered notification |

SDK HTTP logging is disabled. Application errors report the exception type,
not service response bodies or secret values. Keep diagnostic artifacts value-free.

A complete region outage can block source reads and event emission. A private
endpoint in the healthy region does not resurrect the failed vault. Use the
promotion procedure and report the actual recovery boundary.

## Incomplete what-if coverage

`NestedDeploymentShortCircuited` means a nested deployment was skipped, not that
its resources need no changes. Azure can return exit 0 while omitting most of
the topology. `Deploy.ps1` therefore checks structured result status, diagnostics,
resource change types, and additional results, and blocks incomplete previews
before create, even when `-Apply` is present.

Pull the latest repository revision and continue using only your local
`.bicepparam` file with the supported runner. Modules exchange deterministic
resource IDs and discovered existing-vault metadata. Principal IDs and telemetry
properties are resolved inside resource properties, with explicit execution
dependencies retained. No identity-bootstrap deployment, generated-ID parameter,
or user template edit is needed.

If diagnostics remain, retain their exact codes/resource paths and the complete
JSON preview for investigation; remove customer identifiers before sharing.
There is no bypass switch. Unknown property values can still produce prediction
noise, and local compilation/mocked tests do not prove a diagnostic-free live
what-if in every subscription. Resolve the reported provider/analysis limitation
rather than treating a partial resource count as approval.

If only `<vault>/accessPolicies/add` is `Unsupported`, first distinguish managed
vaults from vaults selected by ID. Managed policy-based vaults already contain
both worker grants in the foundation's vault PUT; only reused vaults should
receive the additive child operation. Earlier revisions unnecessarily submitted
both forms for managed vaults. Pull the corrected templates rather than skipping
the coverage check or supplying generated principal IDs manually.

An existing policy-based vault still requires the additive operation to preserve
unrelated policies. Azure can reject prediction while new worker principal IDs
remain unknown. That first-time integration case remains blocked unless its
preview can be resolved; do not infer its success from a new-vault test.
