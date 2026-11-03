import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { oauthSessionIsLive } from './native-auth-decisions'
import { mintGatewayWsTicket, requestWithOauthFallback } from './oauth-rest-request'

for (const mode of ['missing', 'refresh-unavailable', 'bearer-rejected']) {
  test(`SSH native-only auth never falls back to cookies: ${mode}`, async () => {
    const cookie = vi.fn(async () => 'cookie')

    const native = async () => {
      if (mode === 'refresh-unavailable') {
        throw new Error('Temporary refresh failure')
      }

      return mode === 'missing' ? null : 'fixture-token'
    }

    await assert.rejects(
      requestWithOauthFallback('https://stable-identity.test', {
        nativeOnly: true,
        ensureNativeAccessToken: native,
        requestWithCookie: cookie,
        requestWithBearer: async () => {
          throw new Error('Bearer rejected')
        }
      })
    )
    assert.equal(cookie.mock.calls.length, 0)
  })
}

test('SSH ticket mint sends bearer to dial URL with no cookie fallback on rejection', async () => {
  const cookie = vi.fn(async () => ({ ticket: 'wrong' }))

  const fetchJson = vi.fn(async () => {
    const error = new Error('rejected') as Error & { statusCode: number }
    error.statusCode = 401
    throw error
  })

  await assert.rejects(
    mintGatewayWsTicket('http://127.0.0.1:45111', {
      nativeOnly: true,
      ensureNativeAccessToken: async () => 'fixture-bearer',
      fetchJson,
      fetchJsonViaOauthSession: cookie
    })
  )
  assert.equal(cookie.mock.calls.length, 0)
  assert.equal(fetchJson.mock.calls.length, 1)
})

// A cookie from the previous direct connection must not hide SSH Sign in.
test('SSH native-only session indicator rejects a surviving direct cookie', () => {
  assert.equal(oauthSessionIsLive(false, true, true), false)
  assert.equal(oauthSessionIsLive(true, false, true), true)
  assert.equal(oauthSessionIsLive(false, true), true)
})
