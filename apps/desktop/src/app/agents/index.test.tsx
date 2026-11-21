import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { PaneVisibleContext } from '@/components/pane-shell/pane-visibility'
import * as gateway from '@/store/gateway'
import { $activeSessionId, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $subagentsBySession, upsertSubagent } from '@/store/subagents'

import { AgentsView } from './index'

Element.prototype.animate = vi.fn(() => ({ cancel() {} }) as Animation)

const request = vi.fn()

const panel = (visible = true) => (
  <PaneVisibleContext.Provider value={visible}>
    <AgentsView onClose={() => undefined} />
  </PaneVisibleContext.Provider>
)

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(globalThis.document, 'visibilityState', 'get').mockReturnValue('visible')
  vi.spyOn(gateway, 'requestGatewayForAgent').mockImplementation(request)
  request.mockReset()
  $subagentsBySession.set({})
  $activeSessionId.set('foreground')
  setSessionOwnerHint('background', { connectionId: 'remote-a', profile: 'research' })
  setSessionOwnerHint('live-owner', { connectionId: 'remote-b', profile: 'work' })
})

afterEach(() => {
  cleanup()
  $subagentsBySession.set({})
  $activeSessionId.set(null)
  _resetSessionOwnerHintsForTests()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('reconciles background workers through their owners only while the Spawn tree is visible', async () => {
  upsertSubagent('background', { subagent_id: 'ended', goal: 'Finished while asleep', status: 'running' })
  upsertSubagent('live-owner', { subagent_id: 'live', goal: 'Still working', status: 'running' })
  upsertSubagent('live-owner', { subagent_id: 'queued', goal: 'Still queued', status: 'queued' })
  upsertSubagent('finished-owner', { subagent_id: 'done', status: 'completed' })
  request.mockImplementation(async (_connection, _profile, _method, params) => ({
    subagents:
      params.session_id === 'live-owner'
        ? [
            { subagent_id: 'live', status: 'running' },
            { subagent_id: 'queued', status: 'queued' }
          ]
        : []
  }))

  const view = render(panel(false))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000)
  })
  expect(request).not.toHaveBeenCalled()

  view.rerender(panel())
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(request).toHaveBeenCalledTimes(2)
  expect(request).toHaveBeenCalledWith('remote-a', 'research', 'subagent.list', { session_id: 'background' })
  expect(request).toHaveBeenCalledWith('remote-b', 'work', 'subagent.list', { session_id: 'live-owner' })
  expect(screen.queryByText('Finished while asleep')).toBeNull()
  expect(screen.getByText('Still working')).toBeTruthy()
  expect(screen.getByText('Still queued')).toBeTruthy()
  expect($subagentsBySession.get()['finished-owner'][0].status).toBe('completed')

  view.rerender(panel(false))
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000)
  })
  expect(request).toHaveBeenCalledTimes(2)

  view.rerender(panel())
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  request.mockResolvedValue({ subagents: [] })
  await act(async () => {
    await vi.advanceTimersByTimeAsync(5_000)
  })
  expect(screen.queryByText('Still working')).toBeNull()
  expect(screen.queryByText('Still queued')).toBeNull()
  const settledCalls = request.mock.calls.length
  await act(async () => {
    await vi.advanceTimersByTimeAsync(10_000)
  })
  expect(request).toHaveBeenCalledTimes(settledCalls)
})

it('keeps cached workers on request failure and ignores a snapshot after the panel closes', async () => {
  upsertSubagent('background', { subagent_id: 'worker', goal: 'Unconfirmed worker', status: 'running' })
  request.mockRejectedValueOnce(new Error('Network unavailable'))
  const view = render(panel())
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
  expect(request).toHaveBeenCalledOnce()
  expect(screen.getByText('Unconfirmed worker')).toBeTruthy()

  let resolve!: (value: { subagents: [] }) => void
  request.mockImplementationOnce(
    () =>
      new Promise(r => {
        resolve = r
      })
  )
  await act(async () => {
    window.dispatchEvent(new Event('focus'))
  })
  expect(request).toHaveBeenCalledTimes(2)
  view.unmount()
  await act(async () => {
    resolve({ subagents: [] })
    await vi.advanceTimersByTimeAsync(10_000)
  })
  expect($subagentsBySession.get().background[0].status).toBe('running')
  expect(request).toHaveBeenCalledTimes(2)
})
