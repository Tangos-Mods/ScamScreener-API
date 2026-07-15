from __future__ import annotations

import base64
import binascii
import io
import logging
import struct
import zlib
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MAX_COMPRESSED_BYTES = 1_000_000
_MAX_DECOMPRESSED_BYTES = 8_000_000
_DECOMPRESS_CHUNK_BYTES = 64 * 1024
_MAX_NBT_COLLECTION_ITEMS = 100_000
_MAX_NBT_NODES = 100_000


@dataclass(frozen=True, slots=True)
class ParsedAuctionItem:
    count: int
    extra_attributes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedInventoryItem:
    slot: int
    item_id: str
    name: str
    count: int


class _NbtReader:
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)
        self._node_count = 0

    def read_root_compound(self) -> dict[str, Any]:
        tag_type = self._read_unsigned_byte()
        if tag_type != 10:
            raise ValueError("NBT root must be a compound tag.")
        self._read_string()
        payload = self._read_payload(tag_type)
        if not isinstance(payload, dict):
            raise ValueError("NBT root payload was not a compound.")
        return payload

    def _read_payload(self, tag_type: int) -> Any:
        self._node_count += 1
        if self._node_count > _MAX_NBT_NODES:
            raise ValueError("NBT payload exceeded the node limit.")
        if tag_type == 0:
            return None
        if tag_type == 1:
            return self._read_signed_byte()
        if tag_type == 2:
            return self._read(">h")[0]
        if tag_type == 3:
            return self._read(">i")[0]
        if tag_type == 4:
            return self._read(">q")[0]
        if tag_type == 5:
            return self._read(">f")[0]
        if tag_type == 6:
            return self._read(">d")[0]
        if tag_type == 7:
            length = self._read(">i")[0]
            self._validate_collection_length(length)
            return list(self._read_exact(length))
        if tag_type == 8:
            return self._read_string()
        if tag_type == 9:
            item_type = self._read_unsigned_byte()
            length = self._read(">i")[0]
            self._validate_collection_length(length)
            return [self._read_payload(item_type) for _ in range(length)]
        if tag_type == 10:
            result: dict[str, Any] = {}
            while True:
                nested_type = self._read_unsigned_byte()
                if nested_type == 0:
                    return result
                name = self._read_string()
                result[name] = self._read_payload(nested_type)
        if tag_type == 11:
            length = self._read(">i")[0]
            self._validate_collection_length(length)
            return [self._read(">i")[0] for _ in range(length)]
        if tag_type == 12:
            length = self._read(">i")[0]
            self._validate_collection_length(length)
            return [self._read(">q")[0] for _ in range(length)]
        raise ValueError(f"Unsupported NBT tag type: {tag_type}")

    @staticmethod
    def _validate_collection_length(length: int) -> None:
        if length < 0:
            raise ValueError("NBT collection length must not be negative.")
        if length > _MAX_NBT_COLLECTION_ITEMS:
            raise ValueError("NBT collection length exceeded the limit.")

    def _read_string(self) -> str:
        length = self._read(">H")[0]
        return self._read_exact(length).decode("utf-8", errors="replace")

    def _read_unsigned_byte(self) -> int:
        return self._read(">B")[0]

    def _read_signed_byte(self) -> int:
        return self._read(">b")[0]

    def _read(self, fmt: str) -> tuple[Any, ...]:
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self._read_exact(size))

    def _read_exact(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("NBT read length must not be negative.")
        payload = self._buffer.read(size)
        if len(payload) != size:
            raise ValueError("Unexpected end of NBT payload.")
        return payload


def parse_item_bytes_nbt(encoded_item_bytes: str) -> ParsedAuctionItem | None:
    items = _parse_inventory_root(encoded_item_bytes)
    if not items:
        return None

    first_item = items[0]
    if not isinstance(first_item, dict):
        return None

    tag = first_item.get("tag")
    if not isinstance(tag, dict):
        return None

    extra_attributes = tag.get("ExtraAttributes")
    if not isinstance(extra_attributes, dict):
        return None

    count = first_item.get("Count", 1)
    if isinstance(count, bool):
        count = 1
    elif isinstance(count, float):
        count = int(count)
    elif not isinstance(count, int):
        count = 1

    return ParsedAuctionItem(
        count=max(1, int(count)),
        extra_attributes=extra_attributes,
    )


def parse_inventory_nbt(encoded_item_bytes: str) -> list[ParsedInventoryItem] | None:
    items = _parse_inventory_root(encoded_item_bytes)
    if items is None:
        return None

    parsed_items: list[ParsedInventoryItem] = []
    for slot, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        parsed = _parse_inventory_item(item, slot)
        if parsed is not None:
            parsed_items.append(parsed)
    return parsed_items


def _parse_inventory_root(encoded_item_bytes: str) -> list[Any] | None:
    raw_bytes = _decode_item_bytes(encoded_item_bytes)
    if raw_bytes is None:
        return None

    try:
        root = _NbtReader(raw_bytes).read_root_compound()
    except ValueError:
        logger.warning("Could not parse Hypixel inventory NBT payload.", exc_info=True)
        return None

    items = root.get("i")
    if not isinstance(items, list):
        logger.warning("Hypixel inventory NBT payload did not contain an item list.")
        return None
    return items


def _parse_inventory_item(item: dict[str, Any], slot: int) -> ParsedInventoryItem | None:
    tag = item.get("tag")
    if not isinstance(tag, dict):
        return None
    extra_attributes = tag.get("ExtraAttributes")
    if not isinstance(extra_attributes, dict):
        extra_attributes = {}

    item_id = _safe_item_text(extra_attributes.get("id")) or _safe_item_text(item.get("id"))
    if not item_id:
        return None

    display = tag.get("display")
    display_name = display.get("Name") if isinstance(display, dict) else None
    name = _strip_minecraft_formatting(_safe_item_text(display_name) or item_id)
    count = item.get("Count", 1)
    if isinstance(count, bool):
        count = 1
    elif isinstance(count, float):
        count = int(count)
    elif not isinstance(count, int):
        count = 1

    return ParsedInventoryItem(
        slot=slot,
        item_id=item_id,
        name=name or item_id,
        count=max(1, int(count)),
    )


def _safe_item_text(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized[:256]


def _strip_minecraft_formatting(value: str) -> str:
    cleaned: list[str] = []
    skip_next = False
    for character in value:
        if skip_next:
            skip_next = False
            continue
        if character == "\u00a7":
            skip_next = True
            continue
        cleaned.append(character)
    return "".join(cleaned).strip()[:256]


def _decode_item_bytes(encoded_item_bytes: str) -> bytes | None:
    try:
        compressed = base64.b64decode(encoded_item_bytes, validate=True)
    except (ValueError, binascii.Error):
        logger.warning("Hypixel item_bytes value was not valid base64.")
        return None

    if len(compressed) > _MAX_COMPRESSED_BYTES:
        logger.warning("Hypixel item_bytes payload exceeded the compressed size limit.")
        return None

    try:
        return _bounded_decompress(compressed)
    except zlib.error:
        logger.warning("Hypixel item_bytes payload could not be decompressed.")
        return None


def _bounded_decompress(compressed: bytes) -> bytes | None:
    decompressor = zlib.decompressobj(wbits=15 + 32)
    remaining = compressed
    output = bytearray()

    while remaining:
        remaining_capacity = (_MAX_DECOMPRESSED_BYTES + 1) - len(output)
        if remaining_capacity <= 0:
            logger.warning("Hypixel item_bytes payload exceeded the decompressed size limit.")
            return None
        chunk = decompressor.decompress(remaining, min(_DECOMPRESS_CHUNK_BYTES, remaining_capacity))
        output.extend(chunk)
        if len(output) > _MAX_DECOMPRESSED_BYTES:
            logger.warning("Hypixel item_bytes payload exceeded the decompressed size limit.")
            return None
        remaining = decompressor.unconsumed_tail

    remaining_capacity = (_MAX_DECOMPRESSED_BYTES + 1) - len(output)
    if remaining_capacity <= 0:
        logger.warning("Hypixel item_bytes payload exceeded the decompressed size limit.")
        return None
    output.extend(decompressor.flush(remaining_capacity))
    if len(output) > _MAX_DECOMPRESSED_BYTES:
        logger.warning("Hypixel item_bytes payload exceeded the decompressed size limit.")
        return None
    if not decompressor.eof or decompressor.unused_data:
        raise zlib.error("Incomplete or concatenated compressed payload.")
    return bytes(output)
