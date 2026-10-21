"""Allowlisted protocol fields only. Never classify assistant text or exit=1."""


class TransientEOF(ValueError):
    """provider stream ended before a terminal result"""


CODEX = {'usageLimitExceeded': 'QUOTA_EXCEEDED', 'rateLimitExceeded': 'QUOTA_EXCEEDED',
         'contextWindowExceeded': 'CONTEXT_EXHAUSTED', 'unauthorized': 'AUTH',
         'internalServerError': 'TRANSIENT', 'serverOverloaded': 'TRANSIENT',
         'httpConnectionFailed': 'TRANSIENT', 'responseStreamConnectionFailed': 'TRANSIENT',
         'responseStreamDisconnected': 'TRANSIENT'}
CLAUDE = {'rate_limit': 'QUOTA_EXCEEDED', 'authentication_failed': 'AUTH',
          'server_error': 'TRANSIENT'}


def codex_error(error):
    value = error.get('codexErrorInfo') if isinstance(error, dict) else None
    return CODEX.get(value, 'UNKNOWN') if isinstance(value, str) else 'UNKNOWN'


# TB-08 (K-018/2): `agy` reports failures only as free text in result.error / stderr (never the
# assistant response). Unverified until the first natural quota event; anything else stays UNKNOWN.
AGY_QUOTA = ('resource_exhausted', 'quota', 'out of credits', 'usage limit', 'rate limit')
AGY_AUTH = ('please sign in', 'not logged into antigravity')
# TB-43/TB-53: 'request failed: post' doğrulandı (2026-10-20, Google API EOF); diğerleri ilk doğal olayda kontrol edilecek.
AGY_TRANSIENT = ('503', '502', 'overloaded', 'connection reset', 'stream closed', 'request failed: post ')   # not 'internal error': too generic (TB-08 other_error)


def agy_error(text):
    text = (text or '').lower()
    if any(m in text for m in AGY_AUTH):
        return 'AUTH'
    if any(m in text for m in AGY_QUOTA):
        return 'QUOTA_EXCEEDED'
    return 'TRANSIENT' if any(m in text for m in AGY_TRANSIENT) else 'UNKNOWN'


def claude_error(message):
    # SDKAssistantMessage.error is protocol metadata, not message.content text.
    if message.get('type') == 'assistant' and isinstance(message.get('error'), str):
        return CLAUDE.get(message['error'], 'UNKNOWN')
    return 'UNKNOWN'
