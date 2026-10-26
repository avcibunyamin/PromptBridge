"""Validated, user-accepted provider chains; never reloaded inside a run."""
import hashlib
import json
from pathlib import Path
import re
import yaml

PROFILES = {'author': 'WRITE', 'reviewer': 'READ_ONLY'}
DEFAULT = {'roles': {'author': ['codex'], 'reviewer': ['claude']}}
# F3-03: the single capability matrix (adapters enforce the same profiles).
CAPABILITIES = {
    # TB-15: Codex models and efforts come from app-server model/list. `ultra` is deliberately absent:
    # it enables automatic task delegation and needs a separate process-ownership decision.
    'codex': {'profiles': ('WRITE', 'READ_ONLY'), 'context_continuation': True, 'accounts': True, 'models': True,
              'efforts': ('low', 'medium', 'high', 'xhigh', 'max')},
    # TB-10: Claude takes --model (alias or full name) and --effort.
    'claude': {'profiles': ('WRITE', 'READ_ONLY'), 'context_continuation': False, 'accounts': False, 'models': True,
               'efforts': ('low', 'medium', 'high', 'xhigh', 'max')},
    # TB-08 (K-017/K-018): Antigravity CLI. Its default model is Flash, so a model is required.
    # TB-10 Rev 2: agy's effort is part of the model name (gemini-3.8-flash-high), so no separate effort.
    'agy': {'profiles': ('WRITE', 'READ_ONLY'), 'context_continuation': False, 'accounts': True, 'models': True,
            'model_required': True, 'efforts': ()},
    # TB-14: a local Ollama model through Codex app-server (`-c model_provider="ollama"`, own CODEX_HOME,
    # no sign-in). The model is required; there is no account, effort or quota.
    'ollama': {'profiles': ('WRITE', 'READ_ONLY'), 'context_continuation': False, 'accounts': False, 'models': True,
               'model_required': True, 'efforts': ()},
}
TEAM_REASONS = ('provider_not_allowed', 'profile_unsupported', 'duplicate_in_role', 'chain_length', 'unknown_provider',
                'account_unsupported', 'model_unsupported', 'model_required', 'model_unavailable', 'tools_unsupported',
                'effort_unsupported')
# TB-06 (K-016/K-017): a chain entry's identity is `provider[@account][/model]`; TB-10 adds `[~effort]`.
# It travels as the `provider` string everywhere (frozen policy, attempts, checkpoints, panel), so no schema change.
MAX_CHAIN, MAX_ALLOWED = 4, 8
ACCOUNT_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,15}')
MODEL_RE = re.compile(r'[a-z0-9][a-z0-9.:-]{0,47}')   # TB-14: Ollama tags (`gpt-oss:20b`)
EFFORT_RE = re.compile(r'[a-z]{2,12}')
_IDENTITY = re.compile(r'([a-z]+)(?:@([^/@~]+))?(?:/([^/@~]+))?(?:~([^/@~]+))?')


def split(identity):
    """(kind, account, model, effort); ValueError for anything that is not a well-formed identity."""
    m = _IDENTITY.fullmatch(identity) if isinstance(identity, str) else None
    if (not m or (m[2] is not None and not ACCOUNT_RE.fullmatch(m[2])) or (m[3] is not None and not MODEL_RE.fullmatch(m[3]))
            or (m[4] is not None and not EFFORT_RE.fullmatch(m[4]))):
        raise ValueError('malformed provider identity')
    return m[1], m[2], m[3], m[4]


def parse(identity):
    """(kind, account, model); see split() for the effort."""
    return split(identity)[:3]


def effort_of(identity):
    return split(identity)[3]


def covers(entry, identity):
    """TB-10: an `allowed` entry without a model (or effort) admits every model (or effort) of that tool and
    account, so the catalog can offer them; the account must always match."""
    try:
        e, i = split(entry), split(identity)
    except ValueError:
        return False
    return e[:2] == i[:2] and e[2] in (None, i[2]) and e[3] in (None, i[3])


def kind(identity):
    """The tool behind an identity (`codex@is` -> `codex`): adapters, capabilities, install checks, instructions."""
    return parse(identity)[0]


def account_of(identity):
    return parse(identity)[1]


def account_base(identity):
    """The provider and optional account (`agy@hesap1/model` -> `agy@hesap1`, `codex/m~high` -> `codex`)."""
    name, account, _ = parse(identity)
    return f'{name}@{account}' if account else name


def model_of(identity):
    return parse(identity)[2]


def identity_problem(identity, ceiling=False):
    """None for a usable identity, otherwise a TEAM_REASONS code. `ceiling`: an `allowed` entry, which may
    leave a required model open (it then admits every model)."""
    try:
        name, account, model, effort = split(identity)
    except ValueError:
        return 'unknown_provider'
    if name not in CAPABILITIES:
        return 'unknown_provider'
    if account is not None and not CAPABILITIES[name]['accounts']:
        return 'account_unsupported'
    if model is not None and not CAPABILITIES[name]['models']:
        return 'model_unsupported'
    if model is None and CAPABILITIES[name].get('model_required') and not ceiling:
        return 'model_required'
    if name == 'codex' and effort is not None and model is None:
        return 'model_required'
    if effort is not None and effort not in CAPABILITIES[name].get('efforts', ()):
        return 'effort_unsupported'
    return None


def _entry_identity(item):
    """A YAML/team entry -> identity. `account`/`model` keys and the compact `p@a/m` form are both accepted, never mixed."""
    provider = item.get('provider')
    extra = {k: item[k] for k in ('account', 'model', 'effort') if k in item}
    if not isinstance(provider, str) or any(not isinstance(v, str) for v in extra.values()):
        raise ValueError('unknown or duplicate provider')
    if extra:
        if any(c in provider for c in '@/~'):
            raise ValueError('give account/model/effort either inline or as fields, not both')
        provider += (('@' + extra['account'] if 'account' in extra else '') + ('/' + extra['model'] if 'model' in extra else '')
                     + ('~' + extra['effort'] if 'effort' in extra else ''))
    return provider
# F3-02 loop budgets (K1). Opt-in: only a policy with a `loop` section is version 3 and runs turns.
LOOP_DEFAULTS = {'max_turns': 3, 'max_active_minutes': 30}
LOOP_BOUNDS = {'max_turns': (1, 5), 'max_active_minutes': (5, 120)}


def normalize(config):
    if not isinstance(config, dict) or set(config) - {'roles', 'require_handover_approval', 'max_context_continuations', 'loop'}:
        raise ValueError('invalid provider policy fields')
    if config.get('require_handover_approval', False) is not False:
        raise ValueError('require_handover_approval is not supported')
    roles = config.get('roles')
    if not isinstance(roles, dict) or set(roles) != set(PROFILES):
        raise ValueError('author and reviewer chains are required')
    limit = config.get('max_context_continuations', 3)
    if type(limit) is not int or not 0 <= limit <= 3:
        raise ValueError('max_context_continuations must be an integer in 0..3')
    result = {'roles': {}, 'require_handover_approval': False, 'version': 2,
              'max_context_continuations': limit,
              'context_continuation_providers': [p for p, c in CAPABILITIES.items() if c['context_continuation']]}
    for role, chain in roles.items():
        if not isinstance(chain, list) or not 1 <= len(chain) <= MAX_CHAIN:
            raise ValueError(f'each role needs one to {MAX_CHAIN} authorized providers')
        seen = set()
        result['roles'][role] = []
        for item in chain:
            if isinstance(item, str):
                item = {'provider': item, 'profile': PROFILES[role]}
            if not isinstance(item, dict) or not {'provider', 'profile'} <= set(item) <= {'provider', 'profile', 'account', 'model', 'effort'}:
                raise ValueError('provider entries require provider and profile')
            provider = _entry_identity(item)
            problem = identity_problem(provider)
            if problem in ('account_unsupported', 'model_unsupported', 'model_required', 'effort_unsupported'):
                raise ValueError(f'{provider}: {problem}')
            if problem or provider in seen:
                raise ValueError('unknown or duplicate provider')
            if item['profile'] != PROFILES[role] or item['profile'] not in CAPABILITIES[kind(provider)]['profiles']:
                raise ValueError('profile must match role')
            seen.add(provider)
            # Plain entries stay exactly {provider, profile}: existing policies keep their digest.
            result['roles'][role].append({'provider': provider, 'profile': item['profile']})
    if 'loop' in config:
        # Without `loop` the policy stays byte-identical version 2 (same digest, legacy execution).
        result['version'] = 3
        result['loop'] = _normalize_loop(config['loop'])
    return result


def _normalize_loop(loop):
    loop = {} if loop is None else loop   # a bare `loop:` in YAML means "defaults"
    if not isinstance(loop, dict) or set(loop) - set(LOOP_DEFAULTS):
        raise ValueError('loop accepts only max_turns and max_active_minutes')
    result = {}
    for key, default in LOOP_DEFAULTS.items():
        value = loop.get(key, default)
        low, high = LOOP_BOUNDS[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'loop.{key} must be an integer in {low}..{high}')
        result[key] = value
    return result


def loop_budget(policy):
    """The frozen loop budget, or None for legacy (version 2) policies."""
    return dict(policy['loop']) if policy.get('version') == 3 and 'loop' in policy else None


def digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def load_config(path):
    """One snapshot of providers.yaml (F3-03): the default team, the per-role ceiling and budgets.

    Validated as a whole; `allowed` never reaches the effective policy or its digest."""
    path = Path(path)
    if not path.exists():
        config = dict(DEFAULT)
    else:
        if path.stat().st_size > 65536:
            raise ValueError('provider policy too large')
        try:
            config = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise ValueError('invalid providers.yaml') from exc
    if not isinstance(config, dict):
        raise ValueError('invalid provider policy fields')
    config = dict(config)
    has_allowed = 'allowed' in config   # a missing key is the default ceiling; `allowed: null` is an error
    allowed = config.pop('allowed', None)
    default = normalize(config)   # validates everything except `allowed`
    if has_allowed:
        if not isinstance(allowed, dict) or set(allowed) != set(PROFILES):
            raise ValueError('allowed needs author and reviewer lists')
        for role, providers in allowed.items():
            # Types first: set() and membership below must never see a list, number or null item.
            if not isinstance(providers, list) or not all(isinstance(p, str) for p in providers):
                raise ValueError(f'allowed.{role} must be a list of provider names')
            if (not 1 <= len(providers) <= MAX_ALLOWED or len(set(providers)) != len(providers)
                    or any(identity_problem(p, ceiling=True) for p in providers)):
                raise ValueError(f'allowed.{role} must list 1..{MAX_ALLOWED} distinct known providers')
            if any(not any(covers(a, i['provider']) for a in providers) for i in default['roles'][role]):
                raise ValueError(f'roles.{role} must be a subset of allowed.{role}')
        config['allowed'] = {role: list(allowed[role]) for role in PROFILES}
    return config


def allowed_for(config):
    """Per role: explicit `allowed`, otherwise that role's own default chain (never the union)."""
    if 'allowed' in config:
        return config['allowed']
    return {role: [i['provider'] for i in normalize(_without_allowed(config))['roles'][role]] for role in PROFILES}


def _without_allowed(config):
    return {k: v for k, v in config.items() if k != 'allowed'}


def effective(config, team=None):
    """The policy a message freezes. Budgets/loop are carried only when the config has them, so a
    missing `loop` stays missing (version 2) and the default team reproduces load_policy exactly."""
    base = _without_allowed(config)
    return normalize(base if team is None else dict(base, roles={r: list(team[r]) for r in PROFILES}))


def team_problems(config, team):
    """Semantic team validation for the preview and message POST; [] means valid."""
    if not isinstance(team, dict) or set(team) != set(PROFILES):
        return ([{'role': r, 'provider': '', 'reason': 'chain_length'} for r in PROFILES if not isinstance(team, dict) or r not in team]
                + [{'role': r, 'provider': '', 'reason': 'unknown_role'} for r in (team if isinstance(team, dict) else ()) if r not in PROFILES])
    allowed, problems = allowed_for(config), []
    for role in PROFILES:
        chain = team[role]
        if not isinstance(chain, list) or not 1 <= len(chain) <= MAX_CHAIN:
            problems.append({'role': role, 'provider': '', 'reason': 'chain_length'}); continue
        seen = set()
        for provider in chain:
            reason = (identity_problem(provider) or
                      ('duplicate_in_role' if provider in seen else
                       'profile_unsupported' if PROFILES[role] not in CAPABILITIES[kind(provider)]['profiles'] else
                       'provider_not_allowed' if not any(covers(a, provider) for a in allowed[role]) else None))
            if reason:
                problems.append({'role': role, 'provider': str(provider)[:80], 'reason': reason})
            if isinstance(provider, str):
                seen.add(provider)
    return problems


def team_warnings(team):
    # Two accounts or models of one tool in both roles are still one assistant reviewing itself.
    kinds = {r: {kind(p) for p in team[r]} for r in PROFILES}
    return ['same_provider_both_roles'] if kinds['author'] & kinds['reviewer'] else []


def team_of(policy):
    return {role: [i['provider'] for i in chain] for role, chain in policy['roles'].items()}


def load_policy(path):
    return effective(load_config(path))
