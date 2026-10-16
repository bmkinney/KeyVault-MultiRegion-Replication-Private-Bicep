# Local validation and remaining deployment gates

Recorded 2026-10-09. No Azure resources were created or changed.

| Check | Actual result |
| --- | --- |
| Bicep main compilation | Exit 0, no template/linter diagnostics |
| Example parameter compilation | Exit 0 |
| Portal Functions ARM compilation | Exit 0 |
| Python unit tests | 31 tests passed |
| Exact compiled-infrastructure assertions | 8 tests passed |
| Replicate/Reconcile binding discovery | Both found with Service Bus/timer triggers |
| PowerShell syntax | All three scripts parsed successfully |
| Installed dependency compatibility | `pip check`: no broken requirements |
| Deployment-runtime compatibility | Functions SDK 1.25.0 is the latest Python 3.12-compatible SDK; SDK 2.x requires Python 3.13+ |
| Upstream clone | Unmodified; excluded by outer repository `.gitignore` |
| Checkov 3.3.22 (unsuppressed) | **59 passed, 14 failed, 0 skipped, 0 parsing errors; exit 1** |

The scanner is **not clean**. Offline scanning supplies no reliable severity
classification and does not evaluate all ARM expressions/copy loops. The remaining
seven rule IDs are reviewed below; no findings were silently suppressed.

## Scanner dispositions

| Rule | Count | Review |
| --- | --- | --- |
| CKV_AZURE_36 | 2 | Recommends trusted services on all Storage. Runtime storage intentionally has bypass None; only the dedicated dead-letter account uses AzureServices. The ARM copy-index conditional is not resolved by this scanner. Enabling bypass on runtime storage would broaden access. |
| CKV_AZURE_43 | 2 | Cannot validate symbolic ARM storage names. The workload token must be lowercase alphanumeric and length 3-8; generated account names are 16-22 characters with an eight-character deterministic suffix. ARM name validation remains a live gate. |
| CKV_AZURE_206 | 2 | Requests a replication configuration beyond this explicit ZRS/demo selection. ZRS protects local zone failures; two independent regional stamps provide custom regional convergence. Native geo-replication of host/coordination state is not configured or claimed. |
| CKV_AZURE_17 | 2 | Generic Web App incoming-client-certificate recommendation. This app exposes no HTTP trigger and receives Service Bus messages through outbound integration. Publication uses private SCM with Entra authorization, not app mTLS. |
| CKV_AZURE_212 | 2 | Generic App Service minimum-instance check does not model Flex functionAppConfig. The template provisions one always-ready instance per function and maximum 40 on-demand instances; that is not an in-region failover guarantee. |
| CKV_AZURE_213 | 2 | Generic HTTP health-check recommendation. There is no HTTP health endpoint; structured errors and active reconciliation-heartbeat alerts provide worker health. |
| CKV_AZURE_225 | 2 | Flex zone redundancy is not enabled in the approved demo. This is a real, documented availability tradeoff, not a production reliability claim. Enabling it would require changed capacity/cost assumptions and live regional validation. |

Exact infrastructure assertions independently check vault lockdown/purge protection,
ZRS storage/no keys/public data, private identity-only Service Bus, Flex identity
settings, private telemetry, non-destructive replica roles, default fencing,
and Event Grid managed-identity delivery/dead-lettering.

## Reproduce

Run from the root in PowerShell:

```powershell
New-Item -ItemType Directory .\.build -Force | Out-Null
az bicep build --file .\infra\main.bicep --outfile .\.build\main.json
az bicep build-params --file .\infra\parameters\example.bicepparam --outfile .\.build\parameters.json
.\.venv\Scripts\python.exe -m unittest discover -s .\tests\unit -v
.\.venv\Scripts\python.exe .\tests\infrastructure\test_template.py .\.build\main.json -v
.\.venv\Scripts\python.exe -m pip install -r .\requirements-dev.txt
.\.venv\Scripts\python.exe .\.venv\Scripts\checkov --file .\.build\main.json `
  --framework arm --skip-download --compact
```

On Windows Checkov's packaged launcher can select the wrong interpreter; use
the virtual-environment Python explicitly. Run from the same drive as the compiled
template to avoid the scanner's cross-drive `relpath` error.

Regenerate the Portal fallback after modifying its original module:

```powershell
az bicep build --file .\infra\modules\function-app.bicep `
  --outfile .\docs\portal\function-app.json
```

## Phase-6 completion self-check

- **PASS:** Local Bicep and parameter validation; actual exit status recorded above.
- **REVIEWED, NOT CLEAN:** Checkov dispositions recorded without suppression;
  no claim of a zero-finding scan or automated severity clearance.
- **PASS:** Secure defaults in exact assertions: private paths, identity, no keys,
  purge protection, and explicit documented trusted-service exceptions.
- **PASS:** New infrastructure under `infra`; original upstream sources untouched.
- **PENDING:** Live policy/quota, startup, private publication, private telemetry,
  operator notification, Portal parity, load/lag, and recovery exercises.

Deployment requires a chosen subscription, private deployment connectivity,
an approved notification recipient, reviewed what-if, and separate authorization.
Compilation and mocked unit tests do not prove end-to-end Azure behavior.

## Deployment feedback corrections (2026-10-12)

The first user-run deployment failed in both regional foundation modules:

- The Service Bus receiver assignment used an invalid built-in role ID. It now
  uses Microsoft's Azure Service Bus Data Receiver ID
  `4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0`, retaining queue-level scope.
- Both workspace and Application Insights AMPLS associations lacked
  `properties.kind`. They now explicitly declare `Resource` for
  `2023-06-01-preview`, rather than the metric-subscription kind.

Regression assertions cover the receiver role/scope in both stamps and all four
monitor associations. The earlier eight assertions did not cover these provider
requirements. The corrected main template compiles with exit 0, and all 10
compiled-infrastructure assertions pass. No deployment retry or live remediation
is performed by this fix; end-to-end deployment success remains unverified.

## Existing-vault integration (2026-10-13)

Added same-subscription vault/topic references, actual-resource-group IAM/event
scopes, read-only compatibility preflight, source allowlist/prefix selection,
and unmanaged destination collision protection. Existing vault/topic properties
and diagnostics are not redeployed. The Portal fallback was regenerated.

Local results:

| Check | Result |
| --- | --- |
| Main Bicep, new/existing parameter examples, Portal fallback | Compilation exit 0 |
| Runtime/preflight unit tests | 62 passed |
| Compiled infrastructure assertions | 13 passed, including four new/existing combinations |
| PowerShell deployment/publication/test scripts | Parse successfully |
| Azure CLI Python runner on Windows | Local version query passed; no live cloud query |
| Unsuppressed Checkov | 59 passed, 14 failed, 0 skipped, 0 parsing errors; exit 1 |

The scanner findings remain the same seven rule IDs described above. No findings
were suppressed and no clean/security-severity clearance is claimed. The unit
tests mock existing Azure resources; no existing vault was inventoried, changed,
or live-integrated during implementation. Endpoint approval, existing-topic
delivery, subscription policy, and collision/failover exercises remain live gates.

Completion self-check: local compilation **PASS**; secure defaults and preservation
assertions **PASS**; Checkov **REVIEWED, NOT CLEAN**; live integration **PENDING**.

## Dual vault authorization (2026-10-15)

Added access-policy defaults for solution-managed vaults, per-vault RBAC opt-in,
and detection/preservation of reused-vault models, including legacy null/omitted
properties and mixed pairs. Existing policy-based vaults receive one additive
worker policy update; their parent vault and unrelated policies are not replaced.
Managed-vault lists are template-owned and include both worker grants from the
initial PUT. Earlier managed RBAC deployments must explicitly retain `'rbac'`
parameters before adopting this revision.

| Check | Result |
| --- | --- |
| Main Bicep and both parameter examples | Compilation exit 0, no diagnostics |
| Runtime/preflight unit tests | 69 passed; 16 new/existing/model combinations, mixed pairs, legacy properties, invalid models, read-only preservation |
| Exact compiled-infrastructure assertions | 19 passed; per-side defaults/model/tenant/principal wiring, additive least-privilege policies, complete managed-vault lists, conditional RBAC roles/assignments, stable identity/role naming |
| Architecture SVG/Excalidraw and plan JSON | Parse successfully; diagram text dimensions/IDs and local documentation links checked |
| Unsuppressed Checkov 3.3.22 | 59 passed, 14 failed, 0 skipped, 0 parsing errors; exit 1 |

The same seven scanner rule IDs remain open with the dispositions above. The ARM
scanner did not report a vault-authorization rule; its unchanged totals are not
evidence that it evaluated access-policy defaults or runtime-selected branches.
Exact assertions cover the compiled authorization expressions separately.
Microsoft's RBAC recommendation and the vault-write self-grant risk of access
policies are documented explicitly; the customer-requested default is not a
security-recommendation override for other deployments.

Completion self-check: local compilation/regressions **PASS**; private networking,
identity-only non-vault services, non-destructive secret permissions, and
existing-vault preservation assertions **PASS**; Checkov **REVIEWED, NOT CLEAN**;
live access-policy/RBAC propagation, operator permissions, deployment/Portal
parity, and mixed-model failover **PENDING**. No Azure resource was queried or
changed, and no new runtime or certificate behavior is claimed by these checks.

## Complete-preview wiring (2026-10-16)

Replaced runtime-output module inputs throughout the dependency graph with
deterministic resource IDs and a read-only, runner-generated existing-vault
snapshot. Resource properties resolve identity/telemetry values internally.
Explicit dependencies preserve creation order. Read-only IAM inventory retains
previous vault role-assignment names; new grants use identity ARM IDs as stable
name seeds. Users configure only their local parameter file.

The runner requires Azure CLI >=2.75.0, requests full JSON what-if, and rejects
diagnostics, ignored/unsupported/unevaluated resources, potential/paginated
results, errors, and malformed output before create. `-Apply` cannot bypass
that gate. The same temporary prepared parameters are used for validate,
what-if, and create, and removed on success or failure.

| Check | Actual result |
| --- | --- |
| `az bicep build` main and Portal Function module; `az bicep build-params` both examples | Exit 0, no diagnostics; tracked Portal fallback regenerated |
| `python -m unittest discover -s .\tests\unit -q` | 80 passed, including real PowerShell runner execution with local command doubles; no Azure calls |
| `python .\tests\infrastructure\test_template.py .\.build\whatif-main.json -v` | 21 passed; recursively checks all module inputs and resource names/scopes/conditions/copy loops for transitive runtime functions |
| New/existing/model combinations | All 16 combinations checked for read-only preflight and fresh prepared metadata |
| Runner regressions | CLI/REST result envelopes; preview-only and explicit apply; property payload retained; shared prepared parameters; diagnostic/error/unsupported/malformed/old-CLI/command-failure blocking; temporary-file cleanup |
| PowerShell parsing and `git diff --check` | Exit 0 |
| Unsuppressed `checkov --file .\.build\whatif-main.json --framework arm --skip-download --compact --output json` | 59 passed, 14 failed, 0 skipped, 0 parsing errors; exit 1 |

Checkov retains the same seven documented rule IDs and does not prove evaluation
of the new runtime-selected property branches. No suppressions or severity
clearance were added. The static regression excludes runtime functions from
enumeration and module inputs, not from ordinary resource properties or public
deployment outputs where runtime values are intentionally retained.

Completion self-check: local compilation/regressions **PASS**; private/identity
defaults and referenced-resource preservation **PASS**; original excluded clone
untouched and infrastructure under `infra` **PASS**; Checkov **REVIEWED, NOT
CLEAN**; live zero-diagnostic what-if, deployment, and data-plane behavior
**PENDING**. No Azure resource was queried, deployed, or changed for this fix.
These local checks eliminate the identified wiring pattern but cannot guarantee
noise-free analysis or provider acceptance in every subscription. The runner
blocks rather than silently applying if live preview coverage is still incomplete.
