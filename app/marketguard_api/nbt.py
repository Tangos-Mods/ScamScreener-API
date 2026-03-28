from __future__ import annotations

import base64
import binascii
import gzip
import io
import logging
import struct
import zlib
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_MAX_COMPRESSED_BYTES = 1_000_000
_MAX_DECOMPRESSED_BYTES = 8_000_000


@dataclass(frozen=True, slots=True)
class ParsedAuctionItem:
    count: int
    extra_attributes: dict[str, Any]


class _NbtReader:
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

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
            return list(self._read_exact(length))
        if tag_type == 8:
            return self._read_string()
        if tag_type == 9:
            item_type = self._read_unsigned_byte()
            length = self._read(">i")[0]
            if length < 0:
                raise ValueError("NBT list length must not be negative.")
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
            return [self._read(">i")[0] for _ in range(length)]
        if tag_type == 12:
            length = self._read(">i")[0]
            return [self._read(">q")[0] for _ in range(length)]
        raise ValueError(f"Unsupported NBT tag type: {tag_type}")

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
    raw_bytes = _decode_item_bytes(encoded_item_bytes)
    if raw_bytes is None:
        return None

    try:
        root = _NbtReader(raw_bytes).read_root_compound()
    except ValueError:
        logger.warning("Could not parse Hypixel item_bytes NBT payload.", exc_info=True)
        return None

    items = root.get("i")
    if not isinstance(items, list) or not items:
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


def _decode_item_bytes(encoded_item_bytes: str) -> bytes | None:
    try:
        compressed = base64.b64decode(encoded_item_bytes, validate=True)
    except (ValueError, binascii.Error):
        logger.warning("Hypixel item_bytes value was not valid base64.")
        return None

    if len(compressed) > _MAX_COMPRESSED_BYTES:
        logger.warning("Hypixel item_bytes payload exceeded the compressed size limit.")
        return None

    for decompressor in (gzip.decompress, _zlib_decompress):
        try:
            payload = decompressor(compressed)
            if len(payload) > _MAX_DECOMPRESSED_BYTES:
                logger.warning("Hypixel item_bytes payload exceeded the decompressed size limit.")
                return None
            return payload
        except OSError:
            continue

    logger.warning("Hypixel item_bytes payload could not be decompressed.")
    return None


def _zlib_decompress(payload: bytes) -> bytes:
    return zlib.decompress(payload, wbits=15 + 32)
