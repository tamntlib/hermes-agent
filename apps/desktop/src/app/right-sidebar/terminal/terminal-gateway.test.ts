import { beforeEach, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry, DesktopRegistryConnection } from '@/global'

const connection = (
  id: string,
  kind: DesktopRegistryConnection['kind'],
  extra: Partial<DesktopRegistryConnection> = {}
): DesktopRegistryConnection => ({ id, kind, label: id, tokenSet: false, tokenPreview: null, ...extra })

const registry: DesktopConnectionsRegistry = {
  version: 2,
  primary: 'local',
  secureTokenStorage: false,
  connections: [
    connection('local', 'local'),
    connection('managed', 'ssh'),
    connection('tunnel', 'remote', { url: 'https://gateway.example', sshTunnel: { host: 'vps.example' } }),
    connection('invalid', 'remote', { url: 'https://invalid.example', sshTunnelError: 'Invalid SSH tunnel' }),
    connection('plain', 'remote', { url: 'https://plain.example' }),
    connection('cloud', 'cloud', { url: 'https://cloud.example' })
  ]
}

beforeEach(() => {
  window.localStorage.clear()
  vi.resetModules()
})

it('offers tunneled Remote Gateways and invalid SSH intent beside managed SSH in the terminal menu', async () => {
  const { $connectionsRegistry } = await import('@/store/connections')
  const { $terminalShellGateways } = await import('./terminal-gateway')
  $connectionsRegistry.set(registry)

  expect($terminalShellGateways.get().map(source => source.id)).toEqual(['managed', 'tunnel', 'invalid'])
})

it.each(['managed', 'tunnel', 'invalid'])('keeps %s shell ownership and normalizes its profile', async id => {
  const { shellGatewayFor } = await import('./terminal-gateway')

  expect(shellGatewayFor(id, ' work ', registry)).toEqual({ connectionId: id, profile: 'work' })
  expect(shellGatewayFor(id, '', registry)).toEqual({ connectionId: id, profile: 'default' })
})

it.each(['local', 'plain', 'cloud'])('keeps %s on this device without borrowing another gateway SSH', async id => {
  const { shellGatewayFor } = await import('./terminal-gateway')

  expect(shellGatewayFor(id, 'work', registry)).toEqual({ connectionId: 'local' })
})

it('leaves an unknown registry or source to the window resolver', async () => {
  const { shellGatewayFor } = await import('./terminal-gateway')

  expect(shellGatewayFor('tunnel', 'work', null)).toBeUndefined()
  expect(shellGatewayFor('missing', 'work', registry)).toBeUndefined()
})
