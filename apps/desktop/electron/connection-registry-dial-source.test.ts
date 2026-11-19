import assert from 'node:assert/strict'

import { test } from 'vitest'

import { LOCAL_CONNECTION_ID, normalizeRegistry, registryDialSource } from './connection-registry'

test('registry dial resolves only the explicitly requested connection', () => {
  const registry = normalizeRegistry(null)
  const local = registry.connections.find(source => source.id === LOCAL_CONNECTION_ID)

  assert.equal(registryDialSource(registry, ` ${LOCAL_CONNECTION_ID} `), local)
  assert.throws(() => registryDialSource(registry, ''), /No connection with id/)
  assert.throws(() => registryDialSource(registry, 'removed'), /No connection with id "removed"/)
})

test('registry dial refuses a malformed SSH Gateway before starting its backend', () => {
  const registry = normalizeRegistry({
    version: 2,
    primary: 'broken',
    connections: [{
      id: 'broken',
      kind: 'remote',
      label: 'Broken gateway',
      url: 'https://gateway.example.invalid',
      authMode: 'oauth',
      sshTunnel: { host: '' }
    }]
  })

  const broken = registry.connections.find(source => source.id === 'broken')

  assert.ok(broken?.sshTunnelError)
  assert.throws(() => registryDialSource(registry, 'broken'), { message: broken.sshTunnelError })
})
