"""F3-06 gate E: old-DB migration and restore rehearsal on COPIES of real backups (not part of test discovery).

Reads the given production backups read-only (SQLite backup API) into fresh temp roots; never writes to them.
1) every copy migrates to the current schema twice: integrity ok, FK 0, every pre-existing table keeps its row
   count, and a real `serve` on the copy answers GET endpoints;
2) restore rehearsal: 1.6 backup copy → current code (1.7, new data written) → the pre-F3-04 code refuses the 1.7
   DB → restoring the 1.6 copy lets that old code run again, and the rows written after the upgrade are gone.
Run: .venv/bin/python tests/drill_f306_data.py <live .system dir> <old commit> [PORT]
F5-Kapanış: DRILL_BACKUPS (comma list) and DRILL_RESTORE (a backup whose schema the old commit runs) override the
F3-06 defaults; the expected version is the current code's SCHEMA_VERSION.
"""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4

PY = sys.executable
CODE = Path(__file__).resolve().parents[1]
LIVE = Path(sys.argv[1])
OLD_COMMIT = sys.argv[2]
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8892
BACKUPS = os.environ.get('DRILL_BACKUPS', 'state.pre-f203a.db,state.pre-cutover-20260926.db,state.pre-tb02-20260926.db,'
                         'state.pre-r1-20260929.db,state.pre-f303-20260930.db,state.pre-f304-20260930.db').split(',')
RESTORE = os.environ.get('DRILL_RESTORE', 'state.pre-f304-20260930.db')
CURRENT = subprocess.run([PY, '-c', 'from app import store; print(store.SCHEMA_VERSION)'], cwd=CODE,
                         capture_output=True, text=True).stdout.strip()
FAILED = []


def log(*parts):
    print(time.strftime('[%H:%M:%S]'), *parts, flush=True)


def check(name, ok, detail=''):
    log('PASS' if ok else 'FAIL', name, detail)
    if not ok:
        FAILED.append(name)


def copy_ro(src, dst):
    s = sqlite3.connect(f'file:{src}?mode=ro', uri=True); d = sqlite3.connect(dst)
    s.backup(d); d.close(); s.close()


def facts(path):
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    try:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        version = c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone() if 'schema_meta' in tables else None
        return {'version': version and version[0], 'counts': {t: c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables},
                'integrity': c.execute('PRAGMA integrity_check').fetchone()[0], 'fk': len(c.execute('PRAGMA foreign_key_check').fetchall())}
    finally:
        c.close()


def migrate(code_dir, db):
    return subprocess.run([PY, '-c', 'import sys; from app import store; c=store.connect(sys.argv[1]); store.migrate(c); store.migrate(c)', str(db)],
                          cwd=code_dir, capture_output=True, text=True)


def get(path):
    with urllib.request.urlopen(f'http://127.0.0.1:{PORT}{path}', timeout=10) as r:
        return r.status, json.loads(r.read())


def post(path, body):
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read())


def serve(code_dir, root):
    proc = subprocess.Popen([PY, '-m', 'app', '--root', str(root), 'serve', '--port', str(PORT)], cwd=code_dir,
                            stdout=open(root / 'server.log', 'a'), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        try:
            get('/health'); return proc
        except OSError:
            if proc.poll() is not None:
                return proc
            time.sleep(0.25)
    return proc


def stop(proc):
    if proc.poll() is None:
        os.kill(proc.pid, signal.SIGTERM); proc.wait(15)


def main():
    scratch = Path(tempfile.mkdtemp(prefix='conductor-f306-data-'))
    log('scratch', scratch)
    # 1) every real backup copy migrates losslessly and serves
    for name in BACKUPS:
        root = scratch / name.replace('.db', ''); (root / '.system').mkdir(parents=True)
        db = root / '.system/state.db'
        copy_ro(LIVE / name, db)
        before = facts(db)
        result = migrate(CODE, db)
        after = facts(db)
        kept = all(after['counts'].get(t) == n for t, n in before['counts'].items())
        proc = serve(CODE, root)
        codes = [get(p)[0] for p in ('/health', '/api/works', '/api/sessions', '/api/envelopes')] if proc.poll() is None else []
        stop(proc)
        check(f'E1 {name}: {before["version"]} → {after["version"]}',
              result.returncode == 0 and after['version'] == CURRENT and after['integrity'] == 'ok' and after['fk'] == 0 and kept
              and codes == [200, 200, 200, 200],
              f'rows_kept={kept} new_tables={sorted(set(after["counts"]) - set(before["counts"]))} integrity={after["integrity"]} '
              f'fk={after["fk"]} GET={codes} {result.stderr.strip()[-200:]}')

    # 2) restore rehearsal
    old = scratch / 'old-code'; old.mkdir()
    archive = subprocess.run(['git', '-C', str(CODE.parent), 'archive', OLD_COMMIT + ':ai-orchestrator'], capture_output=True, check=True)
    subprocess.run(['tar', '-xf', '-', '-C', str(old)], input=archive.stdout, check=True)
    old_version = subprocess.run([PY, '-c', 'from app import store; print(store.SCHEMA_VERSION)'], cwd=old, capture_output=True, text=True).stdout.strip()
    log('old code', OLD_COMMIT, 'schema', old_version)
    root = scratch / 'restore'; (root / '.system').mkdir(parents=True)
    db = root / '.system/state.db'
    copy_ro(LIVE / RESTORE, db)
    backup_version = facts(db)['version']
    proc = serve(CODE, root)
    wid = str(uuid4())
    created = post('/api/works', {'client_work_id': wid, 'title': 'written after upgrade', 'workspace_path': str(root)})[0]
    stop(proc)
    upgraded = facts(db)
    kept_copy = root / '.system/state.after-upgrade.db'; copy_ro(db, kept_copy)
    log('upgraded to', upgraded['version'], 'works rows', upgraded['counts'].get('works'), '→ current data kept at', kept_copy.name)
    refused = migrate(old, db)
    check(f'E2 old code ({old_version}) refuses the {CURRENT} DB',
          refused.returncode != 0 and f'Unsupported database schema version: {CURRENT}' in refused.stderr,
          refused.stderr.strip().splitlines()[-1] if refused.stderr else 'no error')
    for suffix in ('', '-wal', '-shm'):
        Path(str(db) + suffix).unlink(missing_ok=True)
    copy_ro(LIVE / RESTORE, db)
    proc = serve(old, root)
    codes = [get(p)[0] for p in ('/health', '/api/works', '/api/sessions')] if proc.poll() is None else []
    stop(proc)
    restored = facts(db)
    check(f'E3 restored {backup_version} backup runs on the old code; rows written after the upgrade are gone',
          created == 201 and old_version == backup_version and restored['version'] == backup_version and codes == [200, 200, 200]
          and restored['counts'].get('works', 0) == upgraded['counts']['works'] - 1 and restored['integrity'] == 'ok',
          f'old_GET={codes} restored_version={restored["version"]} works: upgraded={upgraded["counts"]["works"]} restored={restored["counts"].get("works", 0)}')
    log('RESULT', 'ALL PASS' if not FAILED else 'FAILED: ' + ', '.join(FAILED), '| scratch', scratch)
    return 1 if FAILED else 0


if __name__ == '__main__':
    sys.exit(main())
