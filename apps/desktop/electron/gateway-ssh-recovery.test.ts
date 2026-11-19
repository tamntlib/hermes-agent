import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'

import { afterEach, test, vi } from 'vitest'

import type { RegistryConnection } from './connection-registry'
import { GatewaySshTunnels, type GatewayTunnelSnapshot } from './gateway-ssh-tunnel'
import { attachPowerResumeRemoteRevalidation } from './remote-liveness'

const source: RegistryConnection = {
  id: 'remote', kind: 'remote', label: 'Remote', url: 'http://backend:9119',
  authMode: 'oauth', sshTunnel: { host: 'ssh.example', remotePort: 9119 }
}

function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })

  return { promise, resolve, reject }
}

function harness(probe: (snapshot: GatewayTunnelSnapshot) => Promise<void> = async () => {}) {
  const transports: Array<{ open: ReturnType<typeof vi.fn>; forward: ReturnType<typeof vi.fn>; close: ReturnType<typeof vi.fn>; isAlive: ReturnType<typeof vi.fn> }> = []
  let port = 40000

  const tunnels = new GatewaySshTunnels({
    createTransport: () => {
      const transport = {
        open: vi.fn(async () => {}), forward: vi.fn(async () => {}),
        close: vi.fn(async () => {}), isAlive: vi.fn(async () => true)
      }

      transports.push(transport)

      return transport
    },
    pickPort: async () => ++port,
    controlDir: '/synthetic',
    probe
  })

  return { tunnels, transports }
}

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks() })

test('a hung forward probe expires and a failed replacement respects retry backoff', async () => {
  vi.useFakeTimers()
  const blocked = deferred()
  let calls = 0

  const { tunnels, transports } = harness(async () => {
    calls += 1

    if (calls === 2) { await blocked.promise }

    if (calls > 2) { throw new Error('backend unavailable') }
  })

  const old = await tunnels.ensure(source)
  await vi.advanceTimersByTimeAsync(6000)
  let finished = false
  const pending = tunnels.ensure(source)

  const outcome = Promise.allSettled([pending]).then(results => { finished = true;

 return results })

  try {
    await vi.advanceTimersByTimeAsync(10001)
    assert.equal(finished, true)
    assert.equal((await outcome)[0].status, 'rejected')
    assert.equal(old.signal.aborted, true)
    assert.equal(transports.length, 2)
    await assert.rejects(tunnels.ensure(source), /backend unavailable/)
    assert.equal(transports.length, 2)
  } finally {
    blocked.resolve()
    await outcome
    await tunnels.closeAll()
  }
})

test('resume and unlock revalidate existing tunnel owners without replacing healthy connections', async () => {
  let dead: string | undefined

  const probe = vi.fn(async (snapshot: GatewayTunnelSnapshot) => {
    if (snapshot.generation === dead) { throw new Error('dead forward') }
  })

  const { tunnels, transports } = harness(probe)
  const monitor = new EventEmitter()
  const log = vi.fn()

  try {
    const old = await tunnels.ensure(source)
    const healthy = await tunnels.ensure({ ...source, id: 'other', url: 'http://other:9119' })
    dead = old.generation
    let sweep: Promise<void> | undefined
    attachPowerResumeRemoteRevalidation({
      powerMonitor: monitor, log,
      revalidate: () => { sweep = tunnels.revalidate(log);

 return sweep }
    })
    monitor.emit('resume')
    monitor.emit('unlock-screen')
    await sweep
    assert.equal(transports.length, 3)
    assert.equal(healthy.signal.aborted, false)
    assert.equal(old.signal.aborted, true)
    assert.equal((await tunnels.ensure(source)).signal.aborted, false)
    assert.equal(probe.mock.calls.length, 5)
  } finally {
    await tunnels.closeAll()
  }
})

test('forced revalidation detects a broken forward inside the ordinary health-cache window', async () => {
  let dead: string | undefined

  const { tunnels, transports } = harness(async snapshot => {
    if (snapshot.generation === dead) { throw new Error('dead forward') }
  })

  try {
    const old = await tunnels.ensure(source)
    dead = old.generation
    const next = await tunnels.ensure(source, { forceCheck: true })
    assert.notEqual(next.generation, old.generation)
    assert.equal(transports.length, 2)
  } finally {
    await tunnels.closeAll()
  }
})

test('all callers join initial forward validation even after the health-cache interval', async () => {
  let now = 100000
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  const validation = deferred()
  const probe = vi.fn(() => validation.promise)
  const { tunnels } = harness(probe)
  const first = tunnels.ensure(source)

  try {
    await vi.waitFor(() => assert.equal(probe.mock.calls.length, 1))
    now += 6000
    const second = tunnels.ensure(source)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    assert.equal(first, second)
    assert.equal(probe.mock.calls.length, 1)
    validation.resolve()
    assert.equal(await first, await second)
  } finally {
    validation.resolve()
    await Promise.allSettled([first])
    await tunnels.closeAll()
  }
})

test('a live SSH master with a dead HTTP forward is replaced once for concurrent callers', async () => {
  let now = 100000
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  let deadGeneration: string | undefined

  const { tunnels, transports } = harness(async snapshot => {
    if (snapshot.generation === deadGeneration) { throw new Error('connect ECONNREFUSED') }
  })

  try {
    const old = await tunnels.ensure(source)
    deadGeneration = old.generation
    now += 5001
    const [personal, work] = await Promise.all([tunnels.ensure(source), tunnels.ensure(source)])
    assert.notEqual(personal.generation, old.generation)
    assert.notEqual(personal.dialBaseUrl, old.dialBaseUrl)
    assert.equal(personal, work)
    assert.equal(transports.length, 2)
    assert.equal(transports[0].close.mock.calls.length, 1)
    assert.equal(old.signal.aborted, true)
    assert.throws(() => tunnels.forUrl(old.dialBaseUrl))
    assert.equal(personal.canonicalBaseUrl, old.canonicalBaseUrl)
  } finally {
    await tunnels.closeAll()
  }
})

test.each(['resolve', 'reject'] as const)('a late probe %s cannot retire a newer edited connection', async completion => {
  const blocked = deferred()
  let oldGeneration: string | undefined

  const { tunnels, transports } = harness(async snapshot => {
    if (snapshot.generation === oldGeneration) { await blocked.promise }
  })

  const old = await tunnels.ensure(source)
  oldGeneration = old.generation
  const checking = tunnels.ensure(source, { forceCheck: true })
  const outcome = Promise.allSettled([checking])

  try {
    await vi.waitFor(() => assert.equal(transports[0].isAlive.mock.calls.length, 1))
    const next = await tunnels.ensure({ ...source, sshTunnel: { ...source.sshTunnel!, host: 'edited' } })

    if (completion === 'resolve') { blocked.resolve() } else { blocked.reject(new Error('late failure')) }
    assert.equal((await outcome)[0].status, 'rejected')
    assert.equal(next.signal.aborted, false)
    assert.equal(transports[1].close.mock.calls.length, 0)
    assert.equal(tunnels.forUrl(next.dialBaseUrl), next)
    assert.equal(transports.length, 2)
  } finally {
    blocked.resolve()
    await outcome
    await tunnels.closeAll()
  }
})

test('shutdown aborts unfinished initial validation and never publishes a descriptor', async () => {
  const blocked = deferred()
  const probe = vi.fn(() => blocked.promise)
  const { tunnels } = harness(probe)
  const opening = tunnels.ensure(source)
  const outcome = Promise.allSettled([opening])

  try {
    await vi.waitFor(() => assert.equal(probe.mock.calls.length, 1))
    await tunnels.closeAll()
    assert.equal((await outcome)[0].status, 'rejected')
    assert.equal(tunnels.size, 0)
    await assert.rejects(tunnels.ensure(source), /shutting down/)
  } finally {
    blocked.resolve()
    await outcome
    await tunnels.closeAll()
  }
})

test('explicit retry clears only its connection cooldown and preserves unrelated tunnels', async () => {
  let unavailable = true

  const { tunnels, transports } = harness(async snapshot => {
    if (snapshot.connectionId === source.id && unavailable) { throw new Error('offline') }
  })

  try {
    const other = await tunnels.ensure({ ...source, id: 'other', url: 'http://other:9119' })
    await assert.rejects(tunnels.ensure(source), /offline/)
    const failedCount = transports.length
    await assert.rejects(tunnels.ensure(source), /offline/)
    assert.equal(transports.length, failedCount)
    unavailable = false
    await tunnels.invalidate(source.id)
    const retried = await tunnels.ensure(source)
    assert.equal(retried.signal.aborted, false)
    assert.equal(other.signal.aborted, false)
    assert.equal(tunnels.forUrl(other.dialBaseUrl), other)
    assert.equal(transports.length, failedCount + 1)
  } finally {
    await tunnels.closeAll()
  }
})

test('invalidation during retiring a failed forward supersedes its pending recovery', async () => {
  let now = 100000
  vi.spyOn(Date, 'now').mockImplementation(() => now)
  const closing = deferred()
  const { tunnels, transports } = harness()
  const old = await tunnels.ensure(source)
  transports[0].isAlive.mockResolvedValue(false)
  transports[0].close.mockImplementation(() => closing.promise)
  now += 6000
  const recovering = tunnels.ensure(source)
  const outcome = Promise.allSettled([recovering])

  try {
    await vi.waitFor(() => assert.equal(transports[0].close.mock.calls.length, 1))
    await tunnels.invalidate(source.id)
    const replacement = await tunnels.ensure({ ...source, sshTunnel: { ...source.sshTunnel!, host: 'changed' } })
    closing.resolve()
    const [result] = await outcome
    assert.equal(result.status, 'rejected')
    assert.equal(transports.length, 2)
    assert.equal(replacement.signal.aborted, false)
    assert.equal(tunnels.forUrl(replacement.dialBaseUrl), replacement)
    assert.equal(old.signal.aborted, true)
  } finally {
    closing.resolve()
    await outcome
    await tunnels.closeAll()
  }
})
