import {
  authModeFromStatus,
  buildGatewayWsUrl,
  buildGatewayWsUrlWithTicket,
  hostLabelFromBaseUrl,
  normalizeRemoteBaseUrl
} from './connection-config'
import { type ConnectionRegistry, type RegistryConnection, resolvedConnectionId } from './connection-registry'
import type { GatewayTunnelSnapshot } from './gateway-ssh-tunnel'
import { resolveReadinessProbeAuth } from './native-auth-decisions'
import { resolveRemoteOauthTicket } from './remote-oauth-ticket'

export function createRemoteGatewayServices({
  readDesktopConnectionsRegistry,
  resolveGatewayTunnelSource,
  decryptRemoteHeaders,
  mintGatewayWsTicket,
  hasNativeSession,
  rememberRemoteWsHeaders,
  fetchJson,
  ensureNativeAccessToken,
  requestWithOauthFallback,
  fetchJsonViaOauthSession,
  fetchPublicJson
}: {
  readDesktopConnectionsRegistry: () => ConnectionRegistry
  resolveGatewayTunnelSource: (source: RegistryConnection) => Promise<GatewayTunnelSnapshot>
  decryptRemoteHeaders: (headers: any) => Record<string, string>
  mintGatewayWsTicket: (baseUrl: string, headers?: Record<string, string>) => Promise<string>
  hasNativeSession: (baseUrl: string) => boolean
  rememberRemoteWsHeaders: (url: string, headers?: Record<string, string>) => void
  fetchJson: (url: string, token: any, options?: any) => Promise<any>
  ensureNativeAccessToken: (baseUrl: string, options?: any) => Promise<string | null>
  requestWithOauthFallback: <T>(baseUrl: string, deps: any) => Promise<T>
  fetchJsonViaOauthSession: (url: string, options?: any) => Promise<any>
  fetchPublicJson: (url: string, options?: any) => Promise<any>
}) {
  // Build the readiness probe for a connection's auth mode. A gated gateway
  // must be probed with the SAME credentials the rest of the connection uses:
  // an anonymous probe 401s forever against a live session, and it can never
  // see the 404 that identifies a backend predating /api/health (the auth gate
  // answers before the SPA catch-all). `probeIsCredentialed` tells
  // waitForHermesReady how to read a 401 — rejected session vs gated route.
  async function buildReadinessHealthProbe(baseUrl, authMode, token) {
    if (authMode === 'oauth') {
      return {
        // fetchJson takes the bearer via `options.bearer` — a raw `headers`
        // option is ignored, so passing one here would silently probe
        // uncredentialed and reintroduce the 401 loop.
        probeHealth: (url: string, options: any = {}) =>
          requestWithOauthFallback(baseUrl, {
            ensureNativeAccessToken,
            requestWithBearer: (bearer: string) => fetchJson(url, null, { ...options, bearer }),
            requestWithCookie: () => fetchJsonViaOauthSession(url, options)
          }),
        probeIsCredentialed: true
      }
    }

    const probeAuth = resolveReadinessProbeAuth(authMode, null, token)

    if (probeAuth.kind === 'token' && probeAuth.token) {
      return {
        probeHealth: (url, options: any = {}) => fetchJson(url, probeAuth.token, options),
        probeIsCredentialed: true
      }
    }

    return { probeHealth: fetchPublicJson, probeIsCredentialed: false }
  }

  // Build a remote backend connection descriptor from an already-resolved remote
  // config. Handles both auth models (OAuth ws-ticket vs static session token)
  // and is shared by the per-profile, env, and global resolution paths. `token`
  // is the DECRYPTED static token (or null in OAuth mode). `source` is a label
  // for diagnostics ('profile' | 'env' | 'settings').
  async function buildRemoteConnection(
    rawUrl,
    authMode,
    token,
    source,
    remoteHost?,
    remoteKind = 'url',
    remoteIdentity?,
    headers?,
    connectionId?: string
  ) {
    const sourceConnection = connectionId
      ? readDesktopConnectionsRegistry().connections.find(connection => connection.id === connectionId)
      : null

    if (connectionId && !sourceConnection) {
      throw new Error('The selected connection no longer exists.')
    }

    if (sourceConnection?.sshTunnelError) {
      throw new Error(sourceConnection.sshTunnelError)
    }

    const tunnel = sourceConnection?.sshTunnel ? await resolveGatewayTunnelSource(sourceConnection) : undefined
    const baseUrl = tunnel?.dialBaseUrl || normalizeRemoteBaseUrl(rawUrl)
    const remoteHeaders = decryptRemoteHeaders(headers)
    // For token/oauth remotes the meaningful host is the real backend URL; for
    // SSH remotes the caller passes the entered/resolved host explicitly (the
    // baseUrl is a 127.0.0.1 tunnel and would be useless in the pill).
    const host = remoteHost || hostLabelFromBaseUrl(baseUrl)

    if (authMode === 'oauth') {
      const ticket = await resolveRemoteOauthTicket(baseUrl, remoteHeaders, {
        hasNativeSession: url => hasNativeSession(tunnel?.canonicalBaseUrl || url),
        mintGatewayWsTicket
      })

      const wsUrl = buildGatewayWsUrlWithTicket(baseUrl, ticket)

      rememberRemoteWsHeaders(wsUrl, remoteHeaders)

      return {
        baseUrl,
        ...(tunnel
          ? {
              canonicalBaseUrl: tunnel.canonicalBaseUrl,
              sshTunnelGeneration: tunnel.generation,
              connectionId: tunnel.connectionId
            }
          : {}),
        mode: 'remote',
        source,
        authMode: 'oauth',
        remoteHost: host || undefined,
        remoteIdentity,
        remoteKind,
        headers: remoteHeaders,
        // No static token in OAuth mode; REST is cookie-authed via the partition.
        token: null,
        wsUrl
      }
    }

    if (!token) {
      throw new Error(
        'Remote Hermes gateway is selected, but no session token is saved. ' +
          'Open Settings → Gateway and save a token, or switch back to Local.'
      )
    }

    const wsUrl = buildGatewayWsUrl(baseUrl, token)

    rememberRemoteWsHeaders(wsUrl, remoteHeaders)

    return {
      baseUrl,
      mode: 'remote',
      source,
      authMode: 'token',
      remoteHost: host || undefined,
      remoteIdentity,
      remoteKind,
      headers: remoteHeaders,
      token,
      wsUrl
    }
  }

  async function resolveRemoteTestBaseUrl(block: Pick<RegistryConnection, 'url' | 'authMode' | 'headers' | 'token'>) {
    const registry = readDesktopConnectionsRegistry()

    const id = resolvedConnectionId(registry, { mode: 'remote', remoteKind: 'url', baseUrl: block.url,
      authMode: block.authMode, headers: block.headers ?? {}, token: block.token })

    const source = registry.connections.find(connection => connection.id === id)

    if (source?.sshTunnel || source?.sshTunnelError) {
      return (await resolveGatewayTunnelSource(source)).dialBaseUrl
    }

    return normalizeRemoteBaseUrl(block.url)
  }

  async function probeRemoteAuthMode(rawUrl) {
    // Determine how a remote gateway expects callers to authenticate, WITHOUT
    // sending any credentials. ``/api/status`` is public on every Hermes
    // gateway (it backs the portal liveness probe) and reports:
    //   auth_required: true  → OAuth gate is engaged (cookie + ws-ticket auth)
    //   auth_required: false → loopback/--insecure: legacy session-token auth
    // ``/api/auth/providers`` (also public, only meaningful when gated) gives
    // the human-facing provider name(s) for the login button label.
    //
    // The settings UI calls this as the user types a URL so it can render an
    // OAuth login button vs a session-token entry box. Network/parse failures
    // surface as ``reachable: false`` rather than throwing, so a half-typed or
    // unreachable URL degrades to "can't tell yet" instead of a hard error.
    const baseUrl = normalizeRemoteBaseUrl(rawUrl)

    let status

    try {
      status = await fetchPublicJson(`${baseUrl}/api/status`, { timeoutMs: 8_000 })
    } catch (error: any) {
      return {
        baseUrl,
        reachable: false,
        authMode: 'unknown',
        providers: [],
        version: null,
        error: error instanceof Error ? error.message : String(error)
      }
    }

    const authRequired = authModeFromStatus(status) === 'oauth'
    let providers = []

    if (authRequired) {
      // Best-effort: a gated gateway exposes the registered providers so the
      // button can read "Sign in with Nous Research" instead of a generic
      // label, and so a username/password provider can be distinguished from
      // an OAuth-redirect one (``supports_password``). A failure here doesn't
      // change the auth mode, so swallow it.
      try {
        const body = (await fetchPublicJson(`${baseUrl}/api/auth/providers`, { timeoutMs: 8_000 })) as any

        if (Array.isArray(body?.providers)) {
          providers = body.providers
            .filter(p => p && typeof p === 'object')
            .map(p => ({
              name: String(p.name || ''),
              displayName: String(p.display_name || p.name || ''),
              supportsPassword: Boolean(p.supports_password)
            }))
            .filter(p => p.name)
        }
      } catch {
        // Provider listing is optional metadata; the auth mode is already known.
      }
    }

    return {
      baseUrl,
      reachable: true,
      authMode: authRequired ? 'oauth' : 'token',
      providers,
      version: status?.version || null,
      error: null
    }
  }

  return { buildRemoteConnection, buildReadinessHealthProbe, probeRemoteAuthMode, resolveRemoteTestBaseUrl }
}
