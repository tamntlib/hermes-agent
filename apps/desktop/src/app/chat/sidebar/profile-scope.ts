import { ALL_PROFILES, normalizeProfileKey } from '@/store/profile'
import type { SessionInfo } from '@/types/hermes'

export interface SidebarConnectionScope {
  connectionId?: null | string
  primaryConnectionId?: null | string
}

/** Bare aggregate rows belong to the primary pool, not whichever gateway is selected now. */
export function sessionMatchesProfileScope(
  session: SessionInfo,
  profileScope: string,
  connection: SidebarConnectionScope = {}
): boolean {
  if (profileScope === ALL_PROFILES) {
    return true
  }

  if (normalizeProfileKey(session.profile) !== normalizeProfileKey(profileScope)) {
    return false
  }

  const active = connection.connectionId?.trim()
  const owner = session.connection_id?.trim() || connection.primaryConnectionId?.trim()

  return !active || !owner || active === owner
}

/** Concrete scopes filter by gateway and profile; ALL keeps the original cross-gateway pool. */
export function filterSessionsByProfileScope(
  sessions: SessionInfo[],
  profileScope: string,
  connection: SidebarConnectionScope = {}
): SessionInfo[] {
  return profileScope === ALL_PROFILES
    ? sessions
    : sessions.filter(session => sessionMatchesProfileScope(session, profileScope, connection))
}
