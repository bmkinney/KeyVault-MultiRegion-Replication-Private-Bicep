"""Read-only prerequisite report for delegated deployments; never changes Azure."""

import re

ASSIGN_ACTION = "Microsoft.ManagedIdentity/userAssignedIdentities/*/assign/action"
BASE_NAMESPACES = (
    "Microsoft.Authorization", "Microsoft.ContainerInstance", "Microsoft.ManagedIdentity", "Microsoft.Resources",
)


class ReadinessError(ValueError):
    pass


def subscription_of(resource_id):
    return resource_id.split("/")[2]


def provider_namespaces(*templates):
    found = set(BASE_NAMESPACES)

    def walk(node):
        if isinstance(node, dict):
            # Delegations (e.g. Flex Consumption) need their provider registered though they are not resource types.
            for key in ("type", "serviceName"):
                kind = node.get(key)
                if isinstance(kind, str) and re.fullmatch(r"Microsoft\.[A-Za-z]+/.+", kind):
                    found.add(kind.split("/")[0])
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for template in templates:
        walk(template)
    return sorted(found)


def permits(permissions, action):
    target = action.lower()

    def matches(pattern):
        return re.fullmatch(".*".join(re.escape(part) for part in pattern.lower().split("*")), target) is not None

    return any(
        any(matches(item) for item in block.get("actions", []))
        and not any(matches(item) for item in block.get("notActions", []))
        for block in permissions
    )


def effective_permissions(query, scope):
    url = f"https://management.azure.com{scope}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01"
    return query("rest", "--method", "get", "--url", url)["value"]


def check(*, subscription, provisioner_id, worker_ids, override_group, namespaces, query):
    """Run every check so one report lists all gaps; returns (blockers, warnings, provisioner principal)."""
    blockers, warnings = [], []
    identities = [provisioner_id, *(worker for worker in worker_ids if worker)]

    tenants = {}
    for item in sorted({subscription, *map(subscription_of, identities)}, key=str.lower):
        try:
            tenants[item.lower()] = query("account", "show", "--subscription", item)["tenantId"].lower()
        except (RuntimeError, KeyError) as error:
            blockers.append(
                f"Cannot read subscription {item}. Sign in with an account that can see it (az login), "
                f"or ask an administrator for Reader there. ({error})"
            )
    deployment_tenant = tenants.get(subscription.lower())
    for item, tenant in tenants.items():
        if deployment_tenant and tenant != deployment_tenant:
            blockers.append(f"Subscription {item} is in tenant {tenant}, not deployment tenant {deployment_tenant}; "
                            "cross-tenant identities are not supported.")

    for identity in identities:
        try:
            if not permits(effective_permissions(query, identity), ASSIGN_ACTION):
                blockers.append(f"Operator lacks {ASSIGN_ACTION} on {identity}. "
                                "Ask an administrator to assign Managed Identity Operator on that identity.")
        except (RuntimeError, KeyError, TypeError) as error:
            warnings.append(f"Could not verify the operator's assign permission on {identity}: {error}")

    try:
        granted = effective_permissions(query, f"/subscriptions/{subscription}")
        if not permits(granted, "Microsoft.Resources/deployments/write"):
            blockers.append(f"Operator cannot create deployments in subscription {subscription}.")
        if not permits(granted, "Microsoft.Resources/subscriptions/resourceGroups/write"):
            warnings.append("Operator cannot create resource groups; the two solution groups must already exist.")
    except (RuntimeError, KeyError, TypeError) as error:
        warnings.append(f"Could not verify the operator's permissions in subscription {subscription}: {error}")

    for namespace in namespaces:
        try:
            state = query("provider", "show", "--namespace", namespace, "--subscription", subscription).get("registrationState")
        except RuntimeError as error:
            warnings.append(f"Could not read registration of {namespace}: {error}")
            continue
        if state == "Registering":
            warnings.append(f"Resource provider {namespace} is still registering in {subscription}.")
        elif state != "Registered":
            blockers.append(f"Resource provider {namespace} is {state} in {subscription}. "
                            f"Run: az provider register --namespace {namespace} --subscription {subscription}")

    principal = None
    try:
        principal = query(
            "resource", "show", "--ids", provisioner_id, "--subscription", subscription_of(provisioner_id),
            "--api-version", "2024-11-30",
        )["properties"]["principalId"]
    except (RuntimeError, KeyError) as error:
        blockers.append(f"Cannot read provisioning identity {provisioner_id}: {error}")
    if principal:
        try:
            assignments = query(
                "role", "assignment", "list", "--assignee", principal, "--all", "--include-inherited",
                "--subscription", subscription, "--fill-principal-name", "false", "--fill-role-definition-name", "false",
            )
            if not assignments:
                blockers.append(
                    f"Provisioning identity {principal} has no role assignment in subscription {subscription}. "
                    "Grant it the roles listed above at the subscription, or at both solution groups after an "
                    "administrator pre-creates them."
                )
        except RuntimeError:
            warnings.append("Could not list the provisioning identity's role assignments; verify its grants manually.")

    if override_group:
        try:
            query("group", "show", "--name", override_group, "--subscription", subscription)
        except RuntimeError:
            blockers.append(
                f"provisioningResourceGroupName '{override_group}' does not exist in deployment subscription "
                f"{subscription}. Remove the parameter to run in the primary solution group."
            )
    return blockers, warnings, principal


def grant_guide(subscription, provisioner_id, principal, worker_ids):
    reused = [provisioner_id, *(worker for worker in worker_ids if worker)]
    return [
        "Administrator grants required (never granted automatically):",
        f"  Operator, subscription {subscription}: deployment rights (Contributor) for resource groups and deployments.",
        *(f"  Operator, {item}: Managed Identity Operator (read and assign)." for item in reused),
        f"  Provisioning identity {principal or provisioner_id}, subscription {subscription} or both solution groups: "
        "Managed Identity Contributor, Key Vault Contributor, EventGrid Contributor, "
        "and role assignment write (RBAC Administrator, restricted by conditions).",
        *(f"  Provisioning identity, {item}: Reader." for item in reused[1:]),
    ]
