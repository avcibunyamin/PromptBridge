#!/bin/bash
# Run Conductor as a launchd user agent (gui/<uid>), restarted on every exit (KeepAlive).
#   deploy/launchd.sh install     render the plist from the template (PATH = this shell's PATH), load it, wait for /health
#   deploy/launchd.sh status      launchctl state + /health
#   deploy/launchd.sh restart     launchctl kickstart -k
#   deploy/launchd.sh uninstall   launchctl bootout + remove the plist (the ONLY way to stop it; `kill` gets restarted)
# Rehearsal overrides (production defaults unchanged): LABEL=… ROOT=<temp root with .system/> PORT=… PY=<python>
# Never touches .system/ or the database.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
CODE="$(dirname "$HERE")"                                  # ai-orchestrator/
LABEL="${LABEL:-com.promptbridge.conductor}"
PORT="${PORT:-8765}"
ROOT="${ROOT:-}"
PY="${PY:-$CODE/.venv/bin/python}"
TEMPLATE="$HERE/com.promptbridge.conductor.plist.template"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
# macOS may block launchd from writing logs inside ~/Desktop (TCC / com.apple.macl), so logs go to ~/Library/Logs/Conductor.
# Rehearsals keep $ROOT/.system.
LOG="${CONDUCTOR_LOG:-${ROOT:+$ROOT/.system/server-launchd.log}}"
LOG="${LOG:-$HOME/Library/Logs/Conductor/server-launchd.log}"

fail() { echo "launchd.sh: $*" >&2; exit 1; }
loaded() { launchctl print "$SERVICE" >/dev/null 2>&1; }
listening() { lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; }
health() { curl -s --max-time 2 "http://127.0.0.1:$PORT/health"; }

stop_agent() {   # bootout (launchd sends SIGTERM, waits, then SIGKILL); it is asynchronous, so wait until
                 # the service is really gone and the port is free before anything is loaded again
    if loaded; then
        launchctl bootout "$SERVICE" || fail "bootout failed for $SERVICE"
        for _ in $(seq 1 60); do
            if ! loaded && ! listening; then return 0; fi
            sleep 0.5
        done
        fail "$LABEL still loaded or port $PORT still in use after bootout"
    fi
}

bootstrap() {   # a just-removed label can still be refused for a moment; retry briefly
    for _ in 1 2 3 4 5; do
        launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && return 0
        sleep 1
    done
    launchctl bootstrap "$DOMAIN" "$PLIST" || fail "bootstrap failed"
}

render() {   # plistlib does the escaping; only PATH goes into the environment
    "$PY" - "$TEMPLATE" "$1" "$LABEL" "$PY" "$CODE" "$PATH" "$LOG" "$ROOT" "$PORT" <<'PYEOF'
import plistlib, sys
template, out, label, py, code, path, log, root, port = sys.argv[1:]
data = plistlib.load(open(template, 'rb'))
args = [py, '-m', 'app'] + (['--root', root] if root else []) + ['serve'] + (['--port', port] if port != '8765' else [])
values = {'__LABEL__': label, '__WORKDIR__': code, '__PATH__': path, '__LOG__': log}
data['Label'] = values['__LABEL__']
data['ProgramArguments'] = args
data['WorkingDirectory'] = values['__WORKDIR__']
data['EnvironmentVariables'] = {'PATH': values['__PATH__']}
data['StandardOutPath'] = data['StandardErrorPath'] = values['__LOG__']
text = plistlib.dumps(data).decode()
if set(data['EnvironmentVariables']) != {'PATH'} or any(p in text for p in list(values) + ['__PROGRAM_ARGUMENTS__']):
    sys.exit('unfilled placeholder or extra environment variable')
plistlib.dump(data, open(out, 'wb'))
PYEOF
}

case "${1:-}" in
install)
    [ -x "$PY" ] || fail "missing $PY (create the venv first)"
    if [ -n "$ROOT" ]; then [ -d "$(dirname "$LOG")" ] || fail "missing $(dirname "$LOG") (log folder)"; else mkdir -p "$(dirname "$LOG")"; fi
    tmp="$(mktemp -t conductor-plist)"
    render "$tmp"                                           # render and lint before stop_agent so a broken plist never stops a running agent
    plutil -lint "$tmp" >/dev/null || { rm -f "$tmp"; fail "rendered plist failed plutil -lint"; }
    stop_agent                                              # idempotent: a loaded agent is replaced
    listening && { rm -f "$tmp"; fail "port $PORT is in use by another process (e.g. a nohup serve); stop it first"; }
    mkdir -p "$(dirname "$PLIST")"
    mv "$tmp" "$PLIST"; chmod 644 "$PLIST"
    bootstrap
    launchctl kickstart "$SERVICE" >/dev/null 2>&1 || true   # a just-replaced job can stay loaded but not started (2026-10-23); kickstart without -k only starts it if idle
    for _ in $(seq 1 80); do
        if health >/dev/null; then echo "installed $LABEL on port $PORT"; health; echo; exit 0; fi
        sleep 0.25
    done
    fail "agent loaded but /health did not answer; see $LOG and: launchctl print $SERVICE"
    ;;
status)
    if loaded; then
        launchctl print "$SERVICE" | grep -E '^\s*(state|pid|last exit code|runs) =' || true
    else
        echo "$LABEL not loaded"
    fi
    health && echo || echo "/health: no answer on port $PORT"
    ;;
restart)
    loaded || fail "$LABEL not loaded (use install)"
    launchctl kickstart -k "$SERVICE"
    ;;
uninstall)
    stop_agent
    rm -f "$PLIST"
    echo "uninstalled $LABEL (port $PORT free)"
    ;;
*)
    echo "usage: $0 install|status|restart|uninstall" >&2; exit 2 ;;
esac
