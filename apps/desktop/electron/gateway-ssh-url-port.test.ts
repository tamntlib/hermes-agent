import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import {
  mergeConnectionInput,
  normalizeConnectionInput,
  normalizeRegistry,
  normalizeSshTunnel,
  type RegistryConnection,
  type SshTunnelConfig
} from './connection-registry'
import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { createGatewayTunnelRuntime } from './gateway-tunnel-runtime'
import { createNativeAccessTokenCoordinator } from './native-access-token'

const target = { host: 'alice@box:2222', keyPath: '/fixture/key', remotePort: 1234 }

for (const [url, port] of [
  ['http://gateway:9119', 9119],
  ['gateway:8642', 8642],
  ['http://gateway', 80],
  ['http://gateway:80', 80],
  ['https://gateway', 443],
  ['https://gateway:443', 443],
  ['http://[::1]:8642?x=1#fragment', 8642]
] as const) {
  test(`Gateway URL selects backend port for ${url}, independently of SSH login`, () => {
    assert.deepEqual(normalizeSshTunnel(target, url), {
      host: 'box',
      user: 'alice',
      port: 2222,
      keyPath: '/fixture/key',
      remotePort: port
    })
  })
}

for (const obsolete of [undefined, null, 'bad', 0, 65536]) {
  test(`obsolete backend port ${String(obsolete)} cannot override the URL`, () => {
    assert.equal(normalizeSshTunnel({ ...target, remotePort: obsolete }, 'http://gateway:8123').remotePort, 8123)
  })
}

for (const url of ['', 'http://', 'ftp://gateway:9119', 'http://gateway:0', 'http://gateway:65536']) {
  test(`invalid backend URL ${url} cannot fall back to the legacy port`, () => {
    assert.throws(() => normalizeSshTunnel(target, url))
  })
}

test('cold load, legacy terminal SSH and URL-only edits use the URL destination', () => {
  const registry = normalizeRegistry({
    primary: 'gateway',
    connections: [
      {
        id: 'gateway',
        kind: 'remote',
        label: 'Gateway',
        url: 'http://gateway:8123',
        authMode: 'oauth',
        terminalSsh: target
      }
    ]
  })

  const loaded = registry.connections.find(c => c.id === 'gateway')!
  assert.equal(loaded.sshTunnel?.remotePort, 8123)

  const edited = normalizeConnectionInput(
    mergeConnectionInput({ id: loaded.id, kind: 'remote', label: 'Renamed', url: 'https://gateway:8443' }, loaded),
    registry
  )

  assert.equal(edited.sshTunnel?.remotePort, 8443)
  assert.equal(edited.sshTunnel?.port, 2222)
  assert.equal(
    normalizeRegistry({ connections: [edited] }).connections.find(c => c.id === loaded.id)?.sshTunnel?.remotePort,
    8443
  )
})

test('the real tunnel pool forwards the current URL port even for raw stale sources', async () => {
  const transports: ReturnType<typeof transport>[] = []

  function transport() {
    return {
      open: vi.fn(async () => {}),
      forward: vi.fn(async () => {}),
      close: vi.fn(async () => {}),
      isAlive: vi.fn(async () => true)
    }
  }

  const createTransport = vi.fn((_config: SshTunnelConfig) => {
    const next = transport()
    transports.push(next)

    return next
  })

  let local = 45000
  const pool = new GatewaySshTunnels({ createTransport, pickPort: async () => ++local, controlDir: '/fixture/control' })

  const source: RegistryConnection = {
    id: 'gateway',
    kind: 'remote',
    label: 'Gateway',
    url: 'http://gateway:8123',
    authMode: 'oauth',
    sshTunnel: { host: 'box', port: 2222, remotePort: 1 }
  }

  try {
    const first = await pool.ensure(source)
    assert.deepEqual(transports[0].forward.mock.calls[0], [45001, 8123, '127.0.0.1'])
    assert.equal(createTransport.mock.calls[0][0].port, 2222)
    const second = await pool.ensure({ ...source, url: 'http://gateway:8223' })
    assert.deepEqual(transports[1].forward.mock.calls[0], [45002, 8223, '127.0.0.1'])
    assert.notEqual(first.generation, second.generation)
    assert.throws(() => pool.assertCurrent(first))
    assert.equal(first.signal.aborted, true)
  } finally {
    await pool.closeAll()
  }
})

test('a zero URL destination refuses before any SSH transport is constructed', async () => {
  const createTransport = vi.fn(() => ({
    open: vi.fn(async () => {}),
    forward: vi.fn(async () => {}),
    close: vi.fn(async () => {}),
    isAlive: vi.fn(async () => true)
  }))

  const pool = new GatewaySshTunnels({ createTransport, pickPort: async () => 45001, controlDir: '/fixture/control' })
  await assert.rejects(
    pool.ensure({
      id: 'bad',
      label: 'Bad',
      kind: 'remote',
      authMode: 'oauth',
      url: 'http://gateway:0',
      sshTunnel: { host: 'box', remotePort: 9119 }
    })
  )
  assert.equal(createTransport.mock.calls.length, 0)
})

test('real draft routing follows the edited URL, reuses identical routing and honors explicit disable', async () => {
  const registry = normalizeRegistry({
    connections: [
      { id: 'saved', kind: 'remote', label: 'Saved', url: 'http://gateway:8123', authMode: 'oauth', sshTunnel: target }
    ]
  })

  const forwards: unknown[][] = []

  const createTransport = vi.fn(() => ({
    open: async () => {},
    forward: async (...args: [number, number, string]) => {
      forwards.push(args)
    },
    close: async () => {},
    isAlive: async () => true
  }))

  let port = 46000
  const pool = new GatewaySshTunnels({ createTransport, pickPort: async () => ++port, controlDir: '/fixture/control' })

  const auth = createNativeAccessTokenCoordinator({
    loadTokens: () => null,
    clearTokens: () => {},
    storeTokens: () => {},
    normalizeBaseUrl: value => value,
    isRefreshAuthRejection: () => false,
    tokenNeedsRefresh: () => false,
    refreshTokens: vi.fn()
  })

  const drafts = new Map<number, RegistryConnection>()

  const runtime = createGatewayTunnelRuntime({
    gatewaySshTunnels: pool,
    tunnelDraftSources: drafts,
    readDesktopConnectionsRegistry: () => registry,
    nativeAccessTokenCoordinator: auth,
    postJsonNoAuth: vi.fn(),
    openExternalUrl: vi.fn(),
    clearReauth: () => {},
    descriptorMeta: () => ({})
  })

  const sender = { id: 8, once: vi.fn() } as never

  try {
    const saved = registry.connections.find(c => c.id === 'saved')!
    const active = await runtime.resolveGatewayTunnelSource(saved)

    const same = await runtime.gatewayTunnelForDraft(
      saved.url!,
      { connectionId: saved.id, sshTunnel: { host: target.host, keyPath: target.keyPath } },
      sender
    )

    assert.equal(same, active)
    const edited = await runtime.gatewayTunnelForDraft('http://gateway:8223', { connectionId: saved.id }, sender)
    assert.equal(edited?.ssh.remotePort, 8223)
    assert.deepEqual(forwards.at(-1), [46002, 8223, '127.0.0.1'])
    assert.equal(pool.forUrl(active.dialBaseUrl), active)
    const count = createTransport.mock.calls.length
    await assert.rejects(runtime.gatewayTunnelForDraft('http://gateway:0', { connectionId: saved.id }, sender))
    assert.equal(createTransport.mock.calls.length, count)
    assert.equal(
      await runtime.gatewayTunnelForDraft(saved.url!, { connectionId: saved.id, sshTunnel: null }, sender),
      undefined
    )
  } finally {
    await pool.closeAll()
  }
})
