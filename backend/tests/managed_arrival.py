"""Synthetic root receipt fixture; executor signature/integrity tests are separate."""
from pathlib import Path
from uuid import uuid4
from app.vault_master import process_next_move


def complete_gallery_receipt(store, incoming, destinations, playback_publisher=None):
    request_id=uuid4()
    item_id=process_next_move(store,incoming,destinations,playback_publisher,theatre_queue=lambda _:request_id)
    assert item_id is not None
    item=store.get_item(item_id)
    assert item.state=='theatre_promotion_pending'
    source=Path(item.source_path)
    assert source.exists()  # backend worker has not moved the bytes
    destination=destinations['Gallery']/item.proposed_destination.removeprefix('/vault/Gallery/')
    destination.parent.mkdir(parents=True,exist_ok=True)
    assert not destination.exists()
    source.rename(destination)  # emulate the separately tested root executor
    receipt=dict(request_id=str(request_id),item_id=str(item.id),owner_user_id=str(item.owner_user_id),logical_destination=item.proposed_destination,logical_area='Gallery',slot_id='PV-DISK-002',relative_path=item.proposed_destination.removeprefix('/vault/'),expected_sha256=item.sha256,expected_size_bytes=item.size_bytes)
    assert store.publish_arrival_managed_receipt(item.id,receipt)
    return item_id
