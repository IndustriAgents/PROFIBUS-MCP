"""Parser for PROFIBUS DP GSD files.

A GSD (General Station Description) file is an ASCII keyword file, not XML:
it starts with ``#Profibus_DP`` and holds ``Keyword = value`` lines, ``;``
comments, ``\\`` line continuations and blocks such as ``Module ... EndModule``.
Keywords are case-insensitive. Values are decimal or ``0x`` hex numbers,
comma-separated number lists, or double-quoted strings.

GSDML files (XML) describe PROFINET IO devices and are rejected with a clear
error; this server is PROFIBUS DP only.

I/O lengths come from each module's DP configuration identifiers (the bytes
sent in Chk_Cfg), decoded in :func:`decode_config_identifiers`. A module whose
identifiers cannot be decoded is reported with an ``error`` and a warning; it
does not make the whole file unreadable.

Indexed keywords such as ``Ext_User_Prm_Data_Ref(3)`` may legitimately repeat
(bit-field parameters share a byte), so they are kept as lists in
``indexed_keywords``.

When ``PROFIBUS_GSD_PATH`` is set, :class:`GSDParser` only opens files inside
it (after resolving symlinks). The resolved path is then opened one component
at a time with O_NOFOLLOW (O_DIRECTORY for directories), so no symlink is
followed and nothing outside it, a device node for example, is ever opened,
even if a component is swapped between the check and the open. The opened
file must still be the one the path resolves to (same device and inode).
"""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NOCTTY = getattr(os, "O_NOCTTY", 0)
#: The file itself: no symlink, no blocking on a FIFO, never a controlling tty.
_OPEN_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | _NOCTTY | getattr(os, "O_BINARY", 0)
#: Each directory on the way: no symlink, and it must be a directory.
_DIR_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_DIRECTORY", 0) | _NOCTTY
#: Opening relative to a directory descriptor needs dir_fd support (not on Windows).
_CAN_WALK = os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY") and _NOFOLLOW != 0
#: errno values for "a symlink (or non-directory) where none is allowed".
_SWAPPED = {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}

#: Refuse files above this size; real GSD files are far smaller.
MAX_GSD_BYTES = 4 * 1024 * 1024

#: Block keywords (lower case) and the keyword that closes each.
_BLOCKS = {
    "module": "endmodule",
    "prmtext": "endprmtext",
    "extuserprmdata": "endextuserprmdata",
    "slotdefinition": "endslotdefinition",
    "unitdiagtype": "endunitdiagtype",
    "unit_diag_area": "unit_diag_area_end",
    "x_unit_diag_area": "x_unit_diag_area_end",
    "data_area_beg": "data_area_end",
}
_BLOCK_ENDS = {end: start for start, end in _BLOCKS.items()}

#: ``*_supp`` keywords for the DP transmission rates, in bit/s.
_BAUD_KEYWORDS = {
    "9.6_supp": 9600,
    "19.2_supp": 19200,
    "31.25_supp": 31250,
    "45.45_supp": 45450,
    "93.75_supp": 93750,
    "187.5_supp": 187500,
    "500_supp": 500000,
    "1.5m_supp": 1500000,
    "3m_supp": 3000000,
    "6m_supp": 6000000,
    "12m_supp": 12000000,
}

_STATION_TYPES = {0: "DP slave", 1: "DP master (class 1)"}

_KEY_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)\s*(?:\(\s*(?P<index>\d+)\s*\))?$")
_TOKEN_RE = re.compile(r'"(?P<str>[^"]*)"|(?P<hex>0[xX][0-9A-Fa-f]+)\b|(?P<int>\d+)\b|(?P<comma>,)|(?P<other>\S)')


class GSDParserError(RuntimeError):
    """Raised when a file cannot be read or is not a valid GSD file."""


@dataclass(frozen=True, slots=True)
class _Text:
    """A quoted string followed by numbers, e.g. a Module header."""

    text: str
    values: tuple[int, ...]


def decode_config_identifiers(cfg: Sequence[int]) -> tuple[int, int]:
    """Return ``(input_bytes, output_bytes)`` for DP configuration identifiers.

    General format (bits 4-5 not 00): bits 0-3 = length - 1, bits 4-5 =
    01 input / 10 output / 11 input and output, bit 6 = word units, bit 7 =
    consistency. Special format (bits 4-5 = 00): bits 6-7 say which length
    bytes follow (01 input, 10 output, 11 output then input), bits 0-3 count
    the manufacturer-specific bytes after them. ``0x00`` is an empty slot.
    """
    inputs = outputs = 0
    index = 0
    data = list(cfg)

    def take() -> int:
        nonlocal index
        if index >= len(data):
            raise GSDParserError("configuration identifier is truncated")
        value = data[index]
        index += 1
        if not 0 <= value <= 0xFF:
            raise GSDParserError(f"configuration byte {value!r} is out of range")
        return value

    def length_byte() -> int:
        value = take()
        return ((value & 0x3F) + 1) * (2 if value & 0x40 else 1)

    while index < len(data):
        head = take()
        direction = (head >> 4) & 0x03
        if direction:
            size = ((head & 0x0F) + 1) * (2 if head & 0x40 else 1)
            if direction & 0x01:
                inputs += size
            if direction & 0x02:
                outputs += size
            continue
        vendor_bytes = head & 0x0F
        if vendor_bytes == 15:
            raise GSDParserError(f"special identifier 0x{head:02X}: the manufacturer byte count must be 0-14")
        kind = (head >> 6) & 0x03
        if kind in (2, 3):
            outputs += length_byte()
        if kind in (1, 3):
            inputs += length_byte()
        for _ in range(vendor_bytes):
            take()
    return inputs, outputs


def parse_gsd_text(text: str, source: str = "<string>") -> dict[str, Any]:
    """Parse the text of a GSD file. Raises :class:`GSDParserError`."""
    if text.lstrip().startswith("<"):
        raise GSDParserError(
            f"{source} is XML, not a PROFIBUS GSD file. GSDML (XML) describes PROFINET IO devices; "
            "PROFIBUS DP GSD files are ASCII keyword files starting with #Profibus_DP."
        )
    if "\x00" in text:
        raise GSDParserError(f"{source} is not a text file")

    keywords: dict[str, tuple[str, Any]] = {}  # lower-case key -> (spelling, value)
    indexed: dict[str, tuple[str, list[dict[str, Any]]]] = {}  # lower-case name -> (spelling, entries)
    modules: list[dict[str, Any]] = []
    warnings: list[str] = []
    stack: list[tuple[str, int]] = []  # (block keyword, line number)
    current_module: dict[str, Any] | None = None
    seen_header = False

    for line_no, line in _logical_lines(text):
        if not seen_header:
            if line.lower() == "#profibus_dp":
                seen_header = True
                continue
            raise GSDParserError(
                f"{source} is not a PROFIBUS DP GSD file: '#Profibus_DP' must come first (line {line_no})"
            )
        if line.startswith("#"):
            warnings.append(f"line {line_no}: directive ignored")
            continue

        key, sep, raw_value = line.partition("=")
        key = key.strip()
        lowered = key.lower()

        if not sep:
            if lowered in _BLOCK_ENDS:
                if not stack or stack[-1][0] != _BLOCK_ENDS[lowered]:
                    raise GSDParserError(f"{source} line {line_no}: {key} without a matching opening keyword")
                if stack.pop()[0] == "module":
                    current_module = None
            elif lowered in _BLOCKS:
                stack.append((lowered, line_no))
            elif current_module is not None and stack[-1][0] == "module" and line.isdigit():
                current_module["reference"] = int(line)
            elif not stack:
                warnings.append(f"line {line_no}: line without '=' ignored")
            continue

        match = _KEY_RE.match(key)
        name = match.group("name") if match else key
        lowered_name = name.lower()
        value = _parse_value(raw_value)

        if match and lowered_name in _BLOCKS:
            if lowered_name == "module":
                if stack:
                    raise GSDParserError(f"{source} line {line_no}: Module inside another block")
                current_module = _new_module(value, line_no, warnings)
                modules.append(current_module)
            stack.append((lowered_name, line_no))
            continue
        if stack:
            if current_module is not None and stack[-1][0] == "module" and lowered_name == "info_text":
                current_module["info_text"] = value.strip() if isinstance(value, str) else None
            continue  # keywords inside blocks do not belong to the device
        if not match:
            warnings.append(f"line {line_no}: malformed keyword ignored")
            continue

        if match.group("index") is not None:
            entries = indexed.setdefault(lowered_name, (name, []))[1]
            entries.append({"index": int(match.group("index")), "value": _jsonable(value)})
            continue
        if lowered_name in keywords:
            warnings.append(f"line {line_no}: {name} repeated; the last value wins")
        keywords[lowered_name] = (name, value)

    if not seen_header:
        raise GSDParserError(f"{source} is empty or not a PROFIBUS DP GSD file (no #Profibus_DP)")
    if stack:
        block, line_no = stack[-1]
        raise GSDParserError(f"{source}: the {block} block opened on line {line_no} is never closed")

    return _summarise(keywords, indexed, modules, warnings)


class GSDParser:
    """Loads GSD files from disk, caching each result until the file changes."""

    def __init__(self, base_path: Path | str | None = None) -> None:
        self.base_path = Path(base_path).expanduser() if base_path else None
        # resolved path -> (st_dev, st_ino, st_mtime_ns, st_size, result)
        self._cache: dict[Path, tuple[int, int, int, int, dict[str, Any]]] = {}

    def resolve(self, filepath: str | Path) -> Path:
        """Locate ``filepath``.

        Without ``PROFIBUS_GSD_PATH`` any readable path is accepted. With it, a
        relative path is taken relative to that directory, and the resolved
        file (symlinks followed) must lie inside it. Anything else, including
        a path that cannot be resolved at all (a symlink loop, a NUL byte), is
        refused with the same error before checking whether it exists, so the
        error says nothing about files elsewhere.
        """
        shown = repr(str(filepath))
        if self.base_path is None:
            try:
                path = Path(filepath).expanduser()
                if path.is_file():
                    return path.resolve()
            except (OSError, RuntimeError, ValueError):
                pass
            raise GSDParserError(f"GSD file not found: {shown}")
        try:
            base = self.base_path.resolve()
        except (OSError, RuntimeError, ValueError):
            raise GSDParserError("PROFIBUS_GSD_PATH cannot be resolved") from None
        outside = GSDParserError(f"{shown} is outside PROFIBUS_GSD_PATH ({base}); only files there can be loaded")
        try:
            path = Path(filepath).expanduser()
            resolved = (path if path.is_absolute() else base / path).resolve()
            inside = resolved.is_relative_to(base)
        except (OSError, RuntimeError, ValueError):
            raise outside from None
        if not inside:
            raise outside
        try:
            found = resolved.is_file()
        except (OSError, ValueError):
            found = False
        if not found:
            raise GSDParserError(f"GSD file not found in PROFIBUS_GSD_PATH: {shown}")
        return resolved

    def load(self, filepath: str | Path) -> dict[str, Any]:
        return self.load_with_status(filepath)[0]

    def load_with_status(self, filepath: str | Path) -> tuple[dict[str, Any], bool]:
        """Return ``(parsed, from_cache)``. The cache is keyed on device, inode, mtime and size."""
        path = self.resolve(filepath)
        try:
            fd = _open_without_symlinks(path)
        except OSError as exc:
            if exc.errno in _SWAPPED:  # part of the resolved path became a symlink
                raise GSDParserError(f"{str(filepath)!r} changed while it was being opened; refused") from None
            raise GSDParserError(f"Cannot read GSD file {path}: {exc.strerror}") from None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise GSDParserError(f"{path} is not a regular file")
            self._check_still_the_same(filepath, path, info)
            cached = self._cache.get(path)
            stamp = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
            if cached and cached[:4] == stamp:
                return cached[4], True
            if info.st_size > MAX_GSD_BYTES:
                raise GSDParserError(
                    f"{path} is {info.st_size} bytes; GSD files above {MAX_GSD_BYTES} bytes are refused"
                )
            raw = _read_fd(fd, MAX_GSD_BYTES + 1)
        finally:
            os.close(fd)
        if len(raw) > MAX_GSD_BYTES:
            raise GSDParserError(f"{path} grew above {MAX_GSD_BYTES} bytes while being read; refused")
        if raw.startswith(b"\xef\xbb\xbf"):  # UTF-8 byte order mark
            raw = raw[3:]
        # GSD files are ASCII in principle; vendors use UTF-8 or Latin-1 for
        # texts and comments. Latin-1 decodes any byte, so it is the fallback.
        try:
            text, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = raw.decode("latin-1"), "latin-1"
        parsed = parse_gsd_text(text, source=str(path))
        result = {"filepath": str(path), "encoding": encoding, **parsed}
        self._cache[path] = (*stamp, result)
        return result, False

    def _check_still_the_same(self, filepath: str | Path, path: Path, info: os.stat_result) -> None:
        """Refuse if ``filepath`` no longer resolves (inside the base) to the file that was opened."""
        changed = GSDParserError(f"{str(filepath)!r} changed while it was being opened; refused")
        again = self.resolve(filepath)  # re-checks containment
        try:
            now = os.stat(again)
        except OSError:
            raise changed from None
        if again != path or (now.st_dev, now.st_ino) != (info.st_dev, info.st_ino):
            raise changed


def _open_without_symlinks(path: Path) -> int:
    """Open an absolute, already resolved path without following any symlink.

    Each directory is opened relative to the previous one with O_NOFOLLOW and
    O_DIRECTORY, then the file with O_NOFOLLOW. A component replaced by a
    symlink fails with ELOOP/ENOTDIR instead of leading somewhere else. Where
    dir_fd is unsupported (Windows), the whole path is opened at once and the
    device/inode re-check in the caller is the guard.
    """
    if not _CAN_WALK:
        return os.open(path, _OPEN_FLAGS)
    parts = path.parts
    fd = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)  # the root itself
    try:
        for part in parts[1:-1]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return os.open(parts[-1], _OPEN_FLAGS, dir_fd=fd)
    finally:
        os.close(fd)


def _read_fd(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_comment(line: str, in_string: bool = False) -> tuple[str, bool]:
    """Cut a ``;`` comment outside strings. Returns the text and whether a string is still open."""
    for index, char in enumerate(line):
        if char == '"':
            in_string = not in_string
        elif char == ";" and not in_string:
            return line[:index], False
    return line, in_string


def _logical_lines(text: str):
    """Yield ``(first_line_number, line)`` with comments removed and continuations joined.

    A string may continue onto the next line, so whether we are inside a
    string is carried across a ``\\`` continuation: a ``;`` in the continued
    part of a string is text, not a comment.
    """
    buffer = ""
    start: int | None = None
    in_string = False
    for line_no, physical in enumerate(text.splitlines(), 1):
        if start is None:
            start = line_no
            segment = physical
        else:
            segment = physical.lstrip()
        line, in_string = _strip_comment(segment, in_string)
        line = line.rstrip()
        if line.endswith("\\"):
            body = line[:-1]
            buffer += body if in_string else body.rstrip() + " "
            continue
        buffer += line
        if buffer.strip():
            yield start, buffer.strip()
        buffer, start, in_string = "", None, False
    if buffer.strip() and start is not None:
        yield start, buffer.strip()


def _parse_value(raw: str) -> Any:
    """Turn a value into str, int, list[int] or _Text; anything else stays a string."""
    raw = raw.strip()
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(raw):
        kind = match.lastgroup or "other"
        if kind == "other":
            return raw
        tokens.append((kind, match.group(kind)))

    numbers: list[int] = []
    text: str | None = None
    expect_number = True
    for position, (kind, token) in enumerate(tokens):
        if kind == "str":
            if position != 0:
                return raw
            text = token
            continue
        if kind == "comma":
            if expect_number:
                return raw
            expect_number = True
            continue
        if not expect_number:
            if text is None or numbers:
                return raw  # numbers must be separated by commas
        numbers.append(int(token, 16) if kind == "hex" else int(token))
        expect_number = False
    if numbers and expect_number:
        return raw  # trailing comma

    if text is not None:
        return _Text(text, tuple(numbers)) if numbers else text
    if len(numbers) == 1:
        return numbers[0]
    if numbers:
        return numbers
    return raw


def _new_module(value: Any, line_no: int, warnings: list[str]) -> dict[str, Any]:
    """Build a module entry; a header that cannot be decoded becomes an ``error`` and a warning."""
    if isinstance(value, _Text):
        name, config = value.text, list(value.values)
    elif isinstance(value, str):
        name, config = value, []
    else:
        name, config = str(value), []
    name = name.strip()  # vendors pad names to a fixed width
    module: dict[str, Any] = {
        "name": name,
        "config_hex": None,
        "input_length": None,
        "output_length": None,
        "reference": None,
        "info_text": None,
    }
    if not isinstance(value, _Text | str):
        error = "needs a quoted name followed by configuration bytes"
    elif not config:
        error = "has no configuration identifier"
    else:
        try:
            module["config_hex"] = bytes(config).hex()
        except ValueError:
            pass
        try:
            module["input_length"], module["output_length"] = decode_config_identifiers(config)
            return module
        except GSDParserError as exc:
            error = str(exc)
    module["error"] = error
    warnings.append(f"line {line_no}: Module {name!r} {error}; its I/O lengths are unknown")
    return module


def _summarise(
    keywords: dict[str, tuple[str, Any]],
    indexed: dict[str, tuple[str, list[dict[str, Any]]]],
    modules: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    def get(name: str) -> Any:
        entry = keywords.get(name.lower())
        return entry[1] if entry else None

    def get_int(name: str) -> int | None:
        value = get(name)
        return value if isinstance(value, int) else None

    def get_str(name: str) -> str | None:
        value = get(name)
        return value if isinstance(value, str) else None

    ident = get_int("Ident_Number")
    if ident is None:
        warnings.append("Ident_Number is missing or not a number")
    elif not 0 <= ident <= 0xFFFF:
        warnings.append("Ident_Number is out of range")
        ident = None
    station_type = get_int("Station_Type")
    vendor = get_str("Vendor_Name")
    model = get_str("Model_Name")

    modular = get_int("Modular_Station") == 1
    if modular:
        input_length = output_length = None
    elif len(modules) == 1:
        input_length, output_length = modules[0]["input_length"], modules[0]["output_length"]
    else:
        input_length = output_length = None
        warnings.append(
            f"compact station with {len(modules)} Module entries; the configured module decides the I/O lengths"
        )

    return {
        "format": "gsd",
        "metadata": {
            "manufacturer": vendor,
            "type": model,
            "ident_number": f"0x{ident:04X}" if ident is not None else None,
            "vendor_name": vendor,
            "model_name": model,
            "revision": get_str("Revision"),
            "gsd_revision": get_int("GSD_Revision"),
            "hardware_release": get_str("Hardware_Release"),
            "software_release": get_str("Software_Release"),
            "protocol_ident": get_int("Protocol_Ident"),
            "station_type": station_type,
            "station_type_name": _STATION_TYPES.get(station_type) if station_type is not None else None,
        },
        "baud_rates": [rate for key, rate in _BAUD_KEYWORDS.items() if get(key) == 1],
        "io_config": {
            "modular_station": modular,
            "max_module": get_int("Max_Module"),
            "max_input_len": get_int("Max_Input_Len"),
            "max_output_len": get_int("Max_Output_Len"),
            "max_data_len": get_int("Max_Data_Len"),
            "input_length": input_length,
            "output_length": output_length,
        },
        "modules": modules,
        "keywords": {spelled: _jsonable(value) for spelled, value in keywords.values()},
        "indexed_keywords": {spelled: entries for spelled, entries in indexed.values()},
        "warnings": warnings,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, _Text):
        return {"text": value.text, "values": list(value.values)}
    return value
