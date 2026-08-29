"""Vault-wide People API over the canonical People repository."""
from __future__ import annotations
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUsername
from app.config import get_database_conninfo
from app.gallery_people import VaultPerson, get_gallery_people_store
from app.share_grants import PostgresShareGrantStore
from app.vault_master import asset_is_editable_by, get_vault_master_store


router = APIRouter(prefix="/api/people", tags=["people"])


class PersonResponse(BaseModel):
    person_id: UUID
    full_name: str
    preferred_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    date_of_birth: date | None = None
    profile_asset_id: UUID | None = None
    profile_frame: dict[str, float] | None = None
    is_me: bool = False
    relationship_label: str | None = None
    active: bool


class PersonAssetResponse(BaseModel):
    asset_id: UUID
    display_title: str
    asset_type: str
    vault_path: str | None = None


class PersonDetailResponse(PersonResponse):
    associated_asset_count: int
    associated_assets: list[PersonAssetResponse]


class PersonCreate(BaseModel):
    full_name: str = Field(min_length=1)
    preferred_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    date_of_birth: date | None = None
    profile_asset_id: UUID | None = None
    relationship_label: str | None = None
    profile_frame: dict[str, float] | None = None


class PersonUpdate(BaseModel):
    full_name: str | None = None
    preferred_name: str | None = None
    aliases: list[str] | None = None
    date_of_birth: date | None = None
    profile_asset_id: UUID | None = None
    clear_date_of_birth: bool = False
    clear_profile_asset: bool = False
    profile_frame: dict[str, float] | None = None
    clear_profile_frame: bool = False


class PersonMerge(BaseModel):
    source_person_id: UUID


class MeUpdate(BaseModel):
    person_id: UUID


class RelationshipUpdate(BaseModel):
    relationship_label: str = Field(min_length=1)


class FaceCorrection(BaseModel):
    asset_id: UUID
    person_id: UUID | None = None


class AssetPersonCorrection(BaseModel):
    asset_id: UUID
    person_id: UUID
    decision: str = Field(pattern="^(include|exclude)$")


class PeopleService:
    def __init__(self, people_store, vault_store, share_grant_store, user: AuthenticatedUsername) -> None:
        self.people_store, self.vault_store, self.share_grant_store, self.user = people_store, vault_store, share_grant_store, user
        self.user_id = user.user_id

    def _relationship_to(self, person_id: UUID) -> str | None:
        me = self.people_store.resolve_me_person(self.user_id)
        if me is None:
            return None
        return next((item.relationship_label for item in self.people_store.relationships_for_person(self.user_id, me.id) if item.related_person_id == person_id), None)

    def _validate_profile_asset(self, asset_id: UUID | None) -> None:
        """Require profile references to point to an asset editable by this user."""
        if asset_id is None:
            return
        asset = self.vault_store.get_catalogued_asset_by_id(asset_id)
        if asset is None or not asset_is_editable_by(asset, self.user):
            raise HTTPException(status_code=422, detail="Profile asset was not found")

    @staticmethod
    def _validate_profile_frame(frame: dict[str, float] | None) -> None:
        if frame is None:
            return
        if set(frame) != {"scale", "x", "y"}:
            raise HTTPException(status_code=422, detail="Profile framing requires scale, x, and y")
        try:
            scale, x, y = (float(frame["scale"]), float(frame["x"]), float(frame["y"]))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="Profile framing must be numeric") from error
        if not 1 <= scale <= 3 or not 0 <= x <= 100 or not 0 <= y <= 100:
            raise HTTPException(status_code=422, detail="Profile framing is outside the supported range")

    def _response(self, person: VaultPerson) -> PersonResponse:
        me = self.people_store.resolve_me_person(self.user_id)
        return PersonResponse(person_id=person.id, full_name=person.full_name, preferred_name=person.preferred_name,
            aliases=list(person.aliases), date_of_birth=person.date_of_birth, profile_asset_id=person.profile_asset_id, profile_frame=person.profile_frame,
            is_me=bool(me and me.id == person.id), relationship_label=self._relationship_to(person.id), active=person.active)

    def list(self, query: str | None = None) -> list[PersonResponse]:
        values = self.people_store.search_people(self.user_id, query) if query else self.people_store.list_people(self.user_id)
        return [self._response(person) for person in values]

    def get(self, person_id: UUID) -> VaultPerson:
        person = self.people_store.get_person(person_id, self.user_id)
        if person is None:
            raise HTTPException(status_code=404, detail="Person was not found")
        return person

    def assets(self, person_id: UUID) -> list[PersonAssetResponse]:
        ids = self.people_store.matching_asset_ids((person_id,), self.user_id)
        owned = [
            PersonAssetResponse(
                asset_id=asset.id,
                display_title=asset.display_title,
                asset_type=asset.asset_type,
                vault_path=asset.vault_path,
            )
            for asset in self.vault_store.list_owned_catalogued_assets_by_user_id(self.user_id)
            if asset.id in ids
        ]
        shared_ids = self.share_grant_store.included_gallery_assets_for_local_people(
            self.user_id, (person_id,)
        )
        shared = []
        for asset_id in shared_ids:
            asset = self.vault_store.get_catalogued_asset_by_id(asset_id)
            if asset is None or asset.owner_user_id == self.user_id or asset.asset_type.casefold() != "gallery":
                continue
            shared.append(
                PersonAssetResponse(
                    asset_id=asset.id,
                    display_title=asset.display_title,
                    asset_type=asset.asset_type,
                )
            )
        return owned + sorted(shared, key=lambda asset: (asset.display_title.casefold(), str(asset.asset_id)))

    def detail(self, person_id: UUID) -> PersonDetailResponse:
        person = self.get(person_id)
        assets = self.assets(person_id)
        return PersonDetailResponse(**self._response(person).model_dump(), associated_asset_count=len(assets), associated_assets=assets)

    def create(self, body: PersonCreate) -> PersonResponse:
        try:
            self._validate_profile_asset(body.profile_asset_id)
            self._validate_profile_frame(body.profile_frame)
            if body.relationship_label and self.people_store.resolve_me_person(self.user_id) is None:
                raise HTTPException(
                    status_code=409,
                    detail="Set Me before assigning a relationship",
                )
            person = self.people_store.create_person(str(self.user), owner_user_id=self.user_id, full_name=body.full_name,
                preferred_name=body.preferred_name, aliases=body.aliases, date_of_birth=body.date_of_birth,
                profile_asset_id=body.profile_asset_id, profile_frame=body.profile_frame)
            if body.relationship_label:
                me = self.people_store.resolve_me_person(self.user_id)
                if me is not None:
                    self.people_store.set_relationship(self.user_id, me.id, person.id, body.relationship_label)
            return self._response(person)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    def update(self, person_id: UUID, body: PersonUpdate) -> PersonResponse:
        self.get(person_id)
        try:
            if not body.clear_profile_asset:
                self._validate_profile_asset(body.profile_asset_id)
            if not body.clear_profile_frame and not body.clear_profile_asset:
                self._validate_profile_frame(body.profile_frame)
            person = self.people_store.update_person(person_id, self.user_id, full_name=body.full_name,
                preferred_name=body.preferred_name, aliases=body.aliases, date_of_birth=body.date_of_birth,
                profile_asset_id=body.profile_asset_id, clear_date_of_birth=body.clear_date_of_birth,
                clear_profile_asset=body.clear_profile_asset, profile_frame=body.profile_frame,
                clear_profile_frame=body.clear_profile_frame)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        if person is None:
            raise HTTPException(status_code=404, detail="Person was not found")
        return self._response(person)

    def merge(self, target_person_id: UUID, source_person_id: UUID) -> PersonResponse:
        self.get(target_person_id)
        try:
            person = self.people_store.merge_people(source_person_id, target_person_id, self.user_id)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return self._response(person)


def get_share_grant_store() -> PostgresShareGrantStore:
    return PostgresShareGrantStore(get_database_conninfo())


def service(
    user: AuthenticatedUsername,
    people_store=Depends(get_gallery_people_store),
    vault_store=Depends(get_vault_master_store),
    share_grant_store=Depends(get_share_grant_store),
) -> PeopleService:
    return PeopleService(people_store, vault_store, share_grant_store, user)


@router.get("", response_model=list[PersonResponse])
def list_people(query: str | None = Query(None), people: PeopleService = Depends(service)) -> list[PersonResponse]:
    return people.list(query)


@router.post("", response_model=PersonResponse, status_code=status.HTTP_201_CREATED)
def create_person(body: PersonCreate, people: PeopleService = Depends(service)) -> PersonResponse:
    return people.create(body)


@router.get("/me", response_model=PersonResponse | None)
def get_me(people: PeopleService = Depends(service)) -> PersonResponse | None:
    current = people.people_store.resolve_me_person(people.user_id)
    return people._response(current) if current else None


@router.put("/me", response_model=PersonResponse)
def set_me(body: MeUpdate, people: PeopleService = Depends(service)) -> PersonResponse:
    try:
        return people._response(people.people_store.set_me_person(people.user_id, body.person_id))
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Person was not found") from error


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
def clear_me(people: PeopleService = Depends(service)) -> Response:
    people.people_store.clear_me_person(people.user_id)
    return Response(status_code=204)


@router.get("/{person_id}", response_model=PersonDetailResponse)
def get_person(person_id: UUID, people: PeopleService = Depends(service)) -> PersonDetailResponse:
    return people.detail(person_id)


@router.patch("/{person_id}", response_model=PersonResponse)
def update_person(person_id: UUID, body: PersonUpdate, people: PeopleService = Depends(service)) -> PersonResponse:
    return people.update(person_id, body)


@router.post("/{person_id}/merge", response_model=PersonResponse)
def merge_person(person_id: UUID, body: PersonMerge, people: PeopleService = Depends(service)) -> PersonResponse:
    return people.merge(person_id, body.source_person_id)


@router.get("/{person_id}/relationship", response_model=RelationshipUpdate | None)
def get_relationship(person_id: UUID, people: PeopleService = Depends(service)) -> RelationshipUpdate | None:
    people.get(person_id)
    label = people._relationship_to(person_id)
    return RelationshipUpdate(relationship_label=label) if label else None


@router.put("/{person_id}/relationship", response_model=RelationshipUpdate)
def set_relationship(person_id: UUID, body: RelationshipUpdate, people: PeopleService = Depends(service)) -> RelationshipUpdate:
    people.get(person_id)
    me = people.people_store.resolve_me_person(people.user_id)
    if me is None:
        raise HTTPException(status_code=409, detail="Set Me before managing relationships")
    try:
        people.people_store.set_relationship(people.user_id, me.id, person_id, body.relationship_label)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Person was not found") from error
    return body


@router.delete("/{person_id}/relationship", status_code=status.HTTP_204_NO_CONTENT)
def clear_relationship(person_id: UUID, people: PeopleService = Depends(service)) -> Response:
    people.get(person_id)
    me = people.people_store.resolve_me_person(people.user_id)
    if me:
        people.people_store.clear_relationship(people.user_id, me.id, person_id)
    return Response(status_code=204)


@router.put("/{person_id}/faces/{face_id}", status_code=status.HTTP_204_NO_CONTENT)
def correct_face(person_id: UUID, face_id: UUID, body: FaceCorrection, people: PeopleService = Depends(service)) -> Response:
    people.get(person_id)
    try:
        if body.person_id is None:
            people.people_store.clear_face_identity(body.asset_id, face_id, people.user_id)
        else:
            people.get(body.person_id)
            people.people_store.identify_face(body.asset_id, face_id, body.person_id, people.user_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Face evidence was not found") from error
    return Response(status_code=204)


@router.put("/{person_id}/assets", status_code=status.HTTP_204_NO_CONTENT)
def correct_asset_person(person_id: UUID, body: AssetPersonCorrection, people: PeopleService = Depends(service)) -> Response:
    people.get(person_id)
    people.get(body.person_id)
    asset = people.vault_store.get_catalogued_asset_by_id(body.asset_id)
    if asset is None or not asset_is_editable_by(asset, people.user):
        raise HTTPException(status_code=404, detail="Gallery asset was not found")
    try:
        if body.person_id != person_id:
            people.people_store.decide(body.asset_id, person_id, "exclude", people.user_id, "people_correction")
        people.people_store.decide(body.asset_id, body.person_id, body.decision, people.user_id, "people_correction")
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Person or asset was not found") from error
    return Response(status_code=204)


@router.get("/{person_id}/assets", response_model=list[PersonAssetResponse])
def person_assets(person_id: UUID, people: PeopleService = Depends(service)) -> list[PersonAssetResponse]:
    people.get(person_id)
    return people.assets(person_id)
