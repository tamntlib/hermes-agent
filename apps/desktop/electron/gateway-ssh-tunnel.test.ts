import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { test, vi } from 'vitest'

import {
  connectionDialFieldsChanged,
  mergeConnectionInput,
  normalizeConnectionInput,
  normalizeRegistry,
  reconcileAppliedGlobalConnection,
  type SshTunnelConfig
} from './connection-registry'
import { readDesktopSshBudgets } from './desktop-ssh-budgets'
import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { SshConnection } from './ssh-connection'

const config = { host: 'alice@box:2222', keyPath: '/tmp/my key', remotePort: 9119 }

const registry = normalizeRegistry({
  primary: 'a',
  connections: [{ id: 'a', kind: 'remote', label: 'A', url: 'https://a.test', authMode: 'oauth' }]
})

const original = registry.connections.find(c => c.id === 'a')!

test.each([
  { yaml: 'desktop:\n  ssh_connect_timeout: 45\n  ssh_forward_timeout: 60', connect: 45, forward: 60 },
  { yaml: '', connect: 15, forward: 15 }
])('Remote Gateway SSH applies connect=$connect s and forward=$forward s', async ({ yaml, connect, forward }) => {
  let transport!: SshConnection
  const spawnFn = vi.fn((_command: string, _args: string[]) => {
    const child = new EventEmitter()
    queueMicrotask(() => child.emit('close', 0, null))

    return child
  })
  const deps = {
    controlDir: '/tmp/hermes-tests',
    pickPort: async () => 45000,
    sshBudgets: readDesktopSshBudgets(yaml, { log: vi.fn(), warn: vi.fn() }),
    createTransport: (ssh: SshTunnelConfig, options: object) => {
      transport = new SshConnection(ssh, { ...options, mux: true, spawnFn })

      return transport
    }
  }
  const tunnels = new GatewaySshTunnels(deps)

  try {
    await tunnels.ensure(normalizeConnectionInput({ ...original, sshTunnel: config }, registry))
    assert.ok(spawnFn.mock.calls[0][1].includes(`ConnectTimeout=${connect}`))
    assert.equal(transport._forwardTimeoutMs, forward * 1000)
  } finally {
    await tunnels.closeAll()
  }
})

test('SSH tunnel survives cold load, rename and global reconciliation; disabling is explicit', () => {
  const saved = normalizeConnectionInput({ ...original, sshTunnel: config }, registry)
  assert.deepEqual(saved.sshTunnel, {
    host: 'box',
    user: 'alice',
    port: 2222,
    keyPath: '/tmp/my key',
    remotePort: 443
  })
  assert.equal(connectionDialFieldsChanged(original, saved), true)
  const loaded = normalizeRegistry({ primary: 'a', connections: [saved] })

  const renamed = normalizeConnectionInput(
    mergeConnectionInput(
      { id: 'a', kind: 'remote', label: 'New A' },
      loaded.connections.find(c => c.id === 'a')
    ),
    loaded
  )

  assert.deepEqual(renamed.sshTunnel, saved.sshTunnel)
  assert.equal(connectionDialFieldsChanged(saved, renamed), false)
  assert.deepEqual(
    reconcileAppliedGlobalConnection(loaded, {
      mode: 'remote',
      remote: { url: 'https://a.test', authMode: 'oauth' }
    }).connections.find(c => c.id === 'a')?.sshTunnel,
    saved.sshTunnel
  )
  assert.equal(normalizeConnectionInput({ ...renamed, sshTunnel: null }, loaded).sshTunnel, undefined)
})

test('corrupt tunnel primary stays blocked, never normalizes to a local primary', () => {
  for (const sshTunnel of [{ host: '-bad' }, { host: 'box', port: 0 }, { host: 'box', keyPath: '-unsafe' }]) {
    assert.throws(() => normalizeConnectionInput({ ...original, sshTunnel } as never, registry))
    const corrupt = normalizeRegistry({ primary: 'a', lastUsed: 'a', connections: [{ ...original, sshTunnel }] })
    assert.equal(corrupt.primary, 'a')
    assert.equal(corrupt.lastUsed, 'a')
    assert.ok(corrupt.connections.find(c => c.id === 'a')?.sshTunnelError)
    assert.ok(corrupt.quarantined?.length)
  }
})

test('profiles share one tunnel; retired generation cannot close or publish the next owner', async () => {
  const instances: any[] = []
  let unblock!: () => void

  const wait = new Promise<void>(resolve => {
    unblock = resolve
  })

  const tunnels = new GatewaySshTunnels({
    createTransport: () => {
      const transport = {
        open: vi.fn(async () => {
          if (instances.length === 1) {
            await wait
          }
        }),
        forward: vi.fn(async () => {}),
        close: vi.fn(async () => {}),
        isAlive: vi.fn(async () => true)
      }

      instances.push(transport)

      return transport
    },
    pickPort: async () => 45000 + instances.length,
    controlDir: '/tmp/hermes-tests'
  })

  const source = normalizeConnectionInput({ ...original, sshTunnel: config }, registry)
  const first = tunnels.ensure(source)
  const second = tunnels.ensure(source)
  assert.equal(first, second)
  await tunnels.invalidate('a')
  const latest = await tunnels.ensure({ ...source, sshTunnel: { ...source.sshTunnel!, host: 'other' } })
  unblock()
  await assert.rejects(first)
  assert.equal(tunnels.forUrl(latest.dialBaseUrl), latest)
  assert.throws(() => tunnels.assertCurrent({ ...latest, generation: 'retired-generation' }))
  assert.equal(instances[1].close.mock.calls.length, 0)
  await tunnels.closeAll()
  assert.equal(instances[1].close.mock.calls.length, 1)
})

test('missing URL or corrupt kind cannot erase the selected SSH identity during quarantine', () => {
  for (const entry of [
    { ...original, url: '', sshTunnel: { host: 'box' } },
    { ...original, kind: 'broken', sshTunnel: { host: 'box' } }
  ]) {
    const loaded = normalizeRegistry({ primary: 'a', lastUsed: 'a', connections: [entry] })
    assert.equal(loaded.primary, 'a')
    assert.equal(loaded.lastUsed, 'a')
    assert.ok(loaded.connections.find(connection => connection.id === 'a')?.sshTunnelError)
  }
})
