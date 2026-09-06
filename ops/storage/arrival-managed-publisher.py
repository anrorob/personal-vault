#!/usr/bin/env python3
"""Root-only signed logical-destination Arrival Hall publisher."""
from __future__ import annotations
import hashlib, hmac, json, os, re, shutil, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from uuid import UUID

QUEUE=Path('/var/lib/personal-vault-storage/arrival-managed-requests')
RECEIPTS=Path('/var/lib/personal-vault-storage/arrival-managed-receipts')
KEY=Path('/etc/personal-vault/arrival-managed-publisher.key')
ARRIVAL=Path('/vault/Arrival Hall')
MANIFEST=Path('/var/lib/personal-vault-storage/control/active-slots.json')
SCHEMA='personal-vault.slot-managed-manifest.v1'; SLOT_ROOT=Path('/srv/pv/storage-slots')
REQUEST_MAX_AGE=timedelta(minutes=15)

def canonical(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def digest(path):
 h=hashlib.sha256()
 with path.open('rb') as stream:
  for block in iter(lambda:stream.read(1024*1024),b''): h.update(block)
 return h.hexdigest()
def relative(value):
 path=PurePosixPath(value)
 if path.is_absolute() or not path.parts or any(part in {'','.','..'} for part in path.parts): raise ValueError('unsafe relative path')
 return path
def logical(value):
 path=PurePosixPath(value)
 if not path.is_absolute() or path.parts[:2] != ('/','vault') or len(path.parts)<3 or any(part in {'','.','..'} for part in path.parts): raise ValueError('unsafe logical destination')
 return path
def reject(path,reason): path.with_suffix('.rejected').write_text(reason,encoding='utf-8'); os.replace(path,path.with_suffix('.rejected.request'))
def receipt_path(request_id): return RECEIPTS/(request_id+'.json')
def write_receipt(receipt,key):
 RECEIPTS.mkdir(mode=0o700,parents=True,exist_ok=True)
 target=receipt_path(receipt['request_id']); document=canonical({'receipt':receipt,'signature':hmac.new(key,canonical(receipt),hashlib.sha256).hexdigest()})
 try: descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 except FileExistsError:
  if target.read_bytes()!=document: raise ValueError('receipt identity collision')
  return
 with os.fdopen(descriptor,'wb') as stream: stream.write(document); stream.flush(); os.fsync(stream.fileno())
def mapping_for(document,destination):
 matches=[]
 for area,root in document.get('logical_mappings',{}).items():
  if not isinstance(area,str) or not isinstance(root,str): continue
  candidate=logical(root)
  if destination == candidate or destination.is_relative_to(candidate): matches.append((len(candidate.parts),area,candidate))
 if not matches: return None
 matches.sort(reverse=True)
 if len(matches)>1 and matches[0][0]==matches[1][0]: raise ValueError('logical destination area is ambiguous')
 return matches[0][1]
def managed_candidates(manifest,destination,required_bytes):
 if manifest.get('schema')!=SCHEMA or not isinstance(manifest.get('slots'),dict): raise ValueError('managed slot manifest is invalid')
 matches=[]; matched_area=False
 for slot_id,slot in manifest['slots'].items():
  if not isinstance(slot_id,str) or not re.fullmatch(r'PV-DISK-[0-9]{3,}',slot_id) or not isinstance(slot,dict): raise ValueError('managed slot manifest is invalid')
  if slot.get('state')!='active' or slot.get('integration_mode')!='slot_managed': continue
  if not isinstance(slot.get('hardware_id'),str) or not slot['hardware_id'].strip() or not isinstance(slot.get('filesystem_uuid'),str) or not slot['filesystem_uuid'].strip(): continue
  areas=slot.get('areas'); mappings=slot.get('logical_mappings')
  if not isinstance(areas,list) or not isinstance(mappings,dict): continue
  area=mapping_for(slot,destination)
  # Active commissioned slots serving other logical areas are ineligible for
  # this request; they do not invalidate an otherwise valid destination.
  if area is None: continue
  matched_area=True
  if area not in areas: continue
  root=Path(str(slot.get('resolver_root','')))
  if root != SLOT_ROOT/slot_id: raise ValueError('managed resolver root is invalid')
  try: resolved=root.resolve(strict=True)
  except OSError: raise ValueError('managed resolver root is invalid') from None
  if not resolved.is_dir(): raise ValueError('managed resolver root is invalid')
  slot_relative=PurePosixPath(*destination.parts[2:])
  raw=resolved.joinpath(*slot_relative.parts)
  if any(path.is_symlink() for path in [raw,*raw.parents] if path!=resolved and path.is_relative_to(resolved)): raise ValueError('unsafe resolver symlink')
  target=raw.resolve()
  if not target.is_relative_to(resolved): raise ValueError('unsafe resolver target')
  free=shutil.disk_usage(resolved).free
  if free>=required_bytes: matches.append((slot_id,resolved,target,free,area,slot_relative.as_posix()))
 if not matches:
  if not matched_area: raise ValueError('logical destination has no managed area')
  raise ValueError('no capacity-sufficient managed slot')
 areas={candidate[4] for candidate in matches}
 if len(areas)!=1: raise ValueError('logical destination area is inconsistent across slots')
 return matches
def existing_receipt(key,request):
 target=receipt_path(request['request_id'])
 if not target.exists(): return None
 try: document=json.loads(target.read_text(encoding='utf-8'))
 except (OSError,json.JSONDecodeError): raise ValueError('receipt is unreadable') from None
 receipt=document.get('receipt'); signature=document.get('signature')
 if not isinstance(receipt,dict) or not isinstance(signature,str) or not hmac.compare_digest(hmac.new(key,canonical(receipt),hashlib.sha256).hexdigest(),signature): raise ValueError('receipt is invalid')
 return receipt
def process_request(request_path):
 document=json.loads(request_path.read_text()); request=document.get('request',{}); signature=document.get('signature',''); key=KEY.read_bytes()
 required={'request_id','item_id','owner_user_id','source_relative_path','logical_destination','expected_sha256','expected_size_bytes','created_at'}
 if set(request) not in (required,required|{'recovery'}) or not hmac.compare_digest(hmac.new(key,canonical(request),hashlib.sha256).hexdigest(),signature): raise ValueError('invalid request')
 UUID(str(request['request_id'])); UUID(str(request['item_id'])); UUID(str(request['owner_user_id']))
 if len(str(request['expected_sha256']))!=64 or int(request['expected_size_bytes'])<0: raise ValueError('invalid request')
 created=datetime.fromisoformat(request['created_at'])
 if created.tzinfo is None: raise ValueError('invalid request')
 # A signed request that entered the root-owned queue promptly remains durable
 # across executor downtime.  A copied/backdated request still fails closed.
 queued=datetime.fromtimestamp(request_path.stat().st_mtime,timezone.utc)
 if abs(queued-created.astimezone(timezone.utc))>REQUEST_MAX_AGE: raise ValueError('request was not queued when signed')
 recovery=request.get('recovery')
 destination=logical(request['logical_destination']); manifest=json.loads(MANIFEST.read_text())
 if recovery is not None:
  if not isinstance(recovery,dict) or set(recovery)!={'schema','source_logical_path','asset_id','file_id','batch_id','track_id'} or recovery['schema']!='personal-vault.tv-resolver-relocation.v1': raise ValueError('invalid resolver relocation')
  for field in ('asset_id','file_id','batch_id','track_id'): UUID(recovery[field])
  source_logical=logical(recovery['source_logical_path'])
  if not source_logical.is_relative_to('/vault/Home Videos') or not destination.is_relative_to('/vault/Theatre/TV Shows') or source_logical==destination: raise ValueError('invalid resolver relocation areas')
  if request['source_relative_path']!=source_logical.relative_to('/vault').as_posix(): raise ValueError('invalid resolver relocation source')
  sources=managed_candidates(manifest,source_logical,0)
  present=[candidate for candidate in sources if candidate[2].exists()]
  if len(present)>1: raise ValueError('ambiguous resolver relocation source')
  source_path=present[0][2] if present else None
 else: source_path=ARRIVAL.joinpath(*relative(request['source_relative_path']).parts)
 if source_path is not None and source_path.is_symlink(): raise ValueError('source verification failed')
 source=source_path.resolve(strict=True) if source_path is not None and source_path.exists() else None
 if recovery is None and source is not None and not source.is_relative_to(ARRIVAL.resolve(strict=True)): raise ValueError('source verification failed')
 candidates=managed_candidates(manifest,destination,int(request['expected_size_bytes']))
 receipt=existing_receipt(key,request)
 # A logical path is reserved across its whole eligible area, not merely the chosen slot.
 existing=[candidate for candidate in candidates if candidate[2].exists()]
 chosen=sorted(candidates,key=lambda value:(-value[3],value[0]))[0]
 if existing:
  if len(existing)!=1 or (receipt is None and recovery is None) or (receipt is not None and (existing[0][0]!=receipt.get('slot_id') or existing[0][5]!=receipt.get('relative_path'))) or existing[0][2].is_symlink() or existing[0][2].stat().st_size!=request['expected_size_bytes'] or digest(existing[0][2])!=request['expected_sha256']: raise ValueError('logical managed destination collision')
  chosen=existing[0]
 slot_id,root,target,_,area,slot_relative=chosen
 output={'request_id':request['request_id'],'item_id':request['item_id'],'owner_user_id':request['owner_user_id'],'logical_destination':destination.as_posix(),'logical_area':area,'slot_id':slot_id,'relative_path':slot_relative,'expected_sha256':request['expected_sha256'],'expected_size_bytes':request['expected_size_bytes'],'verified_at':datetime.now(timezone.utc).isoformat()}
 if recovery is not None: output['recovery']=recovery
 if recovery is not None and source is not None and (not source.is_file() or source.stat().st_size!=request['expected_size_bytes'] or digest(source)!=request['expected_sha256']): raise ValueError('relocation source verification failed')
 if existing and receipt is not None:
  if any(receipt.get(field)!=output[field] for field in output if field!='verified_at'): raise ValueError('receipt does not match request')
  output=receipt
 if not existing:
  if source is None or not source.is_file() or source.is_symlink() or source.stat().st_size!=request['expected_size_bytes'] or digest(source)!=request['expected_sha256']: raise ValueError('source verification failed')
  target.parent.mkdir(mode=0o755,parents=True,exist_ok=True); temporary=target.with_name('.'+target.name+'.partial')
  if temporary.exists() or temporary.is_symlink(): raise ValueError('managed temporary destination collision')
  try:
   shutil.copy2(source,temporary)
   if recovery is not None:
    with temporary.open('r+b') as stream: os.fsync(stream.fileno())
   if digest(temporary)!=request['expected_sha256']: raise ValueError('destination checksum verification failed')
   os.link(temporary,target)
  finally: temporary.unlink(missing_ok=True)
 write_receipt(output,key)
 if source is not None:
  if not source.is_file() or source.is_symlink() or source.stat().st_size!=request['expected_size_bytes'] or digest(source)!=request['expected_sha256']: raise ValueError('source changed before removal')
  source.unlink()
 os.replace(request_path,request_path.with_suffix('.processed'))

def main():
 if os.geteuid()!=0 or sys.argv[1:] not in (['--next'],['--drain']): raise SystemExit('usage: arrival-managed-publisher.py --next|--drain')
 drain=sys.argv[1:] == ['--drain']; failures=[]
 while request_path:=next(iter(sorted(QUEUE.glob('*.json'))),None):
  try: process_request(request_path)
  except (OSError,ValueError,KeyError,json.JSONDecodeError) as error:
   reject(request_path,str(error)); failures.append(str(error))
  if not drain: break
 if failures: raise SystemExit('; '.join(failures))
if __name__=='__main__': main()
