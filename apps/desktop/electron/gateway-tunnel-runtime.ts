import { hostLabelFromBaseUrl, normalizeRemoteBaseUrl } from './connection-config'
import { type ConnectionRegistry, normalizeSshTunnel, type RegistryConnection } from './connection-registry'
import { type GatewaySshTunnels, gatewayTunnelFingerprint, type GatewayTunnelSnapshot } from './gateway-ssh-tunnel'
import { type createNativeAccessTokenCoordinator, NativeAuthChangedError } from './native-access-token'
import type { NativeAccessTokenOptions } from './native-access-token'
import { runNativeLogin } from './native-oauth-login'
import { requestWithOauthFallback as requestWithDirectOauthFallback } from './oauth-rest-request'

export function createGatewayTunnelRuntime({
  gatewaySshTunnels,
  tunnelDraftSources,
  readDesktopConnectionsRegistry,
  nativeAccessTokenCoordinator,
  postJsonNoAuth,
  openExternalUrl,
  clearReauth,
  descriptorMeta
}: {
  gatewaySshTunnels: GatewaySshTunnels
  tunnelDraftSources: Map<number, RegistryConnection>
  readDesktopConnectionsRegistry: () => ConnectionRegistry
  nativeAccessTokenCoordinator: ReturnType<typeof createNativeAccessTokenCoordinator>
  postJsonNoAuth: (url: string, body: unknown, options?: any) => Promise<any>
  openExternalUrl: (url: string) => Promise<any>
  clearReauth: () => void
  descriptorMeta: () => any
}) {
  function gatewayTunnelRequest(url: string): GatewayTunnelSnapshot | undefined {
    const snapshot = gatewaySshTunnels.forUrl(url.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:'))

    if (snapshot) {
      return snapshot
    }

    const origin = new URL(url).origin

    const source = readDesktopConnectionsRegistry().connections.find(
      connection =>
        connection.kind === 'remote' &&
        (connection.sshTunnel || connection.sshTunnelError) &&
        connection.url &&
        (() => {
          try {
            return new URL(connection.url).origin === origin
          } catch {
            return false
          }
        })()
    )

    if (source) {
      throw new Error(
        source.sshTunnelError || 'This Gateway requires its SSH tunnel. The public URL is not a data or login fallback.'
      )
    }

    return undefined
  }

  async function resolveGatewayTunnelSource(source: RegistryConnection) {
    const snapshot = await gatewaySshTunnels.ensure(source)
    gatewaySshTunnels.assertCurrent(snapshot)

    return snapshot
  }

  async function gatewayTunnelForDraft(
    rawUrl: string,
    rawOpts: any,
    sender: Electron.WebContents
  ): Promise<GatewayTunnelSnapshot | undefined> {
    if (!rawUrl) {
      return undefined
    }

    const registry = readDesktopConnectionsRegistry()
    const canonical = normalizeRemoteBaseUrl(rawUrl)

    const existing = rawOpts?.connectionId
      ? registry.connections.find(connection => connection.id === rawOpts.connectionId)
      : registry.connections.find(connection => connection.kind === 'remote' && connection.url === canonical)

    if (rawOpts?.connectionId && !existing && !rawOpts?.label) {
      throw new Error('The selected connection no longer exists.')
    }

    const provided = rawOpts && Object.prototype.hasOwnProperty.call(rawOpts, 'sshTunnel')
    const rawTunnel = provided ? rawOpts.sshTunnel : existing?.sshTunnel

    if (!rawTunnel) {
      if (existing?.sshTunnelError && !provided) {
        throw new Error(existing.sshTunnelError)
      }

      return undefined
    }

    if (rawOpts?.kind && rawOpts.kind !== 'remote') {
      throw new Error('SSH tunnel is only available for Remote Gateway.')
    }

    const source: RegistryConnection = {
      id: existing?.id || 'draft:' + sender.id,
      kind: 'remote',
      label: existing?.label || 'Draft',
      url: canonical,
      authMode: rawOpts?.authMode || existing?.authMode || 'oauth',
      headers: existing?.headers,
      sshTunnel: normalizeSshTunnel(rawTunnel, canonical)
    }

    if (existing && gatewayTunnelFingerprint(source) === gatewayTunnelFingerprint(existing)) {
      return resolveGatewayTunnelSource(existing)
    }

    source.id = 'draft:' + sender.id
    tunnelDraftSources.set(sender.id, source)
    sender.once('destroyed', () => {
      tunnelDraftSources.delete(sender.id)
      void gatewaySshTunnels.invalidate(source.id)
    })

    return resolveGatewayTunnelSource(source)
  }

  async function signInTunnelGateway(snapshot: GatewayTunnelSnapshot, connectionId: string) {
    const canonical = snapshot.canonicalBaseUrl
    const authIsCurrent = nativeAccessTokenCoordinator.beginLogin(canonical)

    const assertCurrent = () => {
      gatewaySshTunnels.assertCurrent(snapshot)

      if (!authIsCurrent()) {
        throw new NativeAuthChangedError()
      }
    }

    const tokens = await runNativeLogin(
      snapshot.dialBaseUrl,
      {
        signal: snapshot.signal,
        openExternal: async url => {
          assertCurrent()
          const result = await openExternalUrl(url)

          if (result.ok === false) {
            throw new Error('Could not open SSH Gateway sign-in in the system browser.')
          }
        },
        postJson: async (url, body, options) => {
          assertCurrent()
          const result = await postJsonNoAuth(url, body, options)
          assertCurrent()

          return result
        }
      },
      { provider: 'basic' }
    )

    assertCurrent()
    nativeAccessTokenCoordinator.storeTokens(canonical, tokens)
    clearReauth()

    return { ok: true, connected: true, baseUrl: canonical, connectionId }
  }

  async function sshGatewayDescriptor(source: RegistryConnection, profile: string) {
    const tunnel = await resolveGatewayTunnelSource(source)

    return {
      baseUrl: tunnel.dialBaseUrl,
      canonicalBaseUrl: tunnel.canonicalBaseUrl,
      sshTunnelGeneration: tunnel.generation,
      connectionId: source.id,
      mode: 'remote' as const,
      remoteKind: 'url' as const,
      authMode: 'oauth' as const,
      remoteHost: hostLabelFromBaseUrl(tunnel.canonicalBaseUrl),
      profile,
      sharedRemote: true as const,
      headers: {},
      token: null,
      wsUrl: tunnel.dialBaseUrl.replace(/^http:/, 'ws:') + '/api/ws',
      ...descriptorMeta()
    }
  }

  // Return a valid native access token for baseUrl, refreshing via
  // /auth/native/refresh if the stored one is at/near expiry. Returns null when
  // there are no tokens or the refresh is terminally rejected (caller re-logins).
  async function ensureNativeAccessToken(baseUrl: string, options?: NativeAccessTokenOptions): Promise<string | null> {
    const snapshot = gatewayTunnelRequest(baseUrl)

    const token = await nativeAccessTokenCoordinator.ensure(
      snapshot?.canonicalBaseUrl || baseUrl,
      snapshot ? { ...options, transportBaseUrl: snapshot.dialBaseUrl } : options
    )

    if (snapshot) {
      gatewaySshTunnels.assertCurrent(snapshot)
    }

    return token
  }

  function requestWithOauthFallback<T>(baseUrl: string, deps: any): Promise<T> {
    const snapshot = gatewayTunnelRequest(baseUrl)

    return requestWithDirectOauthFallback<T>(snapshot?.canonicalBaseUrl || baseUrl, {
      ...deps,
      ensureNativeAccessToken: () => ensureNativeAccessToken(baseUrl),
      nativeOnly: Boolean(snapshot)
    })
  }

  return {
    requestWithOauthFallback,
    ensureNativeAccessToken,
    gatewayTunnelRequest,
    resolveGatewayTunnelSource,
    gatewayTunnelForDraft,
    signInTunnelGateway,
    sshGatewayDescriptor
  }
}
