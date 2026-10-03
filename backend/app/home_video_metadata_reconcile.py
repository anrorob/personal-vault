"""Explicit, restart-safe Development reconciliation; emits counts/IDs, never prose."""
import json
import os
import subprocess
from pathlib import Path
from uuid import uuid4
from psycopg.types.json import Jsonb
from app import ken_config as ken
from app.video_location import gps_location, VERSION

TITLE_VERSION='home-video-title-backfill-v1'


def stored_location(row):
    if row['has_location_override']:
        value=row['location_override']
        return (value, 'user_override') if isinstance(value,str) and value.strip() else (None,None)
    for key,source in (('imported_location','import:existing'),('detected_location','embedded'),('source_location','embedded')):
        value=row.get(key)
        if isinstance(value,str) and value.strip():return value,source
    return None,None


def backfill_locations(store, *, apply=False, root=Path('/vault/Home Videos')):
    with store.connect() as c:
        rows=c.execute("""SELECT DISTINCT ON(a.id) a.id,a.owner_user_id,a.location,
            a.user_overrides ? 'location' AS has_location_override,
            a.user_overrides->'location' AS location_override,
            a.imported_metadata->'location' AS imported_location,
            a.detected_metadata->'location' AS detected_location,
            a.metadata->'location' AS source_location,f.vault_path,f.sha256
            FROM vault_assets a JOIN vault_files f ON f.asset_id=a.id
            WHERE a.asset_type='Home Videos' AND a.owner_user_id IS NOT NULL
            AND (a.location IS NULL OR a.location='')
            ORDER BY a.id,(f.file_role='primary') DESC,f.created_at""").fetchall()
    report={'version':VERSION,'examined':len(rows),'eligible':0,'applied':0,'no_evidence':0,'source_failures':0}
    for row in rows:
        location,source=stored_location(row); evidence={}; identity=None;path=None
        if row['has_location_override'] and not location:continue
        if not location:
            try:
                path=Path(row['vault_path']).resolve(strict=True)
                if not path.is_relative_to(root.resolve()):raise ValueError('Outside Home Videos')
                identity=(path.stat().st_size,path.stat().st_mtime_ns)
                p=subprocess.run(['ffprobe','-v','error','-show_entries',
                    'format_tags=location,location-eng,com.apple.quicktime.location.ISO6709:stream_tags=location,location-eng,com.apple.quicktime.location.ISO6709',
                    '-of','json',str(path)],capture_output=True,check=True,timeout=20)
                if len(p.stdout)>65536:raise ValueError('Probe too large')
                evidence=gps_location(json.loads(p.stdout));location=evidence.get('location');source='embedded'
                if identity!=(path.stat().st_size,path.stat().st_mtime_ns):raise ValueError('Source changed')
            except (OSError,ValueError,subprocess.SubprocessError):
                report['source_failures']+=1;continue
        if not location:
            report['no_evidence']+=1;continue
        report['eligible']+=1
        if not apply:continue
        with store.connect() as c:
            current=c.execute("SELECT location,user_overrides ? 'location' AS has_override,user_overrides->'location' AS override FROM vault_assets WHERE id=%s AND owner_user_id=%s FOR UPDATE",(row['id'],row['owner_user_id'])).fetchone()
            if current is None or current['location'] or current['has_override']!=row['has_location_override'] or current['override']!=row['location_override']:continue
            if path and identity!=(path.stat().st_size,path.stat().st_mtime_ns):continue
            values={**evidence,'location':location}
            audit={'version':VERSION,'source':source,'catalogue_sha256':row['sha256']}
            c.execute("""UPDATE vault_assets SET location=%s,
                detected_metadata=detected_metadata || %s,metadata=metadata || %s,
                effective_metadata=effective_metadata || %s,
                metadata_provenance=metadata_provenance || %s,updated_at=CURRENT_TIMESTAMP
                WHERE id=%s AND owner_user_id=%s""",
                (location,Jsonb(values if source=='embedded' else {}),Jsonb({**values,'home_video_location_reconciliation':audit}),Jsonb({'location':location}),Jsonb({'location':source}),row['id'],row['owner_user_id']))
            c.execute("""INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values)
                VALUES(%s,%s,'metadata_updated','system:home-video-location',%s,%s)""",
                (uuid4(),row['id'],Jsonb({'location':current['location']}),Jsonb({'location':location,**audit})))
            report['applied']+=1
    return report


def backfill_titles(store, *, apply=False):
    with store.connect() as c:
        # Only latest successful owner-bound KEN evidence, and no manual titles.
        rows=c.execute("""SELECT DISTINCT ON(r.asset_id) r.id,r.asset_id,r.owner_user_id,r.configuration
            FROM vault_ken_runs r JOIN vault_assets a ON a.id=r.asset_id AND a.owner_user_id=r.owner_user_id
            WHERE r.status='completed' AND r.configuration->>'correction_version'=%s
            AND a.asset_type='Home Videos' AND COALESCE(a.user_overrides->>'display_title','')=''
            AND COALESCE(a.metadata_provenance->>'display_title','')<>'user_override'
            AND NOT EXISTS(SELECT 1 FROM vault_ken_runs active WHERE active.asset_id=r.asset_id AND active.status IN ('queued','preparing_input','analysing','saving_result'))
            ORDER BY r.asset_id,r.created_at DESC,r.id DESC""",(ken.PHASE,)).fetchall()
        eligible=[r for r in rows if (not r['configuration'].get('automatic_title') or r['configuration']['automatic_title'].get('status')=='failed') and r['configuration'].get('automatic_title',{}).get('reconciliation_version')!=TITLE_VERSION]
        if apply:
            for r in eligible:
                # Recheck immutable owner and manual authority at write time.
                c.execute("""UPDATE vault_ken_runs r SET configuration=jsonb_set(r.configuration,'{automatic_title}',%s)
                    FROM vault_assets a WHERE r.id=%s AND a.id=r.asset_id AND a.owner_user_id=r.owner_user_id
                    AND COALESCE(a.user_overrides->>'display_title','')='' AND COALESCE(a.metadata_provenance->>'display_title','')<>'user_override'""",
                    (Jsonb({'status':'pending','attempts':0,'reconciliation_version':TITLE_VERSION}),r['id']))
        return {'version':TITLE_VERSION,'eligible':len(eligible),'scheduled':len(eligible) if apply else 0}


def main():
    import argparse
    from app.ken_service import get_ken_store
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if os.environ.get('PV_ENVIRONMENT')!='development':raise RuntimeError('Development only')
    store=get_ken_store()
    with store.worker_lock() as acquired:
        if not acquired:raise RuntimeError('KEN busy; retry when idle')
        print(json.dumps({'locations':backfill_locations(store,apply=args.apply),'titles':backfill_titles(store,apply=args.apply)}))

if __name__=='__main__':main()
