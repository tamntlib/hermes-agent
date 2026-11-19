import { resolvedConnectionId } from './connection-registry'
import { type DesktopRemoteRouteInput, resolveDesktopRemoteRoute } from './desktop-remote-route'
import type { GatewaySshTunnels, GatewayTunnelSnapshot } from './gateway-ssh-tunnel'
import { statusSupportsNativeFlow } from './native-oauth'
import type { PublicJsonRequestOptions } from './public-json-request'
import {
  type RemoteRevalidationCoordinator,
  revalidateRemoteConnection,
  type RevalidateRemoteConnectionOptions
} from './remote-liveness'
import type { WindowConnectionRoute } from './window-connection-route'

/** Verify the tunnel's HTTP path and native-only authentication contract. */
export async function probeGatewayTunnel(
  snapshot: GatewayTunnelSnapshot,
  fetchPublicJson: (url: string, options: PublicJsonRequestOptions) => Promise<any>,
  assertCurrent: (snapshot: GatewayTunnelSnapshot) => void
): Promise<void> {
  const status = await fetchPublicJson(snapshot.dialBaseUrl + '/api/status', { timeoutMs: 8000, freshConnection: true })
  const providers = await fetchPublicJson(snapshot.dialBaseUrl + '/api/auth/providers', { timeoutMs: 8000, freshConnection: true })
  assertCurrent(snapshot)

  if (!status?.auth_required || !statusSupportsNativeFlow(status) || !providers?.providers?.length ||
    !providers.providers.every((provider: { supports_password?: boolean }) => provider.supports_password === true)) {
    throw new Error('SSH Gateway requires a server with native password sign-in. Browser authentication through a public domain is disabled.')
  }
}

/** A retry belongs to the sender's gateway, not another window's foreground route. */
export async function resetWindowGatewayTunnel(
  input: DesktopRemoteRouteInput & { windowRoute: WindowConnectionRoute | null; primaryProfile: string },
  tunnels: Pick<GatewaySshTunnels, 'invalidate'>
): Promise<void> {
  const { registry, windowRoute } = input
  let id: string | undefined | null

  if (windowRoute?.registryScoped) {
    id = windowRoute.connectionId
  } else {
    const route = resolveDesktopRemoteRoute({ ...input, profile: windowRoute?.profile || input.primaryProfile })

    if (route?.kind === 'remote') {
      id = route.source === 'registry' ? route.connectionId : resolvedConnectionId(registry, {
        mode: 'remote', remoteKind: 'url', baseUrl: route.url, authMode: route.authMode,
        headers: route.headers ?? {}, token: route.token
      })
    }
  }

  const source = registry.connections.find(connection => connection.id === id)

  if (source?.sshTunnel || source?.sshTunnelError) {
    await tunnels.invalidate(source.id)
  }
}

/** Remote Gateway SSH tunnels live outside the managed-backend descriptor pool. */
export async function revalidateDesktopRemoteConnections<T extends { baseUrl?: null | string; mode?: null | string; remoteKind?: string }>(
  options: Omit<RevalidateRemoteConnectionOptions<T>, 'connectionPromise'> & {
    connectionPromise: Promise<T> | null
    coordinator: RemoteRevalidationCoordinator
    revalidatePool: () => Promise<unknown>
    revalidateTunnels: () => Promise<unknown>
    teardownSsh: () => Promise<unknown>
  }
) {
  const { connectionPromise, coordinator, revalidatePool, revalidateTunnels, teardownSsh } = options

  if (!connectionPromise) {
    await Promise.all([revalidatePool(), revalidateTunnels()])

    return { ok: true, rebuilt: false }
  }

  return coordinator.run(connectionPromise, async () => {
    const [result] = await Promise.all([
      revalidateRemoteConnection({ ...options, connectionPromise }), revalidatePool(), revalidateTunnels()
    ])

    if (result.rebuilt && (await connectionPromise.catch(() => null))?.remoteKind === 'ssh') {
      await teardownSsh()
    }

    return result
  })
}
