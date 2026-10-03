import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest.mock import patch, Mock

import native_input
import server
import video_ffmpeg
from test_server import request_body


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / "pv-ken-12345678"
        self.work.mkdir()
        self.path = self.root / "fixture.mp4"
        self.path.write_bytes(b"synthetic video only")
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        from app.ken_config import policy
        self.params = {**policy(3000),"prepared_kind":"canonical_video","prepared_sha256":digest,
                       "prepared_stream":{"duration_ms":3000,"width":320,"height":192}}
        self.manifest = {"asset_id": request_body()["asset_id"], "input_mode": "native_video",
                         "native_parameters": self.params, "prepared_video_sha256": digest,"correction_context":{}}
        self.body = {**request_body(), "input_mode": "native_video",
                     "native_parameters":self.params,"correction_context":{},"native_job": self.work.name, "native_sha256": digest, "expected_input_sha256": digest, "expected_input_size": self.path.stat().st_size}
        self.record = {"asset_id": self.body["asset_id"], "run_id": self.body["run_id"], "integrity_manifest": self.manifest,
                       "runtime_video": self.params,
                       "reference": {"kind": "canonical_video", "relative_path": "fixture.mp4"}}
        self.save()
        for name in ("INPUT_ROOT", "VIDEO_ROOT"):
            patcher = patch.object(native_input, name, self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        probe = patch.object(native_input.subprocess, "run", return_value=Mock(stdout=json.dumps(
            {"streams": [{"width": 320, "height": 192, "duration": "3"}]})))
        probe.start()
        self.addCleanup(probe.stop)

    def execute_chunk(self):
        from service_input import evidence
        proof=evidence(self.body)
        native=native_input.resolve_native(self.body,proof)
        with patch('ken_infer.check_context',return_value=10):
            return server.execute_attested(self.body,proof,native_override=native)

    def save(self):
        fingerprint = hashlib.sha256(json.dumps(self.manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
        self.record["input_fingerprint"] = self.body["input_fingerprint"] = fingerprint
        (self.work / "native.json").write_text(json.dumps(self.record))

    def test_actual_bytes_and_asset_binding_are_required(self):
        self.assertEqual(native_input.resolve_native(self.body)[0], self.path)
        with self.assertRaises(ValueError):
            native_input.resolve_native({**self.body, "asset_id": "another-asset"})
        with self.assertRaises(ValueError):
            native_input.resolve_native({**self.body, "run_id": "another-run"})
        self.path.write_bytes(b"stale input from another run")
        with self.assertRaisesRegex(ValueError, "checksum"):
            native_input.resolve_native(self.body)

    def test_paths_and_capabilities_cannot_escape_their_roots(self):
        for token in ("../outside", str(self.root), "pv-ken-../../outside"):
            with self.subTest(token=token), self.assertRaises(ValueError):
                native_input.resolve_native({**self.body, "native_job": token})
        for relative in ("../outside.mp4", "/etc/passwd"):
            self.record["reference"]["relative_path"] = relative
            self.save()
            with self.assertRaises(ValueError):
                native_input.resolve_native(self.body)

    def test_temporal_budget_and_preparation_kind_are_enforced(self):
        for key, value in (("fps", 5), ("fps", 0), ("max_decoded_frames", 17),
                           ("max_dimension", 1024), ("timestamp_interval_ms", 0),
                           ("prepared_kind", "prepared_native_video")):
            original = self.params[key]
            self.params[key] = value
            self.save()
            with self.subTest(key=key), self.assertRaises(ValueError):
                native_input.resolve_native(self.body)
            self.params[key] = original

    def test_native_command_uses_video_and_no_system_policy_or_jpeg_inputs(self):
        commands = []
        def launch(command, **kwargs):
            commands.append(command)
            kwargs["stdout"].write("synthetic protocol response")
            return Mock(returncode=0, wait=Mock(return_value=0))
        with patch.object(server, "launch_model", side_effect=launch):
            result = self.execute_chunk()
        self.assertEqual(result["error"], "Input integrity verification failed")
        self.assertEqual(result["raw_response"], "") # Nonempty CLI text alone is insufficient.
        command = commands[0]
        self.assertNotIn("--image", command)
        self.assertEqual(command[command.index("--video") + 1], str(self.path))
        self.assertEqual(command[command.index("--system-prompt") + 1], "")
        self.assertIn("--video-ffmpeg-dir", command)

    def test_native_video_changed_during_inference_fails(self):
        def launch(command, **kwargs):
            kwargs["stdout"].write("synthetic protocol response")
            self.path.write_bytes(b"changed")
            return Mock(returncode=0, wait=Mock(return_value=0))
        with patch.object(server, "launch_model", side_effect=launch):
            result = self.execute_chunk()
        self.assertIsNone(result["description"])
        self.assertEqual(result["error"], "Native input changed during inference")

    def test_stale_native_bytes_fail_before_model_launch_with_actual_digest(self):
        self.path.write_bytes(b"different synthetic video")
        proof={}
        with patch.object(server,'launch_model') as launch:
            with self.assertRaisesRegex(ValueError,'checksum'):
                native_input.resolve_native(self.body,proof)
        launch.assert_not_called()
        self.assertEqual(proof['resolved_input_sha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_failed_decoder_attestation_terminates_text_only_inference(self):
        def launch(command, **kwargs):
            context = Path(kwargs["env"]["PV_KEN_DECODE_CONTEXT"])
            (context.parent / "decoded.json").write_text(json.dumps({"status": "failed"}))
            return Mock(pid=12345, returncode=0, wait=Mock(side_effect=[subprocess.TimeoutExpired(command, 1), 0]))
        with patch.object(server, "launch_model", side_effect=launch), patch.object(server.os, "killpg") as kill:
            result = self.execute_chunk()
        kill.assert_called_once()
        self.assertEqual(result["error"], "Input integrity verification failed")
        self.assertEqual(result["raw_response"], "")

    def test_decoder_wrapper_bounds_frames_without_changing_runtime_fps(self):
        args = ["-i", "synthetic.mp4", "-vf", "fps=0.350000", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1", "-loglevel", "error"]
        command = video_ffmpeg.command(args)
        self.assertEqual(command[command.index("-frames:v") + 1], "16")
        self.assertEqual(command[command.index("-vf") + 1], "fps=0.350000")
        self.assertLess(command.index("-threads"), command.index("-i"))
        self.assertEqual(command[-2:], ["-loglevel", "error"])
        with self.assertRaises(ValueError):
            video_ffmpeg.command(["-i", "synthetic.mp4", "arbitrary-output.mp4"])
