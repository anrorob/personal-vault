"""Pure Vault Master matching evidence. Never reads or writes canonical files."""
from dataclasses import asdict
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import unicodedata

VERSION = "album-match-v1"


def normalized_title(value, *, filename=False):
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    if filename:
        text = PurePosixPath(text.replace("\\", "/")).stem
        text = re.sub(r"^\s*(?:\(\d{1,3}\)\s*|\d{1,3}\s*[-._]\s*)", "", text)
    text = text.replace("&", " and ")
    text = re.sub(r"['’‘`ʼ]", "", text)
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def coordinate(metadata):
    values = [re.fullmatch(r"\s*(0*[1-9][0-9]*)(?:/0*[1-9][0-9]*)?\s*", str(metadata.get(k))) for k in ("disc_number", "track_number")]
    return tuple(int(v[1]) for v in values) if all(values) else None


def seconds(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError):
        return None


def local_evidence(asset):
    # Prior provider guesses must not reinforce themselves on re-identification.
    metadata = {**asset.detected_metadata, **asset.user_overrides}
    title = metadata.get("display_title") or metadata.get("title")
    return {"asset_id": str(asset.id), "filename": asset.filename,
            "title": normalized_title(title), "filename_title": normalized_title(asset.filename, filename=True),
            "coordinate": coordinate(metadata), "duration": seconds(asset.detected_metadata.get("duration_seconds") or metadata.get("duration_seconds")),
            "artist": normalized_title(metadata.get("artist")), "album": normalized_title(metadata.get("album"))}


def revision(assets, release):
    data = {"version": VERSION, "release": asdict(release),
            "assets": [asdict(a) for a in sorted(assets, key=lambda a: str(a.id))]}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def edge(local, track):
    title = normalized_title(track.title)
    names = [v for v in (local["title"], local["filename_title"]) if v]
    exact = title in names
    similarity = max((SequenceMatcher(None, v, title, autojunk=False).ratio() for v in names), default=0)
    # Edition/performance words are meaningful, even when most characters agree.
    qualifiers = {"live", "remix", "acoustic", "instrumental", "demo", "remaster", "remastered", "edit", "karaoke"}
    variant_conflict = bool(names) and all((set(v.split()) & qualifiers) != (set(title.split()) & qualifiers) for v in names)
    position = local["coordinate"] == (track.disc_number, track.track_number)
    score, reason = 0., "No reliable title or position evidence"
    if position:
        score, reason = 110., "Embedded disc and track number"
    elif local["title"] and local["title"] == title:
        score, reason = 100., "Exact normalized embedded title"
    elif local["filename_title"] == title and title:
        score, reason = 98., "Exact normalized filename title"
    elif similarity >= .94 and not variant_conflict:
        score, reason = 90. + similarity * 5, "High-confidence similar title"
    duration = seconds(track.duration_seconds)
    delta = abs(local["duration"]-duration) if local["duration"] and duration else None
    tolerance = max(2., duration*.02) if duration else None
    warning = None
    if score and delta is not None:
        if delta <= tolerance:
            score += 3
            reason += "; duration agrees"
        elif delta > max(10., duration*.1):
            warning = "Large duration difference; owner review required"
    if position and local["title"] and not exact and similarity < .75:
        warning = "Track number conflicts with title; owner review required"
    if variant_conflict and score:
        warning = "Different performance/version wording; owner review required"
    return {"score": round(score, 3), "reason": reason, "warning": warning,
            "local_duration": local["duration"], "provider_duration": duration, "duration_delta": delta,
            "normalized_local": local["title"] or local["filename_title"], "normalized_provider": title}


def assignment(weights):
    """Rectangular Hungarian maximum-weight assignment, including unmatched dummies."""
    n = len(weights)
    if not n:
        return [], 0
    m = len(weights[0]); u = [0.]*(n+1); v = [0.]*(m+1); p = [0]*(m+1); way = [0]*(m+1)
    for i in range(1, n+1):
        p[0]=i; j0=0; minimum=[float("inf") ]*(m+1); used=[False]*(m+1)
        while True:
            used[j0]=True; i0=p[j0]; delta=float("inf"); j1=0
            for j in range(1,m+1):
                if not used[j]:
                    cur=-weights[i0-1][j-1]-u[i0]-v[j]
                    if cur<minimum[j]: minimum[j]=cur; way[j]=j0
                    if minimum[j]<delta: delta=minimum[j]; j1=j
            for j in range(m+1):
                if used[j]: u[p[j]]+=delta; v[j]-=delta
                else: minimum[j]-=delta
            j0=j1
            if p[j0]==0: break
        while True:
            j1=way[j0]; p[j0]=p[j1]; j0=j1
            if j0==0: break
    result=[-1]*n
    for j in range(1,m+1):
        if p[j]: result[p[j]-1]=j-1
    return result, sum(weights[i][j] for i,j in enumerate(result))


def propose(assets, release):
    if max(len(assets),len(release.tracks))>200:
        raise ValueError("Album matching supports up to 200 local and provider tracks per review")
    keys=[f"{t.disc_number}:{t.track_number}" for t in release.tracks]
    if len(set(keys))!=len(keys) or any(t.disc_number<1 or t.track_number<1 for t in release.tracks):
        raise ValueError("Provider track positions are ambiguous or invalid")
    locals_=[local_evidence(a) for a in assets]
    edges=[[edge(a,t) for t in release.tracks] for a in locals_]
    weights=[[e["score"] for e in row]+[0.]*len(assets) for row in edges]
    chosen,total=assignment(weights)
    results=[]
    for i,j in enumerate(chosen):
        found=j<len(keys) and weights[i][j]>0
        evidence=edges[i][j] if found else {"score":0,"reason":"No reliable match","warning":None}
        ambiguous=False
        if found:
            altered=[row[:] for row in weights]; altered[i][j]=-10000
            _, alternative=assignment(altered)
            ambiguous=total-alternative<2.5
        status="ambiguous" if ambiguous else "review" if found and evidence["warning"] else "confident" if found else "unmatched"
        previous=assets[i].imported_metadata.get("music_match",{})
        proposed=keys[j] if found and status=="confident" else None
        # Preserve owner mapping constraints, including deliberately unmatched files.
        if previous.get("decision")=="override":
            prior=previous.get("provider_track")
            if previous.get("release_id")==release.release_id:
                proposed=prior if prior in keys else None
            elif prior is None:
                proposed=None
            else:
                original=previous.get("provider_original") or {}
                compatible=[keys[k] for k,t in enumerate(release.tracks)
                            if (original.get("recording_id") and t.recording_id==original["recording_id"])
                            or (normalized_title(original.get("title")) and normalized_title(original.get("title"))==normalized_title(t.title))]
                proposed=compatible[0] if len(compatible)==1 else None
            status="owner_override"
        results.append({**locals_[i], **evidence, "status":status,"proposed_track":proposed,
                        "suggested_track":keys[j] if found else None})
    # Never silently restore two stored overrides onto the same provider track.
    counts={key:sum(r["proposed_track"]==key for r in results) for key in keys}
    for result in results:
        key=result["proposed_track"]
        if key and counts[key]>1:
            result.update(proposed_track=None,status="ambiguous")
    return results


def rank_release(candidate, artist, album, assets):
    artist_score=SequenceMatcher(None,normalized_title(artist),normalized_title(candidate.artist),autojunk=False).ratio()
    title_score=SequenceMatcher(None,normalized_title(album),normalized_title(candidate.title),autojunk=False).ratio()
    hints=0
    for field, values in (("edition",[candidate.disambiguation]),("release_date",[candidate.date]),
                          ("country",[candidate.country]),("format",candidate.formats),
                          ("barcode",[candidate.barcode]),("catalog_number",candidate.catalog_numbers)):
        local={normalized_title(a.effective_metadata[field]) for a in assets if a.effective_metadata.get(field)}
        if local and local.intersection(normalized_title(v) for v in values if v): hints+=1
    return (-artist_score,-title_score,abs(candidate.track_count-len(assets)) if candidate.track_count>0 else float('inf'),-hints,-candidate.score,candidate.release_id)
