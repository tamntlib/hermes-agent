"""Awaited maintenance at the delivered-reply boundary of the existing session FIFO."""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def compress_after_settlement(authority, ref, live, admission, settled):
    """Only completed admissions reach the delivered-reply compression boundary."""
    if settled is not None and settled.get('outcome') == 'completed':
        await compress_after_reply(authority, ref, live, admission)


async def compress_after_reply(authority, ref, live, admission):
    """Trusted owner call, never a wire option or a separate worker/turn.

    The completed admission supplies the principal and retry identity. The existing
    mutation machinery owns preparation, transcript fences, commit and publication.
    """
    from gateway.config import Platform
    from gateway.session_mutations import write_mutation
    from gateway.session_unattended_unknown import is_unattended

    from gateway.session_history_mount import is_history_mount

    if (authority.sessions.get(ref.session_id) is not live or is_history_mount(live)
            or live.source.platform != Platform.LOCAL or getattr(authority.runner, '_draining', False)
            or admission['payload'].get('finite') or admission['payload'].get('unattended')
            or is_unattended(admission)):
        return
    try:
        async with live.claim_gate:
            row = await asyncio.to_thread(authority.db.get_session, ref.session_id)
            if row is None or row['runtime_generation'] != admission['generation']:
                return  # A reset or policy change already moved this delivered turn's boundary.
            params = dict(session_id=ref.session_id,
                          request_id='post-turn-compress:' + admission['admission_id'],
                          expected_revision=row['runtime_revision'], expected_generation=row['runtime_generation'],
                          operation='compress', payload={})
            await write_mutation(authority, ref, params, None, principal_id=admission['principal_id'],
                                 commit_lock=live.mutation_lock, automatic_compress=True)
    except Exception:
        # A delivered answer stays delivered. A refusal/failure must not wedge its FIFO follower.
        logger.warning('Post-reply compression failed for %s', ref.session_id, exc_info=True)
