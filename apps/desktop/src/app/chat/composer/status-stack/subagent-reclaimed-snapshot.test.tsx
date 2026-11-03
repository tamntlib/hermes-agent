import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import * as gateway from '@/store/gateway'
import { $activeSessionId, $selectedStoredSessionId, _resetSessionOwnerHintsForTests } from '@/store/session'
import {
  $sessionStates,
  $sessionTiles,
  clearAllSessionStates,
  dropSessionState,
  knownOwnerForSession,
  recordSessionEventScope
} from '@/store/session-states'
import { $subagentsBySession, clearSessionSubagents, upsertSubagent } from '@/store/subagents'

import { useSubagentSnapshot } from './use-subagent-snapshot'

const request = vi.fn()
const owner = { connectionId: 'original-remote', profile: 'personal' }
const gone = Object.assign(new Error('session not found or not owned by this transport'), { code: 4001 })

function Harness() {
  useSubagentSnapshot('old-runtime')

  return null
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(globalThis.document, 'visibilityState', 'get').mockReturnValue('visible')
  vi.spyOn(gateway, 'requestGatewayForAgent').mockImplementation(request)
  request.mockReset().mockResolvedValue({ active: [] })
  $subagentsBySession.set({})
  clearAllSessionStates()
  $sessionTiles.set([])
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  $sessionStates.set({ 'old-runtime': createClientSessionState('parent') })
  recordSessionEventScope({ ...owner, session_id: 'old-runtime' })
  upsertSubagent('old-runtime', { subagent_id: 'finished', status: 'running' })
})

afterEach(() => {
  cleanup()
  $subagentsBySession.set({})
  clearAllSessionStates()
  _resetSessionOwnerHintsForTests()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it.each(['drop', 'clear-all'])(
  'reconciles through the original owner after %s, before any panel was mounted',
  async eviction => {
    upsertSubagent('old-runtime', { subagent_id: 'survivor', status: 'running' })
    upsertSubagent('old-runtime', { subagent_id: 'queued', status: 'queued' })
    upsertSubagent('old-runtime', { subagent_id: 'delegate-tool:call:0', status: 'running' })
    upsertSubagent('old-runtime', { subagent_id: 'history', status: 'completed' })

    if (eviction === 'drop') {
      dropSessionState('old-runtime')
    } else {
      clearAllSessionStates()
    }

    expect(knownOwnerForSession('old-runtime')).toBeUndefined()
    // A late progress frame must carry the retained owner to the next row array.
    upsertSubagent('old-runtime', { subagent_id: 'survivor', text: 'Still working' }, false, 'subagent.thinking')
    recordSessionEventScope({ session_id: 'foreign-runtime', connectionId: 'other-remote', profile: 'personal' })
    upsertSubagent('foreign-runtime', { subagent_id: 'finished', status: 'running' })
    request.mockResolvedValue({ active: [{ subagent_id: 'survivor' }, { subagent_id: 'unrelated' }] })
    render(<Harness />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(request).toHaveBeenCalledWith('original-remote', 'personal', 'delegation.status', {})
    expect($subagentsBySession.get()['old-runtime'].map(x => x.id)).toEqual([
      'survivor',
      'queued',
      'delegate-tool:call:0',
      'history'
    ])
    expect($subagentsBySession.get()['foreign-runtime'][0].id).toBe('finished')
    expect($subagentsBySession.get()['old-runtime'][0].stream.at(-1)?.text).toBe('Still working')
  }
)

it('uses the owner registry on 4001 even without a rebound main pane or tile', async () => {
  request.mockImplementation(async (_c, _p, method) => {
    if (method === 'subagent.list') {
      throw gone
    }

    return { active: [] }
  })
  render(<Harness />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(request).toHaveBeenCalledWith('original-remote', 'personal', 'subagent.list', { session_id: 'old-runtime' })
  expect(request).toHaveBeenCalledWith('original-remote', 'personal', 'delegation.status', {})
  expect($subagentsBySession.get()['old-runtime']).toEqual([])
})

it.each(['owner', 'rows', 'unmount'])('rejects a late registry result after %s changes', async change => {
  dropSessionState('old-runtime')
  let resolve!: (value: { active: [] }) => void
  request.mockImplementation(
    () =>
      new Promise(r => {
        resolve = r
      })
  )
  const view = render(<Harness />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(resolve).toBeTypeOf('function')

  if (change === 'owner') {
    recordSessionEventScope({ session_id: 'old-runtime', connectionId: 'other-remote', profile: 'personal' })
  }

  if (change === 'rows') {
    upsertSubagent('old-runtime', { subagent_id: 'new-child', status: 'running' })
  }

  if (change === 'unmount') {
    view.unmount()
  }

  await act(async () => {
    resolve({ active: [] })
  })
  expect($subagentsBySession.get()['old-runtime'][0].id).toBe('finished')
})

it.each([{}, { active: null }, { active: [{}] }])(
  'keeps rows when the registry response is not a complete roster: %j',
  async response => {
    dropSessionState('old-runtime')
    request.mockResolvedValue(response)
    render(<Harness />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(request).toHaveBeenCalledWith('original-remote', 'personal', 'delegation.status', {})
    expect($subagentsBySession.get()['old-runtime'][0].id).toBe('finished')
  }
)

it('does not retain an owner after its subagent bucket is cleared and recreated', async () => {
  dropSessionState('old-runtime')
  clearSessionSubagents('old-runtime')
  upsertSubagent('old-runtime', { subagent_id: 'unknown', status: 'running' })
  render(<Harness />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(request).not.toHaveBeenCalled()
  expect($subagentsBySession.get()['old-runtime'][0].id).toBe('unknown')
})

it('does not turn a transient scoped error into a registry read', async () => {
  request.mockRejectedValue(new Error('Network unavailable'))
  render(<Harness />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(request).toHaveBeenCalledTimes(1)
  expect(request.mock.calls[0][2]).toBe('subagent.list')
  expect($subagentsBySession.get()['old-runtime'][0].id).toBe('finished')
})

it('keeps rows when the retained owner cannot serve delegation.status', async () => {
  dropSessionState('old-runtime')
  request.mockRejectedValue(new Error('Method not found'))
  render(<Harness />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(20_000)
  })
  expect(request).toHaveBeenCalledTimes(3)
  expect($subagentsBySession.get()['old-runtime'][0].id).toBe('finished')
})
