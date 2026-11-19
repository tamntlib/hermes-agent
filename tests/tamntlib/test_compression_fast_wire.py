"""Explicit compression Fast reaches SDK bodies independently of chat selection."""
import asyncio
import json
from typing import Any

import httpx
import pytest
from anthropic import Anthropic
from openai import OpenAI

from agent import auxiliary_client as aux


@pytest.mark.parametrize('async_mode', [False, True])
@pytest.mark.parametrize('mode', ['chat_completions', 'codex_responses', 'anthropic_messages'])
@pytest.mark.parametrize('policy', ['priority', 'normal', None, 'missing'])
@pytest.mark.parametrize('chat_tier', [None, 'priority'])
def test_compression_selection_reaches_sdk_wire(monkeypatch, async_mode, mode, policy, chat_tier):
    config = {} if policy == 'missing' else {'service_tier': policy}
    monkeypatch.setattr(aux, '_get_auxiliary_task_config', lambda task: config if task == 'compression' else {})
    monkeypatch.setattr(aux, '_with_custom_endpoint_extra_body', lambda body, *args: body or {})
    requests = []
    model, url = 'arbitrary-alias', 'https://proxy.example/v1'

    def respond(request):
        requests.append((json.loads(request.content), dict(request.headers)))
        if mode == 'anthropic_messages':
            return httpx.Response(200, json={'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                'model': model, 'content': [{'type': 'text', 'text': 'summary'}],
                'stop_reason': 'end_turn', 'usage': {'input_tokens': 1, 'output_tokens': 1}})
        if mode == 'codex_responses':
            events = [
                {'type': 'response.output_text.delta', 'delta': 'summary'},
                {'type': 'response.completed', 'response': {'id': 'resp_test', 'object': 'response',
                    'model': model, 'status': 'completed', 'output': [],
                    'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}},
            ]
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                text=''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events))
        return httpx.Response(200, json={'id': 'chat_test', 'object': 'chat.completion', 'created': 0,
            'model': model, 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': 'summary'}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        if mode == 'anthropic_messages':
            sdk = Anthropic(api_key='test-only', base_url=url, http_client=http, timeout=30)
            client = aux.AnthropicAuxiliaryClient(sdk, model, 'test-only', url)
            # Capture the real SDK body without requiring an Anthropic SSE fixture.
            monkeypatch.setattr('agent.anthropic_adapter.create_anthropic_message',
                                lambda sdk, kwargs, **_: sdk.messages.create(**kwargs))
        else:
            sdk = OpenAI(api_key='test-only', base_url=url, http_client=http, max_retries=0)
            client = aux.CodexAuxiliaryClient(sdk, model) if mode == 'codex_responses' else sdk
        if async_mode:
            wrapper = {'anthropic_messages': aux.AsyncAnthropicAuxiliaryClient,
                       'codex_responses': aux.AsyncCodexAuxiliaryClient}.get(mode, aux._AsyncAuxiliaryClientBase)
            client = wrapper(client)
        monkeypatch.setattr(aux, '_get_cached_client', lambda *args, **kwargs: (client, model))
        runtime = {'provider': 'custom', 'requested_provider': 'custom:proxy', 'model': model,
                   'base_url': url, 'api_mode': mode, 'service_tier': chat_tier}
        kwargs: dict[str, Any] = dict(task='compression', main_runtime=runtime, max_tokens=128,
                      messages=[{'role': 'user', 'content': 'summarize'}])
        response = asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(**kwargs)
        assert response.choices[0].message.content == 'summary'
    assert len(requests) == 1
    body, headers = requests[0]
    expected = ({'speed': 'fast'} if mode == 'anthropic_messages' else {'service_tier': 'priority'}) if policy == 'priority' else {}
    assert {key: body[key] for key in ('speed', 'service_tier') if key in body} == expected
    assert ('fast-mode-2026-02-01' in headers.get('anthropic-beta', '')) is (policy == 'priority' and mode == 'anthropic_messages')
