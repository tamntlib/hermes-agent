import assert from 'node:assert/strict'

import { test } from 'vitest'

import type { ConnectionRegistry } from './connection-registry'
import { terminalGatewayTarget } from './terminal-gateway-target'

test('a recorded Remote Gateway tab keeps its configured SSH shell rather than becoming shell-less', () => {
  const registry = {
    connections: [{ id: 'vps', kind: 'remote', url: 'http://gateway.example', authMode: 'none',
      sshTunnel: { host: 'vps-alias', remotePort: 8642 } }], primary: 'vps', version: 2
  } as unknown as ConnectionRegistry
  const target = terminalGatewayTarget({ connectionId: 'vps', profile: 'work' }, registry, new Map())

  assert.equal(typeof target, 'object')
  assert.ok(target && typeof target === 'object')
  assert.equal(target.ssh.host, 'vps-alias')
  assert.equal('terminalOnly' in target && target.terminalOnly, true)
  assert.equal('connectionId' in target && target.connectionId, 'vps')
})
