import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { $activeSessionId, $gatewayState, $selectedStoredSessionId } from '@/store/session'
import { isSessionGoneForBackgroundPolling } from '@/store/session-gone-latch'
import { requestForSessionProfile } from '@/store/session-request-router'
import {
  $sessionTiles,
  knownOwnerForSession,
  requestForOwnedSession,
  storedSessionIdForRuntimeId
} from '@/store/session-states'
import {
  $subagentsBySession,
  reconcileSubagentRegistry,
  reconcileSubagentSnapshot,
  retainedSubagentOwner,
  type SubagentPayload
} from '@/store/subagents'

export const rejectUnownedSubagentRequest = async <T>(): Promise<T> => {
  throw new Error('Subagent owner unavailable')
}

// Resume can replace a runtime while its subagent rows still use the old id.
// Only a bound pane for the same durable session AND owner may supply its roster.
function snapshotRuntimeId(sessionId: string): string {
  const storedId = storedSessionIdForRuntimeId(sessionId)
  const owner = knownOwnerForSession(sessionId)

  if (!storedId || !owner) {
    return sessionId
  }

  const candidates = [
    $selectedStoredSessionId.get() === storedId ? $activeSessionId.get() : null,
    ...$sessionTiles
      .get()
      .filter(tile => tile.storedSessionId === storedId)
      .map(tile => tile.runtimeId)
  ]

  return candidates.find(id => id && JSON.stringify(knownOwnerForSession(id)) === JSON.stringify(owner)) ?? sessionId
}

const snapshotOwner = (sessionId: string) => knownOwnerForSession(sessionId) ?? retainedSubagentOwner(sessionId)

/** Hydrate even an empty composer; live events remain authoritative over reads.
 *  `poll` keeps the 5s safety-net refresh — off when nothing on screen shows
 *  the answer, the one-shot hydrate still lands. */
export function useSubagentSnapshot(sessionId: string | null, poll = true) {
  const gatewayState = useStore($gatewayState)
  const paneVisible = usePaneVisible()
  useEffect(() => {
    if (!sessionId || !paneVisible) {
      // Keep-alive tiles stay mounted; only poll while this pane is the visible tab (reveal re-runs the effect).
      return
    }

    let cancelled = false
    let pending = false
    let failures = 0
    let lastRequestScope = ''

    const refresh = async () => {
      if (cancelled || pending) {
        return
      }

      const runtimeId = snapshotRuntimeId(sessionId)
      const ownerScope = snapshotOwner(sessionId)
      const owner = JSON.stringify(ownerScope)
      const requestScope = JSON.stringify([runtimeId, owner])

      if (requestScope !== lastRequestScope) {
        failures = 0
        lastRequestScope = requestScope
      }

      if (failures >= 3) {
        return
      }

      pending = true
      const before = $subagentsBySession.get()[sessionId]

      try {
        let registry = false
        let snapshot: { active?: unknown; delegations?: SubagentPayload[]; subagents?: SubagentPayload[] }

        try {
          snapshot = await requestForOwnedSession(runtimeId, rejectUnownedSubagentRequest, 'subagent.list', {
            session_id: runtimeId
          })
        } catch (error) {
          if (!ownerScope || (knownOwnerForSession(runtimeId) && !isSessionGoneForBackgroundPolling(error))) {
            throw error
          }

          // A reclaimed runtime cannot serve subagent.list. Its proven owner
          // can still report the live registry without resuming a hidden chat.
          snapshot = await requestForSessionProfile(ownerScope, rejectUnownedSubagentRequest, 'delegation.status')
          registry = true
        }

        if (
          !cancelled &&
          runtimeId === snapshotRuntimeId(sessionId) &&
          owner === JSON.stringify(snapshotOwner(sessionId)) &&
          before === $subagentsBySession.get()[sessionId]
        ) {
          if (registry) {
            reconcileSubagentRegistry(sessionId, snapshot.active)
          } else if (Array.isArray(snapshot.subagents) && runtimeId === sessionId) {
            reconcileSubagentSnapshot(sessionId, snapshot.subagents, snapshot.delegations ?? [])
          } else if (Array.isArray(snapshot.subagents)) {
            // Reconcile only old rows here. New-runtime workers/history belong
            // to their own bucket; copying them would duplicate the global tree.
            const oldIds = new Set(before?.map(item => item.id))
            const currentIds = new Set($subagentsBySession.get()[runtimeId]?.map(item => item.id))
            reconcileSubagentSnapshot(
              sessionId,
              snapshot.subagents.filter(
                item =>
                  typeof item.subagent_id === 'string' &&
                  oldIds.has(item.subagent_id) &&
                  !currentIds.has(item.subagent_id)
              )
            )
          }
        }

        failures = 0
      } catch {
        // Older backends retain their event-fed frame; don't hot-loop a missing RPC.
        failures++
      } finally {
        pending = false
      }
    }

    void refresh()

    if (!poll) {
      return () => {
        cancelled = true
      }
    }

    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void refresh()
      }
    }, 5000)

    const retry = () => {
      failures = 0
      void refresh()
    }

    window.addEventListener('focus', retry)

    return () => {
      cancelled = true
      window.clearInterval(timer)
      window.removeEventListener('focus', retry)
    }
  }, [sessionId, gatewayState, paneVisible, poll])
}
