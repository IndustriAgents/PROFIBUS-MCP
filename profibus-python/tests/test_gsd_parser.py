"""GSD files are ASCII keyword files; these tests use realistic samples."""

from __future__ import annotations

import os
import shutil

import pytest

from profibus_mcp.gsd_parser import (
    MAX_GSD_BYTES,
    GSDParser,
    GSDParserError,
    decode_config_identifiers,
    parse_gsd_text,
)

from .conftest import DATA_DIR, MOCK_GSD_DIR

MINIMAL = '#Profibus_DP\nVendor_Name = "V"\nModel_Name = "M"\nIdent_Number = 0x00AB\nModule = "m" 0x10\nEndModule\n'


@pytest.mark.parametrize(
    ("cfg", "expected"),
    [
        ([0x10], (1, 0)),  # 1 byte input
        ([0x13], (4, 0)),  # 4 byte input
        ([0x20], (0, 1)),  # 1 byte output
        ([0x21], (0, 2)),
        ([0x31], (2, 2)),  # input and output, 2 bytes each
        ([0x51], (4, 0)),  # input, 2 words
        ([0xD0], (2, 0)),  # consistent, 1 word input
        ([0xF3, 0xF1], (12, 12)),  # PPO type 1: 4 PKW + 2 PZD words each way
        ([0x13, 0x21], (4, 2)),
        ([0x00], (0, 0)),  # empty slot
        ([0x42, 0x07, 0x01, 0x02], (8, 0)),  # special format: input length byte + 2 manufacturer bytes
        ([0x80, 0x03], (0, 4)),  # special format: output length byte
        ([0xC0, 0x41, 0x0F], (16, 4)),  # special format: output (2 words) then input (16 bytes)
    ],
)
def test_decode_config_identifiers(cfg, expected):
    assert decode_config_identifiers(cfg) == expected


@pytest.mark.parametrize(
    "cfg",
    [
        [0x42],
        [0x42, 0x07, 0x01],
        [0xC0, 0x01],
        [0x300],
        [0x0F] + [0] * 15,  # special format: 15 manufacturer bytes is reserved (0-14 allowed)
    ],
)
def test_decode_config_identifiers_rejects_truncated_or_bad(cfg):
    with pytest.raises(GSDParserError):
        decode_config_identifiers(cfg)


def test_realistic_modular_gsd():
    result = GSDParser().load(DATA_DIR / "IA_1234.gsd")
    meta = result["metadata"]
    assert meta["vendor_name"] == meta["manufacturer"] == "IndustriAgents Test"
    assert meta["model_name"] == meta["type"] == "DEMO-IO 16"
    assert meta["ident_number"] == "0x1234"
    assert meta["gsd_revision"] == 5
    assert meta["revision"] == "V1.2"
    assert meta["station_type"] == 0 and meta["station_type_name"] == "DP slave"
    assert meta["protocol_ident"] == 0
    assert result["baud_rates"] == [9600, 19200, 45450, 93750, 187500, 500000, 1500000, 3000000, 6000000, 12000000]

    io = result["io_config"]
    assert io["modular_station"] is True
    assert (io["max_module"], io["max_input_len"], io["max_output_len"], io["max_data_len"]) == (4, 32, 32, 64)
    # A modular station's lengths depend on the modules actually plugged.
    assert io["input_length"] is None and io["output_length"] is None

    modules = {m["name"]: m for m in result["modules"]}
    assert list(modules) == [
        "8 DI  DC24V",
        "8 DO  DC24V/0.5A",
        "2 AI  4..20mA",
        "Drive PPO1 (4 PKW + 2 PZD)",
        "Status (special format)",
        "Empty slot",
    ]
    assert (modules["8 DI  DC24V"]["input_length"], modules["8 DI  DC24V"]["output_length"]) == (1, 0)
    assert modules["8 DI  DC24V"]["info_text"] == "8 digital inputs; 1 byte"  # ';' inside quotes is not a comment
    assert modules["8 DI  DC24V"]["reference"] == 1
    assert (modules["2 AI  4..20mA"]["input_length"], modules["2 AI  4..20mA"]["output_length"]) == (4, 0)
    ppo = modules["Drive PPO1 (4 PKW + 2 PZD)"]  # config bytes continue on the next line
    assert ppo["config_hex"] == "f3f1" and (ppo["input_length"], ppo["output_length"]) == (12, 12)
    assert modules["Status (special format)"]["input_length"] == 8
    assert modules["Empty slot"]["config_hex"] == "00"

    keywords = result["keywords"]
    assert keywords["freeze_mode_supp"] == 1  # original spelling kept, matched case-insensitively
    assert keywords["Slave_Family"] == "3@IndustriAgents@Demo"
    indexed = result["indexed_keywords"]
    assert indexed["Unit_Diag_Bit"] == [
        {"index": 16, "value": "Supply voltage missing"},
        {"index": 17, "value": "Module fault; see channel diagnosis"},
    ]
    assert indexed["Ext_User_Prm_Data_Const"] == [{"index": 0, "value": [0, 0, 0]}]  # top level, not the module's
    assert indexed["Ext_User_Prm_Data_Ref"] == [{"index": 2, "value": 1}]
    # Keywords inside PrmText / ExtUserPrmData / Module blocks stay out of the device keywords.
    assert "Prm_Text_Ref" not in keywords and "Ext_Module_Prm_Data_Len" not in keywords
    assert "Text" not in indexed
    assert result["warnings"] == []


@pytest.mark.parametrize(
    ("name", "ident", "inputs", "outputs"),
    [("IA_809C.gsd", "0x809C", 4, 2), ("IA_80A5.gsd", "0x80A5", 8, 4)],
)
def test_mock_slave_gsd_files(name, ident, inputs, outputs):
    result = GSDParser().load(MOCK_GSD_DIR / name)
    assert result["metadata"]["ident_number"] == ident
    assert result["io_config"]["modular_station"] is False
    assert (result["io_config"]["input_length"], result["io_config"]["output_length"]) == (inputs, outputs)
    assert result["warnings"] == []


def test_xml_is_rejected_with_a_clear_message(tmp_path):
    path = tmp_path / "device.xml"
    path.write_text('<?xml version="1.0"?><ISO15745Profile/>', encoding="utf-8")
    with pytest.raises(GSDParserError, match="GSDML"):
        GSDParser().load(path)


def test_the_old_placeholder_xml_format_is_rejected(tmp_path):
    path = tmp_path / "old.gsd"
    path.write_text('<Device Manufacturer="X" IdentNumber="0x1"><Input Len="4"/></Device>', encoding="utf-8")
    with pytest.raises(GSDParserError, match="XML"):
        GSDParser().load(path)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "; only a comment\n",
        'Vendor_Name = "no header"\n#Profibus_DP\n',
        "API_KEY=secret\n",  # e.g. a .env file: refused, and its content is not echoed
    ],
)
def test_files_without_profibus_dp_header_are_rejected(text):
    with pytest.raises(GSDParserError) as info:
        parse_gsd_text(text)
    assert "secret" not in str(info.value)


def test_unclosed_and_mismatched_blocks_are_errors():
    with pytest.raises(GSDParserError, match="never closed"):
        parse_gsd_text('#Profibus_DP\nModule = "m" 0x10\n')
    with pytest.raises(GSDParserError, match="EndModule"):
        parse_gsd_text("#Profibus_DP\nEndModule\n")


@pytest.mark.parametrize(
    ("header", "problem"),
    [
        ('Module = "m"', "no configuration identifier"),
        ('Module = "m" 0x42', "truncated"),
        ('Module = "m" 0x0F,0x01', "must be 0-14"),
        ("Module = 0x10", "quoted name"),
    ],
)
def test_a_broken_module_is_a_warning_not_a_failed_file(header, problem):
    result = parse_gsd_text(f'#Profibus_DP\nIdent_Number = 0x1\n{header}\nEndModule\nModule = "ok" 0x10\nEndModule\n')
    broken, good = result["modules"]
    assert problem in broken["error"]
    assert broken["input_length"] is None and broken["output_length"] is None
    assert any(problem in warning for warning in result["warnings"])
    assert (good["input_length"], good["output_length"]) == (1, 0) and "error" not in good


def test_compact_station_lengths_and_warnings():
    result = parse_gsd_text(MINIMAL)
    assert result["metadata"]["ident_number"] == "0x00AB"
    assert (result["io_config"]["input_length"], result["io_config"]["output_length"]) == (1, 0)

    two = parse_gsd_text(MINIMAL + 'Module = "n" 0x20\nEndModule\n')
    assert two["io_config"]["input_length"] is None
    assert any("2 Module entries" in warning for warning in two["warnings"])

    no_ident = parse_gsd_text('#Profibus_DP\nVendor_Name = "V"\n')
    assert no_ident["metadata"]["ident_number"] is None
    assert any("Ident_Number" in warning for warning in no_ident["warnings"])


def test_value_parsing():
    result = parse_gsd_text(
        "#Profibus_DP\n"
        "A = 12\n"
        "B = 0x1f\n"
        "C = 1, 2 ,0x03\n"
        'D = "text ; with semicolon"   ; real comment\n'
        "E = 16-17\n"
        'F = "name" 0x13,0x23\n'
        "G = 1 2\n"
    )
    keywords = result["keywords"]
    assert keywords["A"] == 12
    assert keywords["B"] == 31
    assert keywords["C"] == [1, 2, 3]
    assert keywords["D"] == "text ; with semicolon"
    assert keywords["E"] == "16-17"
    assert keywords["F"] == {"text": "name", "values": [0x13, 0x23]}
    assert keywords["G"] == "1 2"


def test_cache_refreshes_when_the_file_changes(tmp_path):
    path = tmp_path / "dev.gsd"
    path.write_text(MINIMAL, encoding="ascii")
    parser = GSDParser()
    first, cached = parser.load_with_status(path)
    assert not cached
    again, cached = parser.load_with_status(path)
    assert cached and again is first

    path.write_text(MINIMAL.replace("0x00AB", "0x00AC"), encoding="ascii")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    changed, cached = parser.load_with_status(path)
    assert not cached
    assert changed["metadata"]["ident_number"] == "0x00AC"


def test_lookup_by_name_in_gsd_path(tmp_path):
    shutil.copy(DATA_DIR / "IA_1234.gsd", tmp_path / "IA_1234.gsd")
    (tmp_path / "sub").mkdir()
    shutil.copy(DATA_DIR / "IA_1234.gsd", tmp_path / "sub" / "copy.gsd")
    parser = GSDParser(tmp_path)
    assert parser.load("IA_1234.gsd")["metadata"]["ident_number"] == "0x1234"
    assert parser.load("sub/copy.gsd")["metadata"]["ident_number"] == "0x1234"
    assert parser.load(tmp_path / "IA_1234.gsd")["metadata"]["ident_number"] == "0x1234"  # absolute, inside
    with pytest.raises(GSDParserError, match="not found"):
        parser.load("missing.gsd")


def test_gsd_path_confines_load_gsd_file(tmp_path):
    base = tmp_path / "gsd"
    base.mkdir()
    outside = tmp_path / "secret.gsd"
    shutil.copy(DATA_DIR / "IA_1234.gsd", outside)
    (base / "escape.gsd").symlink_to(outside)
    parser = GSDParser(base)
    for attempt in (str(outside), "../secret.gsd", "escape.gsd", "/etc/passwd"):
        with pytest.raises(GSDParserError, match="outside PROFIBUS_GSD_PATH"):
            parser.load(attempt)
    # The refusal reads the same whether or not the file exists: no existence oracle.
    messages = []
    for attempt in ("../secret.gsd", "../does-not-exist.gsd"):
        with pytest.raises(GSDParserError) as info:
            parser.load(attempt)
        messages.append(str(info.value).replace(attempt, "<path>"))
    assert messages[0] == messages[1]


def test_without_gsd_path_any_readable_file_is_accepted(tmp_path):
    shutil.copy(DATA_DIR / "IA_1234.gsd", tmp_path / "any.gsd")
    assert GSDParser().load(tmp_path / "any.gsd")["metadata"]["ident_number"] == "0x1234"


def test_vendor_quirks():
    result = GSDParser().load(DATA_DIR / "vendor_quirks.gsd")
    assert result["encoding"] == "utf-8"
    assert result["metadata"]["vendor_name"] == "Prüfgeräte GmbH"
    assert result["metadata"]["model_name"] == "Messgerät 2000"
    # A ';' in the continued part of a string is text, not a comment.
    assert result["keywords"]["Info_Text"] == "first part of a long text, second part; still text"
    # Repeated indexed keywords are kept, in order, with spaces inside "( n )".
    assert result["indexed_keywords"]["Ext_User_Prm_Data_Ref"] == [
        {"index": 0, "value": 1},
        {"index": 1, "value": 2},
        {"index": 1, "value": 3},
        {"index": 1, "value": 4},
    ]
    # Padded names are trimmed.
    names = [module["name"] for module in result["modules"]]
    assert names == ["PZD( 2W Cons.)", "AI16 (temperature)", "Broken: 15 vendor bytes"]
    assert (result["modules"][0]["input_length"], result["modules"][0]["output_length"]) == (4, 4)
    assert "must be 0-14" in result["modules"][2]["error"]
    # A non-standard keyword at top level is a warning; inside a block it is skipped.
    assert any("malformed keyword ignored" in warning for warning in result["warnings"])
    assert not any("Vendor-Option" in warning for warning in result["warnings"])
    assert not any("repeated" in warning for warning in result["warnings"])


def test_reviewer_nasty_samples():
    # Valid but awkward files written for the review: comments and a stray
    # quote before the header, CRLF, tabs, lower-case keywords, ';' and '='
    # inside strings, continuation with a comment after the backslash,
    # special-format identifiers and a decimal config without spaces.
    compact = GSDParser().load(DATA_DIR / "nasty_compact.gsd")
    assert compact["metadata"]["vendor_name"] == "ACME; Automation = Inc"
    assert compact["metadata"]["ident_number"] == "0xBEEF"
    assert (compact["io_config"]["input_length"], compact["io_config"]["output_length"]) == (4, 4)
    module = compact["modules"][0]
    assert module["config_hex"] == "c24183aabb" and module["info_text"] == "has = and ; inside"
    assert compact["indexed_keywords"]["Ext_User_Prm_Data_Const"] == [{"index": 0, "value": [0, 0, 0, 0, 10]}]
    assert compact["indexed_keywords"]["Ext_User_Prm_Data_Ref"] == [{"index": 3, "value": 1}, {"index": 3, "value": 2}]
    assert compact["warnings"] == []

    modular = GSDParser().load(DATA_DIR / "nasty_modular.gsd")
    lengths = {m["name"]: (m["input_length"], m["output_length"]) for m in modular["modules"]}
    assert lengths == {
        "empty": (0, 0),
        "8 DI general": (1, 0),
        "16 DO, decimal, no spaces": (0, 3),  # 32 = 1 byte out, 33 = 2 bytes out
        "mixed general+special": (132, 2),  # 4 + 4 + 124 in (0x7D: 62 words), 2 out
        "word consistent IO": (16, 16),
        "special, vendor bytes only": (0, 0),
    }
    assert modular["warnings"] == []


def test_latin1_fallback_is_reported(tmp_path):
    path = tmp_path / "latin1.gsd"
    path.write_bytes(b'#Profibus_DP\nVendor_Name = "Ger\xe4te"\n')
    result = GSDParser().load(path)
    assert result["encoding"] == "latin-1" and result["metadata"]["vendor_name"] == "Geräte"


def test_latin1_comments_and_bom_are_accepted(tmp_path):
    path = tmp_path / "latin1.gsd"
    path.write_bytes(b"\xef\xbb\xbf#Profibus_DP\n; Ger\xe4t f\xfcr Tests\n" + MINIMAL.split("\n", 1)[1].encode())
    assert GSDParser().load(path)["metadata"]["vendor_name"] == "V"


def test_oversized_and_binary_files_are_refused(tmp_path):
    big = tmp_path / "big.gsd"
    with big.open("wb") as fh:
        fh.truncate(MAX_GSD_BYTES + 1)
    with pytest.raises(GSDParserError, match="refused"):
        GSDParser().load(big)
    binary = tmp_path / "bin.gsd"
    binary.write_bytes(b"#Profibus_DP\n\x00\x01")
    with pytest.raises(GSDParserError, match="not a text file"):
        GSDParser().load(binary)


@pytest.mark.parametrize(
    "attempt",
    [
        "loop1.gsd",  # symlink loop inside the base (RuntimeError or OSError from resolve)
        "bad\x00name.gsd",  # NUL byte (ValueError)
        "~no_such_user_profibus/x.gsd",  # unknown home directory (RuntimeError from expanduser)
    ],
)
def test_unresolvable_paths_get_the_same_refusal(tmp_path, attempt):
    base = tmp_path / "gsd"
    base.mkdir()
    (base / "loop1.gsd").symlink_to(base / "loop2.gsd")
    (base / "loop2.gsd").symlink_to(base / "loop1.gsd")
    with pytest.raises(GSDParserError, match="outside PROFIBUS_GSD_PATH"):
        GSDParser(base).load(attempt)
    # Without PROFIBUS_GSD_PATH such a path is simply not found, never a raw exception.
    with pytest.raises(GSDParserError, match="not found"):
        GSDParser().load(str(base / attempt) if attempt == "loop1.gsd" else attempt)


class SwappingOpen:
    """Patches os.open: swap a path component before each open, restore it after,
    and record (device, inode) of everything that actually got opened."""

    def __init__(self, monkeypatch):
        self.monkeypatch = monkeypatch
        self.opened: set[tuple[int, int]] = set()

    def install(self, swap, restore):
        real_open = os.open

        def patched(path, flags, *args, **kwargs):
            swap()
            try:
                fd = real_open(path, flags, *args, **kwargs)
            finally:
                restore()
            info = os.fstat(fd)
            self.opened.add((info.st_dev, info.st_ino))
            return fd

        self.monkeypatch.setattr(os, "open", patched)


@pytest.fixture
def swapping_open(monkeypatch):
    return SwappingOpen(monkeypatch)


def _identity(path) -> tuple[int, int]:
    info = os.stat(path)
    return info.st_dev, info.st_ino


def _secret_gsd(path):
    path.write_text(MINIMAL.replace("0x00AB", "0x5EC2"), encoding="ascii")


def test_final_component_swapped_for_a_symlink_is_refused(tmp_path, swapping_open):
    base = tmp_path / "gsd"
    base.mkdir()
    target = base / "dev.gsd"
    target.write_text(MINIMAL, encoding="ascii")
    outside = tmp_path / "secret.gsd"
    _secret_gsd(outside)
    aside = base / "dev.orig"

    def swap():
        target.rename(aside)
        target.symlink_to(outside)

    def restore():
        target.unlink()
        aside.rename(target)

    swapping_open.install(swap, restore)
    with pytest.raises(GSDParserError, match="changed while it was being opened"):
        GSDParser(base).load("dev.gsd")
    # Refusing afterwards is not enough: the outside file must never be opened.
    assert _identity(outside) not in swapping_open.opened


def test_parent_directory_swapped_for_a_symlink_is_refused(tmp_path, swapping_open):
    base = tmp_path / "gsd"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "dev.gsd").write_text(MINIMAL, encoding="ascii")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _secret_gsd(elsewhere / "dev.gsd")
    sub, aside = base / "sub", base / "sub.orig"

    def swap():
        sub.rename(aside)
        sub.symlink_to(elsewhere, target_is_directory=True)

    def restore():
        sub.unlink()
        aside.rename(sub)

    swapping_open.install(swap, restore)
    with pytest.raises(GSDParserError, match="changed while it was being opened"):
        GSDParser(base).load("sub/dev.gsd")
    assert _identity(elsewhere) not in swapping_open.opened
    assert _identity(elsewhere / "dev.gsd") not in swapping_open.opened


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="needs /dev/null")
def test_a_directory_swapped_for_dev_never_opens_a_device(tmp_path, swapping_open):
    base = tmp_path / "gsd"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "null").write_text(MINIMAL, encoding="ascii")  # looks fine when checked
    sub, aside = base / "sub", base / "sub.orig"

    def swap():
        sub.rename(aside)
        sub.symlink_to("/dev", target_is_directory=True)

    def restore():
        sub.unlink()
        aside.rename(sub)

    swapping_open.install(swap, restore)
    with pytest.raises(GSDParserError, match="changed while it was being opened"):
        GSDParser(base).load("sub/null")
    assert _identity("/dev/null") not in swapping_open.opened
    assert _identity("/dev") not in swapping_open.opened


def test_a_fifo_is_not_read(tmp_path):
    base = tmp_path / "gsd"
    base.mkdir()
    os.mkfifo(base / "pipe.gsd")
    with pytest.raises(GSDParserError):
        GSDParser(base).load("pipe.gsd")


def test_without_dir_fd_the_identity_recheck_refuses_a_swapped_parent(tmp_path, swapping_open, monkeypatch):
    # Where os.open has no dir_fd (Windows) the path is opened in one go; the
    # device/inode re-check is then what refuses a parent swapped mid-open.
    from profibus_mcp import gsd_parser

    monkeypatch.setattr(gsd_parser, "_CAN_WALK", False)
    base = tmp_path / "gsd"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "dev.gsd").write_text(MINIMAL, encoding="ascii")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _secret_gsd(elsewhere / "dev.gsd")
    sub, aside = base / "sub", base / "sub.orig"

    def swap():
        sub.rename(aside)
        sub.symlink_to(elsewhere, target_is_directory=True)

    def restore():
        sub.unlink()
        aside.rename(sub)

    swapping_open.install(swap, restore)
    with pytest.raises(GSDParserError, match="changed while it was being opened"):
        GSDParser(base).load("sub/dev.gsd")


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
@pytest.mark.parametrize("where", ["inside", "above"])
def test_search_only_directories_on_the_way_are_traversed(tmp_path, where):
    # Like a 0711 home directory: --x lets you pass through but not list.
    if where == "inside":
        base = tmp_path / "gsd"
        locked = base / "locked"
        locked.mkdir(parents=True)
        (locked / "dev.gsd").write_text(MINIMAL, encoding="ascii")
        request = "locked/dev.gsd"
    else:
        locked = tmp_path / "home"
        base = locked / "gsd"
        base.mkdir(parents=True)
        (base / "dev.gsd").write_text(MINIMAL, encoding="ascii")
        request = "dev.gsd"
    locked.chmod(0o311)
    try:
        assert GSDParser(base).load(request)["metadata"]["ident_number"] == "0x00AB"
    finally:
        locked.chmod(0o755)


def _locked_tree(tmp_path):
    base = tmp_path / "gsd"
    locked = base / "locked"
    locked.mkdir(parents=True)
    (locked / "dev.gsd").write_text(MINIMAL, encoding="ascii")
    locked.chmod(0o311)
    return base, locked


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions and a non-root user")
def test_without_traversal_only_opens_the_fallback_still_loads(tmp_path, monkeypatch):
    # A platform with neither O_PATH nor O_SEARCH needs read access to walk a
    # directory; EACCES then falls back to the whole-path open plus re-check.
    from profibus_mcp import gsd_parser

    monkeypatch.setattr(gsd_parser, "_DIR_ACCESS", os.O_RDONLY)
    monkeypatch.setattr(gsd_parser, "_DIR_FLAGS", os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_NOCTTY)
    base, locked = _locked_tree(tmp_path)
    try:
        assert GSDParser(base).load("locked/dev.gsd")["metadata"]["ident_number"] == "0x00AB"
    finally:
        locked.chmod(0o755)


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0 or not (hasattr(os, "O_PATH") or hasattr(os, "O_SEARCH")),
    reason="needs O_PATH or O_SEARCH, POSIX permissions and a non-root user",
)
def test_search_only_directories_are_walked_not_bypassed(tmp_path, monkeypatch):
    # Where the OS can open a directory for traversal only, the symlink-proof
    # walk is used even through a --x directory; the whole-path fallback is not.
    base, locked = _locked_tree(tmp_path)
    real_open = os.open
    whole_path_opens = []

    def recording(path, flags, *args, **kwargs):
        if "dir_fd" not in kwargs and str(path) != os.sep:
            whole_path_opens.append(path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording)
    try:
        assert GSDParser(base).load("locked/dev.gsd")["metadata"]["ident_number"] == "0x00AB"
    finally:
        locked.chmod(0o755)
    assert whole_path_opens == []
