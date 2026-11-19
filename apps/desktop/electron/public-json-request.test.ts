import assert from 'node:assert/strict'
import http from 'node:http'

import { test } from 'vitest'

import { createPublicJsonFetcher } from './public-json-request'

async function withServer(run: (url: string, server: http.Server) => Promise<void>, handler: http.RequestListener) {
  const server = http.createServer(handler)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  const url = `http://127.0.0.1:${(server.address() as { port: number }).port}`

  try {
    await run(url, server)
  } finally {
    server.closeAllConnections()
    await new Promise<void>((resolve, reject) => server.close(error => error ? reject(error) : resolve()))
  }
}

const publicJson = createPublicJsonFetcher({ tunnelForUrl: () => undefined, assertCurrent: () => {}, headersForUrl: () => ({}) })

test('fresh probes do not borrow or destroy the ordinary keep-alive request pool', async () => {
  let connections = 0
  const ids = new WeakMap<object, number>()
  await withServer(async url => {
    const ordinary = await publicJson(url)
    await new Promise(setImmediate)
    const fresh = await publicJson(url, { freshConnection: true })
    assert.notEqual(fresh.connection, ordinary.connection)
    assert.deepEqual(await publicJson(url), ordinary)
    assert.equal(connections, 2)
  }, (request, response) => {
    if (!ids.has(request.socket)) { ids.set(request.socket, ++connections) }
    response.end(JSON.stringify({ connection: ids.get(request.socket) }))
  })
})

test.each([
  [204, '', null],
  [200, '{"ok":true}', { ok: true }]
])('public JSON status %s parses the same response contract', async (status, body, expected) => {
  await withServer(async url => assert.deepEqual(await publicJson(url), expected), (_request, response) => {
    response.writeHead(status as number)
    response.end(body as string)
  })
})

test.each([
  [401, '{"reason":"refused"}', 'application/json', /^401:/],
  [503, 'unavailable', 'text/plain', /^503:/],
  [302, '', 'text/plain', /redirected/],
  [200, '<html>login</html>', 'text/html', /got HTML/],
  [200, 'not-json', 'text/plain', /Invalid JSON/]
])('public JSON status %s preserves failures without following redirects or HTTP retries', async (status, body, contentType, expected) => {
  let requests = 0
  await withServer(async url => {
    await assert.rejects(publicJson(url), (error: Error & { statusCode?: number }) => {
      assert.match(error.message, expected as RegExp)

      if ((status as number) >= 400) { assert.equal(error.statusCode, status) }

      return true
    })
    assert.equal(requests, 1)
  }, (_request, response) => {
    requests += 1
    response.writeHead(status as number, { 'Content-Type': contentType as string, Location: '/login' })
    response.end(body as string)
  })
})

test('a POST reset after receipt is not replayed by the extracted public helper', async () => {
  let requests = 0
  await withServer(async url => {
    await assert.rejects(publicJson(url, { method: 'POST', body: { operation: 'synthetic' } }), /socket hang up|ECONNRESET/)
    assert.equal(requests, 1)
  }, (request, _response) => {
    requests += 1
    request.resume()
    request.on('end', () => request.socket.destroy())
  })
})

test('tunnel cancellation aborts fresh public probes without credential or proxy-header fallback', async () => {
  const abort = new AbortController()
  const agent = new http.Agent({ keepAlive: true })
  let received!: () => void
  const requestSeen = new Promise<void>(resolve => { received = resolve })
  let receivedHeaders: http.IncomingHttpHeaders | undefined

  const fetch = createPublicJsonFetcher({
    tunnelForUrl: () => ({ signal: abort.signal, agent } as never),
    assertCurrent: () => { if (abort.signal.aborted) { throw new Error('retired') } },
    headersForUrl: () => { throw new Error('must not fall back to proxy credentials') }
  })

  try {
    await withServer(async url => {
      const pending = fetch(url, { freshConnection: true })
      const result = Promise.allSettled([pending])
      await requestSeen
      abort.abort()
      assert.equal((await result)[0].status, 'rejected')
      assert.equal(receivedHeaders?.authorization, undefined)
      assert.equal(receivedHeaders?.['x-hermes-session-token'], undefined)
    }, (request, _response) => {
      receivedHeaders = request.headers
      received()
    })
  } finally {
    agent.destroy()
  }
})
