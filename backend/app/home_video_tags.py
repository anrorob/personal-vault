"""Home Video display boundary for the existing shared generated vocabulary.

Reconciliation already uses approved_gallery_terms when persisting video-level
evidence. Reuse that authority; neither legacy custom rows nor private tags can
extend it. This does not introduce a separate Home Video taxonomy.
"""
from app.gallery_taxonomy import gallery_taxonomy_terms


def system_terms():
    # The existing video reconciler persists this approved shared vocabulary.
    # Never enumerate arbitrary legacy rows from vault_metadata_terms.
    return [term for term in gallery_taxonomy_terms() if term["namespace"] == "content_tag"]


def effective_system_tags(store, asset_id):
    allowed = {term["slug"] for term in system_terms()}
    result = []
    for value in store.effective(asset_id):
        term = value if isinstance(value, dict) else {
            "namespace": value.namespace, "slug": value.slug,
            "display_name": value.display_name,
        }
        if term["namespace"] == "content_tag" and term["slug"] in allowed:
            result.append(term)
    return result
