"""TB-29 / K-036: Web Push to the paired phone. Real VAPID + RFC 8291 encryption (the HTTP POST is captured and
decrypted with the test "phone's" key); no network."""
import base64
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app import push, store, runner_store as repo, works_store as ws
from app.provider_policy import normalize, digest, DEFAULT
from tests import test_tb12b_interrupt as tb12b
from tests.test_tb12b_interrupt import HOST, LOGIN

FCM = 'https://fcm.googleapis.com/fcm/send/abc123'


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def phone_keys():
    """What a browser makes for a subscription: an ECDH P-256 key pair and a 16-byte auth secret."""
    private = ec.generate_private_key(ec.SECP256R1())
    auth = b'0123456789abcdef'
    return private, {'p256dh': b64(private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)),
                     'auth': b64(auth)}, auth


class Endpoints(unittest.TestCase):
    def test_only_known_push_services(self):
        for url in (FCM, 'https://updates.push.services.mozilla.com/wpush/v2/x', 'https://web.push.apple.com/x',
                    'https://db5p.notify.windows.com/w/?token=x'):
            self.assertTrue(push.valid_endpoint(url), url)
        for url in ('http://fcm.googleapis.com/x', 'https://evil.example/x', 'https://fcm.googleapis.com.evil.example/x',
                    'https://fcm.googleapis.com:8443/x', 'https://u:p@fcm.googleapis.com/x', 'https://127.0.0.1:8765/api',
                    'https://evilpush.apple.com.example/x', 'file:///etc/passwd', 'not a url'):
            self.assertFalse(push.valid_endpoint(url), url)


class Gateway(unittest.TestCase):
    setUp, scopes = tb12b.Interrupt.setUp, tb12b.Interrupt.scopes

    def send(self, method, path, **kw):
        return self.gw.request(method, path, headers={'Authorization': f'Bearer {self.token}', 'Tailscale-User-Login': LOGIN,
                                                      'Origin': f'https://{HOST}'}, **kw)

    def test_subscription_lifecycle(self):
        key = self.send('GET', '/api/push/key').json()['key']
        self.assertEqual(len(base64.urlsafe_b64decode(key + '==')), 65)                  # an uncompressed P-256 point
        self.assertEqual(self.send('GET', '/api/push/key').json()['key'], key)            # made once
        self.assertEqual(oct((Path(self.db).parent / 'push_vapid.pem').stat().st_mode & 0o777), '0o600')
        self.assertEqual(self.local.get('/api/push/key').json()['code'], 'phone_only')   # the Mac panel has none
        _, keys, _ = phone_keys()
        for endpoint in ('https://evil.example/x', 'http://fcm.googleapis.com/x'):
            r = self.send('PUT', '/api/push/subscription', json={'endpoint': endpoint, 'keys': keys})
            self.assertEqual((r.status_code, r.json()['code']), (422, 'push_service'))
        self.assertEqual(self.send('PUT', '/api/push/subscription', json={'endpoint': FCM, 'keys': keys,
                                                                          'expirationTime': None}).status_code, 200)
        self.assertTrue(self.send('GET', '/api/push/subscription').json()['subscribed'])
        self.assertTrue(next(d for d in self.local.get('/api/mobile/status').json()['devices'] if d['id'] == self.device)['push'])
        self.assertFalse(self.send('DELETE', '/api/push/subscription').json()['subscribed'])
        self.send('PUT', '/api/push/subscription', json={'endpoint': FCM, 'keys': keys})
        self.local.delete(f'/api/mobile/devices/{self.device}')                          # revoking drops it too
        self.assertEqual(self.conn.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0], 0)

    def test_test_notice_is_rate_limited(self):
        with patch.object(push, '_send', return_value=201):
            _, keys, _ = phone_keys()
            self.send('PUT', '/api/push/subscription', json={'endpoint': FCM, 'keys': keys})
            self.assertEqual(self.send('POST', '/api/push/test').status_code, 200)
            r = self.send('POST', '/api/push/test')
            self.assertEqual((r.status_code, r.json()['code']), (429, 'rate_limited'))


class Delivery(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup); root = Path(tmp.name)
        self.db = str(root / 'state.db')
        self.conn = store.connect(self.db); self.addCleanup(self.conn.close); store.migrate(self.conn)
        with store.transaction(self.conn):
            self.conn.execute("INSERT INTO mobile_devices(id, name, token_hash, scopes, tailscale_login, created_at) "
                              "VALUES ('dev_1', 'Telefon', 'h', 'view', 'me', 'now')")
        self.private, keys, self.auth = phone_keys()
        push.subscribe(self.conn, 'dev_1', FCM, keys['p256dh'], keys['auth'])
        (root / 'ws').mkdir()
        self.wid = str(uuid4()); ws.create_work(self.conn, self.wid, 'Hesap makinesi', str(root / 'ws'))

    def test_real_encrypted_push_decrypts_on_the_phone(self):
        import http_ece
        posts = []

        class Response:
            status_code = 201
            text = ''
            headers: dict = {}

        def fake_post(session, url, data=None, headers=None, timeout=None, **_):
            self.assertEqual(session.max_redirects, 0)                                 # agy P3: no redirect followed
            posts.append((url, data, headers)); return Response()
        with patch('requests.Session.post', autospec=True, side_effect=fake_post):
            push.notify(self.db, 'Hesap makinesi', 'Bitti', '/ui/#/works/x', wait=True)
        url, body, headers = posts[0]
        self.assertEqual((url, headers['content-encoding']), (FCM, 'aes128gcm'))
        self.assertTrue(headers['authorization'].startswith('vapid t='))
        clear = http_ece.decrypt(body, private_key=self.private, auth_secret=self.auth, version='aes128gcm')
        self.assertEqual(json.loads(clear), {'title': 'Hesap makinesi', 'body': 'Bitti', 'url': '/ui/#/works/x',
                                             'tag': '/ui/#/works/x'})

    def new_run(self):
        policy = normalize(DEFAULT)
        ws.add_message(self.conn, self.wid, str(uuid4()), 'x', digest(policy), policy, digest(policy))
        sid = ws.dispatch_next(self.conn, self.wid)[0]
        self.assertTrue(sid)
        self.wid = str(uuid4())                                                          # the next run gets its own work
        ws.create_work(self.conn, self.wid, 'Hesap makinesi', str(Path(self.db).parent / 'ws'))
        return sid

    def watched(self, action, send_status=201):
        """Run `action`, then wait for every push thread it started; the payloads that would have been sent."""
        sent, threads, real = [], [], threading.Thread

        def record(*a, **k):
            t = real(*a, **k); threads.append(t); return t
        with patch.object(push, '_send', side_effect=lambda db, sub, payload: sent.append(payload) or send_status), \
             patch.object(push.threading, 'Thread', side_effect=record), patch.object(push, 'COMMIT_WAIT_S', 1):
            action()
            for t in threads:
                t.join(5)
        return sent

    def run_status(self, status, send_status=201):
        sid = self.new_run()
        return sid, self.watched(lambda: repo.update_session_status(self.conn, sid, status), send_status)

    def test_run_states_reach_the_phone(self):
        wid = self.wid
        sid, sent = self.run_status('SUCCEEDED')
        self.assertEqual(sent, [{'title': 'Hesap makinesi', 'body': 'Bitti', 'url': f'/ui/#/works/{wid}',
                                 'tag': f'run:{sid}'}])
        _, sent = self.run_status('WAITING_CAPACITY')
        self.assertEqual(sent[0]['body'], 'Kota doldu, bekliyor')

    def test_a_run_the_user_stopped_is_quiet_and_a_gone_subscription_is_dropped(self):
        _, sent = self.run_status('CANCELLED')
        self.assertEqual(sent, [])
        _, sent = self.run_status('FAILED', send_status=410)
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM push_subscriptions').fetchone()[0], 0)

    def test_nobody_subscribed_means_no_thread(self):
        push.unsubscribe(self.conn, 'dev_1')
        sid = self.new_run()
        with patch.object(push.threading, 'Thread') as thread:
            repo.update_session_status(self.conn, sid, 'SUCCEEDED')
        thread.assert_not_called()

    def test_only_a_committed_status_is_sent(self):   # agy TB-29 P2: the write may sit in a caller's transaction
        sid = self.new_run()

        def rolled_back():
            try:
                with store.transaction(self.conn):
                    repo.update_session_status(self.conn, sid, 'FAILED')
                    raise RuntimeError('the caller fails after the write')
            except RuntimeError:
                pass
        self.assertEqual(self.watched(rolled_back), [])

        def committed_later():
            import time
            with store.transaction(self.conn):
                repo.update_session_status(self.conn, sid, 'SUCCEEDED')
                time.sleep(0.4)                                                          # the thread waits for COMMIT
        self.assertEqual([p['body'] for p in self.watched(committed_later)], ['Bitti'])

    def test_revoked_device_gets_nothing(self):
        with store.transaction(self.conn):
            self.conn.execute("UPDATE mobile_devices SET revoked_at='now'")
        with patch.object(push, '_send') as send:
            push.notify(self.db, 't', 'b', wait=True)
        send.assert_not_called()

    def test_an_envelope_waiting_for_approval(self):
        from app.watcher import capture_file
        f = Path(self.db).parent / 'outbox' / 'plan.md'; f.parent.mkdir(); f.write_text('# Plan\nx\n')
        eid, _ = capture_file(f, self.conn)
        sent = []
        real_notify = push.notify
        with patch.object(push, '_send', side_effect=lambda db, sub, payload: sent.append(payload) or 201), \
             patch.object(push, 'notify', side_effect=lambda *a, **k: real_notify(*a, **dict(k, wait=True))):
            push.on_envelope(self.conn, eid)
        self.assertEqual((sent[0]['title'], sent[0]['tag']), ('Dosya onayı bekliyor', f'envelope:{eid}'))


if __name__ == '__main__':
    unittest.main()
