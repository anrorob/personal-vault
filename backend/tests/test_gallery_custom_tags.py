from uuid import uuid4
from app.gallery_custom_tags import MemoryGalleryCustomTagStore


def test_private_tags_are_user_scoped_and_same_text_is_independent() -> None:
    store = MemoryGalleryCustomTagStore(); first, second, asset = uuid4(), uuid4(), uuid4()
    first_tag = store.create(first, "Holiday"); second_tag = store.create(second, "Holiday")
    assert first_tag.id != second_tag.id
    store.assign(first, first_tag.id, asset); store.assign(second, second_tag.id, asset)
    assert [tag.id for tag in store.for_asset(first, asset)] == [first_tag.id]
    assert [tag.id for tag in store.for_asset(second, asset)] == [second_tag.id]
    store.rename(first, first_tag.id, "Trip")
    assert store.list(second)[0].display_name == "Holiday"
    store.delete(first, first_tag.id)
    assert store.for_asset(first, asset) == []
    assert [tag.id for tag in store.for_asset(second, asset)] == [second_tag.id]
