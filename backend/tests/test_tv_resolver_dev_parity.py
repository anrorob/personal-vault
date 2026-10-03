"""Synthetic logical source-identity discovery contract."""
from types import SimpleNamespace

from app.tv_resolver_publication import _source_identity


def test_supplier_provenance_does_not_replace_logical_batch_identity():
    item = SimpleNamespace(relative_path='Unresolved/track.mkv', metadata={
        'source_context': {'source_id': 'synthetic-supplier', 'source_label': 'Synthetic'},
    })
    assert _source_identity([item], None) == 'arrival:Unresolved'
    assert _source_identity([item], ' Example Show ') == 'show:example show'
