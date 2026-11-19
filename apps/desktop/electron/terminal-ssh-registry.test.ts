import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  connectionDialFieldsChanged,
  mergeConnectionInput,
  normalizeConnectionInput,
  normalizeRegistry
} from './connection-registry'

const initial = normalizeRegistry({
  connections: [{ id: 'gateway', kind: 'remote', label: 'Gateway', url: 'https://example.com' }]
})

const existing = initial.connections.find(c => c.id === 'gateway')!

test('terminal SSH survives edit and cold load, and null clears it', () => {
  const saved = normalizeConnectionInput(
    { ...existing, sshTunnel: { host: 'alice@box:2222', keyPath: '/tmp/my key' } },
    initial
  )

  assert.equal(connectionDialFieldsChanged(existing, saved), true)
  assert.deepEqual(saved.sshTunnel, { host: 'box', user: 'alice', port: 2222, keyPath: '/tmp/my key', remotePort: 443 })
  const loaded = normalizeRegistry({ connections: [saved] }).connections.find(c => c.id === 'gateway')!
  assert.deepEqual(loaded.sshTunnel, saved.sshTunnel)

  const renamed = normalizeConnectionInput(
    mergeConnectionInput({ id: 'gateway', kind: 'remote', label: 'Renamed' }, loaded),
    initial
  )

  assert.deepEqual(renamed.sshTunnel, saved.sshTunnel)

  const cleared = normalizeConnectionInput(
    mergeConnectionInput({ id: 'gateway', kind: 'remote', label: 'Renamed', sshTunnel: null }, loaded),
    initial
  )

  assert.equal(cleared.sshTunnel, undefined)
})

test('invalid terminal SSH is rejected rather than silently becoming local', () => {
  for (const host of ['', '-oProxyCommand=bad', 'box:99999']) {
    assert.throws(() => normalizeConnectionInput({ ...existing, sshTunnel: { host } }, initial))
  }

  for (const port of [0, -1, 65536, 'bad', '']) {
    assert.throws(() => normalizeConnectionInput({ ...existing, sshTunnel: { host: 'box', port } } as never, initial))
  }

  assert.throws(() =>
    normalizeConnectionInput({ ...existing, sshTunnel: { host: 'box', keyPath: '-unsafe' } }, initial)
  )

  assert.throws(() =>
    normalizeConnectionInput({ id: 'local', kind: 'local', label: 'Local', sshTunnel: { host: 'box' } }, initial)
  )
  const corrupt = normalizeRegistry({ connections: [{ ...existing, sshTunnel: { host: '-bad' } }] })
  const source = corrupt.connections.find(c => c.id === 'gateway')!
  assert.ok(source.sshTunnelError)
})
