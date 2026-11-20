import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { normalizeRegistry } from './connection-registry'
import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { createGatewayTunnelRuntime } from './gateway-tunnel-runtime'
import { createNativeAccessTokenCoordinator } from './native-access-token'
import { createRemoteGatewayServices } from './remote-gateway-services'

const source = {
  id: 'a',
  label: 'A',
  kind: 'remote' as const,
  url: 'https://identity.invalid',
  authMode: 'oauth' as const,
  sshTunnel: { host: 'box', remotePort: 9119 }
}

test('construction authenticates at canonical identity and publishes only the SSH dial URL', async () => {
  const snapshot: any = {
    connectionId: 'a',
    canonicalBaseUrl: source.url,
    dialBaseUrl: 'http://127.0.0.1:45001',
    generation: 'g'
  }

  const hasNative = vi.fn((_url: string) => true),
    mint = vi.fn(async (_url: string) => 'one-use-fixture-ticket')

  const service = createRemoteGatewayServices({
    readDesktopConnectionsRegistry: () => normalizeRegistry({ connections: [source] }),
    resolveGatewayTunnelSource: async () => snapshot,
    decryptRemoteHeaders: () => ({}),
    mintGatewayWsTicket: mint,
    hasNativeSession: hasNative,
    rememberRemoteWsHeaders: () => {},
    fetchJson: vi.fn(),
    ensureNativeAccessToken: vi.fn(),
    requestWithOauthFallback: vi.fn(),
    fetchJsonViaOauthSession: vi.fn(),
    fetchPublicJson: vi.fn()
  })

  const descriptor = await service.buildRemoteConnection(
    source.url,
    'oauth',
    null,
    'registry:a',
    undefined,
    'url',
    undefined,
    undefined,
    'a'
  )

  // Settings Apply calls the internal preflight without the IPC draft wrapper.
  assert.equal(await service.resolveRemoteTestBaseUrl(source), snapshot.dialBaseUrl)
  assert.equal(await service.resolveRemoteTestBaseUrl({ url: 'https://direct.invalid', authMode: 'oauth' }), 'https://direct.invalid')
  assert.equal(descriptor.baseUrl, snapshot.dialBaseUrl)
  assert.ok('canonicalBaseUrl' in descriptor)
  assert.ok('sshTunnelGeneration' in descriptor)
  assert.equal(descriptor.canonicalBaseUrl, source.url)
  assert.equal(descriptor.authMode, 'oauth')
  assert.equal(descriptor.remoteKind, 'url')
  assert.equal(descriptor.token, null)
  assert.equal(descriptor.sshTunnelGeneration, 'g')
  assert.deepEqual(hasNative.mock.calls[0], [source.url])
  assert.equal(mint.mock.calls[0][0], snapshot.dialBaseUrl)
})

test('draft probe reuses identical owner but target changes remain temporary and cannot replace active source', async () => {
  const registry = normalizeRegistry({ connections: [source] })
  const draftSources = new Map()

  const owner = new GatewaySshTunnels({
    controlDir: '/tmp/test',
    createTransport: () => ({
      open: async () => {},
      forward: async () => {},
      close: async () => {},
      isAlive: async () => true
    }),
    pickPort: (() => {
      let port = 45101

      return async () => port++
    })()
  })

  const auth = createNativeAccessTokenCoordinator({
    loadTokens: () => null,
    clearTokens: () => {},
    storeTokens: () => {},
    normalizeBaseUrl: value => value,
    isRefreshAuthRejection: () => false,
    tokenNeedsRefresh: () => false,
    refreshTokens: vi.fn()
  })

  const runtime = createGatewayTunnelRuntime({
    gatewaySshTunnels: owner,
    tunnelDraftSources: draftSources,
    readDesktopConnectionsRegistry: () => registry,
    nativeAccessTokenCoordinator: auth,
    postJsonNoAuth: vi.fn(),
    openExternalUrl: vi.fn(),
    clearReauth: () => {},
    descriptorMeta: () => ({})
  })

  const sender = { id: 7, once: vi.fn() } as never
  const active = await runtime.resolveGatewayTunnelSource(registry.connections.find(c => c.id === 'a')!)

  const same = await runtime.gatewayTunnelForDraft(
    source.url,
    { connectionId: 'a', sshTunnel: source.sshTunnel },
    sender
  )

  assert.equal(same, active)

  const temporary = await runtime.gatewayTunnelForDraft(
    source.url,
    { connectionId: 'a', sshTunnel: { host: 'other' } },
    sender
  )

  assert.equal(temporary?.connectionId, 'draft:7')
  assert.equal(owner.forUrl(active.dialBaseUrl), active)
  assert.throws(() => runtime.gatewayTunnelRequest(source.url + '/api/profiles'), /not a data or login fallback/)
  await owner.invalidate('draft:7')
  assert.equal(owner.forUrl(active.dialBaseUrl), active)
  await owner.closeAll()
})

test.each(['direct', 'tunnel', 'malformed'] as const)(
  'an unqualified draft does not select a saved SSH Gateway by registry order (%s sibling)',
  async sibling => {
    const other = {
      ...source,
      id: 'b',
      label: 'B',
      url: 'https://IDENTITY.invalid:443/',
      sshTunnel: sibling === 'direct' ? undefined : { host: sibling === 'malformed' ? '' : 'other' }
    }
    let registry = normalizeRegistry({ connections: [other, source] })
    const draftSources = new Map()
    const createTransport = vi.fn(() => ({
      open: async () => {},
      forward: async () => {},
      close: async () => {},
      isAlive: async () => true
    }))
    let port = 45200
    const owner = new GatewaySshTunnels({ controlDir: '/tmp/test', createTransport, pickPort: async () => ++port })
    const runtime = createGatewayTunnelRuntime({
      gatewaySshTunnels: owner,
      tunnelDraftSources: draftSources,
      readDesktopConnectionsRegistry: () => registry,
      nativeAccessTokenCoordinator: createNativeAccessTokenCoordinator({
        loadTokens: () => null,
        clearTokens: () => {},
        storeTokens: () => {},
        normalizeBaseUrl: value => value,
        isRefreshAuthRejection: () => false,
        tokenNeedsRefresh: () => false,
        refreshTokens: vi.fn()
      }),
      postJsonNoAuth: vi.fn(),
      openExternalUrl: vi.fn(),
      clearReauth: () => {},
      descriptorMeta: () => ({})
    })
    const sender = { id: 7, once: vi.fn() } as never

    try {
      for (const connections of [[other, source], [source, other]]) {
        registry = normalizeRegistry({ connections })
        await assert.rejects(runtime.gatewayTunnelForDraft(source.url, undefined, sender), /Select the SSH Gateway/)
        assert.equal(createTransport.mock.calls.length, 0)
        assert.equal(draftSources.size, 0)
      }

      const selected = await runtime.gatewayTunnelForDraft(source.url, { connectionId: source.id }, sender)
      assert.equal(selected?.connectionId, source.id)
      assert.equal(selected.ssh.host, source.sshTunnel.host)
      assert.equal(await runtime.gatewayTunnelForDraft('https://unrelated.invalid', undefined, sender), undefined)
      assert.equal(await runtime.gatewayTunnelForDraft(source.url, { connectionId: source.id, sshTunnel: null }, sender), undefined)
      const draft = await runtime.gatewayTunnelForDraft(source.url, { sshTunnel: source.sshTunnel }, sender)
      assert.equal(draft?.connectionId, 'draft:7')
      assert.equal(owner.forUrl(selected.dialBaseUrl), selected)

      registry = normalizeRegistry({ connections: [source] })
      assert.equal(await runtime.gatewayTunnelForDraft(source.url, undefined, sender), selected)
      await assert.rejects(
        runtime.gatewayTunnelForDraft(source.url, { connectionId: 'retired', label: 'Draft' }, sender),
        /selected connection no longer exists/
      )

      if (sibling === 'malformed') {
        registry = normalizeRegistry({ connections: [other] })
        await assert.rejects(runtime.gatewayTunnelForDraft(source.url, undefined, sender), /Invalid SSH tunnel/)
        const repair = await runtime.gatewayTunnelForDraft(
          source.url,
          { connectionId: other.id, sshTunnel: source.sshTunnel },
          sender
        )
        assert.equal(repair?.connectionId, 'draft:7')
      }
    } finally {
      await owner.closeAll()
    }
  }
)
