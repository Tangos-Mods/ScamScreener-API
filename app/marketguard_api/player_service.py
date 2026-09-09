from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .client import HypixelPlayerClient, MojangNameClient
from .config import MarketGuardSettings
from .exceptions import HypixelAuthenticationError, HypixelRateLimitError, HypixelUpstreamError, MojangUpstreamError
from .models import PlayerFinanceQuery, PlayerProfileQuery
from .nbt import parse_inventory_nbt

logger = logging.getLogger(__name__)

_SKILL_DEFINITION_CACHE_TTL_SECONDS = 60 * 60


@dataclass(frozen=True, slots=True)
class _PlayerIdentity:
    uuid: str
    name: str | None


class PlayerService:
    def __init__(
        self,
        settings: MarketGuardSettings,
        hypixel_client: HypixelPlayerClient | None = None,
        mojang_client: MojangNameClient | None = None,
        max_concurrency: int | None = None,
    ) -> None:
        self._settings = settings
        self._hypixel_client = hypixel_client or HypixelPlayerClient(settings)
        self._mojang_client = mojang_client or MojangNameClient(settings)
        configured_concurrency = settings.players_max_upstream_concurrency if max_concurrency is None else max_concurrency
        self._max_concurrency = max(1, min(10, int(configured_concurrency)))
        self._upstream_semaphore = asyncio.Semaphore(self._max_concurrency)
        self._skill_lock = asyncio.Lock()
        self._skill_definitions: dict[str, dict[str, Any]] | None = None
        self._skill_definitions_expires_at = 0.0

    async def aclose(self) -> None:
        await self._hypixel_client.aclose()
        await self._mojang_client.aclose()

    async def get_players(self, queries: list[PlayerProfileQuery]) -> dict[str, object]:
        if not self._settings.hypixel_api_key.strip():
            return {
                "status": "ok",
                "players": [self._unavailable_result(query) for query in queries],
            }

        skills = await self._load_skill_definitions()
        unique_queries: dict[tuple[str, str], PlayerProfileQuery] = {}
        query_keys: list[tuple[str, str]] = []
        for query in queries:
            requested_profile_id = _normalize_uuid(query.profileId)
            key = (_normalize_player_identifier(query.player), requested_profile_id or "selected")
            unique_queries.setdefault(key, query)
            query_keys.append(key)

        async def _lookup(key: tuple[str, str], query: PlayerProfileQuery) -> tuple[tuple[str, str], dict[str, object]]:
            try:
                return key, await self._lookup_player(query, skills)
            except HypixelAuthenticationError:
                raise
            except (HypixelUpstreamError, MojangUpstreamError):
                logger.warning("Player lookup is temporarily unavailable.")
                return key, self._unavailable_result(query)

        looked_up = await asyncio.gather(*(_lookup(key, query) for key, query in unique_queries.items()))
        results_by_key = dict(looked_up)
        return {
            "status": "ok",
            "players": [dict(results_by_key[key]) for key in query_keys],
        }

    async def get_player_finance(self, query: PlayerFinanceQuery) -> dict[str, object]:
        player_uuid = _normalize_uuid(query.playerUuid)
        profile_id = _normalize_uuid(query.profileId)
        if player_uuid is None or profile_id is None:
            raise ValueError("Player finance UUIDs must be normalized before service use.")

        async with self._upstream_semaphore:
            profile = await self._hypixel_client.fetch_profile(profile_id)
        fetched_at = _epoch_millis()
        if profile is None:
            return {
                "status": "profile_not_found",
                "stale": False,
                "fetchedAt": fetched_at,
                "playerUuid": player_uuid,
                "profile": None,
                "unavailableFields": ["profile"],
            }

        profile_payload = _finance_profile_payload(profile, profile_id)
        member = _profile_member(profile, player_uuid)
        if member is None:
            return {
                "status": "member_not_found",
                "stale": False,
                "fetchedAt": fetched_at,
                "playerUuid": player_uuid,
                "profile": profile_payload,
                "unavailableFields": ["profile.member", "finance", "museum"],
            }

        unavailable_fields: list[str] = []
        bank = _bank_balance(profile)
        if bank is None:
            unavailable_fields.append("finance.bank")
        purse = _coin_purse(member)
        if purse is None:
            unavailable_fields.append("finance.purse")

        museum_payload: dict[str, object]
        try:
            async with self._upstream_semaphore:
                museum_response = await self._hypixel_client.fetch_museum(profile_id)
        except HypixelAuthenticationError:
            raise
        except HypixelRateLimitError:
            raise
        except HypixelUpstreamError:
            logger.warning("Hypixel Museum data is temporarily unavailable.")
            museum_payload = _empty_museum_payload()
            unavailable_fields.extend(_museum_unavailable_fields())
        else:
            museum_record = _museum_member(museum_response, player_uuid)
            museum_payload, museum_unavailable = _museum_payload(museum_record)
            unavailable_fields.extend(museum_unavailable)

        museum_value = museum_payload["value"]
        known_total = _known_total(bank, purse, museum_value)
        if museum_value is None:
            unavailable_fields.append("finance.museumValue")
        if known_total is None:
            unavailable_fields.append("finance.knownTotal")

        profile_payload["finance"] = {
            "bank": bank,
            "purse": purse,
            "museumValue": museum_value,
            "knownTotal": known_total,
        }
        profile_payload["museum"] = museum_payload
        return {
            "status": "partial" if unavailable_fields else "ok",
            "stale": False,
            "fetchedAt": fetched_at,
            "playerUuid": player_uuid,
            "profile": profile_payload,
            "unavailableFields": list(dict.fromkeys(unavailable_fields)),
        }

    @staticmethod
    def _unavailable_result(query: PlayerProfileQuery) -> dict[str, object]:
        player_uuid = _normalize_uuid(query.player)
        return {
            "status": "unavailable",
            "uuid": player_uuid,
            "name": None if player_uuid is not None else query.player,
            "firstJoin": None,
            "fetchedAt": None,
            "source": None,
            "profile": None,
            "unavailableFields": ["firstJoin", "profile"],
        }

    async def _load_skill_definitions(self) -> dict[str, dict[str, Any]] | None:
        now = time.monotonic()
        if self._skill_definitions is not None and self._skill_definitions_expires_at > now:
            return self._skill_definitions

        async with self._skill_lock:
            now = time.monotonic()
            if self._skill_definitions is not None and self._skill_definitions_expires_at > now:
                return self._skill_definitions
            try:
                async with self._upstream_semaphore:
                    definitions = await self._hypixel_client.fetch_skyblock_skills()
            except HypixelAuthenticationError:
                raise
            except HypixelUpstreamError:
                logger.warning("Hypixel SkyBlock skill definitions are temporarily unavailable.")
                return None
            self._skill_definitions = definitions
            self._skill_definitions_expires_at = time.monotonic() + _SKILL_DEFINITION_CACHE_TTL_SECONDS
            return definitions

    async def _lookup_player(
        self,
        query: PlayerProfileQuery,
        skill_definitions: dict[str, dict[str, Any]] | None,
    ) -> dict[str, object]:
        identity = await self._resolve_identity(query.player)
        if identity is None:
            return {
                "status": "not_found",
                "uuid": None,
                "name": None,
                "firstJoin": None,
                "fetchedAt": _epoch_millis(),
                "source": "mojang",
                "profile": None,
                "unavailableFields": [],
            }

        player, profiles = await asyncio.gather(
            self._fetch_player(identity.uuid),
            self._fetch_profiles(identity.uuid),
        )
        fetched_at = _epoch_millis()
        if player is None:
            return _result("not_found", identity=identity, fetched_at=fetched_at, source="hypixel")

        name = _safe_text(player.get("displayname")) or identity.name
        first_join = _non_negative_int(player.get("firstLogin"))
        requested_profile_id = _normalize_uuid(query.profileId)
        if requested_profile_id is None:
            profile = next((candidate for candidate in profiles if candidate.get("selected") is True), None)
        else:
            profile = next(
                (
                    candidate
                    for candidate in profiles
                    if _normalize_uuid(candidate.get("profile_id")) == requested_profile_id
                ),
                None,
            )
        if profile is None:
            return _result(
                "profile_not_found",
                identity=identity,
                name=name,
                first_join=first_join,
                fetched_at=fetched_at,
                source="hypixel",
            )

        profile_payload = _profile_payload(profile)
        member = _profile_member(profile, identity.uuid)
        if member is None:
            profile_payload["wealth"] = {
                "bank": None,
                "purse": None,
                "equipment": None,
                "armor": None,
            }
            profile_payload["skills"] = None
            unavailable_fields = ["bank", "purse", "equipment", "armor", "skills", "activePet", "activeWeapon"]
            if first_join is None:
                unavailable_fields.insert(0, "firstJoin")
            return _result(
                "profile_unavailable",
                identity=identity,
                name=name,
                first_join=first_join,
                profile=profile_payload,
                unavailable_fields=unavailable_fields,
                fetched_at=fetched_at,
                source="hypixel",
            )

        unavailable_fields: list[str] = []
        if first_join is None:
            unavailable_fields.append("firstJoin")

        bank = _bank_balance(profile)
        if bank is None:
            unavailable_fields.append("bank")

        purse = _coin_purse(member)
        if purse is None:
            unavailable_fields.append("purse")

        equipment = _inventory(member, "equippment_contents", "equipment_contents")
        if equipment is None:
            unavailable_fields.append("equipment")

        armor = _inventory(member, "inv_armor")
        if armor is None:
            unavailable_fields.append("armor")

        skills = _skills(member, skill_definitions)
        if skills is None:
            unavailable_fields.append("skills")

        active_pet, active_pet_available = _active_pet(member)
        if not active_pet_available:
            unavailable_fields.append("activePet")

        # The public SkyBlock profile API exposes inventories but not the currently held slot.
        # Do not guess a weapon from a hotbar item.
        unavailable_fields.append("activeWeapon")

        profile_payload["wealth"] = {
            "bank": bank,
            "purse": purse,
            "equipment": equipment,
            "armor": armor,
        }
        profile_payload["skills"] = skills
        profile_payload["activePet"] = active_pet
        profile_payload["activeWeapon"] = None
        status = "partial" if unavailable_fields else "ok"
        return _result(
            status,
            identity=identity,
            name=name,
            first_join=first_join,
            profile=profile_payload,
            unavailable_fields=unavailable_fields,
            fetched_at=fetched_at,
            source="hypixel",
        )

    async def _resolve_identity(self, player: str) -> _PlayerIdentity | None:
        player_uuid = _normalize_uuid(player)
        if player_uuid is not None:
            return _PlayerIdentity(uuid=player_uuid, name=None)

        async with self._upstream_semaphore:
            resolved = await self._mojang_client.resolve_name(player)
        if resolved is None:
            return None
        resolved_uuid, resolved_name = resolved
        return _PlayerIdentity(uuid=resolved_uuid, name=resolved_name)

    async def _fetch_player(self, player_uuid: str) -> dict[str, Any] | None:
        async with self._upstream_semaphore:
            return await self._hypixel_client.fetch_player(player_uuid)

    async def _fetch_profiles(self, player_uuid: str) -> list[dict[str, Any]]:
        async with self._upstream_semaphore:
            return await self._hypixel_client.fetch_profiles(player_uuid)


def _result(
    status: str,
    *,
    identity: _PlayerIdentity,
    name: str | None = None,
    first_join: int | None = None,
    profile: dict[str, object] | None = None,
    unavailable_fields: list[str] | None = None,
    fetched_at: int | None = None,
    source: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "uuid": identity.uuid,
        "name": name if name is not None else identity.name,
        "firstJoin": first_join,
        "fetchedAt": fetched_at,
        "source": source,
        "profile": profile,
        "unavailableFields": unavailable_fields or [],
    }
    return payload


def _epoch_millis() -> int:
    return int(time.time() * 1_000)


def _profile_payload(profile: dict[str, Any]) -> dict[str, object]:
    profile_id = _normalize_uuid(profile.get("profile_id"))
    if profile_id is None:
        raise HypixelUpstreamError("Hypixel API returned a SkyBlock profile without a valid profile ID.")
    return {
        "id": profile_id,
        "name": _safe_text(profile.get("cute_name")) or None,
        "selected": profile.get("selected") is True,
        "wealth": {
            "bank": None,
            "purse": None,
            "equipment": None,
            "armor": None,
        },
        "skills": None,
        "activePet": None,
        "activeWeapon": None,
    }


def _finance_profile_payload(profile: dict[str, Any], expected_profile_id: str) -> dict[str, object]:
    profile_id = _normalize_uuid(profile.get("profile_id"))
    if profile_id != expected_profile_id:
        raise HypixelUpstreamError("Hypixel API returned a mismatched SkyBlock profile ID.")
    return {
        "id": profile_id,
        "name": _safe_text(profile.get("cute_name")) or None,
        "selected": profile.get("selected") is True,
        "finance": {
            "bank": None,
            "purse": None,
            "museumValue": None,
            "knownTotal": None,
        },
        "museum": _empty_museum_payload(),
    }


def _profile_member(profile: dict[str, Any], player_uuid: str) -> dict[str, Any] | None:
    members = profile.get("members")
    if not isinstance(members, dict):
        return None
    for member_uuid, member in members.items():
        if _normalize_uuid(member_uuid) == player_uuid and isinstance(member, dict):
            return member
    return None


def _bank_balance(profile: dict[str, Any]) -> float | None:
    banking = profile.get("banking")
    if not isinstance(banking, dict):
        return None
    return _non_negative_number(banking.get("balance"))


def _coin_purse(member: dict[str, Any]) -> float | None:
    return _non_negative_number(member.get("coin_purse"))


def _museum_member(payload: dict[str, Any], player_uuid: str) -> dict[str, Any] | None:
    profile_payload = payload.get("profile")
    if _looks_like_museum_record(profile_payload):
        return profile_payload
    member = _member_from_map(profile_payload, player_uuid)
    if member is not None:
        return member
    return _member_from_map(payload.get("members"), player_uuid)


def _looks_like_museum_record(value: Any) -> bool:
    return isinstance(value, dict) and any(field in value for field in ("value", "appraisal", "items", "special"))


def _member_from_map(value: Any, player_uuid: str) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    for raw_uuid, member in value.items():
        if _normalize_uuid(raw_uuid) == player_uuid and isinstance(member, dict):
            return member
    return None


def _museum_payload(record: dict[str, Any] | None) -> tuple[dict[str, object], list[str]]:
    if record is None:
        return _empty_museum_payload(), _museum_unavailable_fields()

    unavailable_fields: list[str] = []
    value = _non_negative_number(record.get("value"))
    if value is None:
        unavailable_fields.append("museum.value")

    appraisal_raw = record.get("appraisal")
    appraisal = appraisal_raw if isinstance(appraisal_raw, bool) else None
    if appraisal is None:
        unavailable_fields.append("museum.appraisal")

    donated_ids = _museum_position_ids(record.get("items"))
    if donated_ids is None:
        unavailable_fields.extend(["museum.donatedIds", "museum.donatedCount"])

    special_ids = _museum_special_ids(record.get("special"))
    if special_ids is None:
        unavailable_fields.extend(["museum.specialIds", "museum.specialCount"])

    return (
        {
            "value": value,
            "appraisal": appraisal,
            "donatedIds": donated_ids,
            "donatedCount": len(donated_ids) if donated_ids is not None else None,
            "specialIds": special_ids,
            "specialCount": len(special_ids) if special_ids is not None else None,
        },
        unavailable_fields,
    )


def _empty_museum_payload() -> dict[str, object]:
    return {
        "value": None,
        "appraisal": None,
        "donatedIds": None,
        "donatedCount": None,
        "specialIds": None,
        "specialCount": None,
    }


def _museum_unavailable_fields() -> list[str]:
    return [
        "museum.value",
        "museum.appraisal",
        "museum.donatedIds",
        "museum.donatedCount",
        "museum.specialIds",
        "museum.specialCount",
    ]


def _museum_position_ids(value: Any) -> list[str] | None:
    if not isinstance(value, dict):
        return None
    identifiers: list[str] = []
    for raw_identifier in value:
        identifier = _museum_identifier(raw_identifier)
        if identifier is None:
            return None
        identifiers.append(identifier)
    return sorted(identifiers)


def _museum_special_ids(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    identifiers: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            identifier = _museum_identifier(entry)
            if identifier is None:
                return None
            identifiers.append(identifier)
            continue
        if not isinstance(entry, dict):
            return None

        explicit_identifier = next(
            (
                _museum_identifier(entry.get(field))
                for field in ("id", "item_id", "itemId", "donation_id", "donationId")
                if entry.get(field) is not None
            ),
            None,
        )
        if explicit_identifier is not None:
            identifiers.append(explicit_identifier)
            continue

        encoded_items = entry.get("items")
        if not isinstance(encoded_items, dict):
            return None
        encoded = encoded_items.get("data")
        if isinstance(encoded, str) and encoded.strip():
            parsed = parse_inventory_nbt(encoded)
            if parsed is None or not parsed:
                return None
            identifiers.extend(item.item_id for item in parsed)
            continue

        nested_ids = _museum_position_ids(encoded_items)
        if nested_ids is None or not nested_ids:
            return None
        identifiers.extend(nested_ids)
    return identifiers


def _museum_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 128 or not all(character.isalnum() or character in "_:-." for character in normalized):
        return None
    return normalized


def _known_total(bank: float | None, purse: float | None, museum_value: object) -> float | None:
    if bank is None or purse is None or not isinstance(museum_value, (int, float)) or isinstance(museum_value, bool):
        return None
    total = bank + purse + float(museum_value)
    return total if math.isfinite(total) and total >= 0.0 else None


def _inventory(member: dict[str, Any], *keys: str) -> list[dict[str, object]] | None:
    for key in keys:
        inventory = member.get(key)
        if not isinstance(inventory, dict):
            continue
        encoded = inventory.get("data")
        if not isinstance(encoded, str) or not encoded.strip():
            continue
        parsed = parse_inventory_nbt(encoded)
        if parsed is None:
            return None
        return [
            {
                "slot": item.slot,
                "id": item.item_id,
                "name": item.name,
                "count": item.count,
            }
            for item in parsed
        ]
    return None


def _skills(member: dict[str, Any], definitions: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, float | int]] | None:
    if definitions is None:
        return None
    player_data = member.get("player_data")
    if not isinstance(player_data, dict):
        return None
    experience = player_data.get("experience")
    if not isinstance(experience, dict):
        return None

    result: dict[str, dict[str, float | int]] = {}
    for skill_id, definition in definitions.items():
        normalized_id = str(skill_id).strip().upper()
        if not normalized_id:
            continue
        xp = _non_negative_number(experience.get(f"SKILL_{normalized_id}")) or 0.0
        result[normalized_id.lower()] = {
            "level": _skill_level(xp, definition),
            "xp": xp,
        }
    return result


def _active_pet(member: dict[str, Any]) -> tuple[dict[str, object] | None, bool]:
    pets_data = member.get("pets_data")
    pets = pets_data.get("pets") if isinstance(pets_data, dict) else member.get("pets")
    if not isinstance(pets, list):
        return None, False

    active_pets = [pet for pet in pets if isinstance(pet, dict) and pet.get("active") is True]
    if not active_pets:
        return None, True
    if len(active_pets) != 1:
        return None, False

    pet = active_pets[0]
    pet_type = _safe_text(pet.get("type")).upper()
    if not pet_type:
        return None, False
    return {
        "type": pet_type,
        "tier": _safe_text(pet.get("tier")).upper() or None,
        "xp": _non_negative_number(pet.get("exp")),
        "heldItem": _safe_text(pet.get("heldItem")).upper() or None,
    }, True


def _skill_level(xp: float, definition: dict[str, Any]) -> int:
    max_level = _non_negative_int(definition.get("maxLevel")) or 0
    levels = definition.get("levels")
    if not isinstance(levels, list):
        return 0
    level = 0
    for candidate in levels:
        if not isinstance(candidate, dict):
            continue
        candidate_level = _non_negative_int(candidate.get("level"))
        required_xp = _non_negative_number(candidate.get("totalExpRequired"))
        if candidate_level is None or required_xp is None:
            continue
        if xp >= required_xp:
            level = max(level, candidate_level)
    return min(level, max_level)


def _normalize_player_identifier(value: str) -> str:
    normalized_uuid = _normalize_uuid(value)
    return normalized_uuid if normalized_uuid is not None else str(value).strip().lower()


def _normalize_uuid(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "")
    if len(normalized) != 32 or not all(character in "0123456789abcdef" for character in normalized):
        return None
    return normalized


def _safe_text(value: object) -> str:
    return str(value or "").strip()[:256]


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _non_negative_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed
