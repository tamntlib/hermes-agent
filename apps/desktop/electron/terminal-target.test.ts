import assert from 'node:assert/strict'

import { test } from 'vitest'

import { normalizeRegistry } from './connection-registry'
import type { DesktopRemoteRoute } from './desktop-remote-route'
import { resolveTerminalTarget } from './terminal-target'

const registry = normalizeRegistry({
  primary: 'b',
  connections: [
    { id: 'a', kind: 'remote', label: 'A', url: 'https://a.test', sshTunnel: { host: 'box-a' } },
    { id: 'b', kind: 'remote', label: 'B', url: 'https://b.test', sshTunnel: { host: 'box-b' } },
    { id: 'old', kind: 'remote', label: 'Old', url: 'https://old.test' },
    { id: 'ssh', kind: 'ssh', label: 'SSH', host: 'managed' },
    { id: 'cloud', kind: 'cloud', label: 'Cloud', url: 'https://cloud.test' }
  ]
})

const unused = () => {
  throw new Error('Must not bootstrap a backend or consult managed SSH')
}

const resolve = (id: string, profile = 'default') =>
  resolveTerminalTarget({
    registry,
    windowRoute: { connectionId: id, registryScoped: true, profile },
    profile,
    legacyRoute: unused,
    managedTarget: unused
  })

test('window source owns terminal target regardless of primary or profile', () => {
  for (const [id, host] of [
    ['a', 'box-a'],
    ['b', 'box-b']
  ]) {
    for (const profile of ['default', 'work']) {
      const target = resolve(id, profile)
      assert.ok(target && target !== 'pending' && target.terminalOnly)
      assert.equal(target.ssh.host, host)
      assert.equal(target.connectionId, id)
      assert.ok(target.routeKey.includes(profile))
    }
  }

  assert.throws(() => resolve('retired'), /no longer exists/)
})

test('old Remote Gateway, local, Cloud and managed SSH retain their destination', () => {
  for (const id of ['old', 'local', 'cloud', 'ssh']) {
    const managed = id === 'ssh' ? { scope: 'managed', ssh: { host: 'managed' } } : null
    assert.equal(
      resolveTerminalTarget({
        registry,
        windowRoute: { connectionId: id, registryScoped: true, profile: 'work' },
        profile: 'work',
        legacyRoute: unused,
        managedTarget: () => managed
      }),
      managed
    )
  }
})

test('legacy routes require unique full identity; corrupt SSH never becomes local', () => {
  const legacy = (route: DesktopRemoteRoute) =>
    resolveTerminalTarget({
      registry,
      windowRoute: null,
      profile: 'default',
      legacyRoute: () => route,
      managedTarget: unused
    })

  const target = legacy({ kind: 'remote', source: 'settings', url: 'https://a.test', authMode: 'token' })
  assert.ok(target && target !== 'pending' && target.terminalOnly)
  assert.equal(target.ssh.host, 'box-a')
  assert.throws(
    () => legacy({ kind: 'remote', source: 'settings', url: 'https://unknown.test', authMode: 'token' }),
    /Cannot identify/
  )

  const corrupt = normalizeRegistry({
    connections: [{ id: 'bad', kind: 'remote', label: 'Bad', url: 'https://bad.test', sshTunnel: { host: '-bad' } }]
  })

  assert.throws(
    () =>
      resolveTerminalTarget({
        registry: corrupt,
        profile: 'default',
        windowRoute: { connectionId: 'bad', registryScoped: true, profile: 'default' },
        legacyRoute: unused,
        managedTarget: unused
      }),
    /Invalid SSH tunnel/
  )
})
