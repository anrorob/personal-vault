"""Internal, single-inference Qwen adapter service. No Vault/data credentials."""

import binascii
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import resource
import re
import subprocess
import tempfile
import threading
import time
from uuid import UUID

from service_input import evidence
try:
    from ken_attestation import valid_attestation
except ModuleNotFoundError:
    from app.ken_attestation import valid_attestation

REVISION = "f982a07559d4a2f6c8744d840bf6fccab30eea96"
RUNTIME = "llama.cpp b10818 (4d9176092) / ken-owner-style-v1"
MODEL_ROOT = Path(os.getenv("PV_KEN_MODEL_ROOT", "/models"))
BINARY = os.getenv("PV_KEN_BINARY", "/opt/llama/llama-mtmd-cli")
FILES = {
    "Qwen3VL-8B-Instruct-Q8_0.gguf": "0d264b3941185d00a74f75c4245521dae088ff1efc90ab8d1754e83f5844adb0",
    "mmproj-Qwen3VL-8B-Instruct-F16.gguf": "ca524100ebf825c9a870db1c580d03879e0da0ab2541697e2458e64891cf9d38",
}
LOCK = threading.Lock()
STATE = "loading"
MAX_BODY = 44 * 1024 * 1024  # Bounded context metadata, never the source video.


def verify_models():
    global STATE
    try:
        for name, digest in FILES.items():
            with (MODEL_ROOT / name).open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise ValueError("Model checksum mismatch")
        STATE = "available"
    except (OSError, ValueError):
        STATE = "failed"


def launch_model(command, **kwargs):
    return subprocess.Popen(command, **kwargs)


def execute(body: dict) -> dict:
    attestation = evidence(body)
    try:
        if body.get('native_parameters', {}).get('version') == 'ken-full-video-v1':
            from ken_infer import execute_ken
            return execute_ken(body, execute_attested)
        raise ValueError("Only full-video KEN requests are supported")
    except (ValueError, OSError, subprocess.SubprocessError):
        return {**{key: body.get(key) for key in ("asset_id", "run_id", "input_fingerprint")},
                **({'correction_fingerprint': body['correction_fingerprint']} if 'correction_fingerprint' in body else {}),
                "model_revision": REVISION, "runtime": RUNTIME, "input_mode": body.get("input_mode"),
                "description": None, "raw_response": "", "error": "Input integrity verification failed",
                "input_attestation": attestation}


def execute_attested(body: dict, attestation: dict, *, native_override=None) -> dict:
    binding = {key: body[key] for key in ("asset_id", "run_id", "input_fingerprint")}
    for key in ("asset_id", "run_id"):
        if str(UUID(binding[key])) != binding[key]:
            raise ValueError("Invalid KEN identity")
    if not re.fullmatch(r"[a-f0-9]{64}", binding["input_fingerprint"]):
        raise ValueError("Invalid input fingerprint")
    if (not isinstance(body.get("expected_input_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", body["expected_input_sha256"])
            or type(body.get("expected_input_size")) is not int or body["expected_input_size"] <= 0
            or str(UUID(body["request_nonce"])) != body["request_nonce"]):
        raise ValueError("Missing service input identity")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= (12000 if native_override else 8000):
        raise ValueError("Invalid prompt")
    params = body.get("parameters", {})
    if native_override is None:
        raise ValueError('KEN chunk input is required')
    expected = {"temperature": 0, "seed": 1, "max_tokens": params.get('max_tokens'), "context_size": 16384, "threads": 18, "quantisation": "Q8_0"}
    if expected['max_tokens'] not in (192, 384):
        raise ValueError('KEN context budget exceeded')
    from ken_infer import check_context
    text_tokens = check_context(prompt, visual_tokens=44//2*192, output_tokens=expected['max_tokens'])
    if params != expected:
        raise ValueError("Unsupported parameters")
    mode = body.get("input_mode")
    native = None
    with tempfile.TemporaryDirectory(prefix="pv-model-") as directory, ExitStack() as cleanup:
        work = Path(directory)
        fds = []
        cleanup.callback(lambda: [os.close(fd) for fd in fds])
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("LLAMA_ARG_") and key not in ("MTMD_TEST_RESPONSE_MARKER", "PV_KEN_DECODE_CONTEXT")}
        command = [BINARY, "-m", str(MODEL_ROOT / "Qwen3VL-8B-Instruct-Q8_0.gguf"),
                   "--mmproj", str(MODEL_ROOT / "mmproj-Qwen3VL-8B-Instruct-F16.gguf"),
                   "--no-mmproj-offload", "-ngl", "0", "-t", "18", "-c", "16384",
                   "-n", str(params["max_tokens"]), "--temp", "0", "--seed", "1", "--no-warmup", "--perf", "--offline",
                   "--system-prompt", ""]
        if mode == "native_video":
            path, video_params, identity = native_override
            command += ["--image-min-tokens", str(video_params["image_min_tokens"]),
                        "--image-max-tokens", str(video_params["image_max_tokens"])]
            native = (path, identity)
            context = {"attestation": attestation, "expected_input_sha256": body["expected_input_sha256"],
                       "expected_input_size": body["expected_input_size"], "max_decoded_frames": video_params["max_decoded_frames"], "fps": video_params["fps"],
                       "width": video_params["prepared_stream"]["width"], "height": video_params["prepared_stream"]["height"]}
            context_path = work / "decoder-context.json"
            context_path.write_text(json.dumps(context))
            environment["PV_KEN_DECODE_CONTEXT"] = str(context_path)
            command += ["--video", str(path), "--video-fps", str(video_params["fps"]),
                        "--video-timestamp-interval", str(video_params["timestamp_interval_ms"]),
                        "--video-ffmpeg-dir", "/app/video-bin"]
        else:
            raise ValueError("Invalid input mode")
        command += ["-p", prompt]
        if native_override:
            # Upstream maps libllama INFO timing counters to TRACE (4), while
            # prompt-debug logging remains disabled at DEBUG (5).
            command += ['--repeat-penalty', '1.1', '--log-verbosity', '4']
        started = time.monotonic()
        failure = None
        repetition_stopped = False
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        with (work / "stdout").open("w+") as out, (work / "stderr").open("w+") as err:
            process = launch_model(command, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                       start_new_session=True, pass_fds=tuple(fds), env=environment)
            try:
                if not native:
                    process.wait(timeout=1800)
                else:
                    deadline = time.monotonic() + 1800
                    while True:
                        try:
                            process.wait(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            if native_override:
                                try:
                                    from ken_repetition import repeated_tail
                                except ModuleNotFoundError:
                                    from app.ken_repetition import repeated_tail
                                with (work / 'stdout').open() as progress:
                                    tail = progress.read(1024 * 1024).split()
                                if repeated_tail(tail):
                                    import signal
                                    os.killpg(process.pid, signal.SIGKILL)
                                    process.wait()
                                    repetition_stopped = True
                                    break
                            path = work / "decoded.json"
                            if path.exists() and json.loads(path.read_text()).get("status") != "verified":
                                failure = "Input integrity verification failed"
                                raise
                            if time.monotonic() >= deadline:
                                raise
            except subprocess.TimeoutExpired:
                # Kill the entire model/decoder process group, then reap it before admitting another run.
                import signal
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                failure = failure or "Inference timeout"
            if native:
                attestation_path = work / "decoded.json"
                if attestation_path.is_file() and attestation_path.stat().st_size <= 65536:
                    attestation = json.loads(attestation_path.read_text())
            verified = valid_attestation(body, attestation)
            if not verified:
                failure = "Input integrity verification failed"
            out.seek(0); err.seek(0)
            raw = out.read(1024 * 1024) if verified else ""
            diagnostics = err.read(1024 * 1024) if verified else ""
            # Keep only numeric timing data. Runtime stderr may echo prompts/output;
            # never return it to operational logging or duplicate it in run records.
            load = re.search(r"load time\s*=\s*([0-9.]+) ms", diagnostics)
            load_ms = float(load.group(1)) if load else None
            evaluated = re.search(r"prompt eval time\s*=.*?/\s*(\d+) tokens", diagnostics)
            prompt_eval_tokens = int(evaluated.group(1)) if evaluated else None
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        if native and (native[0].stat().st_size, native[0].stat().st_mtime_ns) != native[1]:
            failure = "Native input changed during inference"
        if process.returncode != 0 and not failure and not repetition_stopped:
            failure = "Model execution failed"
        if not raw.strip() and not failure:
            failure = "Model returned no description"
        return {
            **binding,
            "model_revision": REVISION, "runtime": RUNTIME, "input_mode": mode,
            "description": raw.strip() if not failure else None, "structured_observations": None, "error": failure,
            "raw_response": raw if not failure else "", "model_load_ms": load_ms,
            "input_attestation": attestation,
            "prompt_eval_tokens": prompt_eval_tokens, "max_output_tokens": params["max_tokens"],
            "processing_ms": round((time.monotonic() - started) * 1000),
            'repetition_stopped': repetition_stopped,
            'text_tokens': text_tokens if native_override else None,
            "resources": {"process_lifetime_peak_rss_kib": after.ru_maxrss,
                          "cpu_seconds": round(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3)},
            "warnings": [f"KEN output for user verification. Maximum output: {params['max_tokens']} tokens; long responses may still be truncated.",
                         "Native temporal sampling is bounded and timestamps approximate; brief events between observations can still be missed." if mode == "native_video" else
                         "Ordered samples cover the duration; brief events between samples may be missed."],
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Do not log media, prompts, request bodies, or paths.

    def send_json(self, status, body):
        content = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path != "/health":
            return self.send_json(404, {})
        self.send_json(200, {"status": "busy" if LOCK.locked() else STATE, "model_revision": REVISION, "runtime": RUNTIME})

    def do_POST(self):
        if self.path not in ("/analyse", "/title"):
            return self.send_json(404, {})
        if STATE != "available" or not LOCK.acquire(blocking=False):
            return self.send_json(503, {"error": "Analyser unavailable or busy"})
        try:
            self.connection.settimeout(36000)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            if self.path == "/title":
                from ken_infer import execute_title
                self.send_json(200, execute_title(body))
            else:
                self.send_json(200, execute(body))
        except (ValueError, KeyError, TypeError, binascii.Error, subprocess.SubprocessError):
            self.send_json(400, {"error": "Invalid analyser input"})
        except Exception:
            self.send_json(500, {"error": "Model execution failed"})
        finally:
            LOCK.release()


if __name__ == "__main__":
    threading.Thread(target=verify_models, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
