"""Explicit local routes. Message content cannot select an agent or a path."""
from pathlib import Path
from types import MappingProxyType

TARGETS = MappingProxyType({'chatgpt': 'claude', 'claude': 'chatgpt'})


def target_for(source: str) -> str:
    if source not in TARGETS:
        raise ValueError(f'Unsupported source: {source}')
    return TARGETS[source]


def inbox_for(root: Path, envelope: dict) -> Path:
    expected = target_for(envelope['source_agent'])
    if envelope['target_agent'] != expected:
        raise ValueError('Unsupported source/target pair')
    return Path(root).resolve() / 'agents' / expected / 'inbox'
