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

/**
 * Sessions visible in one sidebar profile scope.
 *
 * ALL (`__all__`) returns the caller's list unchanged — including the
 * single-profile case, where Grouping → Profile persists that sentinel
 * while grouped rendering stays off. Never filter against `__all__`.
 */
export function filterSessionsByProfileScope(
  sessions: SessionInfo[],
  profileScope: string,
  connection: SidebarConnectionScope = {}
): SessionInfo[] {
  return profileScope === ALL_PROFILES
    ? sessions
    : sessions.filter(session => sessionMatchesProfileScope(session, profileScope, connection))
}
