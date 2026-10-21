"""Claude Code print adapter, with role-scoped WRITE and READ_ONLY tool profiles."""
import asyncio
import json
import os
from pathlib import Path
import re
from app.adapters.base import BaseAdapter, AdapterResult
from app.provider_errors import claude_error, TransientEOF
from app.adapters.process import executable, drain_stderr, stop_process, spawn_owned, release_gate
from app import quota


def claude_binary():
    root = Path.home() / 'Library/Application Support/Claude/claude-code'
    # The Claude app keeps Claude Code under <version>/ and, since 2.1.289 (2026-10-07), <version>/<hash>/.
    found = [*root.glob('*/claude.app/Contents/MacOS/claude'), *root.glob('*/*/claude.app/Contents/MacOS/claude')]
    candidates = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
    return executable('CONDUCTOR_CLAUDE_BIN', 'claude', candidates)


ALIASES = ('fable', 'opus', 'sonnet', 'haiku')


def model_matches(wanted, running):
    """TB-10 Rev 3: an alias matches a `claude-…` id that has it as a separate dash-delimited part (old
    `claude-3-5-sonnet-20241022` and new `claude-sonnet-5-5` ids alike, an optional `[1m]` suffix allowed);
    a full id must match exactly. Nothing silently stands in for the wanted model (K-023/6)."""
    if not running:
        return False
    if wanted in ALIASES:
        return re.fullmatch(rf'claude(-[0-9a-z.]+)*-{wanted}(-[0-9a-z.]+)*(\[[0-9a-z]+\])?', running) is not None
    return running == wanted


def _with_window(result, running_model):
    """K-039: the usage plus the running model's context window from `modelUsage`, when reported."""
    usage = result.get('usage')
    models = result.get('modelUsage')
    if not isinstance(usage, dict) or not isinstance(models, dict):
        return usage
    entry = models.get(running_model) or (next(iter(models.values())) if len(models) == 1 else None)
    window = entry.get('contextWindow') if isinstance(entry, dict) else None
    return {**usage, 'context_window': window} if isinstance(window, int) and window > 0 else usage


class ClaudeAdapter(BaseAdapter):
    def __init__(self, model=None, effort=None):
        self.model, self.effort = model, effort

    async def run(self, prompt, workspace_path, provider_session_id=None, *, profile):
        if profile not in ('WRITE', 'READ_ONLY'):
            raise ValueError('invalid profile')
        tools = 'Read,Glob,Grep' + (',Write,Edit' if profile == 'WRITE' else '')
        args = [claude_binary(), '--safe-mode', '-p', '--output-format',
                'stream-json', '--verbose', '--include-partial-messages',
                '--tools', tools,
                '--permission-mode', 'acceptEdits' if profile == 'WRITE' else 'dontAsk', '--strict-mcp-config',
                '--mcp-config', '{"mcpServers":{}}']
        if self.model:
            args += ['--model', self.model]          # never --fallback-model (K-023/6)
        if self.effort:
            args += ['--effort', self.effort]
        if provider_session_id:
            args += ['--resume', provider_session_id]
        process, owner, gate = await spawn_owned(args, workspace_path)
        stderr = asyncio.create_task(drain_stderr(process.stderr))
        result = None
        protocol_error = None
        session_id = provider_session_id
        running_model = None
        try:
            yield {'type': 'process_started', 'ownership': owner}
            release_gate(gate)
            gate = None
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()
            async for line in process.stdout:
                data = json.loads(line)
                classified = claude_error(data)
                if classified != 'UNKNOWN':
                    protocol_error = {'error_code': classified, 'signal': {'type': data['type'], 'error': data['error']}}
                if data.get('session_id'):
                    session_id = data['session_id']
                if data.get('type') == 'system' and data.get('subtype') == 'init':
                    running_model = data.get('model')
                    yield {'type': 'started', 'provider_session_id': session_id, 'model': running_model}
                elif data.get('type') == 'stream_event':
                    delta = data.get('event', {}).get('delta', {})
                    if delta.get('type') == 'text_delta':
                        yield {'type': 'output_delta', 'text': delta.get('text', '')}
                elif data.get('type') == 'rate_limit_event':
                    quota.record_claude(data.get('rate_limit_info'))   # K-039: display only
                elif data.get('type') == 'result':
                    if result is not None:
                        raise ValueError('duplicate Claude result')
                    result = data
            code = await process.wait()
            if result is None:
                raise TransientEOF('Claude EOF without result')
            reason = result.get('terminal_reason')
            if reason is None and result.get('subtype') == 'success':
                reason = 'completed'
            denied = bool(result.get('permission_denials'))
            failed = (code != 0 or result.get('is_error') is not False
                      or reason != 'completed' or denied)
            mismatch = bool(self.model) and not model_matches(self.model, running_model)
            if mismatch:
                failed, reason = True, 'model_mismatch'
                protocol_error = {'error_code': 'UNKNOWN', 'signal': {'wanted': self.model, 'got': running_model}}
            if not failed:
                protocol_error = None
            if denied:
                yield {'type': 'approval_required', 'details': 'Claude tool permission denied'}
            answer = AdapterResult(result.get('result', ''), session_id,
                                   _with_window(result, running_model), result.get('stop_reason'),
                                   failed or bool(protocol_error), 'permission_denied' if denied else reason, code,
                                   'PERMISSION' if denied else protocol_error['error_code'] if protocol_error else ('UNKNOWN' if failed else None),
                                   protocol_error['signal'] if protocol_error else None)
        finally:
            if gate is not None:
                os.close(gate)
            try:
                await stop_process(process, owner)
            finally:
                stderr.cancel()
                await asyncio.gather(stderr, return_exceptions=True)
        yield {'type': 'process_stopped', 'ownership': owner}
        yield {'type': 'completed' if answer.successful else 'failed', 'result': answer}
