#!/usr/bin/env python3
"""Opt-in real OS process for crash rehearsal; never a real model response."""
import json
import sys
import time


def emit(message):
    print(json.dumps(message),flush=True)


def read():
    return json.loads(sys.stdin.readline())


request=read(); emit({'id':request['id'],'result':{}})
assert read()['method']=='initialized'
request=read(); emit({'id':request['id'],'result':{'thread':{'id':'crash-fixture-thread'}}})
request=read(); emit({'id':request['id'],'result':{'turn':{'id':'fixture-turn'}}})
for n in range(60):
    try:
        emit({'method':'item/agentMessage/delta','params':{'threadId':'crash-fixture-thread',
              'turnId':'fixture-turn','itemId':'message','delta':f'Fixture event {n}; no model invoked.\n'}})
    except BrokenPipeError:
        break
    time.sleep(1)
# Survives transport EOF/server crash until the real recovery reaper terminates it.
time.sleep(300)
