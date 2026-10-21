"""TB-45 built-in prompt recipes (role-specific checklists frozen into message policy)."""

RECIPES = {
    'guvenlik': {
        'version': 1,
        'title': 'Güvenlik odaklı',
        'hint': 'Girdi doğrulama, yetki sınırları, sır sızıntısı, kabuk/yol enjeksiyonu',
        'author': (
            '- Validate and bound all external inputs (types, lengths, allowed values) before use.\n'
            '- Enforce authority and permission boundaries on every mutating or sensitive path.\n'
            '- Never log, return, or commit secrets, tokens, or sensitive local paths.\n'
            '- Avoid shell invocation and canonicalize file paths against traversal or symlink escape.\n'
            '- Fail closed with explicit error codes when security preconditions are not met.'
        ),
        'reviewer': (
            '- Verify strict input validation and bounds on every new or modified parameter.\n'
            '- Check that authentication, loopback, and mutation guards cover all state-changing paths.\n'
            '- Confirm no secrets, credentials, or internal filesystem paths leak in responses or logs.\n'
            '- Inspect subprocess and filesystem calls for shell injection, path traversal, or TOCTOU risks.\n'
            '- Ensure error handling fails closed without exposing sensitive internals.'
        ),
    },
    'hata': {
        'version': 1,
        'title': 'Hata ayıklama + regresyon testi',
        'hint': 'Önce yeniden üret, kök neden, düzeltmeyi kanıtlayan test',
        'author': (
            '- Reproduce the failure mode first and trace it to the exact root cause before editing.\n'
            '- Fix the underlying cause rather than masking symptoms or swallowing exceptions.\n'
            '- Keep the fix minimal and scoped to the affected code path.\n'
            '- Add a deterministic regression test that fails without the fix and passes with it.\n'
            '- Verify that existing tests and invariants around the changed path still hold.'
        ),
        'reviewer': (
            '- Check that the root cause is addressed rather than a surface symptom.\n'
            '- Verify the regression test directly exercises the bug scenario and would fail without the fix.\n'
            '- Look for off-by-one, race, or state-cleanup edge cases in the surrounding logic.\n'
            '- Confirm no unrelated refactors or silent behavior changes were introduced.'
        ),
    },
    'mimari': {
        'version': 1,
        'title': 'Mimari uyum',
        'hint': 'Mevcut kalıplar, gereksiz soyutlama yok, karar kaydı gerekiyor mu',
        'author': (
            '- Follow existing module responsibilities, naming conventions, and data flow patterns.\n'
            '- Do not introduce extra layers, wrappers, or abstractions unless required by the task.\n'
            '- Preserve SQLite transaction, state-transition, and audit invariants.\n'
            '- Check whether any architectural deviation requires a decision record before proceeding.\n'
            '- Keep public interfaces small, explicit, and backward-compatible where expected.'
        ),
        'reviewer': (
            '- Verify the change fits existing module boundaries and codebase conventions.\n'
            '- Flag unnecessary abstractions, indirection, or speculative generality.\n'
            '- Check that transaction boundaries, state transitions, and idempotency rules remain intact.\n'
            '- Note whether the change alters architecture in a way that requires a decision log entry.'
        ),
    },
    'arayuz': {
        'version': 1,
        'title': 'Arayüz uyumu',
        'hint': 'Mac ve telefon, erişilebilirlik, React hook kuralı, metinler Türkçe',
        'author': (
            '- Ensure layouts and interactions work cleanly on both desktop (Mac) and mobile viewports.\n'
            '- Maintain accessibility: semantic elements, labels, keyboard navigation, and focus states.\n'
            '- Follow React hook rules strictly (top-level calls only, accurate dependency arrays).\n'
            '- Write all user-facing labels, messages, and hints in clear, natural Turkish.\n'
            '- Handle loading, empty, and error states without layout jumps.'
        ),
        'reviewer': (
            '- Check responsive behavior and touch/click usability for both Mac and phone screens.\n'
            '- Verify accessibility attributes, semantic markup, and keyboard/focus behavior.\n'
            '- Inspect React components for hook rule violations, stale closures, or render loops.\n'
            '- Confirm all user-visible text is in Turkish and consistent with existing panel terminology.'
        ),
    },
}


def frozen(rid):
    if not isinstance(rid, str) or rid not in RECIPES:
        raise ValueError(f'unknown recipe: {rid}')
    r = RECIPES[rid]
    return {
        'id': rid,
        'version': r['version'],
        'title': r['title'],
        'author': r['author'],
        'reviewer': r['reviewer'],
    }
