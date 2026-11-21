import type { ConnectionRegistry, SshTunnelConfig } from './connection-registry'
import { matchingConnectionId } from './connection-route-identity'
import type { DesktopRemoteRoute } from './desktop-remote-route'
import type { WindowConnectionRoute } from './window-connection-route'

export interface ManagedTerminalTarget {
  ssh: any
  scope: string
  terminalOnly?: false
}

export interface GatewayTerminalTarget {
  ssh: Omit<SshTunnelConfig, 'remotePort'>
  scope: string
  terminalOnly: true
  connectionId: string
  routeKey: string
}

export type TerminalTarget = GatewayTerminalTarget | ManagedTerminalTarget | null | 'pending'

/** Resolve Terminal independently of Preview and managed backend ownership. */
export function resolveTerminalTarget({
  registry,
  windowRoute,
  legacyRoute,
  profile,
  managedTarget
}: {
  registry: ConnectionRegistry
  windowRoute: WindowConnectionRoute | null
  legacyRoute: () => DesktopRemoteRoute | null
  profile: string
  managedTarget: () => TerminalTarget
}): TerminalTarget {
  let id = windowRoute?.connectionId

  if (!windowRoute?.registryScoped) {
    const route = legacyRoute()

    id = route?.source === 'registry' ? route.connectionId : null

    if (route?.kind === 'remote' && route.source !== 'registry') {
      id = matchingConnectionId(registry, route, 'unique') ?? null
    }

    if (
      route?.kind === 'remote' &&
      !id &&
      registry.connections.some(source => source.kind === 'remote' && (source.sshTunnel || source.sshTunnelError))
    ) {
      throw new Error('Cannot identify the active connection. Select it in Connections before opening Terminal.')
    }
  }

  const source = id ? registry.connections.find(connection => connection.id === id) : null

  if (windowRoute?.registryScoped && !source) {
    throw new Error('The selected connection no longer exists.')
  }

  if (source?.sshTunnelError) {
    throw new Error(source.sshTunnelError)
  }

  if (source?.kind === 'remote' && source.sshTunnel) {
    return {
      ssh: {
        host: source.sshTunnel.host,
        user: source.sshTunnel.user,
        port: source.sshTunnel.port,
        keyPath: source.sshTunnel.keyPath
      },
      scope: `terminal:${source.id}`,
      terminalOnly: true,
      connectionId: source.id,
      routeKey: JSON.stringify([source.id, profile, source.url, source.authMode, source.sshTunnel])
    }
  }

  return managedTarget()
}
