import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { normalizeRegistry } from './connection-registry'

const repo = path.resolve(import.meta.dirname, '../../..')
const python = process.env.HERMES_PYTHON || 'python3'

const base = { id: 'remote', kind: 'remote', label: 'Remote', url: 'http://backend:9119', authMode: 'oauth' }
const intents = [
  ...[0, 65536, -1, '0', '65536', ' 22 ', '22 ', '\t22', '２２', '٢٢', '+22', '22.0', [], {}, true]
    .map(port => ({ sshTunnel: { host: 'ssh.example', port } })),
  ...['ssh.example:0', 'ssh.example:99999', 'alice@ssh.example:0', '[::1]:0', '[::1]:99999']
    .flatMap(host => [{ sshTunnel: { host } }, { sshTunnel: { host, port: 2222 } }]),
  ...['sshTunnelError', 'terminalSshError'].flatMap(field =>
    [[], {}, [0], { reason: 'bad' }, true, 1].map(value => ({ [field]: value }))
  ),
  { sshTunnel: false }, { sshTunnel: '' }, { sshTunnel: false, sshTunnelError: 'stored error' },
  { terminalSsh: false }, { sshTunnelError: 'stored error' },
  { sshTunnel: { host: 'ssh.example' }, url: '' },
  { sshTunnel: { host: 'ssh.example' }, kind: 'corrupt' },
  { sshTunnelError: 'stored error', kind: 'corrupt', url: '' },
  { terminalSsh: false, url: '' },
  { sshTunnel: { host: 'ssh.example', unexpected: true } },
  { sshTunnel: { host: 123 } },
  { sshTunnel: { host: 'ssh.example', user: [] } },
  { sshTunnel: { host: 'ssh.example', port: '' } },
  { sshTunnel: { host: 'ssh.example', port: false } }
]

const validTunnels = [
  { host: 'ssh.example' }, { host: 'ssh.example:22', port: 22 },
  { host: 'ssh.example', port: [22] }, { host: 'ssh.example', port: '0022' },
  { host: 'ssh.example:2222', port: 2223 }, { host: 'alice@ssh.example:2222' },
  { host: '[::1]:2222' }, { host: 'ssh alice@ssh.example:2222' }
]
for (const [index, sshTunnel] of validTunnels.entries()) {
  test(`valid SSH login ${index} stays equivalent across Python and Electron`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-tunnel-valid-'))
    try {
      const raw = { connections: [{ ...base, sshTunnel }], primary: 'remote', lastUsed: 'remote' }
      const expected = JSON.parse(JSON.stringify(normalizeRegistry(raw)))
      const output = execFileSync(python, ['-B', '-c', `
import json,sys
from hermes_cli import gateway_connections as store
from hermes_cli.gateway_connections_normalize import normalize_registry
raw=json.loads(sys.stdin.read())
initial=normalize_registry(raw)
store.store_path().write_text(json.dumps(raw))
store.set_launch_mode('last-used')
store.set_launch_mode('primary')
print(json.dumps({'initial':initial,'saved':store.read_registry()}))
`], { cwd: repo, input: JSON.stringify(raw), encoding: 'utf8', env: { ...process.env, HERMES_HOME: dir } })
      const result = JSON.parse(output)
      assert.deepEqual(result.initial, expected)
      assert.deepEqual(normalizeRegistry(result.saved), normalizeRegistry(expected))
      assert.ok(result.saved.connections.find((entry: any) => entry.id === 'remote')?.sshTunnel)
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })
}

for (const [index, intent] of intents.entries()) {
  test(`blocked SSH intent ${index} survives the actual Python shared-store writer`, () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-tunnel-roundtrip-'))
    try {
      const raw = { connections: [{ ...base, ...intent }], primary: 'remote', lastUsed: 'remote' }
      const expected = JSON.parse(JSON.stringify(normalizeRegistry(raw)))
      const output = execFileSync(python, ['-B', '-c', `
import json,sys
from hermes_cli import gateway_connections as store
from hermes_cli.gateway_connections_normalize import normalize_registry
raw=json.loads(sys.stdin.read())
initial=normalize_registry(raw)
store.store_path().write_text(json.dumps(raw))
store.set_launch_mode('last-used')
store.set_launch_mode('primary')
registry=store.read_registry()
def refuse_secret(*args):
    raise AssertionError('Credential access before SSH refusal')
store._secret_of=refuse_secret
try:
    store.dial_snapshot('remote')
except store.ConnectionStoreError as exc:
    assert 'Desktop' in str(exc), str(exc)
else:
    raise AssertionError('Malformed intent allowed a direct route')
print(json.dumps({'initial':initial,'saved':registry}))
`], { cwd: repo, input: JSON.stringify(raw), encoding: 'utf8', env: { ...process.env, HERMES_HOME: dir } })
      const result = JSON.parse(output)
      assert.deepEqual(result.initial, expected)
      assert.equal(result.saved.primary, 'remote')
      assert.equal(result.saved.lastUsed, 'remote')
      const reloaded = normalizeRegistry(result.saved)
      assert.equal(reloaded.primary, 'remote')
      assert.equal(reloaded.lastUsed, 'remote')
      assert.ok(reloaded.connections.find(entry => entry.id === 'remote')?.sshTunnelError)
    } finally {
      fs.rmSync(dir, { recursive: true, force: true })
    }
  })
}
