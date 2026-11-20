import { MessagePrimitive, useAuiState } from '@assistant-ui/react'
import { useStore } from '@nanostores/react'
import { type FC, useEffect, useState } from 'react'

import { $apiRequestScope, hermesApi } from '@/api/client'
import { useComposerScope } from '@/app/chat/composer/scope'
import { MarkdownTextContent } from '@/components/assistant-ui/markdown-text'
import { messageContentText } from '@/components/assistant-ui/thread/content'
import { MessageTimelineTimestamp } from '@/components/assistant-ui/thread/timeline-timestamp'
import { UserMessageText } from '@/components/assistant-ui/thread/user-message-text'
import { SCAFFOLD_GLYPH_CLASS, SCAFFOLD_LABEL_CLASS, ScaffoldRow } from '@/components/chat/scaffold-row'
import { Codicon } from '@/components/ui/codicon'
import { LogView } from '@/components/ui/log-view'
import { ToolIcon } from '@/components/ui/tool-icon'
import { useI18n } from '@/i18n'
import { mediaTagValues } from '@/lib/chat-messages/parts'
import { LinkifiedText } from '@/lib/external-link'
import { mediaName } from '@/lib/media'
import { cn } from '@/lib/utils'

const SLASH_STATUS_RE = /^slash:(?<command>\/[^\n]+)\n(?<output>[\s\S]*)$/
const STEER_NOTE_RE = /^steer:(?<text>[\s\S]+)$/
const REVIEW_NOTE_RE = /^review:(?<label>[^:\n]+):?\s*(?<detail>[\s\S]*)$/
// A usage-window plan gauge exactly as the backend renders it: `[` + 10 cells of
// `|` then spaces + `]`. Only a window plan's /usage output carries one.
const USAGE_WINDOW_GAUGE_RE = /\[(?=[| ]{10}\])\|* *\]/

interface BackgroundResultProps {
  text: string
  report: string
  process?: boolean
}

export const BackgroundResult: FC<BackgroundResultProps> = ({ text, report, process }) => {
  const [open, setOpen] = useState(false)

  return (
    <div
      className="flex w-full min-w-0 flex-col self-start py-1 pl-(--message-text-indent)"
      data-slot="aui_background-result"
    >
      <div data-conversation-scaffold="">
        <ScaffoldRow
          onToggle={report ? () => setOpen(!open) : undefined}
          open={open}
          trailing={
            <>
              {' '}
              <MessageTimelineTimestamp />
            </>
          }
        >
          {process && (
            <span className={SCAFFOLD_GLYPH_CLASS}>
              <ToolIcon className="text-(--ui-text-tertiary)" name="terminal" size="0.875rem" />
            </span>
          )}
          <span className={cn(SCAFFOLD_LABEL_CLASS, 'min-w-0 truncate')}>{text}</span>
        </ScaffoldRow>
      </div>
      {open &&
        (process ? (
          <LogView className="mt-2 max-h-80 overscroll-x-contain overscroll-y-auto">{report}</LogView>
        ) : (
          <div className="mt-2 max-h-80 min-w-0 max-w-full overflow-auto overscroll-x-contain overscroll-y-auto wrap-anywhere">
            <MarkdownTextContent isRunning={false} text={report} />
          </div>
        ))}
    </div>
  )
}

// The refusal wire shape carries only native Responses cache entries, not general attachments.
const RESPONSES_IMAGE_PATH = /\/cache\/generated\/images\/responses_image_\d{8}_\d{6}_[0-9a-f]{8}\.(?:png|jpe?g|webp)$/
const RASTER_DATA_URL = /^data:image\/(?:png|jpeg|webp);base64,/
// eslint-disable-next-line no-control-regex -- control characters are not valid artifact references
const CONTROL_CHARS = /[\x00-\x1f\x7f]/

function splitRefusedNotice(notice: string) {
  const [firstLine, ...lines] = notice.split('\n')
  const text = [firstLine]
  const paths = new Set<string>()
  for (const line of lines) {
    if (!line.startsWith('MEDIA:')) {
      text.push(line)
      continue
    }
    const marker = line.endsWith('\r') ? line.slice(0, -1) : line
    const path = marker.slice('MEDIA:'.length)
    const normalized = path.replace(/\\/g, '/')
    if (
      /^(?:\/(?![\\/])|[a-z]:[\\/])/i.test(path) &&
      !/(?:^|\/)\.{1,2}(?:\/|$)/.test(normalized) &&
      !CONTROL_CHARS.test(path) &&
      RESPONSES_IMAGE_PATH.test(normalized) &&
      mediaTagValues(marker)[0] === path
    ) {
      paths.add(path)
    }
  }
  return { text: text.join('\n'), paths }
}

interface RefusedImageProps {
  path: string
  connectionId: string | null
  profile: string | null
}

async function readRefusedImage({ path, connectionId, profile }: RefusedImageProps): Promise<string | null> {
  // Unlike the general attachment resolver, this endpoint enforces resolved media roots
  // and a size cap. Do not fall back to the desktop filesystem or a URL from the notice.
  const response = await hermesApi<{ data_url?: unknown }>({
    path: `/api/media?path=${encodeURIComponent(path)}`,
    ...(connectionId ? { connectionId } : {}),
    ...(profile ? { profile } : {})
  })
  const src = response?.data_url
  return typeof src === 'string' && RASTER_DATA_URL.test(src) ? src : null
}

function RefusedImage({ path, connectionId, profile }: RefusedImageProps) {
  const { t } = useI18n()
  const [src, setSrc] = useState<null | string>('')
  useEffect(() => {
    let cancelled = false
    const settle = (value: string | null) => {
      if (!cancelled) {
        setSrc(value)
      }
    }
    void readRefusedImage({ path, connectionId, profile }).then(settle, () => settle(null))
    return () => {
      cancelled = true
    }
  }, [path, connectionId, profile])

  if (src === null) {
    return <span className="text-xs text-muted-foreground">{t.desktop.imagePreviewFailed}</span>
  }
  return src ? (
    <img
      alt={mediaName(path)}
      className="block h-auto w-auto max-h-(--image-preview-height) max-w-full rounded-lg object-contain"
      onError={() => setSrc(null)}
      src={src}
    />
  ) : null
}

function RefusedImages({ paths }: { paths: Set<string> }) {
  const scope = useComposerScope()
  const ambient = useStore($apiRequestScope)
  if (scope.target !== 'main' && (!scope.connectionId || !scope.profile)) {
    return null
  }
  const connectionId = scope.connectionId ?? ambient.connectionId
  const profile = scope.profile ?? ambient.profile

  return [...paths].map(path => (
    <RefusedImage
      connectionId={connectionId}
      key={JSON.stringify([connectionId, profile, path])}
      path={path}
      profile={profile}
    />
  ))
}

/** A safety-refused turn kept for display: the user's own message rendered
 *  greyed and non-interactive (no edit, no actions), with a short notice that
 *  the model's safety filter refused it. Right-aligned like the prompt it was,
 *  but a dashed muted card — never the blue user bubble, never a reply. */
const ContentRefusedNote: FC<{ notice: string; userText: string }> = ({ notice, userText }) => {
  const { text, paths } = splitRefusedNotice(notice)
  return (
    <MessagePrimitive.Root
      className="flex w-full min-w-0 max-w-full flex-col items-stretch gap-1 self-end pb-(--conversation-turn-gap)"
      data-role="system"
      data-slot="aui_content-refused"
    >
      <div className="ml-auto flex min-w-0 max-w-full flex-col gap-1.5 rounded-xl border border-dashed border-(--ui-stroke-tertiary) px-3 py-2 text-left opacity-70">
        {userText && <UserMessageText className="wrap-anywhere text-(--ui-text-tertiary)" text={userText} />}
        <span className="flex items-center gap-1.5 text-[0.6875rem] leading-5 text-muted-foreground/70">
          <Codicon className="shrink-0 text-muted-foreground/60" name="shield" size="0.75rem" />
          <span className="wrap-anywhere">{text}</span>
        </span>
        {paths.size > 0 && <RefusedImages paths={paths} />}
      </div>
      <MessageTimelineTimestamp className="self-end pr-1.5" />
    </MessagePrimitive.Root>
  )
}

export const SystemMessage: FC = () => {
  const { t } = useI18n()
  const text = useAuiState(s => messageContentText(s.message.content))
  const asyncResult = useAuiState(s => s.message.metadata.custom?.asyncResult)
  const processResult = useAuiState(s => s.message.metadata.custom?.asyncResultKind === 'process')

  const contentRefused = useAuiState(
    s => s.message.metadata.custom?.contentRefused as undefined | { notice?: string }
  )

  // A safety-refused turn, kept for display only: the user's own message, greyed
  // and non-interactive, with a short notice. Not a user bubble, not a reply.
  if (contentRefused) {
    return <ContentRefusedNote notice={contentRefused.notice ?? t.assistant.thread.contentRefusedNotice} userText={text} />
  }

  if (!text) {
    return null
  }

  if (processResult || (typeof asyncResult === 'string' && asyncResult)) {
    return (
      <MessagePrimitive.Root
        className="w-full min-w-0 self-start"
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <BackgroundResult
          process={processResult}
          report={typeof asyncResult === 'string' ? asyncResult : ''}
          text={text}
        />
      </MessagePrimitive.Root>
    )
  }

  // The self-improvement review saved something to memory/skills — the same
  // kind of event as a landed `memory` write, so it wears the same chrome:
  // brain glyph with the gold→purple glow, gradient label, purple detail,
  // left-aligned in the reading column like every other scaffold line.
  const reviewNote = text.match(REVIEW_NOTE_RE)

  if (reviewNote?.groups) {
    const detail = reviewNote.groups.detail.trim()

    return (
      <MessagePrimitive.Root
        className="flex w-full min-w-0 max-w-full items-start gap-1.5 self-start py-0.5"
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <span className="tool-memory-legendary-glyph flex h-(--conversation-line-height) w-3.5 shrink-0 items-center justify-center">
          <ToolIcon className="text-(--tool-memory-legendary-icon)" name="brain" size="0.875rem" />
        </span>
        <span className={cn(SCAFFOLD_LABEL_CLASS, 'tool-memory-legendary-title shrink-0 text-transparent')}>
          {reviewNote.groups.label.trim()}
        </span>
        {detail && (
          <span className={cn(SCAFFOLD_LABEL_CLASS, 'tool-memory-legendary-meta min-w-0 wrap-anywhere')}>{detail}</span>
        )}
      </MessagePrimitive.Root>
    )
  }

  const steerNote = text.match(STEER_NOTE_RE)

  if (steerNote?.groups) {
    return (
      <MessagePrimitive.Root
        className="flex max-w-[min(86%,44rem)] items-center gap-1.5 self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/60"
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <Codicon className="text-muted-foreground/55" name="compass" size="0.75rem" />
        <span className="text-muted-foreground/55">steered</span>
        <span className="text-muted-foreground/35">·</span>
        <span className="whitespace-pre-wrap">{steerNote.groups.text.trim()}</span> <MessageTimelineTimestamp />
      </MessagePrimitive.Root>
    )
  }

  const slashStatus = text.match(SLASH_STATUS_RE)

  if (slashStatus?.groups) {
    const output = slashStatus.groups.output.trim()
    // Single-line status (e.g. "model → x") reads best centered inline; padded
    // multiline output (catalogs, usage tables) needs left-aligned, wider room
    // or the column alignment breaks.
    const multiline = output.includes('\n')
    // A window plan's /usage (token table + `[||||||    ]` plan gauges) only lines
    // up in a monospace face. Every other output keeps the default face.
    const tabular = multiline && USAGE_WINDOW_GAUGE_RE.test(output)

    return (
      <MessagePrimitive.Root
        className={cn(
          'w-[60%] max-w-[44rem] self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/60',
          multiline ? 'text-left' : 'text-center'
        )}
        data-role="system"
        data-slot="aui_system-message-root"
      >
        <span className="font-mono text-muted-foreground/55">{slashStatus.groups.command}</span>
        {multiline ? (
          <LinkifiedText
            className={cn('mt-0.5 block whitespace-pre-wrap', tabular && 'font-mono text-[0.65rem] leading-[1.15rem]')}
            explicitOnly
            pretty={false}
            text={output}
          />
        ) : (
          <>
            <span className="mx-1.5 text-muted-foreground/35">·</span>
            <LinkifiedText className="whitespace-pre-wrap" explicitOnly pretty={false} text={output} />
          </>
        )}{' '}
        <MessageTimelineTimestamp className={cn(multiline ? 'mt-0.5 block' : 'ml-1.5')} />
      </MessagePrimitive.Root>
    )
  }

  const multiline = text.includes('\n')

  return (
    <MessagePrimitive.Root
      className={cn(
        'w-[60%] max-w-[44rem] self-center px-2 py-0.5 text-[0.6875rem] leading-5 text-muted-foreground/55',
        multiline ? 'text-left' : 'text-center'
      )}
      data-role="system"
      data-slot="aui_system-message-root"
    >
      <LinkifiedText className="whitespace-pre-wrap" explicitOnly pretty={false} text={text} />{' '}
      <MessageTimelineTimestamp className={cn(multiline ? 'mt-0.5 block' : 'ml-1.5')} />
    </MessagePrimitive.Root>
  )
}
