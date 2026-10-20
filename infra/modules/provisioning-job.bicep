param location string
param provisioningIdentityResourceId string
@minLength(32)
@maxLength(32)
param runId string
param payloadDigest string
@secure()
@maxLength(48000)
param payload string

var validator = base64(loadTextContent('../../scripts/preview_validation.py'))
var entrypoint = 'import base64,pathlib,sys,tempfile\nmodule_dir = pathlib.Path(tempfile.mkdtemp())\n(module_dir / \'preview_validation.py\').write_bytes(base64.b64decode(\'${validator}\'))\nsys.path.insert(0, str(module_dir))\n${loadTextContent('../../scripts/provisioning_task.py')}'

resource job 'Microsoft.ContainerInstance/containerGroups@2026-07-01' = {
  name: 'aci-mrkv-${take(runId, 12)}'
  location: location
  tags: { workload: 'mrkv-provisioning', execution: runId, payload: payloadDigest }
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${provisioningIdentityResourceId}': {} }
  }
  properties: {
    osType: 'Linux'
    restartPolicy: 'Never'
    containers: [
      {
        name: 'provision'
        properties: {
          image: 'mcr.microsoft.com/azure-cli@sha256:933eb8dcb81aecb6f77e03c5b8660f0a1dfa9e1d16525763a27f86ff3b83a044'
          command: ['python3', '-c', entrypoint]
          resources: { requests: { cpu: 1, memoryInGB: json('1.5') } }
          environmentVariables: [
            { name: 'MRKV_RUN_ID', value: runId }
            { name: 'MRKV_DIGEST', value: payloadDigest }
            { name: 'MRKV_PAYLOAD', secureValue: payload }
          ]
        }
      }
    ]
  }
}
