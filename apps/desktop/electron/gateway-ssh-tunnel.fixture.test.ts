import assert from 'node:assert/strict'
import { type ChildProcess, execFileSync, spawn } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'
import { WebSocket, WebSocketServer } from 'ws'

import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { pickLocalPort, SshConnection } from './ssh-connection'

// A real disposable OpenSSH listener and an authenticated HTTP/WS fixture.
// No Hermes service, existing key or user's SSH configuration participates.
test.runIf(process.platform === 'darwin')(
  'real loopback SSH forward preserves HTTP auth, one-use tickets and shared ownership',
  async () => {
    const root = fs.mkdtempSync('/private/tmp/hermes-gateway-fixture-')
    const user = os.userInfo().username
    let sshd: ChildProcess | undefined
    let tunnels: GatewaySshTunnels | undefined
    const token = crypto.randomBytes(24).toString('hex')
    const tickets = new Set<string>()

    const backend = http.createServer((request, response) => {
      response.setHeader('Content-Type', 'application/json')

      if (request.url === '/api/status') {
        response.end(JSON.stringify({ auth_required: true, auth_flows: ['native_pkce'] }))

        return
      }

      if (request.headers.authorization !== `Bearer ${token}`) {
        response.writeHead(401)
        response.end('{}')

        return
      }

      if (request.url === '/api/auth/ws-ticket') {
        const ticket = crypto.randomUUID()
        tickets.add(ticket)
        response.end(JSON.stringify({ ticket }))

        return
      }

      response.end(JSON.stringify({ profile: new URL(request.url!, 'http://fixture').searchParams.get('profile') }))
    })

    const wsServer = new WebSocketServer({ noServer: true })
    backend.on('upgrade', (request, socket, head) => {
      const ticket = new URL(request.url!, 'http://fixture').searchParams.get('ticket') || ''

      if (!tickets.delete(ticket)) {
        socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')

        return
      }

      wsServer.handleUpgrade(request, socket, head, ws => {
        ws.send('fixture-ready')
      })
    })
    const port = (await pickLocalPort()) as number
    let diagnostics = ''

    try {
      for (const name of ['host', 'identity']) {
        execFileSync('/usr/bin/ssh-keygen', ['-t', 'ed25519', '-N', '', '-f', path.join(root, name)], {
          stdio: 'ignore'
        })
      }

      fs.writeFileSync(
        path.join(root, 'sshd_config'),
        [
          'ListenAddress 127.0.0.1',
          `Port ${port}`,
          `HostKey ${root}/host`,
          `AuthorizedKeysFile ${root}/identity.pub`,
          `PidFile ${root}/pid`,
          'StrictModes no',
          'PasswordAuthentication no',
          'KbdInteractiveAuthentication no',
          'UsePAM no',
          `AllowUsers ${user}`,
          'AllowTcpForwarding local',
          'LogLevel ERROR'
        ].join('\n') + '\n'
      )
      sshd = spawn('/usr/sbin/sshd', ['-D', '-e', '-f', path.join(root, 'sshd_config')], {
        stdio: ['ignore', 'ignore', 'pipe']
      })
      sshd.stderr?.on('data', chunk => {
        diagnostics += String(chunk)
      })
      const wrapper = path.join(root, 'ssh')
      fs.writeFileSync(
        wrapper,
        `#!/bin/sh\nexec /usr/bin/ssh -F /dev/null -o UserKnownHostsFile=${root}/known_hosts "$@"\n`,
        { mode: 0o700 }
      )
      await new Promise<void>(resolve => backend.listen(0, '127.0.0.1', resolve))
      const remotePort = (backend.address() as { port: number }).port
      const transports: SshConnection[] = []
      tunnels = new GatewaySshTunnels({
        controlDir: path.join(root, 'control'),
        pickPort: () => pickLocalPort() as Promise<number>,
        createTransport: (config, options) => {
          const ssh = new SshConnection(config, { ...options, sshBinary: wrapper, connectTimeoutMs: 5000 })
          transports.push(ssh)

          return ssh
        }
      })

      const source = {
        id: 'fixture',
        kind: 'remote' as const,
        label: 'Fixture',
        url: `https://identity.invalid:${remotePort}`,
        authMode: 'oauth' as const,
        sshTunnel: { host: '127.0.0.1', user, port, keyPath: path.join(root, 'identity'), remotePort }
      }

      const [a, b] = await Promise.all([tunnels.ensure(source), tunnels.ensure(source)])
      assert.equal(a, b)
      assert.equal(transports.length, 1)
      assert.equal(a.canonicalBaseUrl, source.url)
      const anonymous = await fetch(a.dialBaseUrl + '/api/profiles')
      assert.equal(anonymous.status, 401)
      const headers = { Authorization: `Bearer ${token}` }

      for (const profile of ['default', 'work']) {
        const body = (await (await fetch(`${a.dialBaseUrl}/api/profiles?profile=${profile}`, { headers })).json()) as {
          profile: string
        }

        assert.equal(body.profile, profile)
      }

      const body = (await (await fetch(a.dialBaseUrl + '/api/auth/ws-ticket', { method: 'POST', headers })).json()) as {
        ticket: string
      }

      const wsUrl = a.dialBaseUrl.replace('http:', 'ws:') + '/api/ws?ticket=' + encodeURIComponent(body.ticket)
      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(wsUrl, { origin: 'file://' })
        ws.on('error', reject)
        ws.on('message', message => {
          assert.equal(String(message), 'fixture-ready')
          ws.close()
          resolve()
        })
      })
      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(wsUrl)
        ws.on('open', () => {
          ws.close()
          reject(new Error('Replayed ticket accepted'))
        })
        ws.on('unexpected-response', (_request, response) => {
          assert.equal(response.statusCode, 403)
          response.resume()
          resolve()
        })
        ws.on('error', reject)
      })
      const old = a.dialBaseUrl
      await tunnels.invalidate(source.id)
      assert.throws(() => tunnels!.forUrl(old))
      assert.equal(tunnels.size, 0)
    } catch (error) {
      throw new Error(
        `Real SSH fixture failed: ${error instanceof Error ? error.message : String(error)}; ${diagnostics}`
      )
    } finally {
      await tunnels?.closeAll()

      for (const client of wsServer.clients) {
        client.terminate()
      }

      wsServer.close()
      backend.closeAllConnections()
      await new Promise<void>(resolve => backend.close(() => resolve()))
      sshd?.kill()
      fs.rmSync(root, { recursive: true, force: true })
    }
  },
  30000
)
