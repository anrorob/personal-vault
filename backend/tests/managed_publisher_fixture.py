"""Disposable synthetic adapter for the actual public signed host executor."""
import importlib.util
from pathlib import Path
import pytest

def load_publisher():
    path=Path(__file__).parents[2]/'ops/storage/arrival-managed-publisher.py'
    spec=importlib.util.spec_from_file_location('synthetic_publisher',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    def process(values,request):
        for key,name in {'queue':'QUEUE','receipts':'RECEIPTS','key':'KEY','arrival':'ARRIVAL','manifest':'MANIFEST','slot_root':'SLOT_ROOT'}.items():
            setattr(module,name,values[key])
        return module.process_request(request)
    module.process=process
    return module

@pytest.fixture
def publisher():
    return load_publisher()
