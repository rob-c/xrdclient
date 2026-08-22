"""C++ type names, read only as far as a file needs them read.

A branch says what it holds in the words the class was declared in -
``vector<float>``, ``map<string,short>``, ``unsigned int`` - and everything
about how to decode it follows from that name. This module turns the name
into a small tree of :class:`Prim`, :class:`Str`, :class:`Seq` and
:class:`Mapping`, and gives back ``None`` for a name it does not recognise,
which is how a column ends up refused with its C++ type quoted rather than
guessed at.
"""

from __future__ import annotations

__all__ = ["Prim", "Str", "Seq", "Mapping", "Pair", "parse", "py_name"]

#: Every spelling of a fundamental type ROOT writes, and what it is here.
_PRIMS = [
    ("bool", "b", 1, ("bool", "Bool_t")),
    ("int8", "b", 1, ("char", "Char_t", "int8_t", "signed char")),
    ("uint8", "B", 1, ("unsigned char", "UChar_t", "uint8_t", "Byte_t")),
    ("int16", "h", 2, ("short", "short int", "Short_t", "int16_t", "Version_t")),
    ("uint16", "H", 2, ("unsigned short", "unsigned short int", "UShort_t", "uint16_t")),
    ("int32", "i", 4, ("int", "Int_t", "int32_t")),
    ("uint32", "I", 4, ("unsigned", "unsigned int", "UInt_t", "uint32_t")),
    ("int64", "q", 8, ("long", "long int", "long long", "long long int", "Long_t", "Long64_t")),
    (
        "uint64",
        "Q",
        8,
        (
            "unsigned long",
            "unsigned long int",
            "unsigned long long",
            "unsigned long long int",
            "ULong_t",
            "ULong64_t",
        ),
    ),
    ("float32", "f", 4, ("float", "Float_t")),
    ("float64", "d", 8, ("double", "Double_t")),
]

#: Containers whose contents are simply one value after another. ``RVec`` is
#: the vector ``RDataFrame`` hands out, and is written exactly like one.
SEQUENCES = ("vector", "list", "forward_list", "deque", "set", "multiset", "unordered_set", "RVec")

#: Containers of pairs. ``multimap`` is missing on purpose: a Python dict
#: would silently drop the duplicate keys that are the point of one.
MAPPINGS = ("map", "unordered_map")


class Prim:
    """A fundamental type: how wide it is and what :mod:`array` calls it."""

    __slots__ = ("typename", "typecode", "itemsize")

    def __init__(self, typename: str, typecode: str, itemsize: int) -> None:
        self.typename = typename
        self.typecode = typecode
        self.itemsize = itemsize

    def __repr__(self) -> str:
        return f"<Prim {self.typename}>"


class Str:
    """``std::string`` or ``TString``, which are not written quite alike."""

    __slots__ = ("record",)

    def __init__(self, record: bool) -> None:
        #: Is a run of these introduced by a record of its own? A
        #: ``std::string`` is streamed as a class and carries one; a
        #: ``TString`` is a length and its bytes, and carries nothing.
        self.record = record

    def __repr__(self) -> str:
        return "<Str>"


class Seq:
    """A container holding one type, in order."""

    __slots__ = ("item",)

    def __init__(self, item: object) -> None:
        self.item = item

    def __repr__(self) -> str:
        return f"<Seq of {self.item!r}>"


class Mapping:
    """A container of key-value pairs."""

    __slots__ = ("key", "value")

    def __init__(self, key: object, value: object) -> None:
        self.key = key
        self.value = value

    def __repr__(self) -> str:
        return f"<Mapping {self.key!r} to {self.value!r}>"


class Pair:
    """``std::pair``, which comes back as the tuple it obviously is."""

    __slots__ = ("first", "second")

    def __init__(self, first: object, second: object) -> None:
        self.first = first
        self.second = second

    def __repr__(self) -> str:
        return f"<Pair of {self.first!r} and {self.second!r}>"


_BY_NAME = {
    alias: (typename, code, size) for typename, code, size, aliases in _PRIMS for alias in aliases
}
_STRINGS = {"string": True, "basic_string<char>": True, "TString": False}


def _split(text: str) -> tuple[str, str] | None:
    """``'int,vector<short> '`` into its two halves, ignoring nested commas."""
    depth = 0
    for index, char in enumerate(text):
        depth += (char == "<") - (char == ">")
        if char == "," and depth == 0:
            return text[:index], text[index + 1 :]
    return None


def parse(name: str) -> object | None:
    """The type a C++ name describes, or ``None`` if this reader has no idea.

    >>> parse("vector<float>")
    <Seq of <Prim float32>>
    """
    text = name.replace("std::", "").strip().rstrip("*&").strip()  # a pointer holds the same
    text = text.replace("ROOT::VecOps::", "")
    direct, value = _direct(text)
    if direct:
        return value
    return _template(text)


def _direct(text: str) -> tuple[bool, object | None]:
    if text in _STRINGS:
        return True, Str(_STRINGS[text])
    found = _BY_NAME.get(text)
    if found is not None:
        return True, Prim(*found)
    return False, None


def _template(text: str) -> object | None:
    head, angle, rest = text.partition("<")
    if not angle or not rest.endswith(">"):
        return None
    inside = rest[:-1]
    if head in SEQUENCES:
        return _sequence(inside)
    if head == "bitset" and inside.strip().isdigit():
        # A byte a bit, lowest bit first, and the count is in the file as well,
        # so the width in the name is not needed to read one.
        return Seq(Prim(*_BY_NAME["bool"]))
    if head in MAPPINGS:
        return _mapping(inside)
    if head == "pair":
        return _pair(inside)
    return None


def _sequence(inside: str) -> Seq | None:
    item = parse(inside)
    return None if item is None else Seq(item)


def _mapping(inside: str) -> Mapping | None:
    halves = _split(inside)
    if halves is None:
        return None
    key, value = parse(halves[0]), parse(halves[1])
    return None if key is None or value is None else Mapping(key, value)


def _pair(inside: str) -> Pair | None:
    halves = _split(inside)
    if halves is None:
        return None
    first, second = parse(halves[0]), parse(halves[1])
    return None if first is None or second is None else Pair(first, second)


def py_name(node: object) -> str:
    """What the values look like once they are Python.

    >>> py_name(parse("map<string,vector<int> >"))
    'dict[str, list[int32]]'
    """
    if isinstance(node, Prim):
        return node.typename
    if isinstance(node, Str):
        return "str"
    if isinstance(node, Seq):
        return f"list[{py_name(node.item)}]"
    if isinstance(node, Pair):
        return f"tuple[{py_name(node.first)}, {py_name(node.second)}]"
    assert isinstance(node, Mapping)
    return f"dict[{py_name(node.key)}, {py_name(node.value)}]"
