from datetime import date
from uuid import uuid4

import pytest

from app.gallery_people import MemoryGalleryPeopleStore


def test_person_foundation_keeps_uuid_and_supports_duplicate_names_search_and_dob() -> None:
    people = MemoryGalleryPeopleStore()
    owner = uuid4()
    profile_asset_id = uuid4()
    first = people.create_person(
        "owner", "Owner", owner, full_name="Owner Kowalski", preferred_name="Rob",
        aliases=["Bobby", "RK"], date_of_birth=date(1980, 1, 2), profile_asset_id=profile_asset_id,
    )
    duplicate = people.create_person("owner", "Owner", owner, full_name="Owner Kowalski", preferred_name="Rob")

    assert first.id != duplicate.id
    assert people.get_person(first.id, owner).date_of_birth == date(1980, 1, 2)  # type: ignore[union-attr]
    assert people.get_person(first.id, owner).profile_asset_id == profile_asset_id  # type: ignore[union-attr]
    assert [person.id for person in people.search_people(owner, "kowalski")] == [first.id, duplicate.id]
    assert [person.id for person in people.search_people(owner, "rob")] == [first.id, duplicate.id]
    assert [person.id for person in people.search_people(owner, "bobby")] == [first.id]

    updated = people.update_person(first.id, owner, full_name="Owner J. Kowalski", clear_date_of_birth=True)
    assert updated is not None and updated.id == first.id
    assert updated.full_name == "Owner J. Kowalski" and updated.date_of_birth is None


def test_person_me_relationships_and_gallery_associations_are_owner_scoped() -> None:
    people = MemoryGalleryPeopleStore()
    robert_user, anita_user = uuid4(), uuid4()
    owner = people.create_person("owner", "Owner", robert_user, full_name="Owner Kowalski")
    recipient = people.create_person("owner", "Recipient", robert_user, full_name="Recipient Kowalski")
    tymek = people.create_person("owner", "Tymek", robert_user, full_name="Tymek Kowalski")
    same_name_elsewhere = people.create_person("recipient", "Owner", anita_user, full_name="Owner Kowalski")

    assert people.resolve_me_person(robert_user) is None
    assert people.set_me_person(robert_user, owner.id).id == owner.id
    assert people.set_me_person(robert_user, tymek.id).id == tymek.id
    assert people.resolve_me_person(robert_user).id == tymek.id  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        people.set_me_person(robert_user, same_name_elsewhere.id)
    people.clear_me_person(robert_user)
    assert people.resolve_me_person(robert_user) is None

    wife = people.set_relationship(robert_user, owner.id, recipient.id, "wife")
    husband = people.set_relationship(robert_user, recipient.id, owner.id, "husband")
    son = people.set_relationship(robert_user, owner.id, tymek.id, "son")
    assert wife.relationship_label == "wife" and husband.relationship_label == "husband"
    assert [relationship.id for relationship in people.relationships_for_person(robert_user, owner.id)] == [son.id, wife.id]
    with pytest.raises(ValueError):
        people.set_relationship(anita_user, same_name_elsewhere.id, owner.id, "friend")

    asset_id = uuid4()
    people.decide(asset_id, owner.id, "include", robert_user)
    people.associate(asset_id, recipient.id, "vault_master")
    assert {value.person_id for value in people.effective_people(asset_id, robert_user)} == {owner.id, recipient.id}
    assert people.matching_asset_ids((owner.id,), robert_user) == {asset_id}
