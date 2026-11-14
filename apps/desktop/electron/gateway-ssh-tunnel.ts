import crypto from 'node:crypto'
import http from 'node:http'

import { normalizeRemoteBaseUrl } from './connection-config'
import { normalizeSshTunnel, type RegistryConnection, type SshTunnelConfig } from './connection-registry'
import { NativeAuthChangedError } from './native-access-token'

export interface GatewayTunnelTransport {
  open: (options?: { signal?: AbortSignal }) => Promise<unknown>
  forward: (localPort: number, remotePort: number, remoteHost: string) => Promise<unknown>
  close: () => Promise<unknown>
  isAlive: () => Promise<boolean>
}

export interface GatewayTunnelSnapshot {
  connectionId: string
  canonicalBaseUrl: string
  dialBaseUrl: string
  generation: string
  fingerprint: string
  ssh: SshTunnelConfig
  signal: AbortSignal
  agent: http.Agent
}

interface TunnelOwner {
  snapshot: GatewayTunnelSnapshot
  abort: AbortController
  transport: GatewayTunnelTransport
  flight: Promise<GatewayTunnelSnapshot>
  retired: boolean
  checkedAt: number
  checking: boolean
}

export function gatewayTunnelFingerprint(source: RegistryConnection): string {
  return JSON.stringify([
    source.id,
    source.kind,
    source.url,
    source.authMode,
    source.headers ?? null,
    source.sshTunnel ?? null,
    source.sshTunnelError ?? null
  ])
}

export class GatewaySshTunnels {
  get size(): number {
    return this.owners.size
  }

  private shuttingDown = false

  private readonly owners = new Map<string, TunnelOwner>()
  // Retain retired origins as tombstones: a stale descriptor must never acquire
  // permission to send credentials to a recycled loopback port.
  private readonly failures = new Map<
    string,
    { fingerprint: string; retryAt: number; attempts: number; error: unknown }
  >()
  private readonly origins = new Map<string, TunnelOwner>()

  constructor(
    private readonly deps: {
      createTransport: (
        config: SshTunnelConfig,
        options: { scope: string; ownershipId: string; controlDir: string }
      ) => GatewayTunnelTransport
      pickPort: () => Promise<number>
      controlDir: string
      isSourceCurrent?: (connectionId: string, fingerprint: string) => boolean
      probe?: (snapshot: GatewayTunnelSnapshot) => Promise<void>
    }
  ) {}

  ensure(source: RegistryConnection): Promise<GatewayTunnelSnapshot> {
    let ssh: SshTunnelConfig

    try {
      if (this.shuttingDown) {
        throw new Error('SSH Gateway is shutting down.')
      }

      ssh = this.validateSource(source)
    } catch (error) {
      return Promise.reject(error)
    }

    const fingerprint = gatewayTunnelFingerprint(source)
    const failure = this.failures.get(source.id)

    if (failure?.fingerprint === fingerprint && Date.now() < failure.retryAt) {
      return Promise.reject(failure.error)
    }

    const current = this.owners.get(source.id)

    if (current && !current.retired && current.snapshot.fingerprint === fingerprint) {
      if (!current.snapshot.dialBaseUrl || current.checking || Date.now() - current.checkedAt < 5000) {
        return current.flight
      }

      current.checking = true
      current.flight = (async () => {
        const alive = await current.transport.isAlive()
        this.assertCurrent(current.snapshot)
        current.checkedAt = Date.now()
        current.checking = false

        if (alive) {
          return current.snapshot
        }

        await this.retire(current)

        return this.ensure(source)
      })().catch(async error => {
        await this.retire(current)
        throw error
      })

      return current.flight
    }

    if (current) {
      void this.retire(current)
    }

    const abort = new AbortController()
    const generation = crypto.randomUUID()

    const snapshot: GatewayTunnelSnapshot = {
      connectionId: source.id,
      canonicalBaseUrl: normalizeRemoteBaseUrl(source.url),
      dialBaseUrl: '',
      fingerprint,
      generation,
      ssh,
      signal: abort.signal,
      agent: new http.Agent({ keepAlive: true, maxSockets: 50 })
    }

    const transport = this.deps.createTransport(snapshot.ssh, {
      scope: `gateway:${source.id}:${generation}`,
      ownershipId: generation,
      controlDir: this.deps.controlDir
    })

    const owner: TunnelOwner = {
      snapshot,
      abort,
      transport,
      flight: null!,
      retired: false,
      checkedAt: Date.now(),
      checking: false
    }

    this.owners.set(source.id, owner)
    owner.flight = this.open(owner)

    return owner.flight
  }

  private validateSource(source: RegistryConnection): SshTunnelConfig {
    if (source.sshTunnelError) {
      throw new Error(source.sshTunnelError)
    }

    if (source.kind !== 'remote' || !source.sshTunnel) {
      throw new Error('Remote Gateway SSH tunnel is not configured.')
    }

    if (source.authMode !== 'oauth') {
      throw new Error('SSH Gateway requires native sign-in. Select Sign in authentication in Connections.')
    }

    if (Object.keys(source.headers ?? {}).length) {
      throw new Error('SSH Gateway cannot forward proxy headers. Remove extra headers before enabling SSH.')
    }

    const url = new URL(normalizeRemoteBaseUrl(source.url))

    if (url.pathname !== '/') {
      throw new Error('SSH Gateway does not support a canonical URL with a path prefix.')
    }

    return normalizeSshTunnel(source.sshTunnel, source.url!)
  }

  private async open(owner: TunnelOwner): Promise<GatewayTunnelSnapshot> {
    try {
      await owner.transport.open({ signal: owner.snapshot.signal })
      this.assertCurrent(owner.snapshot)
      let forwarded = false

      for (let attempt = 0; attempt < 3; attempt += 1) {
        const port = await this.deps.pickPort()
        this.assertCurrent(owner.snapshot)
        const origin = `http://127.0.0.1:${port}`

        if (this.origins.has(origin)) {
          continue
        }

        try {
          await owner.transport.forward(port, owner.snapshot.ssh.remotePort, '127.0.0.1')
          this.assertCurrent(owner.snapshot)
          owner.snapshot.dialBaseUrl = origin
          this.origins.set(origin, owner)
          forwarded = true

          break
        } catch (error) {
          this.assertCurrent(owner.snapshot)

          if (attempt === 2) {
            throw error
          }
        }
      }

      if (!forwarded) {
        throw new Error('Could not allocate an unused SSH Gateway loopback port.')
      }

      await this.deps.probe?.(owner.snapshot)
      this.assertCurrent(owner.snapshot)
      this.failures.delete(owner.snapshot.connectionId)

      return owner.snapshot
    } catch (error) {
      if (this.owners.get(owner.snapshot.connectionId) === owner && !owner.retired) {
        const attempts = (this.failures.get(owner.snapshot.connectionId)?.attempts ?? 0) + 1
        this.failures.set(owner.snapshot.connectionId, {
          fingerprint: owner.snapshot.fingerprint,
          retryAt: Date.now() + Math.min(30000, 1000 * 2 ** attempts),
          attempts,
          error
        })
      }

      await this.retire(owner)
      throw error
    }
  }

  forUrl(rawUrl: string): GatewayTunnelSnapshot | undefined {
    const owner = this.origins.get(new URL(rawUrl).origin)

    if (!owner) {
      return undefined
    }

    this.assertCurrent(owner.snapshot)

    return owner.snapshot
  }

  forCanonical(rawUrl: string): GatewayTunnelSnapshot | undefined {
    const canonical = normalizeRemoteBaseUrl(rawUrl)

    const matches = [...this.owners.values()].filter(
      owner => !owner.retired && owner.snapshot.canonicalBaseUrl === canonical
    )

    if (matches.length !== 1) {
      return undefined
    }

    this.assertCurrent(matches[0].snapshot)

    return matches[0].snapshot
  }

  assertCurrent(snapshot: GatewayTunnelSnapshot): void {
    const owner = this.owners.get(snapshot.connectionId)

    if (
      !owner ||
      owner.retired ||
      owner.snapshot.generation !== snapshot.generation ||
      snapshot.signal.aborted ||
      (this.deps.isSourceCurrent && !this.deps.isSourceCurrent(snapshot.connectionId, snapshot.fingerprint))
    ) {
      throw new NativeAuthChangedError()
    }
  }

  async isAlive(snapshot: GatewayTunnelSnapshot): Promise<boolean> {
    this.assertCurrent(snapshot)
    const owner = this.owners.get(snapshot.connectionId)!
    const alive = await owner.transport.isAlive()
    this.assertCurrent(snapshot)

    if (!alive) {
      await this.retire(owner)
    }

    return alive
  }

  private async retire(owner: TunnelOwner): Promise<void> {
    owner.retired = true
    owner.abort.abort(new NativeAuthChangedError())
    owner.snapshot.agent.destroy()

    if (this.owners.get(owner.snapshot.connectionId) === owner) {
      this.owners.delete(owner.snapshot.connectionId)
    }

    await owner.transport.close()
  }

  invalidate(connectionId: string): Promise<void> {
    this.failures.delete(connectionId)
    const owner = this.owners.get(connectionId)

    return owner ? this.retire(owner) : Promise.resolve()
  }

  async closeAll(): Promise<void> {
    this.shuttingDown = true
    const owners = [...this.owners.values()]
    await Promise.allSettled(owners.map(owner => this.retire(owner)))
    await Promise.allSettled(owners.map(owner => owner.flight))
  }
}
