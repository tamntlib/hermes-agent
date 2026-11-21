import { normalizeRemoteBaseUrl, normalizeSshConfig } from './connection-config'
import type { ConnectionInput, ConnectionKind, RegistryConnection } from './connection-registry'
import { validateKeyPath, validateSshTarget } from './ssh-connection'

export interface SshTunnelConfig {
  remotePort: number
  host: string
  user?: string
  port?: number
  keyPath?: string
}

const SSH_TUNNEL_KEYS = new Set(['host', 'user', 'port', 'keyPath', 'remotePort'])

function validateSshTunnelFields(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Invalid SSH tunnel configuration.')
  }

  const input = value as Record<string, unknown>

  if (Object.keys(input).some(key => !SSH_TUNNEL_KEYS.has(key))) {
    throw new Error('Invalid SSH tunnel configuration field.')
  }

  if (
    typeof input.host !== 'string' ||
    ['user', 'keyPath'].some(key => input[key] != null && typeof input[key] !== 'string')
  ) {
    throw new Error('Invalid SSH tunnel configuration.')
  }

  return input
}

function validateSshTunnelPorts(host: string, rawPortValue: unknown, gatewayUrl: unknown): number {
  // Legacy remotePort is compatibility input only; the Gateway URL owns routing.
  const backend = new URL(normalizeRemoteBaseUrl(gatewayUrl))
  const remotePort = Number(backend.port || (backend.protocol === 'https:' ? 443 : 80))

  if (remotePort < 1 || remotePort > 65535) {
    throw new Error('Invalid remote backend port (must be an integer from 1 to 65535).')
  }

  const rawPort = rawPortValue == null ? '' : String(rawPortValue)
  const hostPort = /(?:\]|^[^:]+):(\d+)$/.exec(host.trim())?.[1]

  if (rawPortValue != null && !rawPort) {
    throw new Error('Invalid SSH port.')
  }

  for (const port of [rawPort, hostPort]) {
    if (port && (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535)) {
      throw new Error('Invalid SSH port.')
    }
  }

  return remotePort
}

export function normalizeSshTunnel(value: unknown, gatewayUrl: unknown): SshTunnelConfig {
  const input = validateSshTunnelFields(value)
  const remotePort = validateSshTunnelPorts(input.host as string, input.port, gatewayUrl)
  const ssh = normalizeSshConfig({ ...input, mode: 'ssh' })

  if (!ssh) {
    throw new Error('SSH host is required.')
  }

  validateSshTarget(ssh.host, ssh.user || '', ssh.port ?? 22)
  validateKeyPath(ssh.keyPath)
  const { host, user, port, keyPath } = ssh

  return {
    remotePort,
    host,
    ...(user ? { user } : {}),
    ...(port ? { port } : {}),
    ...(keyPath ? { keyPath } : {})
  }
}

export function assertSshTunnelKind(input: ConnectionInput): void {
  if (input.kind !== 'remote' && input.sshTunnel != null) {
    throw new Error('SSH tunnel is only available for Remote Gateway connections.')
  }
}

export function attachConnectionSshTunnel(
  entry: RegistryConnection,
  sshTunnel: ConnectionInput['sshTunnel'],
  url: string
): void {
  if (entry.kind === 'remote' && sshTunnel != null) {
    entry.sshTunnel = normalizeSshTunnel(sshTunnel, url)
  }
}

function hasStoredSshTunnelIntent(entry: Record<string, unknown>): boolean {
  return (
    entry.sshTunnel != null ||
    entry.terminalSsh != null ||
    Boolean(entry.sshTunnelError) ||
    Boolean(entry.terminalSshError)
  )
}

export function applyStoredSshTunnel(
  entry: Record<string, unknown>,
  kind: ConnectionKind,
  clean: RegistryConnection,
  item: unknown,
  quarantine: (reason: string, entry: unknown) => void,
  safeEntryCopy: (item: unknown) => unknown
): void {
  if (!hasStoredSshTunnelIntent(entry)) {
    return
  }

  const rawTunnel = entry.sshTunnel ?? entry.terminalSsh

  try {
    if (kind !== 'remote') {
      throw new Error('Invalid SSH tunnel connection kind.')
    }

    if ((entry.sshTunnelError || entry.terminalSshError) && rawTunnel == null) {
      throw new Error('Invalid stored SSH tunnel.')
    }

    clean.sshTunnel = normalizeSshTunnel(rawTunnel, entry.url)
  } catch {
    quarantine('entry-invalid-ssh-tunnel', safeEntryCopy(item))
    clean.sshTunnelError = 'Invalid SSH tunnel configuration. Update it in Connections.'
  }
}

/**
 * A quarantined SSH route retains an addressable blocked identity. Otherwise
 * a corrupt primary could silently become local, or a legacy URL could dial
 * directly after the tunnel-bearing entry disappeared during normalization.
 */
export function retainQuarantinedSshRoutes(
  rawConnections: unknown[],
  connections: RegistryConnection[],
  uniqueLabel: (label: string, taken: Iterable<string>) => string
): void {
  for (const item of rawConnections) {
    if (!item || typeof item !== 'object') {
      continue
    }

    const entry = item as Record<string, unknown>

    if (!hasStoredSshTunnelIntent(entry)) {
      continue
    }

    const id = typeof entry.id === 'string' ? entry.id.trim() : ''

    if (!id || connections.some(connection => connection.id === id)) {
      continue
    }

    connections.push({
      id,
      kind: 'remote',
      label: uniqueLabel(
        String(entry.label || 'Invalid SSH Gateway'),
        connections.map(connection => connection.label)
      ),
      url: typeof entry.url === 'string' ? entry.url : undefined,
      sshTunnelError: 'Invalid SSH tunnel connection. Update it in Connections.'
    })
  }
}
