import assert from 'node:assert/strict'

import { beforeEach, test, vi } from 'vitest'

const state = vi.hoisted(() => ({ handles: new Map<string, (...args: any[]) => any>(), spawn: vi.fn() }))
vi.mock('electron', () => ({
  app: { getPath: () => '/tmp', getVersion: () => 'test' },
  ipcMain: {
    handle: (name: string, handler: (...args: any[]) => any) => state.handles.set(name, handler)
  }
}))
vi.mock('node-pty', () => ({ default: { spawn: (...args: unknown[]) => state.spawn(...args) } }))
vi.mock('./spawn-helper-perms', () => ({ ensureSpawnHelperExecutable: () => ({ fixed: [], errors: [] }) }))

import { registerTerminalIpc } from './terminal-ipc'
import type { GatewayTerminalTarget, TerminalTarget } from './terminal-target'

const target = (id: string, host = id): GatewayTerminalTarget => ({
  terminalOnly: true,
  connectionId: id,
  routeKey: JSON.stringify([id, 'work']),
  scope: `terminal:${id}`,
  ssh: { host, user: 'alice', port: 2222, keyPath: "/tmp/it's a key" }
})

function pty() {
  const listeners: { data?: (data: string) => void; exit?: (exit: { exitCode: number; signal?: number }) => void } = {}

  return {
    listeners,
    pid: 4242,
    kill: vi.fn(),
    write: vi.fn(),
    resize: vi.fn(),
    onData: vi.fn((callback: (data: string) => void) => {
      listeners.data = callback
    }),
    onExit: vi.fn((callback: (exit: { exitCode: number; signal?: number }) => void) => {
      listeners.exit = callback
    })
  }
}

function sender(id: number) {
  return { id, isDestroyed: () => false, once: vi.fn(), send: vi.fn() }
}

const call = (name: string, ...args: any[]) => state.handles.get(`hermes:terminal:${name}`)!(...args)

function setup(getTarget: (id: number) => TerminalTarget) {
  const ensureBackend = vi.fn(async () => {
    throw new Error('No backend bootstrap for terminal-only SSH')
  })

  const getState = vi.fn(() => {
    throw new Error('No managed SSH state for terminal-only SSH')
  })

  const api = registerTerminalIpc({
    isWindows: false,
    findOnPath: () => null,
    rememberLog: () => {},
    activeSshTerminalTarget: getTarget,
    sshBinary: () => '/usr/bin/ssh',
    ensureBackend,
    getSshConnectionState: getState,
    gatewayTarget: () => {
      throw new Error('No recorded gateway in sender-routed terminal fixture')
    },
    ensureGatewayBackend: ensureBackend
  })

  return { api, ensureBackend, getState }
}

beforeEach(() => {
  state.handles.clear()
  state.spawn.mockReset()
})

test('IPC spawns independent SSH panes on the sender host with quoted remote cwd and no backend lifecycle', async () => {
  const targets = new Map<number, TerminalTarget>([
    [1, target('a')],
    [2, target('b')]
  ])

  const { api, ensureBackend, getState } = setup(id => targets.get(id) ?? null)

  const a = pty(),
    b = pty(),
    c = pty(),
    sa = sender(1),
    sb = sender(2)

  state.spawn.mockReturnValueOnce(a).mockReturnValueOnce(b).mockReturnValueOnce(c)
  const cwd = "/remote/space and 'quote; $(touch SHOULD_NOT_RUN)"
  const one = await call('start', { sender: sa }, { cwd, cols: 91, rows: 31 })
  const two = await call('start', { sender: sb }, { cwd: '/remote/b' })
  const three = await call('start', { sender: sa }, { cwd: '/remote/a2' })
  assert.equal(one.connectionId, 'a')
  assert.equal(two.connectionId, 'b')
  assert.equal(three.connectionId, 'a')
  assert.equal(call('attach', { sender: sb }, one.id), false)
  assert.equal(call('write', { sender: sb }, one.id, 'bad'), false)
  assert.equal(call('resize', { sender: sb }, one.id, { cols: 2, rows: 2 }), false)
  assert.equal(await call('cwd', { sender: sb }, one.id), null)
  assert.equal(call('dispose', { sender: sb }, one.id), false)
  const [binary, argv, options] = state.spawn.mock.calls[0]
  assert.equal(binary, '/usr/bin/ssh')
  assert.ok(argv.includes('-tt'))
  assert.ok(argv.includes('ControlMaster=no'))
  assert.ok(argv.includes('ControlPath=none'))
  assert.ok(argv.includes('ClearAllForwardings=yes'))
  assert.ok(argv.includes('alice@a'))
  assert.ok(argv.includes('2222'))
  assert.ok(argv.includes("/tmp/it's a key"))
  assert.ok(argv.at(-1).includes("'\\''"))
  assert.equal(options.cwd, '/tmp')
  assert.equal(one.cwd, null)
  assert.equal(one.shell, 'ssh')
  assert.equal(state.spawn.mock.calls[1][1].includes('alice@b'), true)
  assert.equal(ensureBackend.mock.calls.length, 0)
  assert.equal(getState.mock.calls.length, 0)
  call('attach', { sender: sa }, one.id)
  call('write', { sender: sa }, one.id, 'hostname\r')
  call('resize', { sender: sa }, one.id, { cols: 120, rows: 40 })
  assert.deepEqual(a.write.mock.calls[0], ['hostname\r'])
  assert.deepEqual(a.resize.mock.calls[0], [120, 40])
  a.listeners.data!('remote-host\r\n')
  assert.ok(sa.send.mock.calls.some(([, data]) => data === 'remote-host\r\n'))
  call('dispose', { sender: sa }, one.id)
  assert.equal(a.kill.mock.calls.length, 1)
  assert.equal(c.kill.mock.calls.length, 0)
  api.disposeTerminalSessionsForConnection('a')
  assert.equal(c.kill.mock.calls.length, 1)
  assert.equal(b.kill.mock.calls.length, 0)
  assert.equal(call('write', { sender: sa }, three.id, 'x'), false)
  assert.equal(call('write', { sender: sb }, two.id, 'x'), true)
  api.disposeAllTerminalSessions()
  assert.equal(b.kill.mock.calls.length, 1)
})

test('changing, removing or enabling SSH while a start is suspended never spawns a stale or local shell', async () => {
  for (const change of ['host', 'remove', 'enable', 'profile']) {
    let current: TerminalTarget = change === 'enable' ? null : target('a')
    const { api } = setup(() => current)
    const pending = call('start', { sender: sender(1) }, {})
    current =
      change === 'remove' ? null : change === 'profile' ? { ...target('a'), routeKey: 'new-profile' } : target('b')
    await assert.rejects(pending, /Terminal connection changed/)
    assert.equal(state.spawn.mock.calls.length, 0)
    api.disposeAllTerminalSessions()
  }
})

test('SSH spawn errors and failed authentication exit without opening a local shell', async () => {
  const { api, ensureBackend } = setup(() => target('a'))
  state.spawn.mockImplementationOnce(() => {
    throw new Error('SSH binary missing')
  })
  await assert.rejects(call('start', { sender: sender(1) }, {}), /SSH binary missing/)

  const a = pty(),
    sa = sender(1)

  state.spawn.mockReturnValueOnce(a)
  const pane = await call('start', { sender: sa }, {})
  call('attach', { sender: sa }, pane.id)
  a.listeners.data!('Permission denied (publickey).\r\n')
  a.listeners.exit!({ exitCode: 255 })
  assert.ok(sa.send.mock.calls.some(([, data]) => data?.code === 255))
  assert.equal(state.spawn.mock.calls.length, 2)
  assert.equal(ensureBackend.mock.calls.length, 0)
  api.disposeAllTerminalSessions()
})
