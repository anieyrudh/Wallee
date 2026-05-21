"""Decode Prusa binary G-code containers into canonical plaintext G-code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct
import zlib


_BGCODE_MAGIC = b"GCDE"
_BLOCK_PARAM_LENGTHS = {
    0: 2,  # file metadata
    1: 2,  # gcode encoding
    2: 2,  # slicer metadata
    3: 2,  # printer metadata
    4: 2,  # print metadata
    5: 6,  # thumbnail
}
_METADATA_BLOCK_TYPES = {0, 2, 3, 4}
_GCODE_BLOCK_TYPE = 1
_HEATSHRINK_WINDOW_LOOKAHEAD = {
    2: (11, 4),
    3: (12, 4),
}


class BgcodeDecodeError(RuntimeError):
    """Raised when a Prusa `.bgcode` container cannot be decoded."""


@dataclass(slots=True)
class _BgcodeBlock:
    block_type: int
    compression: int
    payload: bytes
    params: bytes


def normalize_prusa_print_text(*, file_path: str | None, raw_bytes: bytes | None) -> str | None:
    """Return canonical plaintext G-code for one downloaded printer file."""
    if not raw_bytes:
        return None
    suffix = Path(file_path or "").suffix.lower()
    if suffix in {".bgcode", ".bgc"} or raw_bytes.startswith(_BGCODE_MAGIC):
        return decode_bgcode_text(raw_bytes)
    return _decode_plain_text(raw_bytes)


def decode_bgcode_text(raw_bytes: bytes) -> str:
    """Return canonical plaintext G-code from a Prusa `GCDE` container."""
    blocks, checksum_type = _parse_bgcode_blocks(raw_bytes)
    if checksum_type not in {0, 1}:
        raise BgcodeDecodeError(f"unsupported bgcode checksum type: {checksum_type}")

    metadata_chunks: list[str] = []
    gcode_chunks: list[str] = []
    for block in blocks:
        if block.block_type in _METADATA_BLOCK_TYPES:
            metadata_chunks.append(_decode_metadata_block(block))
            continue
        if block.block_type == _GCODE_BLOCK_TYPE:
            gcode_chunks.append(_decode_gcode_block(block))
            continue

    if not gcode_chunks:
        raise BgcodeDecodeError("bgcode contains no gcode blocks")

    gcode_text = "".join(gcode_chunks)
    if not any(marker in gcode_text for marker in (";LAYER_CHANGE", ";LAYER:", ";TYPE:", ";Z:")):
        raise BgcodeDecodeError("bgcode decode did not yield canonical layer/type markers")

    header = "".join(chunk for chunk in metadata_chunks if chunk)
    return header + gcode_text


def _parse_bgcode_blocks(raw_bytes: bytes) -> tuple[list[_BgcodeBlock], int]:
    if len(raw_bytes) < 10:
        raise BgcodeDecodeError("bgcode payload too short")
    if raw_bytes[:4] != _BGCODE_MAGIC:
        raise BgcodeDecodeError("missing GCDE magic")
    version = struct.unpack("<I", raw_bytes[4:8])[0]
    if version != 1:
        raise BgcodeDecodeError(f"unsupported bgcode version: {version}")
    checksum_type = struct.unpack("<H", raw_bytes[8:10])[0]

    offset = 10
    blocks: list[_BgcodeBlock] = []
    while offset < len(raw_bytes):
        if offset + 8 > len(raw_bytes):
            raise BgcodeDecodeError("truncated bgcode block header")
        block_type, compression = struct.unpack("<HH", raw_bytes[offset : offset + 4])
        uncompressed_size = struct.unpack("<I", raw_bytes[offset + 4 : offset + 8])[0]
        if compression == 0:
            compressed_size = uncompressed_size
            header_len = 8
        else:
            if offset + 12 > len(raw_bytes):
                raise BgcodeDecodeError("truncated compressed bgcode block header")
            compressed_size = struct.unpack("<I", raw_bytes[offset + 8 : offset + 12])[0]
            header_len = 12
        params_len = _BLOCK_PARAM_LENGTHS.get(block_type)
        if params_len is None:
            raise BgcodeDecodeError(f"unsupported bgcode block type: {block_type}")
        params_start = offset + header_len
        data_start = params_start + params_len
        data_end = data_start + compressed_size
        checksum_len = 4 if checksum_type else 0
        if data_end + checksum_len > len(raw_bytes):
            raise BgcodeDecodeError("truncated bgcode block payload")
        params = raw_bytes[params_start:data_start]
        compressed_payload = raw_bytes[data_start:data_end]
        payload = _decompress_block(compression, compressed_payload, expected_size=uncompressed_size)
        blocks.append(_BgcodeBlock(block_type=block_type, compression=compression, payload=payload, params=params))
        offset = data_end + checksum_len
    return blocks, checksum_type


def _decompress_block(compression: int, payload: bytes, *, expected_size: int) -> bytes:
    if compression == 0:
        return payload
    if compression == 1:
        try:
            return zlib.decompress(payload)
        except zlib.error as exc:  # pragma: no cover - defensive decode path
            raise BgcodeDecodeError(f"deflate block decode failed: {exc}") from exc
    if compression in _HEATSHRINK_WINDOW_LOOKAHEAD:
        try:
            import heatshrinkpy
        except Exception as exc:  # pragma: no cover - import failure path
            raise BgcodeDecodeError("heatshrinkpy is required to decode Prusa bgcode blocks") from exc
        window_sz2, lookahead_sz2 = _HEATSHRINK_WINDOW_LOOKAHEAD[compression]
        try:
            return heatshrinkpy.decompress(payload, window_sz2=window_sz2, lookahead_sz2=lookahead_sz2)
        except Exception as exc:  # pragma: no cover - defensive decode path
            raise BgcodeDecodeError(f"heatshrink block decode failed: {exc}") from exc
    raise BgcodeDecodeError(f"unsupported bgcode compression type: {compression}")


def _decode_metadata_block(block: _BgcodeBlock) -> str:
    if len(block.params) != 2:
        raise BgcodeDecodeError("metadata block parameters malformed")
    encoding = struct.unpack("<H", block.params)[0]
    if encoding != 0:
        raise BgcodeDecodeError(f"unsupported metadata encoding: {encoding}")
    text = block.payload.decode("utf-8", errors="replace")
    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lines.append(line if line.startswith(";") else f"; {line}")
    return "".join(f"{line}\n" for line in lines)


def _decode_gcode_block(block: _BgcodeBlock) -> str:
    if len(block.params) != 2:
        raise BgcodeDecodeError("gcode block parameters malformed")
    encoding = struct.unpack("<H", block.params)[0]
    if encoding == 0:
        return _decode_plain_text(block.payload)
    if encoding in {1, 2}:
        return _decode_plain_text(_decode_meatpack(block.payload))
    raise BgcodeDecodeError(f"unsupported gcode encoding: {encoding}")


def _decode_plain_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1", errors="replace")


def _decode_meatpack(payload: bytes) -> bytes:
    command_enable_packing = 251
    command_disable_packing = 250
    command_reset_all = 249
    command_enable_no_spaces = 247
    command_disable_no_spaces = 246
    signal_byte = 0xFF
    first_not_packed = 0x0F
    second_not_packed = 0xF0

    unbinarizing = False
    no_spaces_enabled = False
    command_active = False
    command_count = 0
    full_char_queue = 0
    char_buffer: int | None = None
    pending_output: list[int] = []
    output = bytearray()
    add_space = False
    gline_parameters = {ord(ch) for ch in "XYZEFIJRSPWHCA"}

    def handle_command(value: int) -> None:
        nonlocal unbinarizing, no_spaces_enabled
        if value == command_enable_packing:
            unbinarizing = True
        elif value == command_disable_packing:
            unbinarizing = False
        elif value == command_enable_no_spaces:
            no_spaces_enabled = True
        elif value == command_disable_no_spaces:
            no_spaces_enabled = False
        elif value == command_reset_all:
            unbinarizing = False
            no_spaces_enabled = False

    def get_char(value: int) -> int:
        if value == 0:
            return ord("0")
        if value == 1:
            return ord("1")
        if value == 2:
            return ord("2")
        if value == 3:
            return ord("3")
        if value == 4:
            return ord("4")
        if value == 5:
            return ord("5")
        if value == 6:
            return ord("6")
        if value == 7:
            return ord("7")
        if value == 8:
            return ord("8")
        if value == 9:
            return ord("9")
        if value == 10:
            return ord(".")
        if value == 11:
            return ord("E" if no_spaces_enabled else " ")
        if value == 12:
            return ord("\n")
        if value == 13:
            return ord("G")
        if value == 14:
            return ord("X")
        return 0

    def unpack_chars(value: int) -> tuple[bool, bool, int, int]:
        next_first = (value & first_not_packed) == first_not_packed
        next_second = (value & second_not_packed) == second_not_packed
        first = 0 if next_first else get_char(value & 0x0F)
        second = 0 if next_second else get_char((value >> 4) & 0x0F)
        return next_first, next_second, first, second

    def handle_output_char(value: int) -> None:
        pending_output.append(value)

    def handle_rx_char(value: int) -> None:
        nonlocal full_char_queue, char_buffer
        if unbinarizing:
            if full_char_queue > 0:
                handle_output_char(value)
                if char_buffer is not None:
                    handle_output_char(char_buffer)
                    char_buffer = None
                full_char_queue -= 1
                return
            next_first, next_second, first, second = unpack_chars(value)
            if next_first:
                full_char_queue += 1
                if next_second:
                    full_char_queue += 1
                else:
                    char_buffer = second
                return
            handle_output_char(first)
            if first != ord("\n"):
                if next_second:
                    full_char_queue += 1
                else:
                    handle_output_char(second)
            return
        handle_output_char(value)

    def flush_pending_output() -> None:
        nonlocal add_space
        if not pending_output:
            return
        for value in pending_output:
            current_length = len(output)
            new_line = False
            if value == ord("G") and (current_length == 0 or output[-1] == ord("\n")):
                add_space = True
                new_line = True
            elif value == ord("\n"):
                add_space = False
            if (
                not new_line
                and add_space
                and (current_length == 0 or output[-1] != ord(" "))
                and value in gline_parameters
            ):
                output.append(ord(" "))
            if value != ord("\n") or current_length == 0 or output[-1] != ord("\n"):
                output.append(value)
        pending_output.clear()

    for value in payload:
        if value == signal_byte:
            if command_count > 0:
                command_active = True
                command_count = 0
            else:
                command_count += 1
        else:
            if command_active:
                handle_command(value)
                command_active = False
            else:
                if command_count > 0:
                    handle_rx_char(signal_byte)
                    command_count = 0
                handle_rx_char(value)
        flush_pending_output()

    if command_count > 0:
        handle_rx_char(signal_byte)
    flush_pending_output()
    return bytes(output)
