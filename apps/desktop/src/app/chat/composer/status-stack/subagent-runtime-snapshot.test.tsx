import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import * as gateway from '@/store/gateway'
import {
  $activeSessionId,
  $selectedStoredSessionId,
  _resetSessionOwnerHintsForTests,
  setSessionOwnerHint
} from '@/store/session'
import * as sessionStates from '@/store/session-states'
import { $sessionStates, $sessionTiles } from '@/store/session-states'
import { $subagentsBySession, upsertSubagent } from '@/store/subagents'

import { useSubagentSnapshot } from './use-subagent-snapshot'

const request = vi.fn()

function Harness() {
  useSubagentSnapshot('old-runtime')

  return null
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(globalThis.document, 'visibilityState', 'get').mockReturnValue('visible')
  vi.spyOn(gateway, 'requestGatewayForAgent').mockImplementation(request)
  request.mockReset().mockRejectedValue(new Error('session not found or not owned by this transport'))
  $sessionTiles.set([])
  $sessionStates.set({ 'old-runtime': createClientSessionState('stored-parent') })
  $activeSessionId.set('new-runtime')
  $selectedStoredSessionId.set('stored-parent')
  setSessionOwnerHint('stored-parent', { connectionId: 'remote', profile: 'work' })
  $subagentsBySession.set({})
  upsertSubagent('old-runtime', { subagent_id: 'finished', status: 'running' })
})

afterEach(() => {
  cleanup()
  $subagentsBySession.set({})
  $sessionStates.set({})
  $sessionTiles.set([])
  $activeSessionId.set(null)
  $selectedStoredSessionId.set(null)
  _resetSessionOwnerHintsForTests()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it.each(['main', 'tile'])(
  'uses the rebound %s runtime to retire stale rows without duplicating new workers',
  async surface => {
    if (surface === 'tile') {
      $activeSessionId.set('other-runtime')
      $selectedStoredSessionId.set('other-parent')
      $sessionTiles.set([{ storedSessionId: 'stored-parent', runtimeId: 'new-runtime' }])
    }

    upsertSubagent('old-runtime', { subagent_id: 'survivor', status: 'running' })
    upsertSubagent('old-runtime', { subagent_id: 'already-mirrored', status: 'running' })
    upsertSubagent('new-runtime', { subagent_id: 'already-mirrored', status: 'running' })
    upsertSubagent('new-runtime', { subagent_id: 'new-worker', status: 'running' })
    request.mockImplementation(async (_connection, _profile, _method, params) => {
      if (params.session_id !== 'new-runtime') {
        throw new Error('session not found or not owned by this transport')
      }

      return {
        subagents: ['survivor', 'already-mirrored', 'new-worker'].map(subagent_id => ({
          subagent_id,
          status: 'running'
        }))
      }
    })
    render(<Harness />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(request).toHaveBeenCalledWith('remote', 'work', 'subagent.list', { session_id: 'new-runtime' })
    expect($subagentsBySession.get()['old-runtime'].map(x => x.id)).toEqual(['survivor'])
    expect($subagentsBySession.get()['new-runtime'].map(x => x.id)).toEqual(['already-mirrored', 'new-worker'])
  }
)

it('rejects a snapshot from a superseded runtime and retries the new binding after three failures', async () => {
  $activeSessionId.set('old-runtime')
  render(<Harness />)
  await act(async () => {
    await vi.advanceTimersByTimeAsync(15_000)
  })
  expect(request).toHaveBeenCalledTimes(3)
  let resolve!: (value: { subagents: [] }) => void
  request.mockImplementationOnce(
    () =>
      new Promise(r => {
        resolve = r
      })
  )
  $activeSessionId.set('new-runtime')
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000)
  })
  expect(request).toHaveBeenCalledTimes(4)
  $activeSessionId.set('third-runtime')
  await act(async () => {
    resolve({ subagents: [] })
  })
  expect($subagentsBySession.get()['old-runtime'][0].status).toBe('running')
  request.mockResolvedValue({ subagents: [] })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000)
  })
  expect($subagentsBySession.get()['old-runtime']).toEqual([])
})

it.each(['different-parent', 'different-owner'])(
  'does not use a %s snapshot to remove an old worker',
  async mismatch => {
    if (mismatch === 'different-parent') {
      $selectedStoredSessionId.set('unrelated-parent')
    } else {
      const original = sessionStates.knownOwnerForSession
      vi.spyOn(sessionStates, 'knownOwnerForSession').mockImplementation(sid =>
        sid === 'new-runtime' ? { connectionId: 'another-remote', profile: 'work' } : original(sid)
      )
    }

    render(<Harness />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(request).toHaveBeenCalledWith('remote', 'work', 'subagent.list', { session_id: 'old-runtime' })
    expect($subagentsBySession.get()['old-runtime'][0].status).toBe('running')
  }
)
