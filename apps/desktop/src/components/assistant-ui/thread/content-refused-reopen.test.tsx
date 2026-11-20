import { cleanup, render, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import { type ComposerScope, ComposerScopeProvider, MAIN_COMPOSER_SCOPE } from '@/app/chat/composer/scope'
import { en } from '@/i18n/en'
import { toChatMessages } from '@/lib/chat-messages'
import { toRuntimeMessage } from '@/lib/chat-runtime'
import type { SessionMessage } from '@/types/hermes'

import { stubThreadEnvironment, ThreadRuntime } from '../test-utils'

import { Thread } from '.'

const notice = "This message was refused by the model's safety filter — try a different query."
const userText = 'The original prompt'
const path = '/srv/profile with spaces/cache/generated/images/responses_image_20261120_123456_aabbccdd.png'
const png = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jMZsAAAAASUVORK5CYII='
const scope = { ...MAIN_COMPOSER_SCOPE, connectionId: 'owning-gateway', profile: 'owning-profile' }

stubThreadEnvironment()

afterEach(() => {
  cleanup()
  Reflect.deleteProperty(window, 'hermesDesktop')
  setApiRequestConnection(null)
})

function reopen(storedNotice: string, transcriptScope: ComposerScope = scope, prompt = userText) {
  const row: SessionMessage = {
    role: 'user',
    content: prompt,
    display_kind: 'content_refused',
    display_metadata: JSON.stringify({ content_refused: { notice: storedNotice } }),
    timestamp: 1
  }
  const messages = toChatMessages([row]).map(toRuntimeMessage)
  const { container } = render(
    <ComposerScopeProvider value={transcriptScope}>
      <ThreadRuntime messages={messages}>
        <Thread />
      </ThreadRuntime>
    </ComposerScopeProvider>
  )
  const block = container.querySelector<HTMLElement>('[data-slot="aui_content-refused"]')!
  expect(block).toBeTruthy()
  return block
}

it('reopens a retained Responses image inside the refusal using the transcript owner', async () => {
  setApiRequestConnection('foreground-gateway')
  const api = vi.fn().mockResolvedValue({ data_url: png })
  const readFileDataUrl = vi.fn()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, readFileDataUrl } })

  const block = reopen(`${notice}\nMEDIA:${path}`)
  expect(block.textContent).toContain(notice)
  expect(block.textContent).toContain(userText)
  expect((await within(block).findByRole('img')).getAttribute('src')).toBe(png)
  expect(block.textContent).not.toContain('MEDIA:')
  expect(within(block).queryByRole('link')).toBeNull()
  expect(within(block).queryByRole('button')).toBeNull()
  expect(api).toHaveBeenCalledExactlyOnceWith({
    path: `/api/media?path=${encodeURIComponent(path)}`,
    connectionId: scope.connectionId,
    profile: scope.profile
  })
  expect(readFileDataUrl).not.toHaveBeenCalled()
})

it('ignores unsupported notice directives and never reads a marker from the prompt', async () => {
  const api = vi.fn()
  const readFileDataUrl = vi.fn()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, readFileDataUrl } })

  const block = reopen(
    `${notice}\nMEDIA:https://example.invalid/image.png\nMEDIA:/fixtures/not-generated.png\nMEDIA:${path.replace('.png', '.svg')}`,
    scope,
    `My literal prompt: MEDIA:${path}`
  )
  expect(block.textContent).toContain(notice)
  expect(block.textContent).toContain(`My literal prompt: MEDIA:${path}`)
  expect(block.textContent).not.toContain('not-generated.png')
  expect(within(block).queryByRole('img')).toBeNull()
  expect(api).not.toHaveBeenCalled()
  expect(readFileDataUrl).not.toHaveBeenCalled()
})

it('does not read on the foreground gateway for a tile without an owner', () => {
  setApiRequestConnection('foreground-gateway')
  const api = vi.fn().mockResolvedValue({ data_url: png })
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api } })

  const block = reopen(`${notice}\nMEDIA:${path}`, { ...MAIN_COMPOSER_SCOPE, target: 'tile:reopened' })
  expect(block.textContent).toContain(notice)
  expect(api).not.toHaveBeenCalled()
})

it('keeps the refusal readable when the owner can no longer serve the image', async () => {
  const api = vi.fn().mockRejectedValue(new Error('File not found'))
  const readFileDataUrl = vi.fn()
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: { api, readFileDataUrl } })

  const block = reopen(`${notice}\nMEDIA:${path}`)
  expect(await within(block).findByText(en.desktop.imagePreviewFailed)).toBeTruthy()
  expect(block.textContent).toContain(notice)
  expect(block.textContent).not.toContain('MEDIA:')
  expect(within(block).queryByRole('img')).toBeNull()
  expect(api).toHaveBeenCalledTimes(1)
  expect(readFileDataUrl).not.toHaveBeenCalled()
})
