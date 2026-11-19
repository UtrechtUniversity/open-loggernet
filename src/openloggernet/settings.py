"""Name, decode and encode DevConfig settings using Campbell's own device descriptions.

`PakBusLink.get_settings` returns settings by number only; what each one is called and how its
bytes are laid out is described in the `.dd`/`.ddi` files Device Configuration Utility installs
under `C:\\Campbellsci\\Lib\\DevConfigLib\\en\\v2`. Those files are Campbell's and are not part of
this package: they are read from the local install, or from `$OLN_DEVCONFIG_LIB`, when present.
Without them every setting is shown by number with its raw bytes, and can only be written raw.

A description is chosen by the device type in the Get Settings reply (69 = CR300, 55 = CR6,
78 = CR1000X) and the catalogue whose major version matches the reply's.
"""

import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIBRARY = Path(r"C:\Campbellsci\Lib\DevConfigLib\en\v2")

# Fixed-width component types: struct format.
FIXED = {
    "uint1": ">B",
    "uint2": ">H",
    "uint4": ">L",
    "int1": ">b",
    "int2": ">h",
    "int4": ">l",
    "bool": ">B",
    "enum": ">B",
    "enumi4": ">l",
    "float": ">f",
    "double": ">d",
    "serial-no": ">L",
}


@dataclass
class Component:
    kind: str
    name: str
    items: dict = field(default_factory=dict)  # enum value -> name
    as_string: bool = False
    bits: int = 0


@dataclass
class SettingDef:
    id: int
    name: str
    components: list
    repeat: int = 0
    read_only: bool = False
    tab: str = ""


def library_path() -> Path | None:
    path = Path(os.environ.get("OLN_DEVCONFIG_LIB", DEFAULT_LIBRARY))
    return path if path.is_dir() else None


def _parse_xml(path: Path) -> ET.Element:
    # The files carry a DOCTYPE with an external DTD, which ElementTree ignores.
    return ET.fromstring(path.read_bytes())


def _setting_defs(element: ET.Element, base: Path, out: dict) -> None:
    for child in element:
        if child.tag == "include":
            _setting_defs(_parse_xml(base / child.attrib["href"]), base, out)
        elif child.tag == "setting":
            comps = []
            for c in child:
                if c.tag in ("description", "description-url", "validate", "enable-expr"):
                    continue
                items = {int(i.attrib["value"]): i.attrib["name"] for i in c.iter("item") if "value" in i.attrib}
                comps.append(
                    Component(
                        c.tag,
                        c.attrib.get("name", ""),
                        items,
                        c.attrib.get("as-string") == "true",
                        int(c.attrib.get("size", 0) or 0),
                    )
                )
            out[int(child.attrib["id"])] = SettingDef(
                int(child.attrib["id"]),
                child.attrib.get("name", "").strip(),
                comps,
                int(child.attrib.get("repeat-count", 0) or 0),
                child.attrib.get("read-only") == "true",
                child.attrib.get("tab-name", ""),
            )


def load_definitions(device_type: int, major: int, library: Path | None = None) -> dict[int, SettingDef]:
    """{setting id: SettingDef} for this device type and catalogue version; {} without a library."""
    library = library or library_path()
    if library is None:
        return {}
    for dd in sorted(library.glob("*.dd")):
        head = dd.read_bytes()[:600]
        if not re.search(rb'device-type="%d"' % device_type, head):
            continue
        root = _parse_xml(dd)
        catalogs = root.findall("setting-catalog")
        chosen = next((c for c in catalogs if int(c.attrib.get("major-version", -1)) == major), None)
        defs: dict = {}
        _setting_defs(chosen if chosen is not None else root, library, defs)
        return defs
    return {}


def _decode_component(comp: Component, raw: bytes, at: int) -> tuple[object, int]:
    if comp.kind in FIXED:
        fmt = FIXED[comp.kind]
        (value,) = struct.unpack_from(fmt, raw, at)
        at += struct.calcsize(fmt)
        if comp.kind == "bool":
            return bool(value), at
        if comp.items:
            return comp.items.get(value, value), at
        return value, at
    if comp.kind in ("string", "choice", "ipaddrany") or (comp.kind == "ipaddr" and comp.as_string):
        end = raw.find(b"\0", at)
        end = len(raw) if end < 0 else end
        return raw[at:end].decode("latin-1"), end + 1
    if comp.kind == "ipaddr":
        return ".".join(str(b) for b in raw[at : at + 4]), at + 4
    raise ValueError(f"component type {comp.kind} is not decoded")


def decode(definition: SettingDef | None, raw: bytes):
    """A setting's value: one value, a list of values for several components, or a list of those
    for a repeated setting. Anything not understood comes back as hex."""
    if definition is None or not definition.components:
        return raw.hex(" ")
    try:
        rows, at = [], 0
        while at < len(raw):
            row = []
            for comp in definition.components:
                value, at = _decode_component(comp, raw, at)
                row.append(value)
            rows.append(row[0] if len(row) == 1 else row)
            if not definition.repeat:
                break
        if at != len(raw):
            return raw.hex(" ")
        if not definition.repeat:
            return rows[0] if rows else None
        return rows
    except ValueError, struct.error:
        return raw.hex(" ")


def encode(definition: SettingDef | None, text: str) -> bytes:
    """Bytes for a setting with a single component, from its text form. `hex:` gives raw bytes."""
    if text.startswith("hex:"):
        return bytes.fromhex(text[4:])
    if definition is None or len(definition.components) != 1 or definition.repeat:
        raise ValueError("only single-value settings can be written by value; give hex:<bytes>")
    comp = definition.components[0]
    if comp.kind in ("string", "choice", "ipaddrany") or (comp.kind == "ipaddr" and comp.as_string):
        return text.encode("latin-1") + b"\0"
    if comp.kind == "ipaddr":
        return bytes(int(part) for part in text.split("."))
    if comp.kind == "bool":
        return b"\1" if text.lower() in ("1", "true", "yes", "on") else b"\0"
    if comp.kind in FIXED:
        if comp.items:
            by_name = {v.lower(): k for k, v in comp.items.items()}
            value = by_name.get(text.lower())
            if value is None:
                value = int(text)
        elif comp.kind in ("float", "double"):
            value = float(text)
        else:
            value = int(text)
        return struct.pack(FIXED[comp.kind], value)
    raise ValueError(f"component type {comp.kind} cannot be written by value; give hex:<bytes>")


def find(definitions: dict, key: str) -> int:
    """A setting id from a number or a (case-insensitive) name."""
    if key.isdigit():
        return int(key)
    for sid, d in definitions.items():
        if d.name.lower() == key.lower():
            return sid
    raise ValueError(f"no setting named {key!r}" + ("" if definitions else "; no DevConfig library, use the number"))


# --- settings by name ----------------------------------------------------------------------------
# Without the DevConfig library the CR6 still names its settings: terminal menu F, then S, lists
# them (name, type, range, ReadOnly), and BMP5 Get/Set Values reach each one by name in the
# `Settings` table. A Set Values there is kept: Station Name written this way showed up in the
# DevConfig Get Settings reply. Confirmed on CR6 serial 27946 (OS 14.4.0) on 2026-11-19.

SETTING_TYPES = ("String", "Long", "Boolean", "Uint1", "Uint2", "Uint4", "Int1", "Int2", "Float", "Double")
_LISTED = re.compile(r"^ (\S.*?)\s*\b(" + "|".join(SETTING_TYPES) + r")\b\s*(.*?)\s*$")


def parse_setting_list(text: str) -> list[dict]:
    """The terminal's settings list as [{name, type, range, read_only}]. Long names run into the
    type column ("DeleteCardFilesOnMismatch(1)Boolean"), so the type word splits the line."""
    rows = []
    for line in text.splitlines():
        match = _LISTED.match(line)
        if match:
            name, kind, rest = match.groups()
            read_only = rest.endswith("ReadOnly")
            rows.append(
                {"name": name, "type": kind, "range": rest.removesuffix("ReadOnly").strip(), "read_only": read_only}
            )
    return rows


def _terminal(link, tran: int, keys: bytes, timeout: float) -> str:
    """Send `keys` and read until the logger is quiet at a prompt (a line ending in > or :)."""
    import time

    link.terminal_write(tran, keys)
    out = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = link.terminal_read(tran, 0.3)
        out.extend(chunk)
        if not chunk and out.rstrip().endswith((b">", b":")):
            break
    return out.decode("latin-1")


def list_named(link) -> list[dict]:
    """Every setting the logger names on its terminal; [] when its terminal has no such list."""
    tran, _ = link.terminal_open()
    menu = _terminal(link, tran, b"F\r", 5)
    if "S: Settings" not in menu:
        _terminal(link, tran, b"\r", 2)
        return []
    return parse_setting_list(_terminal(link, tran, b"S\r", 20))


def read_named(link) -> list[dict]:
    """`list_named` with each setting's current value (the logger's own formatting), or an error."""
    from .pakbus import PakBusError

    rows = list_named(link)
    for row in rows:
        try:
            row["value"] = link.get_values("Settings", row["name"])[0]
        except PakBusError as exc:
            row["value"], row["error"] = None, str(exc)
    return rows


def named_value(kind: str, text: str) -> tuple[object, str]:
    """(value, Set Values type) for writing `text` to a setting of terminal type `kind`."""
    if kind == "String":
        return text, "ascii"
    if kind == "Boolean":
        return (-1 if text.strip().lower() in ("1", "-1", "true", "yes", "on") else 0), "long"
    if kind in ("Float", "Double"):
        return float(text), "ieee4" if kind == "Float" else "ieee8"
    return int(text), "long"
