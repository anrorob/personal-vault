from pathlib import Path
import runpy
from uuid import uuid4
import pytest
from app import ken_service as lab,ken_api as api,ken_config as ken
from tests.test_ken_service import ken_api_fixture,FakeAdapter
from tests.test_vault_libraries import authenticate


def test_only_qwen_engine_is_available():
    assert lab.ENGINE.model_id == ken.MODEL_ID
    assert lab.ADAPTER.selected == lab.ENGINE
    assert not hasattr(lab,'CANDIDATES')
