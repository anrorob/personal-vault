"""Closed Gallery display taxonomy. User tags and raw evidence never extend it."""

GALLERY_TAXONOMY: tuple[tuple[str, str, str], ...] = (
    ("photo_type", "portrait", "Portrait"),
    ("photo_type", "selfie", "Selfie"),
    ("photo_type", "landscape", "Landscape"),
    ("photo_type", "animal", "Animal"),
    ("photo_type", "food", "Food"),
    ("photo_type", "document", "Document"),
    ("photo_type", "screenshot", "Screenshot"),
    ("photo_type", "architecture", "Building / Architecture"),
    ("photo_type", "vehicle", "Vehicle"),
    ("photo_type", "night_photo", "Night photo"),
    ("content_tag", "motorcycle", "Motorcycle"),
    ("content_tag", "outdoors", "Outdoors"),
    ("content_tag", "building", "Building"),
    ("content_tag", "animal", "Animal"),
    ("content_tag", "cat", "Cat"),
    ("content_tag", "beach", "Beach"),
    ("content_tag", "sea", "Sea"),
)

GALLERY_TERM_NAMES = {(namespace, slug): name for namespace, slug, name in GALLERY_TAXONOMY}


def gallery_taxonomy_terms() -> list[dict[str, str]]:
    return [
        {"namespace": namespace, "slug": slug, "display_name": name}
        for namespace, slug, name in sorted(GALLERY_TAXONOMY, key=lambda term: (term[0], term[2]))
    ]


def approved_gallery_terms(terms):
    return tuple((namespace, slug) for namespace, slug in terms if (namespace, slug) in GALLERY_TERM_NAMES)
