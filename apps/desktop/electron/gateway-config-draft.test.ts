import { afterEach, expect, test, vi } from 'vitest'

import { normalizeRegistry } from './connection-registry'
import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { createGatewayTunnelRuntime } from './gateway-tunnel-runtime'
import { createNativeAccessTokenCoordinator } from './native-access-token'
import { runNativeLogin } from './native-oauth-login'

vi.mock('./native-oauth-login', () => ({ runNativeLogin: vi.fn() }))

const saved = {
  id: 'ssh-gateway',
  label: 'SSH Gateway',
  kind: 'remote',
  url: 'https://gateway.invalid',
  authMode: 'oauth',
  sshTunnel: { host: 'fixture.invalid' }
} as const
const direct = { ...saved, id: 'direct', label: 'Direct', url: 'https://direct.invalid', sshTunnel: undefined }

function fixture(connections: unknown[] = [saved, direct]) {
  const registry = normalizeRegistry({ connections })
  const open = vi.fn(async () => {})
  const owner = new GatewaySshTunnels({
    controlDir: '/fixture/control',
    createTransport: () => ({ open, forward: async () => {}, close: async () => {}, isAlive: async () => true }),
    pickPort: async () => 45101
  })
  const storeTokens = vi.fn()
  const runtime = createGatewayTunnelRuntime({
    gatewaySshTunnels: owner,
    tunnelDraftSources: new Map(),
    readDesktopConnectionsRegistry: () => registry,
    nativeAccessTokenCoordinator: createNativeAccessTokenCoordinator({
      loadTokens: () => null,
      clearTokens: vi.fn(),
      storeTokens,
      normalizeBaseUrl: url => url,
      isRefreshAuthRejection: () => false,
      tokenNeedsRefresh: () => false,
      refreshTokens: vi.fn()
    }),
    postJsonNoAuth: vi.fn(),
    openExternalUrl: vi.fn(),
    clearReauth: vi.fn(),
    descriptorMeta: () => ({})
  })
  return { registry, open, owner, runtime, storeTokens, sender: { id: 7, once: vi.fn() } as never }
}

afterEach(() => vi.resetAllMocks())

test.each([
  ['unmatched path at the saved origin', saved.url + '/other', undefined],
  ['a selected direct sibling at the saved origin', saved.url + '/other', { connectionId: direct.id }],
  ['an explicit tunnel-disable edit at the saved origin', saved.url, { connectionId: saved.id, sshTunnel: null }]
] as const)('sign-in refuses %s before invoking either login transport', async (_case, url, draft) => {
  const { runtime, sender, open, storeTokens } = fixture([saved, { ...direct, url: saved.url + '/other' }])
  const loginDirect = vi.fn()
  await expect(runtime.loginGatewayConfig(url, draft, sender, loginDirect)).rejects.toThrow(/requires its SSH tunnel/)
  expect(loginDirect).not.toHaveBeenCalled()
  expect(runNativeLogin).not.toHaveBeenCalled()
  expect(open).not.toHaveBeenCalled()
  expect(storeTokens).not.toHaveBeenCalled()
})

test('ambiguous ingress requires an explicit selection, while selected SSH sign-in stays on its own forward', async () => {
  const { runtime, sender, open, owner, storeTokens } = fixture([saved, { ...direct, url: saved.url }])
  const loginDirect = vi.fn()
  const tokens = { accessToken: 'fixture-access', refreshToken: 'fixture-refresh', expiresAt: 123456, provider: 'basic', userId: 'fixture' }
  vi.mocked(runNativeLogin).mockResolvedValue(tokens)
  try {
    await expect(runtime.loginGatewayConfig(saved.url, undefined, sender, loginDirect)).rejects.toThrow(/Select the SSH Gateway/)
    expect(open).not.toHaveBeenCalled()
    expect(runNativeLogin).not.toHaveBeenCalled()
    await expect(runtime.loginGatewayConfig(saved.url, { connectionId: saved.id }, sender, loginDirect)).resolves.toMatchObject({
      connected: true, connectionId: saved.id, baseUrl: saved.url
    })
    expect(runNativeLogin).toHaveBeenCalledExactlyOnceWith(
      'http://127.0.0.1:45101', expect.anything(), { provider: 'basic' }
    )
    expect(storeTokens).toHaveBeenCalledExactlyOnceWith(saved.url, tokens)
    expect(loginDirect).not.toHaveBeenCalled()
  } finally {
    await owner.closeAll()
  }
})

test('exact normalized ingress uses SSH and an independent origin still reaches the direct login ladder', async () => {
  const { runtime, sender, owner } = fixture()
  const loginDirect = vi.fn(async () => ({ connected: false }))
  vi.mocked(runNativeLogin).mockResolvedValue({ accessToken: 'fixture-access', refreshToken: 'fixture-refresh', expiresAt: 123456, provider: 'basic', userId: 'fixture' })
  try {
    await expect(runtime.loginGatewayConfig('https://GATEWAY.invalid:443/', undefined, sender, loginDirect))
      .resolves.toMatchObject({ connected: true, baseUrl: saved.url })
    expect(loginDirect).not.toHaveBeenCalled()
    await runtime.loginGatewayConfig('https://DIRECT.invalid:443/', { connectionId: direct.id }, sender, loginDirect)
    expect(loginDirect).toHaveBeenCalledExactlyOnceWith(direct.url)
    expect(runNativeLogin).toHaveBeenCalledTimes(1)
  } finally {
    await owner.closeAll()
  }
})

test.each([{ ...saved }, { ...saved, sshTunnel: { host: '' } }])(
  'test of an unsaved explicit disable at a protected origin reports inability instead of testing the stored tunnel',
  async entry => {
    const { registry, runtime, sender, open } = fixture([entry])
    const before = structuredClone(registry)
    const testDirectOrLegacy = vi.fn(async () => ({ ok: true }))
    const testTunnel = vi.fn(async () => ({ ok: true }))
    await expect(runtime.testGatewayConfig(
      { remoteUrl: saved.url, mode: 'remote' }, { connectionId: saved.id, sshTunnel: null }, sender,
      testDirectOrLegacy, testTunnel
    )).rejects.toThrow(/Cannot test this Gateway without SSH/)
    expect(testDirectOrLegacy).not.toHaveBeenCalled()
    expect(testTunnel).not.toHaveBeenCalled()
    expect(open).not.toHaveBeenCalled()
    expect(registry).toEqual(before)
  }
)

test('omitted tunnel intent still tests the saved tunnel rather than the public URL', async () => {
  const { runtime, sender, owner } = fixture()
  const testDirectOrLegacy = vi.fn()
  const testTunnel = vi.fn(async () => ({ ok: true }))
  try {
    await expect(runtime.testGatewayConfig(
      { remoteUrl: saved.url, mode: 'remote' }, { connectionId: saved.id }, sender, testDirectOrLegacy, testTunnel
    )).resolves.toEqual({ ok: true })
    expect(testTunnel).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      connectionId: saved.id, dialBaseUrl: 'http://127.0.0.1:45101'
    }))
    expect(testDirectOrLegacy).not.toHaveBeenCalled()
  } finally {
    await owner.closeAll()
  }
})

test('an explicit disable at a new origin pins the URL and keeps the existing authenticated test', async () => {
  const { runtime, sender, open } = fixture()
  const testDirectOrLegacy = vi.fn(async () => ({ ok: true }))
  const testTunnel = vi.fn()
  const payload = { remoteUrl: 'https://DIRECT.invalid:443/', mode: 'remote', remoteAuthMode: 'oauth' }
  await expect(runtime.testGatewayConfig(payload, { connectionId: saved.id, sshTunnel: null }, sender, testDirectOrLegacy, testTunnel))
    .resolves.toEqual({ ok: true })
  expect(testDirectOrLegacy).toHaveBeenCalledExactlyOnceWith(payload, direct.url)
  expect(testTunnel).not.toHaveBeenCalled()
  expect(open).not.toHaveBeenCalled()
})
