#!/usr/local/bin/python
"""Attest the runtime's actual video buffer and the RGB bytes sent back to it."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from ken_attestation import decoded_manifest, digest_json
except ModuleNotFoundError:
    from app.ken_attestation import decoded_manifest, digest_json


def command(arguments, frame_budget=16):
    if type(frame_budget) is not int or not 1 <= frame_budget <= 128:
        raise ValueError("Invalid decoder frame budget")
    outputs = [i for i, value in enumerate(arguments) if value in ("pipe:1", "-")]
    if len(outputs) != 1 or "rawvideo" not in arguments or "rgb24" not in arguments:
        raise ValueError("Expected runtime RGB video pipe")
    index = outputs[0]
    # b10818 puts '-loglevel error' AFTER its output; output need not be last.
    return ["/usr/bin/ffmpeg", "-threads", "4", *arguments[:index], "-threads", "4",
            "-filter_threads", "2", "-frames:v", str(frame_budget), *arguments[index:]]


def attest(arguments, context, incoming, outgoing):
    if arguments[arguments.index("-i") + 1] != "cache:pipe:0":
        raise ValueError("Unattested runtime input transport")
    width, height = context["width"], context["height"]
    if not 0 < min(width, height) <= max(width, height) <= 672:
        raise ValueError("Invalid decoder dimensions")
    if arguments[arguments.index("-vf") + 1] != f"fps={context['fps']:.6f}":
        raise ValueError("Unexpected decoder sampling")
    result = dict(context["attestation"])
    frame_budget = context.get("max_decoded_frames", 16)
    process = subprocess.Popen(command(arguments, frame_budget), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    digest, size, failures = hashlib.sha256(), [0], []

    def feed():
        try:
            writable = True
            while chunk := incoming.read(1024 * 1024):
                digest.update(chunk)
                size[0] += len(chunk)
                if writable:
                    try:
                        process.stdin.write(chunk)
                    except BrokenPipeError:
                        writable = False
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
        except Exception:
            failures.append("Input stream failed")
            process.kill()

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    try:
        # Bounded by the validated per-request frame budget and dimensions. Deliver no pixels until the WHOLE
        # runtime-opened video buffer has been independently verified.
        frame_size = width * height * 3
        pixels = process.stdout.read(frame_size * frame_budget + 1)
        if len(pixels) > frame_size * frame_budget:
            process.kill()
        process.wait(timeout=120)
        feeder.join(timeout=120)
        result.update(runtime_opened_sha256=digest.hexdigest(), runtime_opened_size=size[0])
        if feeder.is_alive() or failures or process.returncode != 0:
            raise ValueError("Runtime video decode failed")
        if digest.hexdigest() != context["expected_input_sha256"] or size[0] != context["expected_input_size"]:
            raise ValueError("Runtime opened input mismatch")
        if not pixels or len(pixels) % frame_size or len(pixels) > frame_size * frame_budget:
            raise ValueError("Missing or incomplete decoded frames")
        frames = [pixels[i:i + frame_size] for i in range(0, len(pixels), frame_size)]
        manifest = decoded_manifest(frames, [int(i * 1000 / context["fps"]) for i in range(len(frames))],
                                    [(width, height)] * len(frames))
        outgoing.write(pixels)
        outgoing.flush()
        result.update(status="verified", decoded_manifest=manifest, decoded_input_fingerprint=digest_json(manifest),
                      decoder="llama-b10818-buffer-ffmpeg-rgb24-v1")
    except Exception:
        result.update(status="failed", error="Runtime input or decoded-frame verification failed")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
    return result


def main():
    context_path = Path(os.environ["PV_KEN_DECODE_CONTEXT"])
    context = json.loads(context_path.read_text())
    try:
        result = attest(sys.argv[1:], context, sys.stdin.buffer, sys.stdout.buffer)
    except Exception:
        result = {**context["attestation"], "status": "failed", "error": "Runtime decoder attestation failed"}
    target = context_path.parent / "decoded.json"
    temporary = target.with_suffix(".partial")
    temporary.write_text(json.dumps(result))
    temporary.replace(target)
    return 0 if result["status"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
