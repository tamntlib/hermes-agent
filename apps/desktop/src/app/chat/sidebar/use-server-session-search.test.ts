import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import type { HermesConnection } from '@/global'
import { ALL_PROFILES } from '@/store/profile'
import { $connection } from '@/store/session'

import { useServerSessionSearch } from './use-server-session-search'

const hit = { session_id: 'unloaded', model: null, role: null, session_started: 1, snippet: '', source: 'cli' }
const api = vi.fn()

const remoteConnection: HermesConnection = {
  baseUrl: 'http://example.invalid',
  wsUrl: 'ws://example.invalid',
  token: '',
  logs: [],
  isFullscreen: false,
  nativeOverlayWidth: 0,
  windowButtonPosition: null,
  mode: 'remote',
  connectionId: 'vps-a'
}

beforeEach(() => {
  vi.useFakeTimers()
  api.mockReset()
  vi.stubGlobal('hermesDesktop', { api })
  setApiRequestConnection('vps-a')
  $connection.set(remoteConnection)
})
afterEach(() => {
  cleanup()
  setApiRequestConnection(null)
  $connection.set(null)
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

const debounce = () =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(200)
  })

describe('sidebar server search ownership', () => {
  it('suppresses departed hits during the first render of a new scope, including All profiles', async () => {
    api.mockResolvedValue({ results: [hit] })
    const renders: string[][] = []
    renderHook(() => {
      const search = useServerSessionSearch('needle', ALL_PROFILES)
      renders.push(search.serverMatches.map(row => row.session_id))

      return search
    })
    await debounce()
    expect(renders.at(-1)).toEqual(['unloaded'])
    renders.length = 0
    act(() => {
      setApiRequestConnection('vps-b')
      $connection.set({ ...remoteConnection, connectionId: 'vps-b' })
    })
    expect(renders[0]).toEqual([])
  })

  it('stamps unloaded hits with the gateway and profile that served the request', async () => {
    api.mockResolvedValue({ results: [hit] })
    const { result } = renderHook(() => useServerSessionSearch('needle', 'work'))
    await debounce()

    expect(result.current.serverMatches).toEqual([{ ...hit, connection_id: 'vps-a', profile: 'work' }])
    expect(api).toHaveBeenCalledWith(expect.objectContaining({ connectionId: 'vps-a', profile: 'work' }))
  })

  it('reissues the same query after a gateway switch and rejects the departed response', async () => {
    let finishOld!: (value: unknown) => void
    api
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            finishOld = resolve
          })
      )
      .mockResolvedValue({ results: [{ ...hit, session_id: 'new-owner' }] })
    const { result } = renderHook(() => useServerSessionSearch('needle', 'default'))
    await debounce()
    act(() => {
      setApiRequestConnection('vps-b')
      $connection.set({ ...remoteConnection, connectionId: 'vps-b' })
    })
    await debounce()
    await act(async () => finishOld({ results: [{ ...hit, session_id: 'departed-owner' }] }))

    expect(api).toHaveBeenCalledTimes(2)
    expect(result.current.serverMatches).toEqual([
      { ...hit, session_id: 'new-owner', connection_id: 'vps-b', profile: 'default' }
    ])
    expect(result.current.searchPending).toBe(false)
  })
})
