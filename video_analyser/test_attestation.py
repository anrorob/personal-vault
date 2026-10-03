"""Real FFmpeg inputs, no user media or model weights needed."""
import hashlib
import io
import subprocess
import unittest

import video_ffmpeg
from service_input import evidence
from test_server import request_body


class DecoderAttestationTests(unittest.TestCase):
    def video(self, colour):
        return subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color={colour}:s=32x32:r=4:d=1",
                               "-c:v", "libx264", "-threads", "1", "-movflags", "frag_keyframe+empty_moov",
                               "-f", "mp4", "pipe:1"], capture_output=True, check=True).stdout

    def decode(self, actual, expected=None):
        expected = actual if expected is None else expected
        context = {"width": 32, "height": 32, "fps": 2.0, "expected_input_size": len(expected),
                   "expected_input_sha256": hashlib.sha256(expected).hexdigest(), "attestation": evidence(request_body())}
        output = io.BytesIO()
        result = video_ffmpeg.attest(["-nostdin", "-read_ahead_limit", "-1", "-i", "cache:pipe:0", "-vf", "fps=2.000000",
                                     "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1", "-loglevel", "error"],
                                    context, io.BytesIO(actual), output)
        return result, output.getvalue()

    def test_exact_pinned_argument_order_decodes_and_hashes_actual_rgb_bytes(self):
        source = self.video("red")
        a, pixels = self.decode(source)
        self.assertEqual(a["status"], "verified")
        self.assertEqual(a["runtime_opened_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(a["runtime_opened_size"], len(source))
        self.assertEqual(a["decoded_manifest"]["frame_count"], 2)
        self.assertEqual(a["decoded_manifest"]["frames"][0]["sha256"], hashlib.sha256(pixels[:32*32*3]).hexdigest())
        repeated, _ = self.decode(source)
        self.assertEqual(a["decoded_input_fingerprint"], repeated["decoded_input_fingerprint"])
        b, _ = self.decode(self.video("blue"))
        self.assertNotEqual(a["runtime_opened_sha256"], b["runtime_opened_sha256"])
        self.assertNotEqual(a["decoded_input_fingerprint"], b["decoded_input_fingerprint"])

    def test_stale_runtime_buffer_emits_no_frames_and_keeps_actual_hash(self):
        actual, expected = self.video("blue"), self.video("red")
        result, pixels = self.decode(actual, expected)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(pixels, b"")
        self.assertEqual(result["runtime_opened_sha256"], hashlib.sha256(actual).hexdigest())

    def test_invalid_video_cannot_succeed_with_zero_visual_input(self):
        result, pixels = self.decode(b"not a video")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(pixels, b"")

    def test_three_second_composition_change_has_multiple_dense_observations(self):
        source = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:s=32x32:r=4:d=40",
                                 "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='between(t,17,20)'",
                                 "-c:v", "libx264", "-threads", "1", "-movflags", "frag_keyframe+empty_moov",
                                 "-f", "mp4", "pipe:1"], capture_output=True, check=True).stdout
        observations = []
        for fps, budget in ((0.35,16),(2.0,128)):
            context = {"width":32,"height":32,"fps":fps,"max_decoded_frames":budget,
                       "expected_input_size":len(source),"expected_input_sha256":hashlib.sha256(source).hexdigest(),
                       "attestation":evidence(request_body())}
            output = io.BytesIO()
            result = video_ffmpeg.attest(["-nostdin","-read_ahead_limit","-1","-i","cache:pipe:0",
                                         "-vf",f"fps={fps:.6f}","-f","rawvideo","-pix_fmt","rgb24","pipe:1","-loglevel","error"],
                                        context,io.BytesIO(source),output)
            self.assertEqual(result["status"],"verified")
            pixels = output.getvalue()
            red = sum(pixels[i] > 200 and pixels[i+2] < 50 for i in range(0,len(pixels),32*32*3))
            observations.append(red)
            self.assertEqual(result["decoded_manifest"]["frame_count"], round(40*fps))
        self.assertGreaterEqual(observations[1],6)
        self.assertGreater(observations[1],observations[0])
