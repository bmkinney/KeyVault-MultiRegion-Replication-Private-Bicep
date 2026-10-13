using '../main.bicep'

param workload = 'mrkv'
param primaryLocation = 'eastus'
param secondaryLocation = 'centralus'
param alertEmail = ''
param enableEventRouting = false
param enableWorker = false
param authoritativeRegion = 'primary'
