"""Approved TV extras retain canonical show/season identity through publication."""
def extra_destination(title, season, filename):
    if (not isinstance(season, int) or season <= 0 or not title or not filename
        or any(c in title + filename for c in '/\\\x00')
        or title in {'.', '..'} or filename in {'.', '..'}):
        raise ValueError('TV extra has an unsafe name or unresolved season')
    return f'/vault/Theatre/TV Shows/{title}/Season {season:02d}/Extras/{filename}'


def extra_projection(cursor, item, track):
    review = track['review_metadata']
    destination = extra_destination(track['proposed_show_title'], track['proposed_season_number'], track['original_filename'])
    if (not review.get('extras_approved_at') or review.get('extras_approved_by') != str(item.owner_user_id)
        or track['canonical_destination'] != destination or track['proposed_episode_number'] is not None):
        raise ValueError('TV extra has no approved season mapping')
    cursor.execute('''SELECT s.id, s.show_id FROM vault_tv_seasons s
        JOIN vault_tv_shows show ON show.id=s.show_id
        WHERE show.owner_user_id=%s AND show.title=%s AND s.season_number=%s
        AND show.visibility=%s''', (item.owner_user_id, track['proposed_show_title'], track['proposed_season_number'], review['audience']))
    season = cursor.fetchone()
    if season is None:
        raise ValueError('TV extra published show/season is unavailable')
    metadata = {k: v for k, v in item.metadata.items() if k != 'tv_publication_set'}
    metadata.update({
        'tv_resolver_batch_id': str(track['batch_id']),
        'tv_extra': {'show_id': str(season['show_id']), 'season_id': str(season['id']),
                     'season_number': track['proposed_season_number'], 'show_title': track['proposed_show_title']},
        'tv_resolver_publication': {'track_id': str(track['id']), 'item_id': str(item.id),
            'owner_user_id': str(item.owner_user_id), 'sha256': item.sha256, 'canonical_destination': destination},
    })
    return {'proposed_category': 'TV Shows', 'proposed_destination': destination,
            'publication_audience': review['audience'], 'metadata': metadata}


def valid_extra_receipt(cursor, item):
    from app.tv_publication_authority import approved_projection
    try:
        projection = approved_projection(cursor, item)
    except ValueError:
        return False
    return bool(projection and projection['metadata'].get('tv_extra') == item.metadata.get('tv_extra')
        and item.proposed_destination == projection['proposed_destination']
        and item.publication_audience == projection['publication_audience']
        and item.proposed_category == 'TV Shows')
