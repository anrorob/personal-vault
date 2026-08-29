from pathlib import Path
from uuid import uuid4

from app.federated_download_executor import FederatedDownloadPromotionRequest, canonical_payload, queue_signed_request, sign_request, verify_receipt


def test_theatre_promotion_request_is_identity_bound_and_queue_is_idempotent_safe(tmp_path: Path) -> None:
    request=FederatedDownloadPromotionRequest.create(operation_id=uuid4(),local_asset_id=uuid4(),owner_user_id=uuid4(),origin_vault_id=uuid4(),origin_asset_id=uuid4(),staging_name='verified.part',filename='film.mp4',expected_sha256='a'*64,expected_size_bytes=7)
    assert request.destination_vault_path.endswith(f"/{request.local_asset_id}/film.mp4")
    queued=queue_signed_request(request,queue_root=tmp_path/'queue',key=b'k'*32)
    assert queued.is_file()
    try: queue_signed_request(request,queue_root=tmp_path/'queue',key=b'k'*32)
    except FileExistsError: pass
    else: raise AssertionError('same operation request must not be queued twice')


def test_theatre_promotion_rejects_paths_outside_the_allowlist() -> None:
    try: FederatedDownloadPromotionRequest.create(operation_id=uuid4(),local_asset_id=uuid4(),owner_user_id=uuid4(),origin_vault_id=uuid4(),origin_asset_id=uuid4(),staging_name='../escape',filename='film.mp4',expected_sha256='a'*64,expected_size_bytes=7)
    except ValueError: pass
    else: raise AssertionError('unsafe staging path accepted')
    assert verify_receipt({'receipt':{},'signature':'bad'},b'k'*32) is None


def test_theatre_promotion_receipt_requires_the_exact_verified_identity() -> None:
    request=FederatedDownloadPromotionRequest.create(operation_id=uuid4(),local_asset_id=uuid4(),owner_user_id=uuid4(),origin_vault_id=uuid4(),origin_asset_id=uuid4(),staging_name='verified.part',filename='film.mp4',expected_sha256='b'*64,expected_size_bytes=9)
    assert canonical_payload(request)
    assert len(sign_request(request,b'k'*32)) == 64
    receipt={'request_id':str(request.request_id),'operation_id':str(request.operation_id),'local_asset_id':str(request.local_asset_id),'destination_vault_path':request.destination_vault_path,'expected_sha256':request.expected_sha256,'expected_size_bytes':request.expected_size_bytes,'verified_at':'2026-08-17T12:00:00+00:00'}
    signature=__import__('hmac').new(b'k'*32,__import__('json').dumps(receipt,sort_keys=True,separators=(',',':')).encode(),__import__('hashlib').sha256).hexdigest()
    assert verify_receipt({'receipt':receipt,'signature':signature},b'k'*32) == receipt
    receipt['local_asset_id']=str(uuid4())
    assert verify_receipt({'receipt':receipt,'signature':signature},b'k'*32) is None
