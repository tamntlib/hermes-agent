import { backendScopeKey } from '@hermes/shared'
import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { $apiRequestScope, ambientOwnerConnectionId } from '@/api/client'
import { searchSessions } from '@/hermes'
import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'
import { $connection } from '@/store/session'

import type { ScopedSessionSearchResult } from './search-results'

const SEARCH_DEBOUNCE_MS = 200

export interface ServerSessionSearch {
  serverMatches: ScopedSessionSearchResult[]
  searchPending: boolean
}

interface ScopedSearchState extends ServerSessionSearch {
  key: string
}

/**
 * Debounced full-text search across *all* sessions (not just the loaded page)
 * so old sessions stay findable. Hits come from the profile the sidebar shows:
 * an unscoped request searches the primary backend's launch profile, whatever
 * profile is on screen. "All profiles" asks the primary as before.
 */
export function useServerSessionSearch(query: string, profileScope: string): ServerSessionSearch {
  const [search, setSearch] = useState<ScopedSearchState>({ key: '', serverMatches: [], searchPending: false })
  const { connectionId } = useStore($apiRequestScope)
  const connection = useStore($connection)
  const profile = profileScope === ALL_PROFILES ? null : normalizeProfileKey(profileScope)

  // Track both request authority and the displayed source across a gateway re-home.
  const key = JSON.stringify([
    query,
    backendScopeKey(connectionId, profileScope),
    connection?.connectionId ?? null,
    connection?.mode ?? null
  ])

  useEffect(() => {
    setSearch({ key, serverMatches: [], searchPending: Boolean(query) })

    if (!query) {
      return
    }

    let cancelled = false

    const id = window.setTimeout(() => {
      const requestConnectionId = ambientOwnerConnectionId()
      void searchSessions(query, profile)
        .then(res => {
          if (!cancelled) {
            setSearch({
              key,
              searchPending: false,
              serverMatches: res.results.map(result => ({
                ...result,
                connection_id: requestConnectionId,
                profile: profile ?? undefined
              }))
            })
          }
        })
        .catch(() => {
          if (!cancelled) {
            setSearch({ key, serverMatches: [], searchPending: false })
          }
        })
    }, SEARCH_DEBOUNCE_MS)

    return () => {
      cancelled = true
      window.clearTimeout(id)
    }
  }, [query, profile, key])

  // Effects run after render: never paint the previous scope's hits for even one frame.
  return search.key === key ? search : { searchPending: Boolean(query), serverMatches: [] }
}
