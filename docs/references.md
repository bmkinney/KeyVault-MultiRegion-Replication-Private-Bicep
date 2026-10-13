# Microsoft documentation evidence matrix

Reviewed 2026-10-09. These references establish Azure capabilities; they do not
certify this custom application's behavior. Local and live tests are separate.

| Decision | Official source |
| --- | --- |
| East US and Central US are not paired with each other | [Region list](https://learn.microsoft.com/en-us/azure/reliability/regions-list) |
| Customer-controlled vault failover and Microsoft-managed limits | [Key Vault reliability](https://learn.microsoft.com/en-us/azure/reliability/reliability-key-vault) |
| Object backup subscription/geography constraints | [Key Vault backup](https://learn.microsoft.com/en-us/azure/key-vault/general/backup) |
| Soft delete, purge protection, recovery integration loss | [Soft delete](https://learn.microsoft.com/en-us/azure/key-vault/general/soft-delete-overview) |
| Vault read/write roles and independent control/data planes | [Key Vault RBAC](https://learn.microsoft.com/en-us/azure/key-vault/general/rbac-guide) |
| Exact non-destructive secret data actions for custom replica role | [Key Vault permissions](https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/security#microsoftkeyvault) |
| Secret event fields and available events | [Key Vault Event Grid schema](https://learn.microsoft.com/en-us/azure/event-grid/event-schema-key-vault) |
| Explicit system-topic creation, regional/subscription constraints | [System topics](https://learn.microsoft.com/en-us/azure/event-grid/system-topics) |
| Push cannot target private endpoints | [Private event delivery](https://learn.microsoft.com/en-us/azure/event-grid/consume-private-endpoints) |
| Trusted service/system identity delivery | [Managed-identity delivery](https://learn.microsoft.com/en-us/azure/event-grid/deliver-events-using-managed-identity) |
| Delivery/dead-letter identity setup | [Event Grid identity](https://learn.microsoft.com/en-us/azure/event-grid/managed-service-identity) |
| At-least-once, no guaranteed ordering, retry/dead-letter limits | [Delivery and retry](https://learn.microsoft.com/en-us/azure/event-grid/delivery-and-retry) |
| Service Bus Premium networking and trusted-service access | [Service Bus firewall](https://learn.microsoft.com/en-us/azure/service-bus-messaging/service-bus-ip-filtering) |
| Service Bus built-in role IDs, including Data Receiver | [Integration roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/integration#azure-service-bus-data-receiver) |
| Functions Service Bus binding and extension bundle | [Service Bus bindings](https://learn.microsoft.com/en-us/azure/azure-functions/functions-bindings-service-bus) |
| Functions identity connections and exact roles | [Connections](https://learn.microsoft.com/en-us/azure/azure-functions/manage-connections?pivots=functions-auth-identity&tabs=host) |
| Flex hosting, always-ready costs and scaling | [Flex Consumption](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-plan) |
| Flex delegation, deployment identity, Portal limitations | [Configure Flex](https://learn.microsoft.com/en-us/azure/azure-functions/flex-consumption-how-to) |
| Host services and Azure Files limitations | [Functions storage](https://learn.microsoft.com/en-us/azure/azure-functions/storage-considerations) |
| Supported private deployment and remote build | [Deployment technologies](https://learn.microsoft.com/en-us/azure/azure-functions/functions-deployment-technologies) |
| Private inbound versus outbound integration | [Functions networking](https://learn.microsoft.com/en-us/azure/azure-functions/functions-networking-options) |
| Core Tools publication | [Core Tools](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local) |
| Portal upload of an ARM template | [Deploy a custom template](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/deploy-portal) |
| Cross-region endpoints and public-access independence | [Private endpoint overview](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-overview) |
| Private zone naming | [Private endpoint DNS](https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns) |
| Storage trusted exceptions with public access disabled | [Storage firewall limitations](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-limitations) |
| Event Grid is a trusted identity-based Storage service | [Trusted services](https://learn.microsoft.com/en-us/azure/storage/common/storage-network-security-trusted-azure-services) |
| Blob leases/concurrency | [Blob concurrency](https://learn.microsoft.com/en-us/azure/storage/blobs/concurrency-manage) |
| Secret get/set/update properties | [SecretClient](https://learn.microsoft.com/en-us/python/api/azure-keyvault-secrets/azure.keyvault.secrets.secretclient?view=azure-python) |
| Private monitoring scope and shared DNS caveats | [AMPLS concepts](https://learn.microsoft.com/en-us/azure/azure-monitor/fundamentals/private-link-security) |
| AMPLS resources and endpoint setup | [AMPLS configuration](https://learn.microsoft.com/en-us/azure/azure-monitor/fundamentals/private-link-configure) |
| AMPLS association resource kind for the selected API | [Scoped resources 2023-06-01-preview](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-06-01-preview/privatelinkscopes/scopedresources) |
| Subscription deployments and cross-group modules | [Subscription-scope Bicep](https://learn.microsoft.com/en-us/azure/azure-resource-manager/bicep/deploy-to-subscription) |
| Resource name constraints | [Name rules](https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/resource-name-rules) |

## Well-Architected review

[Functions](https://learn.microsoft.com/en-us/azure/well-architected/service-guides/azure-functions),
[Service Bus](https://learn.microsoft.com/en-us/azure/well-architected/service-guides/azure-service-bus),
[Event Grid](https://learn.microsoft.com/en-us/azure/well-architected/service-guides/azure-event-grid),
and [Blob Storage](https://learn.microsoft.com/en-us/azure/well-architected/service-guides/azure-blob-storage)
inform managed identity, private networking, retries, idempotency, monitoring,
redundancy, and release/recovery procedures.

## EP1 alternative (not implemented)

Elastic Premium remains an option when required by another workload constraint.
For keyless deployment without Azure Files, use an identity-authorized external
Blob package and the documented no-Azure-Files configuration. Account for scaling
limitations and explicit trigger synchronization. Do not substitute SMB share
RBAC for a Functions Azure Files content connection or combine incompatible
remote-build and run-from-package settings.
