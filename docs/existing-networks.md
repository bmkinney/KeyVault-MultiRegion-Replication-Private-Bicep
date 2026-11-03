# Reusing existing VNets and private DNS

**Illustration only; not intended for production use.** See [DISCLAIMER.md](../DISCLAIMER.md).

By default each region gets its own VNet (`10.35.0.0/16`, `10.36.0.0/16`) and its
own private DNS zones. When the address space or firewall rules cannot change,
reuse an existing VNet in each region instead. The deployment then creates no VNet
and no DNS zone; it only adds a Function integration subnet (optionally) and
private endpoints to subnets you already own, mirroring the
[Azure Files replication project](https://github.com/kinlab-azure/azure-file-replication-non-region-pair).

Each region chooses independently, so a new and an existing network can be mixed.
Every setting below is a parameter; do not edit the Bicep files.

## Network modes

| `primaryNetworkMode` / `secondaryNetworkMode` | VNet | Function integration subnet | Private endpoint subnet | Private DNS zones |
| --- | --- | --- | --- | --- |
| `new` (default) | Created from `primaryNetwork` / `secondaryNetwork` | Created | Created | Created and linked |
| `newSubnet` | Existing, unchanged | Created in the VNet at `...IntegrationSubnetPrefix` | Existing | Existing, supplied by ID |
| `existing` | Existing, unchanged | Existing, supplied by name | Existing, supplied by name | Existing, supplied by ID |

`existing` and `newSubnet` never write the VNet, its address space, or any other
subnet. `newSubnet` adds one subnet with a `Microsoft.App/environments` delegation.

## Parameters (per region; shown for `primary`)

| Parameter | Used by | Meaning |
| --- | --- | --- |
| `primaryNetworkMode` | all | `new`, `newSubnet`, or `existing` |
| `primaryVnetName` | existing, newSubnet | Existing VNet in the deployment subscription and `primaryLocation` |
| `primaryVnetResourceGroupName` | existing, newSubnet | Resource group of that VNet (it can differ from the solution groups) |
| `primaryIntegrationSubnetName` | existing (required), newSubnet (optional) | Existing empty subnet delegated only to `Microsoft.App/environments`, or the name of the subnet to create (default `snet-<workload>-integration`) |
| `primaryIntegrationSubnetPrefix` | newSubnet | Free IPv4 prefix inside the VNet, `/27` or larger |
| `primaryPrivateEndpointSubnetName` | existing, newSubnet | Existing undelegated subnet that receives this deployment's private endpoints |
| `primaryEndpointsToCreate` | existing, newSubnet | Reused vaults that need an endpoint in this VNet: `primaryVault`, `secondaryVault` |
| `primaryPrivateDnsZoneIds` | existing, newSubnet | Existing `privatelink` zone resource IDs, keyed by service |

The secondary region uses the same names with a `secondary` prefix.
`primaryNetwork` / `secondaryNetwork` (CIDRs) apply only to `new`.

```bicep
param primaryNetworkMode = 'existing'
param primaryVnetName = 'vnet-hub-eus'
param primaryVnetResourceGroupName = 'rg-network-eus'
param primaryIntegrationSubnetName = 'snet-mrkv-integration'
param primaryPrivateEndpointSubnetName = 'snet-private-endpoints'
param primaryPrivateDnsZoneIds = {
  vault: '/subscriptions/<hub-subscription>/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.vaultcore.azure.net'
  bus: '/subscriptions/<hub-subscription>/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.servicebus.windows.net'
  blob: '/subscriptions/<hub-subscription>/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.blob.core.windows.net'
  queue: '/subscriptions/<hub-subscription>/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.queue.core.windows.net'
  table: '/subscriptions/<hub-subscription>/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.table.core.windows.net'
  app: '/subscriptions/<hub-subscription>/resourceGroups/rg-dns/providers/Microsoft.Network/privateDnsZones/privatelink.azurewebsites.net'
}

param secondaryNetworkMode = 'newSubnet'
param secondaryVnetName = 'vnet-hub-cus'
param secondaryVnetResourceGroupName = 'rg-network-cus'
param secondaryIntegrationSubnetPrefix = '10.60.1.0/27'
param secondaryPrivateEndpointSubnetName = 'snet-private-endpoints'
```

`infra\parameters\delegated.example.bicepparam`, `contributor.example.bicepparam`,
and `existing-vaults.example.bicepparam` carry the same block, commented out.

## Subnet requirements

- **Integration subnet:** empty, delegated only to `Microsoft.App/environments`
  (Flex Consumption), and `/27` or larger. Microsoft recommends `/26` if more
  apps will share it. Its size cannot change after an app joins it.
- **Private endpoint subnet:** not delegated, different from the integration
  subnet, with enough free addresses. Each region creates up to nine endpoints
  (including the Function app), and the Azure Monitor endpoint takes several
  addresses, so leave a comfortable margin. Apply the NSG/route policy your
  network team requires: workers need outbound 443 from the integration subnet to
  the endpoint subnet, and publishing needs inbound 443 from the operator network.
- The VNet must be in the deployment subscription, in the region of its
  `...Location` parameter. Private endpoints and VNet integration must use the VNet
  region. The Function app, storage, Service Bus, and endpoints still deploy to the
  two solution resource groups, even when the VNet is in another group.

## Private endpoints

Always created in the reused endpoint subnet: Service Bus, runtime Blob/Queue/Table,
dead-letter Blob, Azure Monitor (AMPLS), and the Function app. The vault endpoints
depend on who owns each vault:

| Vault | Endpoint in a `new` network | Endpoint in an `existing`/`newSubnet` network |
| --- | --- | --- |
| Solution-created | Created | Created |
| Reused (`...VaultResourceId` set) | Created | Created only if listed in `...EndpointsToCreate` |

Use `EndpointsToCreate` for a reused vault that has no endpoint reachable from the
VNet. Omit it when the customer already has one, so you do not create a duplicate
connection or conflicting DNS records. `primaryEndpointsToCreate` controls
endpoints in the **primary** VNet, whichever vault they target, so the secondary
vault in it is `secondaryVault`.

## DNS

Zones are never created for reused networks. Supply the full resource ID of the
existing zone per service key: `vault`, `bus`, `blob`, `queue`, `table`, `app`,
`monitor`, `oms`, `ods`, `agent`. The zone name must match its key, and the zones
must resolve from the VNet (linked to it, or reachable through the customer's
resolver or forwarders).

- **Omit a key** when Azure Policy or another process creates the records. The
  endpoint is created without a DNS zone group; verify resolution yourself.
- **Azure Monitor:** the AMPLS endpoint uses `monitor`, `oms`, `ods`, `agent`, and
  `blob`. These names are global per Monitor scope, so the two regions must not
  point at the same `monitor`/`oms`/`ods`/`agent` zones, and a zone shared with
  another AMPLS changes resolution for everything linked to it. Use separate zones
  per region, or omit those keys and manage the records deliberately. Do not merge
  both regions' AMPLS records into one resolver without redesign.
- **Permissions:** the operator needs `Microsoft.Network/privateDnsZones/join/action`
  on each zone (for example Private DNS Zone Contributor or Network Contributor at
  the zone), including zones in other subscriptions of the tenant.

## Permissions

The operator (Contributor on the solution groups, in delegated mode too) also needs
on the reused network:

| Scope | Action |
| --- | --- |
| Integration subnet | `Microsoft.Network/virtualNetworks/subnets/join/action` |
| Private endpoint subnet | `Microsoft.Network/virtualNetworks/subnets/join/action` |
| VNet (`newSubnet` only) | `Microsoft.Network/virtualNetworks/subnets/write` |

Network Contributor on the VNet covers all three. The provisioning identity of
[delegated provisioning](deployment-delegated.md) needs nothing on the network:
networking stays in the Contributor-applied infrastructure stage.

## Preflight and validation

`scripts\Deploy.ps1` (direct and delegated) reads the networks before ARM validation
and, in delegated mode, before any container, group, or identity is created. It stops
without changing anything when:

- a required parameter is missing, a name is malformed, or an option is unsupported;
- the VNet is missing, not `Succeeded`, or in a region other than the configured one;
- the endpoint subnet is missing or delegated, or equals the integration subnet;
- an `existing` integration subnet is missing, delegated to anything but
  `Microsoft.App/environments`, or smaller than `/27`;
- a `newSubnet` prefix is invalid, smaller than `/27`, outside the VNet, or overlaps
  another subnet (a rerun that finds its own previously created subnet passes);
- a DNS zone ID does not match its service key or a readable zone does not exist.
  A zone the operator cannot read produces a warning, not an error.

The template repeats the parameter checks and fails ARM validation with the
message before any resource changes. Preflight cannot prove NSG, route, DNS
resolution, or permissions; use the [post-deployment checks](deployment-delegated.md#after--apply-complete-and-test-the-deployment).

## Migrating a deployment that created its own VNets

A deployed region can move from `new` to `existing` or `newSubnet` without
recreating vaults, secrets, storage, Service Bus, identities, grants, or monitoring.
Only the network layer changes: the private endpoints are replaced and the Function
app is re-pointed. Incremental deployments never delete, so
`scripts\Deploy.ps1 -MigrateNetwork` removes the stale endpoints first.

1. **Configure.** In your parameter file set `...NetworkMode` and the VNet, subnet,
   DNS zone, and `...EndpointsToCreate` values for each region to migrate. Keep the
   same `workload`, subscription, and locations. Keep both activation flags `false`
   for now; enable them in step 6.
2. **Preview.** Nothing changes:

   ```powershell
   .\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
     -ParametersFile .\infra\parameters\deployment.local.bicepparam -MigrateNetwork
   ```

   It validates the target network as the deployment preflight does, finds the two
   deployed solution groups, and lists what it would delete, keep, and leave.
3. **Remove the old endpoints.** Replication stops here. If workers are active, add
   `-FenceWorkers`; without it, `-Apply` refuses to continue while replication is on.

   ```powershell
   .\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' `
     -ParametersFile .\infra\parameters\deployment.local.bicepparam `
     -MigrateNetwork -Apply -FenceWorkers
   ```

4. **Deploy.** Preview, then apply with the same parameter file (delegated mode uses
   the same commands). This recreates the endpoints in the reused subnets with the
   supplied DNS zones and moves each app to the new integration subnet:

   ```powershell
   .\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' -ParametersFile .\infra\parameters\deployment.local.bicepparam
   .\scripts\Deploy.ps1 -SubscriptionId '<subscription-id>' -ParametersFile .\infra\parameters\deployment.local.bicepparam -Apply
   ```

5. **Verify.** Follow the [post-deployment checks](deployment-delegated.md#after--apply-complete-and-test-the-deployment):
   endpoints Approved, private resolution from the new VNet, both apps list
   `Replicate` and `Reconcile`, and no host identity errors.
6. **Reactivate.** Set the activation flags and `authoritativeRegion` to their
   intended values and apply again, then run the acceptance test. Reconciliation
   copies anything missed while fenced; events queued in Service Bus are processed.
7. **Remove the old network yourself.** The script prints the old `vnet-<prefix>`
   and private DNS zones it finds in each solution group but never deletes them.
   Delete them only after verification, and only if nothing else uses them. The old
   integration subnet may take a while to release after the app moves.

### What it will and will not delete

It lists private endpoints only in the two deployed solution resource groups,
`rg-<workload>-p-<token>` and `rg-<workload>-s-<token>`. It deletes an endpoint only
when all of these hold, and otherwise leaves it:

- its name is exactly one this deployment creates (`pe-<prefix>-vault-local`,
  `-vault-remote`, `-bus`, `-blob`, `-queue`, `-table`, `-deadletter`, `-monitor`,
  `-app`);
- its Private Link target and subresource are exactly the ones this deployment
  uses for that name (for example the solution's own storage account or Service Bus);
- it is not already in the configured endpoint subnet.

Endpoints that existed before the deployment, including those a customer created
for a reused vault in their own network and resource group, are never listed,
modified, or deleted. A region whose network mode is still `new` is left alone.
If a vault you reuse had its endpoint created by the old deployment, that
endpoint is deleted and **not recreated** unless you list the vault in
`...EndpointsToCreate`; the script warns for each case. Make sure the reused network
already reaches that vault privately, or list it.

Migration is idempotent. If a later step fails, fix it and rerun; endpoints already
moved are kept, and a rerun after success finds nothing to delete. Nothing is
rolled back automatically, and reverse migration from reused networks back to
`new` is not scripted.

If Azure rejects moving the Function app to the new integration subnet during step 4,
remove its VNet integration with
`az functionapp vnet-integration remove --name <app> --resource-group <group>` while it
is fenced, and rerun step 4. This path is untested against a live app.

## Not supported

- A VNet in another subscription. DNS zones may be in another subscription.
- Placing the deployment's private endpoints in a different resource group than the
  solution group.
- Using one VNet for both regions; VNets are regional.
- Creating or modifying DNS zones, links, NSGs, route tables, or peering.
