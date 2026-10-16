# Approved architecture decisions

Approved through interactive clarification on 2026-10-09.

| Decision | Selection | Rationale |
| --- | --- | --- |
| Repository | Outer `-Bicep` folder; exclude untouched nested clone | Independent implementation and history |
| Hosting | Flex Consumption; EP1 documented alternative only | Managed-identity Blob deployment, no Azure Files content dependency |
| Authority | Single writer with manual promotion/failback | Prevent replication loops and define conflict authority |
| Scope | Current secret state only | No false promises about version history, keys, certificates, or automatic deletion |
| Regions | East US primary, Central US secondary | Not paired with each other; example remains in US geography |
| Telemetry | Private-only ingestion/query through AMPLS | Private worker telemetry paths |
| Event Grid | Accept documented trusted-service delivery exception | Push delivery cannot target Private Link |
| Objectives | 60-second healthy replication; 30-minute manual recovery; five-minute reconciliation | Demo acceptance targets, not service guarantees |
| Size | 100 secrets, 10 updates/minute | Bounded demo workload |
| Provenance | Original implementation with attribution | No copying from an upstream clone without a license |
| Existing vaults | Independent new/existing selection within the subscription | Preserve settings; scoped integration resources only |
| Vault authorization (subsequent customer decision) | Access-policy default for managed vaults; per-vault RBAC opt-in; reused models detected/preserved | Accommodate policy-based customers and mixed pairs without automatic migration |
| Source selection for reused vaults | Explicit name allowlist/prefix or whole-vault opt-in | Avoid accidental copying from shared vaults |
| Destination collisions | Reject unmanaged names unless replacement is explicitly approved | Preserve existing unrelated contents |
| What-if wiring (subsequent customer decision) | Static module inputs, automatic read-only discovery, and a fail-closed complete-preview gate | Users edit only parameters; runtime module outputs must not hide downstream deployments |

## Implementation refinements

- ZRS storage and zone-redundant Service Bus protect local dependencies.
  Flex is not zone redundant in this example. Always-ready instances reduce
  cold starts, but do not guarantee latency or zone resilience.
- Each app has one always-ready instance for Replicate and one for Reconcile.
  Minimum allowed Flex maximum scale-out is 40; this is a ceiling, not 40
  allocated instances. These choices have recurring costs.
- A single `authoritativeRegion` parameter selects the active regional worker
  and route. `enableWorker` and `enableEventRouting` default to false.
- Inactive Event Grid subscriptions use a deliberately nonmatching subject
  prefix. This permits repeatable incremental updates without relying on
  conditional-resource deletion, which incremental ARM deployments do not do.
- Opposite-region replica-write permissions are preassigned for recovery using
  secret Get/List/Set policies or a custom RBAC role limited to readMetadata,
  getSecret, setSecret, and update. Policy Set includes attribute updates.
  Secrets Officer was narrowed because it includes delete/purge/backup/restore
  operations that this implementation does not need.
- Microsoft supports both vault models but recommends RBAC for separation of
  duties. Under policies, vault-write principals can grant themselves data
  access; the policy default is a customer-selected tradeoff, not a claim of
  stronger isolation. Source filtering is not an IAM boundary under either model.
- Managed vaults include both worker policies from their first PUT; creating
  identities ahead of both foundations avoids cross-foundation dependencies and
  temporary empty policy lists on redeploy. The template owns managed-vault
  lists. Reused vaults instead receive an additive child policy update without
  replacing other principals' entries. Existing RBAC deployments must pin
  `'rbac'` explicitly before adopting the new defaults.
- Nested modules receive deterministic ARM IDs/names and a runner-generated
  snapshot of existing-vault model/tenant/URI. Runtime identity/telemetry reads
  stay inside resource properties; explicit dependencies preserve actual
  creation order. RBAC names use identity ARM IDs for new grants and inventory
  preserves prior principal-seeded assignment names. A partial successful
  what-if is rejected before apply; no user-side Bicep rewiring is required.
- Three reserved secret tags store non-value lineage. Sources may use at most
  12 other tags. Copying a secret with more tags fails visibly.
- Unreadable latest versions with identical creation timestamps are not guessed.
  The worker fails and alerts for operator review.
- AMPLS is regional-by-configuration, one per isolated DNS view. If the networks
  are later connected to a common DNS view, redesign to one shared scope and
  correct reachable private endpoint records before integrating.
- AMPLS uses a preview ARM API because its current public schema is preview.
  Microsoft Learn is the reference when the MCP schema resolver cannot provide
  that resource type. Other resources use supported pinned API versions.

## Not yet selected or verified

Target subscription, actual quotas, policy effects, notification recipient,
private publishing workstation/runner connectivity, and production SLOs.
The repository is illustration-only and subject to the
[sample code disclaimer](../DISCLAIMER.md); these pending decisions do not
imply suitability for production.
No public IP, VPN gateway, Bastion, firewall, paid runner, or GitHub remote is
implicitly provisioned. Connectivity to both regional management networks is
a deployment prerequisite, not an undocumented third regional dependency.
