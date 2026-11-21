import assert from 'node:assert/strict'
import http from 'node:http'
import net from 'node:net'

import { test } from 'vitest'

import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { probeGatewayTunnel } from './gateway-tunnel-recovery'
import { createPublicJsonFetcher } from './public-json-request'

// net.Server.close() stops accepts without http.Server's automatic idle-socket cleanup.
function fixture() {
  const sockets = new Set<net.Socket>()
  let accepted = 0

  const server = net.createServer(socket => {
    accepted += 1
    sockets.add(socket)
    socket.on('close', () => sockets.delete(socket))
    let pending = ''
    socket.on('data', chunk => {
      pending += chunk.toString()
      let end: number

      while ((end = pending.indexOf('\r\n\r\n')) >= 0) {
        const request = pending.slice(0, end)
        pending = pending.slice(end + 4)

        const body = JSON.stringify(request.startsWith('GET /api/auth/providers ')
          ? { providers: [{ supports_password: true }] }
          : { auth_required: true, auth_flows: ['native_pkce'] })

        socket.write(`HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(body)}\r\nConnection: keep-alive\r\n\r\n${body}`)
      }
    })
  })

  return {
    server, sockets, accepted: () => accepted,
    listen: (port = 0): Promise<number> => new Promise((resolve, reject) => {
      server.once('error', reject)
      server.listen(port, '127.0.0.1', () => {
        server.removeListener('error', reject)
        resolve((server.address() as net.AddressInfo).port)
      })
    }),
    close: async () => {
      for (const socket of sockets) { socket.destroy() }

      if (server.listening) { await new Promise<void>(resolve => server.close(() => resolve())) }
    }
  }
}

test('production HTTP probe replaces a dead listener even while pooled keep-alive requests still succeed', async () => {
  const fixtures: ReturnType<typeof fixture>[] = []
  let publicJson!: ReturnType<typeof createPublicJsonFetcher>

  const tunnels = new GatewaySshTunnels({
    controlDir: '/synthetic',
    pickPort: async () => {
      const reservation = fixture()
      const port = await reservation.listen()
      await reservation.close()

      return port
    },
    createTransport: () => {
      const transport = fixture()
      fixtures.push(transport)

      return { open: async () => {}, forward: async port => { await transport.listen(port) }, close: transport.close, isAlive: async () => true }
    },
    probe: snapshot => probeGatewayTunnel(snapshot, publicJson, value => tunnels.assertCurrent(value))
  })

  publicJson = createPublicJsonFetcher({
    tunnelForUrl: url => tunnels.forUrl(url), assertCurrent: snapshot => tunnels.assertCurrent(snapshot), headersForUrl: () => ({})
  })

  try {
    const source = { id: 'remote', kind: 'remote' as const, label: 'Remote', url: 'http://identity.invalid:9119', authMode: 'oauth' as const,
      sshTunnel: { host: 'ssh.example', remotePort: 9119 } }

    const old = await tunnels.ensure(source)
    // Warm the ordinary production request pool separately from the liveness probe.
    await publicJson(old.dialBaseUrl + '/api/status')
    await new Promise(setImmediate)
    const oldSocket = fixtures[0].sockets.values().next().value!
    const accepts = fixtures[0].accepted()
    fixtures[0].server.close()
    assert.equal(fixtures[0].server.listening, false)

    const freshRequest = new Promise((resolve, reject) => {
      http.get(old.dialBaseUrl + '/api/status', { agent: false }, resolve).on('error', reject)
    })

    await assert.rejects(freshRequest, { code: 'ECONNREFUSED' })
    assert.equal((await publicJson(old.dialBaseUrl + '/api/status')).auth_required, true)
    assert.equal(fixtures[0].accepted(), accepts)
    assert.equal(oldSocket.destroyed, false)
    await tunnels.revalidate(() => {})
    const recovered = await tunnels.ensure(source)
    assert.notEqual(recovered.generation, old.generation)
    assert.equal(fixtures.length, 2)
    assert.equal(old.signal.aborted, true)
    assert.equal((await publicJson(recovered.dialBaseUrl + '/api/status')).auth_required, true)
  } finally {
    await tunnels.closeAll()
    await Promise.all(fixtures.map(item => item.close()))
  }
})
