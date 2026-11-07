import { describe, expect, it } from 'vitest'

import { ALL_PROFILES } from '@/store/profile'
import type { SessionInfo } from '@/types/hermes'

import { filterSessionsByProfileScope } from './profile-scope'

/** Build the smallest session row needed by the profile-scope tests. */
const row = (id: string, profile?: string, connection_id?: string): SessionInfo =>
  ({ id, connection_id, message_count: 1, profile, source: 'signal', started_at: 0, title: id }) as SessionInfo

describe('filterSessionsByProfileScope', () => {
  it.each(['default', 'work'])('isolates the selected gateway for profile %s', profile => {
    const rows = [row('local', profile, 'local'), row('remote', profile, 'vps')]

    expect(
      filterSessionsByProfileScope(rows, profile, { connectionId: 'local', primaryConnectionId: 'vps' }).map(
        session => session.id
      )
    ).toEqual(['local'])
    expect(
      filterSessionsByProfileScope(rows, profile, { connectionId: 'vps', primaryConnectionId: 'vps' }).map(
        session => session.id
      )
    ).toEqual(['remote'])
  })

  it('resolves bare rows to the primary pool, not the selected device', () => {
    const rows = [row('bare'), row('local', 'default', 'local')]

    expect(
      filterSessionsByProfileScope(rows, 'default', { connectionId: 'local', primaryConnectionId: 'vps' }).map(
        session => session.id
      )
    ).toEqual(['local'])
    expect(
      filterSessionsByProfileScope(rows, 'default', { connectionId: 'vps', primaryConnectionId: 'vps' }).map(
        session => session.id
      )
    ).toEqual(['bare'])
  })

  it('keeps legacy profile filtering when connection identity is unknown', () => {
    const rows = [row('bare'), row('remote', 'default', 'vps'), row('work', 'work')]

    expect(filterSessionsByProfileScope(rows, 'default', {}).map(session => session.id)).toEqual(['bare', 'remote'])
    expect(filterSessionsByProfileScope(rows, 'default', { connectionId: 'local' }).map(session => session.id)).toEqual(
      ['bare']
    )
  })

  it('normalizes owner and profile identities while explicit row owners win', () => {
    const rows = [row('local', ' work ', ' local '), row('bare', 'work')]

    expect(
      filterSessionsByProfileScope(rows, ' work ', { connectionId: ' local ', primaryConnectionId: ' vps ' }).map(
        session => session.id
      )
    ).toEqual(['local'])
  })

  it('preserves the cross-gateway array in All profiles', () => {
    const rows = [row('local', 'default', 'local'), row('remote', 'default', 'vps')]

    expect(
      filterSessionsByProfileScope(rows, ALL_PROFILES, { connectionId: 'local', primaryConnectionId: 'vps' })
    ).toBe(rows)
  })

  it('keeps only rows from the selected profile', () => {
    const rows = [row('default-row', 'default'), row('work-row', 'work')]

    expect(filterSessionsByProfileScope(rows, 'work').map(session => session.id)).toEqual(['work-row'])
  })

  it('treats legacy rows without a profile as default', () => {
    const rows = [row('legacy-row'), row('work-row', 'work')]

    expect(filterSessionsByProfileScope(rows, 'default').map(session => session.id)).toEqual(['legacy-row'])
  })

  it('preserves every row in the canonical all-profiles scope', () => {
    const rows = [row('default-row', 'default'), row('work-row', 'work')]

    expect(filterSessionsByProfileScope(rows, ALL_PROFILES)).toBe(rows)
  })

  it('does not empty ALL scope when every row is one profile', () => {
    // Grouping → Profile persists ALL even with one profile. Filtering
    // against the `__all__` sentinel would empty recents and pins.
    const rows = [row('a', 'default'), row('b', 'default'), row('c', 'default')]

    expect(filterSessionsByProfileScope(rows, ALL_PROFILES)).toBe(rows)
  })
})
