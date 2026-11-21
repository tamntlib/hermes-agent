import assert from 'node:assert/strict'
import http from 'node:http'

import { test } from 'vitest'

import { normalizeRegistry } from './connection-registry'
import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { createGatewayTunnelRuntime } from './gateway-tunnel-runtime'
import { createNativeAccessTokenCoordinator } from './native-access-token'

function listen(server: http.Server, port = 0): Promise<number> {
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', () => {
      server.removeListener('error', reject)
      resolve((server.address() as { port: number }).port)
    })
  })
}

function close(server: http.Server): Promise<void> {
  server.closeAllConnections()

  return new Promise((resolve, reject) => {
    if (!server.listening) { resolve();

 return }

    server.close(error => error ? reject(error) : resolve())
  })
}

test('HTTP listener loss recovers through runtime profile descriptors while the SSH master stays alive', async () => {
  const listeners: http.Server[] = []

  const registry = normalizeRegistry({ connections: [{
    id: 'remote', label: 'Remote', kind: 'remote', url: 'http://identity.invalid:9119',
    authMode: 'oauth', sshTunnel: { host: 'ssh.example', remotePort: 9119 }
  }] })

  const source = registry.connections.find(connection => connection.id === 'remote')!

  const tunnels = new GatewaySshTunnels({
    controlDir: '/synthetic',
    pickPort: async () => {
      const reservation = http.createServer()
      const port = await listen(reservation)
      await close(reservation)

      return port
    },
    createTransport: () => {
      const server = http.createServer((_request, response) => {
        response.setHeader('Content-Type', 'application/json')
        response.end(JSON.stringify({ ok: true }))
      })

      listeners.push(server)

      return { open: async () => {}, forward: async port => { await listen(server, port) }, close: () => close(server), isAlive: async () => true }
    },
    probe: async snapshot => {
      const response = await fetch(snapshot.dialBaseUrl + '/api/status', { signal: snapshot.signal })
      assert.equal(response.status, 200)
      assert.deepEqual(await response.json(), { ok: true })
    }
  })

  const runtime = createGatewayTunnelRuntime({
    gatewaySshTunnels: tunnels, tunnelDraftSources: new Map(), readDesktopConnectionsRegistry: () => registry,
    nativeAccessTokenCoordinator: createNativeAccessTokenCoordinator({
      loadTokens: () => null, clearTokens: () => {}, storeTokens: () => {}, normalizeBaseUrl: value => value,
      isRefreshAuthRejection: () => false, tokenNeedsRefresh: () => false, refreshTokens: async (_url, tokens) => tokens
    }),
    postJsonNoAuth: async () => ({}), openExternalUrl: async () => ({}), clearReauth: () => {}, descriptorMeta: () => ({})
  })

  try {
    const [personal, work] = await Promise.all([runtime.sshGatewayDescriptor(source, 'default'), runtime.sshGatewayDescriptor(source, 'work')])
    assert.equal(personal.sshTunnelGeneration, work.sshTunnelGeneration)
    assert.equal(personal.profile, 'default')
    assert.equal(work.profile, 'work')
    assert.equal(listeners.length, 1)
    await close(listeners[0])
    await assert.rejects(fetch(personal.baseUrl + '/api/status'))
    await tunnels.revalidate(() => {})

    const [recoveredPersonal, recoveredWork] = await Promise.all([
      runtime.sshGatewayDescriptor(source, 'default'), runtime.sshGatewayDescriptor(source, 'work')
    ])

    assert.equal(listeners.length, 2)
    assert.notEqual(recoveredPersonal.sshTunnelGeneration, personal.sshTunnelGeneration)
    assert.equal(recoveredPersonal.sshTunnelGeneration, recoveredWork.sshTunnelGeneration)
    assert.equal(recoveredPersonal.canonicalBaseUrl, personal.canonicalBaseUrl)
    assert.deepEqual(await (await fetch(recoveredPersonal.baseUrl + '/api/status')).json(), { ok: true })
    assert.throws(() => runtime.gatewayTunnelRequest(personal.baseUrl))
    assert.throws(() => runtime.gatewayTunnelRequest(source.url!))
  } finally {
    await tunnels.closeAll()
    await Promise.all(listeners.map(close))
  }
})
