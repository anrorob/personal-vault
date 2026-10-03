"""Synthetic model assets and shared-read-only mount contracts."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]








def test_actual_model_verifier_only_reads_synthetic_files(tmp_path):
    # Extract the actual pure verifier without importing platform-specific inference
    # dependencies. Never load weights, execute a model or substitute real media.
    tree = ast.parse((ROOT / "video_analyser/server.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "verify_models")
    model = tmp_path / "example.gguf"
    model.write_bytes(b"synthetic immutable model")
    before = (model.read_bytes(), model.stat().st_mtime_ns, model.stat().st_size)
    scope = {"MODEL_ROOT": tmp_path, "FILES": {model.name: hashlib.sha256(before[0]).hexdigest()},
             "hashlib": hashlib, "STATE": "loading"}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "model-verifier", "exec"), scope)
    scope["verify_models"]()
    assert scope["STATE"] == "available"
    assert (model.read_bytes(), model.stat().st_mtime_ns, model.stat().st_size) == before
    assert list(tmp_path.iterdir()) == [model]
