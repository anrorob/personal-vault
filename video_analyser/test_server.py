import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import server


def request_body():
    return {'asset_id':'00000000-0000-0000-0000-000000000001',
            'run_id':'00000000-0000-0000-0000-000000000002',
            'request_nonce':'00000000-0000-0000-0000-000000000003',
            'input_fingerprint':'a'*64,'prompt':'Synthetic visual instruction',
            'parameters':{'temperature':0,'seed':1,'max_tokens':384,'context_size':16384,'threads':18,'quantisation':'Q8_0'}}


class ServiceTests(unittest.TestCase):
    def test_unsupported_modes_never_launch_model(self):
        for mode in ('sampled_frames','unknown'):
            with patch.object(server,'launch_model') as launch:
                result=server.execute({**request_body(),'input_mode':mode})
                self.assertIsNotNone(result['error'])
                launch.assert_not_called()

    def test_health_checksum_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as path, patch.object(server, "MODEL_ROOT", Path(path)):
            server.verify_models()
            self.assertEqual(server.STATE, "failed")

    def test_http_single_flight_and_health(self):
        service = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{service.server_port}"
        entered, release = threading.Event(), threading.Event()
        def blocked(body):
            entered.set()
            release.wait(5)
            return {"description": "result"}
        responses = []
        def invoke():
            with urlopen(Request(url + "/analyse", data=json.dumps(request_body()).encode()), timeout=10) as response:
                responses.append(json.load(response))
        try:
            with patch.object(server, "STATE", "available"), patch.object(server, "execute", blocked):
                request_thread = threading.Thread(target=invoke)
                request_thread.start()
                self.assertTrue(entered.wait(3))
                with urlopen(url + "/health") as response:
                    self.assertEqual(json.load(response)["status"], "busy")
                with self.assertRaises(HTTPError) as error:
                    invoke()
                self.assertEqual(error.exception.code, 503)
                release.set()
                request_thread.join(5)
                self.assertEqual(responses, [{"description": "result"}])
        finally:
            release.set()
            service.shutdown()
            service.server_close()
