"""Trusted QuickTime GPS extraction using PV's existing offline place lookup."""
import re
import reverse_geocode

VERSION = 'home-video-location-v1'
GPS_KEYS = ('com.apple.quicktime.location.ISO6709', 'location', 'location-eng')


def gps_location(probe):
    format_data=probe.get('format')
    streams=probe.get('streams')
    tags=[format_data.get('tags', {})] if isinstance(format_data,dict) else []
    tags += [s.get('tags', {}) for s in (streams if isinstance(streams,list) else []) if isinstance(s, dict)]
    coordinates=set()
    for group in tags:
        if not isinstance(group, dict): continue
        for key in GPS_KEYS:
            value=group.get(key)
            if not isinstance(value,str): continue
            match=re.fullmatch(r'([+-]\d{1,2}(?:\.\d+)?)([+-]\d{1,3}(?:\.\d+)?)(?:[+-]\d+(?:\.\d+)?)?/?',value.strip())
            if not match: continue
            lat,lon=map(float,match.groups())
            if -90<=lat<=90 and -180<=lon<=180:coordinates.add((lat,lon))
    if len(coordinates)!=1:return {}
    lat,lon=coordinates.pop()
    result={'gps_latitude':lat,'gps_longitude':lon,'location_source':'video_container_gps'}
    try: place=reverse_geocode.search([(lat,lon)])[0]
    except (IndexError,KeyError,OSError,TypeError,ValueError):return result
    parts=[str(place.get('city') or '').strip(),str(place.get('country') or place.get('country_code') or '').strip()]
    location=', '.join(p for p in parts if p)
    if location:result['location']=location
    return result

def ken_location_display(name, metadata):
    """Presentation only. Use verified structured locality; never guess from commas."""
    if not isinstance(name,str) or not name.strip():return None
    metadata=metadata if isinstance(metadata,dict) else {}
    # Only an explicitly matching full location licenses its components.
    details=metadata.get('location_details')
    if isinstance(details,dict) and details.get('name')==name:
        for key in ('city','town','locality','municipality','island'):
            value=details.get(key)
            if isinstance(value,str) and value.strip():return value.strip()
    # Existing offline geocoder already produces canonical city/country labels.
    # Reuse its structured result only if it reproduces the authoritative label.
    lat,lon=metadata.get('gps_latitude'),metadata.get('gps_longitude')
    if type(lat) in (float,int) and type(lon) in (float,int) and -90<=lat<=90 and -180<=lon<=180:
        try:
            place=reverse_geocode.search([(lat,lon)])[0]
            city=str(place.get('city') or '').strip()
            country=str(place.get('country') or place.get('country_code') or '').strip()
            if name==', '.join(p for p in (city,country) if p):return city or country or name
        except (IndexError,KeyError,OSError,TypeError,ValueError):pass
    return name
