"""Bounded owner writing guidance, shared by backend and local KEN runtime."""
import json

VERSION = 'ken-owner-style-v1'
DEFAULTS = dict(activity_first=True, use_trusted_names=True, direct_activity_language=True,
    avoid_vague_adult_terms=True, setting_priority='low', camera_commentary='relevant_only',
    negative_filler=False, concise_descriptions=True, location_style='locality_only')
RULES = {
    'activity_first': 'Focus on the main activity and relevant participant interactions; stop rather than pad.',
    'use_trusted_names': 'Use trusted names consistently; do not substitute generic person labels merely for variety.',
    'direct_activity_language': 'Name a clearly supported activity directly and specifically; retain uncertainty when ambiguous.',
    'avoid_vague_adult_terms': "Avoid 'sexual activity', 'intimate activity', 'intimate interaction', 'physical engagement', 'genital area' or 'pelvic area' when a specific visible action is supported. Do not force specificity when ambiguous.",
    'setting_priority': 'Keep setting secondary; omit decor and incidental background detail.',
    'camera_commentary': 'Mention camera behaviour only when it materially explains the event; distinguish camera and subject motion.',
    'negative_filler': 'Omit negative/absence filler and unchanged-scene commentary.',
    'concise_descriptions': 'Write concise natural prose; stop once meaningful activity is covered.',
    'location_style': 'Mention only the supplied locality when available, once where useful; do not append country or invent a finer location.',
}


def validate_preferences(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('Unknown KEN preference')
    for key, item in value.items():
        expected=DEFAULTS[key]
        if type(expected) is bool:
            if type(item) is not bool: raise ValueError('Invalid KEN preference')
        elif item != expected: raise ValueError('Unsupported KEN preference value')
    return dict(value)


def validate_style(style):
    if not isinstance(style,dict) or style.get('version')!=VERSION: raise ValueError('Invalid owner style version')
    if set(style)-{'version','preference_revision','preferences'}:
        raise ValueError('Only structured owner preferences are allowed')
    validate_preferences(style.get('preferences',{}))
    return style


def style_sections(style, people, location):
    validate_style(style)
    prefs=style['preferences']
    rules=[rule for key,rule in RULES.items() if key in prefs and (prefs[key] is True or prefs[key] in ('low','relevant_only','locality_only') or (key=='negative_filler' and prefs[key] is False))]
    return ('\nOwner writing preferences (style only):\n'+json.dumps({'preferences':prefs,'guidance':rules},ensure_ascii=False,sort_keys=True)
        +'\nTrusted current-video context:\n'+json.dumps({'trusted_people':people,'trusted_location':location},ensure_ascii=False,sort_keys=True))
