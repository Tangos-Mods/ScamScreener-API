from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .nbt import parse_item_bytes_nbt

logger = logging.getLogger(__name__)

_PET_RARITIES = ("COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "MYTHIC")
_PET_LEVEL_PATTERN = re.compile(r"\[Lvl (?P<level>\d+)]", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ResolvedAuctionItem:
    keys: tuple[str, ...]
    unit_price: float


def resolve_auction_item(auction: dict[str, Any]) -> ResolvedAuctionItem | None:
    item_bytes = str(auction.get("item_bytes", "") or "").strip()
    if not item_bytes:
        return None

    parsed_item = parse_item_bytes_nbt(item_bytes)
    if parsed_item is None:
        return None

    price = _parse_unit_price(auction.get("starting_bid"), parsed_item.count)
    if price is None:
        return None

    internal_name = _resolve_internal_name(parsed_item.extra_attributes)
    if not internal_name:
        return None

    keys: list[str] = [internal_name]

    if _is_pet(parsed_item.extra_attributes) and _is_level_100_pet(str(auction.get("item_name", "") or "")):
        keys.append(f"{internal_name}+100")

    attribute_keys = _resolve_attribute_keys(internal_name, parsed_item.extra_attributes.get("attributes"))
    keys.extend(attribute_keys)

    return ResolvedAuctionItem(keys=tuple(dict.fromkeys(keys)), unit_price=price)


def _resolve_internal_name(extra_attributes: dict[str, Any]) -> str | None:
    raw_internal_name = str(extra_attributes.get("id", "") or "").strip()
    if not raw_internal_name:
        return None

    normalized = raw_internal_name.upper().replace(":", "-")
    if normalized == "PET":
        return _resolve_pet_name(extra_attributes)
    if normalized == "RUNE":
        return _resolve_rune_name(extra_attributes)
    if normalized == "ENCHANTED_BOOK":
        return _resolve_enchanted_book_name(extra_attributes)
    if normalized in {"PARTY_HAT_CRAB", "PARTY_HAT_CRAB_ANIMATED"}:
        return _resolve_crab_hat_name(extra_attributes)
    return normalized


def _resolve_pet_name(extra_attributes: dict[str, Any]) -> str | None:
    pet_info_raw = str(extra_attributes.get("petInfo", "") or "").strip()
    if not pet_info_raw:
        return None

    try:
        pet_info = json.loads(pet_info_raw)
    except json.JSONDecodeError:
        logger.warning("Could not decode Hypixel petInfo payload.")
        return None

    pet_type = str(pet_info.get("type", "") or "").strip().upper()
    pet_tier = str(pet_info.get("tier", "") or "").strip().upper()
    if not pet_type or pet_tier not in _PET_RARITIES:
        return None

    rarity_index = _PET_RARITIES.index(pet_tier)
    return f"{pet_type};{rarity_index}"


def _resolve_rune_name(extra_attributes: dict[str, Any]) -> str | None:
    runes = extra_attributes.get("runes")
    if not isinstance(runes, dict) or not runes:
        return None

    if len(runes) != 1:
        return None

    rune_name = next(iter(runes))
    rune_level = _coerce_int(runes.get(rune_name))
    if not rune_name or rune_level is None:
        return None
    return f"{str(rune_name).upper()}_RUNE;{rune_level}"


def _resolve_enchanted_book_name(extra_attributes: dict[str, Any]) -> str | None:
    enchantments = extra_attributes.get("enchantments")
    if not isinstance(enchantments, dict) or not enchantments:
        return None

    if len(enchantments) != 1:
        return None

    enchantment_name = next(iter(enchantments))
    enchantment_level = _coerce_int(enchantments.get(enchantment_name))
    if not enchantment_name or enchantment_level is None:
        return None
    return f"{str(enchantment_name).upper()};{enchantment_level}"


def _resolve_crab_hat_name(extra_attributes: dict[str, Any]) -> str | None:
    color = str(extra_attributes.get("party_hat_color", "") or "").strip().upper()
    year = _coerce_int(extra_attributes.get("party_hat_year"))
    if not color or year is None:
        return None
    suffix = "_ANIMATED" if year == 2022 else ""
    return f"PARTY_HAT_CRAB_{color}{suffix}"


def _resolve_attribute_keys(base_key: str, raw_attributes: Any) -> list[str]:
    if not isinstance(raw_attributes, dict) or not raw_attributes:
        return []

    normalized_attributes: list[tuple[str, int]] = []
    for attribute_name, attribute_level in raw_attributes.items():
        level = _coerce_int(attribute_level)
        normalized_name = str(attribute_name or "").strip().upper()
        if not normalized_name or level is None:
            continue
        normalized_attributes.append((normalized_name, level))

    if not normalized_attributes:
        return []

    normalized_attributes.sort(key=lambda item: item[0])
    keys = [f"{base_key}+ATTRIBUTE_{name};{level}" for name, level in normalized_attributes]
    if len(normalized_attributes) > 1:
        combined = "".join(f"+ATTRIBUTE_{name}" for name, _level in normalized_attributes)
        keys.append(f"{base_key}{combined}")
    return keys


def _parse_unit_price(raw_price: Any, count: int) -> float | None:
    if isinstance(raw_price, bool):
        return None
    if isinstance(raw_price, int):
        price = float(raw_price)
    elif isinstance(raw_price, float):
        price = raw_price
    else:
        return None

    if price < 0:
        return None

    safe_count = max(1, int(count))
    return price / safe_count


def _is_level_100_pet(item_name: str) -> bool:
    match = _PET_LEVEL_PATTERN.search(item_name or "")
    if not match:
        return False
    try:
        return int(match.group("level")) >= 100
    except ValueError:
        return False


def _is_pet(extra_attributes: dict[str, Any]) -> bool:
    raw_internal_name = str(extra_attributes.get("id", "") or "").strip()
    return raw_internal_name.upper().replace(":", "-") == "PET"


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
