import assert from 'node:assert/strict'

import { test } from 'vitest'

import { normalizeRegistry } from './connection-registry'
import { resolveDesktopRemoteDialRoute } from './desktop-remote-route'

const tunnel = {
  id: 'tunnel',
  kind: 'remote',
  label: 'SSH Gateway',
  url: 'https://ssh.invalid',
  authMode: 'oauth',
  sshTunnel: { host: 'box' }
}
const env = { url: 'https://direct.invalid', token: 'direct-fixture-token' }

test('an unrelated saved SSH Gateway does not block a direct environment route', () => {
  const registry = normalizeRegistry({ connections: [tunnel] })

  assert.deepEqual(resolveDesktopRemoteDialRoute({ config: { mode: 'local' }, env, registry }), {
    authMode: 'token',
    kind: 'remote',
    source: 'env',
    ...env
  })
})

test('an unrelated malformed tunnel primary does not block a direct per-profile override', () => {
  const registry = normalizeRegistry({
    primary: tunnel.id,
    connections: [{ ...tunnel, sshTunnel: { host: '-invalid' } }]
  })
  assert.ok(registry.connections.find(entry => entry.id === tunnel.id)?.sshTunnelError)

  const route = resolveDesktopRemoteDialRoute({
    config: { mode: 'local', profiles: { work: { mode: 'remote', authMode: 'token', ...env } } },
    profile: 'work',
    registry
  })

  assert.equal(route?.source, 'profile')
  assert.equal(route.kind === 'remote' && route.url, env.url)
  assert.equal(route.connectionId, undefined)
})

test('an unrelated SSH Gateway does not erase the app-global primary for duplicate direct rows', () => {
  const direct = { id: 'direct', kind: 'remote', label: 'Primary', authMode: 'oauth', url: env.url }
  const registry = normalizeRegistry({
    primary: direct.id,
    connections: [direct, { ...direct, id: 'copy', label: 'Copy' }, tunnel]
  })

  const route = resolveDesktopRemoteDialRoute({ config: { mode: 'remote', remote: direct }, registry })

  assert.equal(route?.source, 'settings')
  assert.equal(route.connectionId, direct.id)
})

test('a unique direct registry row does not acquire app-global settings identity unless it is primary', () => {
  const direct = { id: 'direct', kind: 'remote', label: 'Direct', authMode: 'oauth', url: env.url }
  const config = { mode: 'remote', remote: direct }
  const registry = normalizeRegistry({ primary: tunnel.id, connections: [direct, tunnel] })
  const route = resolveDesktopRemoteDialRoute({ config, registry })
  assert.equal(route?.source, 'settings')
  assert.equal(route.connectionId, undefined)

  const withDirectPrimary = normalizeRegistry({ primary: direct.id, connections: [direct, tunnel] })
  assert.equal(resolveDesktopRemoteDialRoute({ config, registry: withDirectPrimary })?.connectionId, direct.id)
})

test('a direct environment route does not acquire an unrelated settings identity', () => {
  const direct = { id: 'direct', kind: 'remote', label: 'Direct', authMode: 'token', ...env }
  const registry = normalizeRegistry({ primary: direct.id, connections: [direct, tunnel] })
  const route = resolveDesktopRemoteDialRoute({ config: { mode: 'local' }, env, registry })
  assert.equal(route?.source, 'env')
  assert.equal(route.connectionId, undefined)
})

test('an exact SSH Gateway match preserves its identity rather than dialing the public URL', () => {
  const registry = normalizeRegistry({ connections: [tunnel] })
  const route = resolveDesktopRemoteDialRoute({
    config: { mode: 'remote', remote: { authMode: 'oauth', url: tunnel.url } },
    registry
  })

  assert.equal(route?.connectionId, tunnel.id)
})

test('ambiguous SSH Gateway candidates at the same destination still require explicit selection', () => {
  const registry = normalizeRegistry({
    connections: [tunnel, { ...tunnel, id: 'other', label: 'Other SSH Gateway', sshTunnel: { host: 'other' } }]
  })

  assert.throws(
    () =>
      resolveDesktopRemoteDialRoute({ config: { mode: 'remote', remote: { authMode: 'oauth', url: tunnel.url } }, registry }),
    /Select the SSH Gateway in Connections/
  )
})

test.each([{ host: 'box' }, { host: '-invalid' }])(
  'a tunnel for $host blocks a nonmatching auth envelope at the same origin',
  sshTunnel => {
    const registry = normalizeRegistry({ connections: [{ ...tunnel, sshTunnel }] })

    assert.throws(
      () => resolveDesktopRemoteDialRoute({
        config: { mode: 'local' },
        env: { ...env, url: 'https://SSH.invalid:443/prefix' },
        registry
      }),
      /Select the SSH Gateway in Connections/
    )
  }
)

test('a malformed selected registry primary with no URL must not silently boot local', () => {
  const registry = normalizeRegistry({
    primary: tunnel.id,
    connections: [{ ...tunnel, url: '' }]
  })

  assert.throws(() => resolveDesktopRemoteDialRoute({ config: { mode: 'local' }, registry }), /Invalid SSH tunnel/)
})
