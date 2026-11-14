import assert from 'node:assert/strict'

import { test } from 'vitest'

import { GatewaySshTunnels } from './gateway-ssh-tunnel'
import { invalidateExternalGatewayRuntime } from './gateway-tunnel-runtime'

for (const reason of ['saved', 'updated', 'removed'] as const) {
  test(`external ${reason} preserves labels but fences changed tunnel traffic and terminals`, async () => {
    let closed = 0
    const disposed: string[] = []
    const pool = new GatewaySshTunnels({
      createTransport: () => ({
        open: async () => {},
        forward: async () => {},
        close: async () => { closed++ },
        isAlive: async () => true
      }),
      pickPort: async () => 39999,
      controlDir: '/synthetic',
      probe: async () => {}
    })
    const snapshot = await pool.ensure({
      id: 'remote', kind: 'remote', label: 'Remote', url: 'http://backend:9119',
      authMode: 'oauth', sshTunnel: { host: 'ssh.example', remotePort: 9119 }
    })
    const retiring = invalidateExternalGatewayRuntime([{ connectionId: 'remote', reason }], pool, id => disposed.push(id))
    if (reason === 'saved') {
      pool.assertCurrent(snapshot)
      assert.deepEqual(disposed, [])
      assert.equal(closed, 0)
    } else {
      assert.throws(() => pool.assertCurrent(snapshot))
      assert.equal(snapshot.signal.aborted, true)
      assert.deepEqual(disposed, ['remote'])
    }
    await retiring
    assert.equal(closed, reason === 'saved' ? 0 : 1)
    await pool.closeAll()
  })
}
