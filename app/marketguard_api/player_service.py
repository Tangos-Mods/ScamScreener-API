from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Any

from .client import HypixelPlayerClient, MojangNameClient
from .config import MarketGuardSettings
from .exceptions import HypixelUpstreamError, MojangUpstreamError
from .models import PlayerProfileQuery
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
            key = (_normalize_player_identifier(query.player), _normalize_uuid(query.profileId) or query.profileId.lower())
            unique_queries.setdefault(key, query)
            query_keys.append(key)

        async def _lookup(key: tuple[str, str], query: PlayerProfileQuery) -> tuple[tuple[str, str], dict[str, object]]:
            try:
                return key, await self._lookup_player(query, skills)
            except (HypixelUpstreamError, MojangUpstreamError):
                logger.warning("Player lookup is temporarily unavailable.")
                return key, self._unavailable_result(query)

        looked_up = await asyncio.gather(*(_lookup(key, query) for key, query in unique_queries.items()))
        results_by_key = dict(looked_up)
        return {
            "status": "ok",
            "players": [dict(results_by_key[key]) for key in query_keys],
        }

    @staticmethod
    def _unavailable_result(query: PlayerProfileQuery) -> dict[str, object]:
        player_uuid = _normalize_uuid(query.player)
        return {
            "status": "unavailable",
            "uuid": player_uuid,
            "name": None if player_uuid is not None else query.player,
            "firstJoin": None,
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
            return {"status": "not_found"}

        player, profiles = await asyncio.gather(
            self._fetch_player(identity.uuid),
            self._fetch_profiles(identity.uuid),
        )
        if player is None:
            return _result("not_found", identity=identity)

        name = _safe_text(player.get("displayname")) or identity.name
        first_join = _non_negative_int(player.get("firstLogin"))
        requested_profile_id = _normalize_uuid(query.profileId)
        profile = next(
            (
                candidate
                for candidate in profiles
                if _normalize_uuid(candidate.get("profile_id")) == requested_profile_id
            ),
            None,
        )
        if profile is None:
            return _result("profile_not_found", identity=identity, name=name, first_join=first_join)

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
            unavailable_fields = ["bank", "purse", "equipment", "armor", "skills"]
            if first_join is None:
                unavailable_fields.insert(0, "firstJoin")
            return _result(
                "profile_unavailable",
                identity=identity,
                name=name,
                first_join=first_join,
                profile=profile_payload,
                unavailable_fields=unavailable_fields,
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

        profile_payload["wealth"] = {
            "bank": bank,
            "purse": purse,
            "equipment": equipment,
            "armor": armor,
        }
        profile_payload["skills"] = skills
        status = "partial" if unavailable_fields else "ok"
        return _result(
            status,
            identity=identity,
            name=name,
            first_join=first_join,
            profile=profile_payload,
            unavailable_fields=unavailable_fields,
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
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "uuid": identity.uuid,
        "name": name if name is not None else identity.name,
        "firstJoin": first_join,
        "profile": profile,
        "unavailableFields": unavailable_fields or [],
    }
    return payload


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
