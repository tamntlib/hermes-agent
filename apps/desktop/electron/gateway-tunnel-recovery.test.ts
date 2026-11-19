import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { normalizeRegistry } from './connection-registry'
import { resetWindowGatewayTunnel, revalidateDesktopRemoteConnections } from './gateway-tunnel-recovery'
import { RemoteLivenessTracker, RemoteRevalidationCoordinator } from './remote-liveness'

const registry = normalizeRegistry({
  primary: 'primary', connections: [
    { id: 'primary', kind: 'remote', label: 'Primary', url: 'http://primary:9119', authMode: 'oauth', sshTunnel: { host: 'primary', remotePort: 9119 } },
    { id: 'secondary', kind: 'remote', label: 'Secondary', url: 'http://secondary:9119', authMode: 'oauth', sshTunnel: { host: 'secondary', remotePort: 9119 } },
    { id: 'direct', kind: 'remote', label: 'Direct', url: 'http://direct:9119', authMode: 'oauth' }
  ]
})

test('manual retry invalidates only the requesting window tunnel, including legacy profile overrides', async () => {
  const invalidate = vi.fn(async (_id: string) => {})
  const input = { registry, primaryProfile: 'default', config: { mode: 'local' } }
  await resetWindowGatewayTunnel({ ...input, windowRoute: { connectionId: 'secondary', profile: 'work', registryScoped: true } }, { invalidate })
  assert.deepEqual(invalidate.mock.calls, [['secondary']])
  invalidate.mockClear()
  await resetWindowGatewayTunnel({ ...input, windowRoute: { connectionId: 'direct', profile: 'default', registryScoped: true } }, { invalidate })
  await resetWindowGatewayTunnel({ ...input, windowRoute: { connectionId: 'removed', profile: 'default', registryScoped: true } }, { invalidate })
  assert.equal(invalidate.mock.calls.length, 0)
  await resetWindowGatewayTunnel({ ...input, windowRoute: null }, { invalidate })
  assert.deepEqual(invalidate.mock.calls, [['primary']])
  invalidate.mockClear()
  await resetWindowGatewayTunnel({ ...input,
    config: { mode: 'local', profiles: { work: { mode: 'remote', url: 'http://secondary:9119', authMode: 'oauth' } } },
    windowRoute: { connectionId: null, profile: 'work', registryScoped: false }
  }, { invalidate })
  assert.deepEqual(invalidate.mock.calls, [['secondary']])
})

test('renderer revalidation reaches tunnel owners even without a cached primary or pooled descriptor', async () => {
  const revalidateTunnels = vi.fn(async () => {})
  const revalidatePool = vi.fn(async () => {})

  const result = await revalidateDesktopRemoteConnections({
    connectionPromise: null, currentConnectionPromise: () => null,
    coordinator: new RemoteRevalidationCoordinator(), tracker: new RemoteLivenessTracker(),
    log: vi.fn(), probe: vi.fn(), resetConnection: vi.fn(),
    revalidatePool, revalidateTunnels, teardownSsh: vi.fn()
  })

  assert.deepEqual(result, { ok: true, rebuilt: false })
  assert.equal(revalidateTunnels.mock.calls.length, 1)
  assert.equal(revalidatePool.mock.calls.length, 1)
})

test('legacy primary SSH recovery still awaits transport teardown after dropping a dead descriptor', async () => {
  const connectionPromise = Promise.resolve({ mode: 'remote', baseUrl: 'http://loopback:9119', remoteKind: 'ssh' })
  const order: string[] = []

  const result = await revalidateDesktopRemoteConnections({
    connectionPromise, currentConnectionPromise: () => connectionPromise,
    coordinator: new RemoteRevalidationCoordinator(), tracker: new RemoteLivenessTracker(1), log: vi.fn(),
    probe: async () => { throw new Error('offline') },
    resetConnection: () => { order.push('reset') }, revalidatePool: async () => {}, revalidateTunnels: async () => {},
    teardownSsh: async () => { await Promise.resolve(); order.push('teardown') }
  })

  assert.deepEqual(result, { ok: true, rebuilt: true })
  assert.deepEqual(order, ['reset', 'teardown'])
})
