import http from 'node:http'
import https from 'node:https'

import { htmlResponseError, httpStatusError, jsonAgentFor, withRetry } from './api-transport'
import type { GatewayTunnelSnapshot } from './gateway-ssh-tunnel'
import { DEFAULT_FETCH_TIMEOUT_MS, resolveTimeoutMs } from './hardening'

export interface PublicJsonRequestOptions {
  body?: unknown
  headers?: Record<string, string>
  method?: string
  timeoutMs?: number
  /** Liveness must prove a new dial, never borrow an already-connected socket. */
  freshConnection?: boolean
}

function parsePublicJsonResponse(url: string, response: http.IncomingMessage, text: string): unknown {
  const status = response.statusCode || 500

  if (status >= 400) { throw httpStatusError(status, text, response.statusMessage) }

  if (status >= 300) { throw htmlResponseError(url, status, response.headers.location) }

  if (!text) { return null }

  if (/^\s*<(?:!doctype|html)/i.test(text) || String(response.headers['content-type'] || '').includes('text/html')) {
    throw htmlResponseError(url, status)
  }

  try {
    return JSON.parse(text)
  } catch {
    throw new Error(`Invalid JSON from ${url} (status ${response.statusCode}): ${text.slice(0, 200)}`)
  }
}

/** Credential-free public requests, sharing the same tunnel fences as authenticated REST. */
export function createPublicJsonFetcher(deps: {
  tunnelForUrl: (url: string) => GatewayTunnelSnapshot | undefined
  assertCurrent: (snapshot: GatewayTunnelSnapshot) => void
  headersForUrl: (url: string) => Record<string, string>
}) {
  return function fetchPublicJson(url: string, options: PublicJsonRequestOptions = {}): Promise<any> {
    const snapshot = deps.tunnelForUrl(url)

    return withRetry((requestState: { bodySent: boolean }) => new Promise((resolve, reject) => {
      if (snapshot) { deps.assertCurrent(snapshot) }
      const body = options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body))
      let parsed: URL

      try {
        parsed = new URL(url)
      } catch (error) {
        reject(new Error(`Invalid URL: ${(error as Error).message}`))

        return
      }

      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
        reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

        return
      }

      const client = parsed.protocol === 'https:' ? https : http

      const req = client.request(parsed, {
        agent: options.freshConnection ? false : snapshot?.agent || jsonAgentFor(parsed.protocol),
        signal: snapshot?.signal,
        method: options.method || 'GET',
        headers: {
          ...(snapshot ? {} : deps.headersForUrl(url)), ...(options.headers || {}),
          'Content-Type': 'application/json', ...(body ? { 'Content-Length': String(body.length) } : {})
        }
      }, response => {
        const chunks: Buffer[] = []
        response.on('error', reject)
        response.on('data', chunk => chunks.push(chunk))
        response.on('end', () => {
          try {
            if (snapshot) { deps.assertCurrent(snapshot) }
            resolve(parsePublicJsonResponse(url, response, Buffer.concat(chunks).toString('utf8')))
          } catch (error) {
            reject(error)
          }
        })
      })

      req.on('error', reject)
      const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)
      req.setTimeout(timeoutMs, () => req.destroy(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`)))
      requestState.bodySent = true

      if (body) { req.write(body) }
      req.end()
    }), { method: options.method || 'GET' })
  }
}
