"""Synthetic scanner/maintenance-input tests, without private runtime values."""
import importlib.util
from pathlib import Path
import json
import subprocess
import sys
import tarfile
from uuid import UUID, uuid4
import pytest
from app.home_video_legacy_tag_cleanup import cleanup

spec = importlib.util.spec_from_file_location('source_privacy', Path(__file__).resolve().parents[2] / 'scripts/check_source_privacy.py')
privacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(privacy)


def test_unreviewed_identity_is_reported_without_value():
    value = str(uuid4()).encode()
    result = privacy.inspect('backend/app/example.py', value)
    assert result == [{'file': 'backend/app/example.py', 'line': 1, 'kind': 'non-synthetic-uuid'}]
    assert value.decode() not in str(result)


def test_fake_identity_is_allowed_only_in_fixtures():
    value = str(UUID(int=42)).encode()
    assert privacy.inspect('backend/tests/test_example.py', value) == []
    assert privacy.inspect('backend/app/example.py', value)


def test_runtime_artifacts_secret_material_and_disguised_images_are_rejected():
    assert privacy.inspect('data/example.db', b'fixture')[0]['kind'] == 'runtime-artifact'
    assert privacy.inspect('docs/example.svg', b'\x89PNGfake')[0]['kind'] == 'unreviewed-binary'
    embedded = b'<svg>data:image/' + b'png;base64,YQ==</svg>'
    assert privacy.inspect('docs/example.svg', embedded)[0]['kind'] == 'unreviewed-binary'
    assert privacy.inspect('public/favicon.svg', embedded)[0]['kind'] == 'unreviewed-binary'
    secret = b'-----BEGIN ' + b'PRIVATE KEY-----'
    assert privacy.inspect('example.txt', secret)[0]['kind'] == 'secret-material'
    home_path = b'C:' + b'\\' + b'Users\\example\\media'
    assert privacy.inspect('example.txt', home_path)[0]['kind'] == 'personal-home-path'


@pytest.mark.parametrize('targets,copied', [({}, UUID(int=1)), ({UUID(int=1): 'example'}, UUID(int=1)),
    ({UUID(int=1): 'a', UUID(int=2): 'b', UUID(int=3): 'c'}, UUID(int=4))])
def test_cleanup_rejects_missing_or_ambiguous_manifest_before_connecting(targets, copied):
    with pytest.raises(ValueError, match='Exactly three'):
        cleanup('must-not-connect', targets=targets, copied_term=copied)
