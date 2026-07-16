"""Tagged-scalar JSON, shared by the artifact and the snapshot store.

**This exists because JSON has no decimal type and never will.** ADR-0002 [2.2] is the whole
argument, and it survives ADR-0004 in exactly one place: `NUMERIC` made the *columns* exact
natively, so `decisions.amount` needs no tagging at all — but the frozen `Snapshot` payload is
JSONB, and JSONB is JSON. Plain `json.dumps` turns a `Decimal` into a **float**, and a cent that
round-trips through a float is no longer the cent the engine decided on.

Two layers, and they are different problems:

- **Scalars** (`encode_scalar`/`decode_scalar`). `Decimal` → `{"$dec": "400.00"}`, `date` →
  `{"$date": ...}`, `Enum` → `{"$enum": [TypeName, value]}`. Decoding needs a **name registry**
  because the caller has no idea what type to expect: `Reason.params` is an open mapping.
- **Trees** (`encode_tree`/`decode_tree`). A whole dataclass graph, for the snapshot payload.
  Decoding here is **type-driven** — the target dataclass says what each field is, so no registry
  is needed and an unknown enum is impossible rather than a runtime surprise.

## Decoding a Decimal does not re-quantize

Amounts are written as the exact text `money()` already produced and read back with
`Decimal(text)`, never `money(text)`. Re-quantizing on the way in is a second rounding of an
already-rounded figure — silent, in the one place the type system exists to keep silent roundings
out of. An APR is why this matters in practice: it is a *rate*, and quantizing 0.2399 to cents
turns 23.99% into 24%.

## Why the tree codec is generic rather than hand-written

A hand-written `Snapshot` encoder would need a line per field, and would silently drop the next
field someone adds — the payload would still decode, still validate, and quietly describe a
different snapshot than the engine saw. That is precisely the drift ticket `0019` was about:
`build()` and `replay()` each had their own idea of what a `Snapshot` was, and they disagreed for
months without a single test going red. **A codec that reads the dataclass cannot drift from the
dataclass.**
"""

from __future__ import annotations

import dataclasses
import types
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints


class CodecError(ValueError):
    """A value could not be encoded or decoded. Raised at a boundary, never mid-record."""


# --- scalars ------------------------------------------------------------------------


def encode_scalar(value: object, *, error: type[Exception] = CodecError) -> Any:
    # bool before int (a bool *is* an int), Enum before str (our enums are str enums).
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Enum):
        return {"$enum": [type(value).__name__, value.value]}
    if isinstance(value, int | float | str):
        return value
    raise error(f"cannot encode {value!r} of type {type(value).__name__}")


def decode_scalar(
    value: Any, enums: dict[str, type[Enum]], *, error: type[Exception] = CodecError
) -> object:
    if not isinstance(value, dict):
        return value

    if "$dec" in value:
        return Decimal(str(value["$dec"]))  # exact text, never re-quantized — see module docs
    if "$date" in value:
        return date.fromisoformat(str(value["$date"]))
    if "$enum" in value:
        name, raw = value["$enum"]
        if name not in enums:
            raise error(f"unknown enum type {name!r}")
        return enums[name](raw)

    raise error(f"unrecognised tagged value {value!r}")


# --- trees --------------------------------------------------------------------------


def encode_tree(obj: object, *, error: type[Exception] = CodecError) -> Any:
    """A dataclass graph as tagged JSON.

    Tuples and frozensets both become lists — JSON has neither. `decode_tree` puts them back from
    the field's declared type, which is why the round-trip is exact despite the flattening.
    `frozenset` is not hypothetical: `UserPolicy.blackout_dates` is one.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: encode_tree(getattr(obj, f.name), error=error) for f in dataclasses.fields(obj)
        }
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [encode_tree(x, error=error) for x in obj]
    if isinstance(obj, dict):
        return {str(k): encode_tree(v, error=error) for k, v in obj.items()}
    return encode_scalar(obj, error=error)


def _unwrap_optional(tp: Any) -> tuple[Any, bool]:
    """`Decimal | None` → `(Decimal, True)`. Anything else → `(tp, False)`."""
    if get_origin(tp) in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


def decode_tree(cls: Any, data: Any, *, error: type[Exception] = CodecError) -> Any:
    """Rebuild `cls` from `encode_tree`'s output. The target type drives everything.

    No enum-name registry: the field's declared type already says which enum it is, so an unknown
    one cannot occur. That is the advantage of knowing the shape, and the reason `Reason.params`
    (which does not) still needs `decode_scalar`.
    """
    tp, optional = _unwrap_optional(cls)
    if optional and data is None:
        return None

    origin = get_origin(tp)

    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(data, dict):
            raise error(f"expected an object for {tp.__name__}, got {type(data).__name__}")
        hints = get_type_hints(tp)
        kwargs = {}
        for f in dataclasses.fields(tp):
            if f.name not in data:
                raise error(f"{tp.__name__} is missing field {f.name!r}")
            kwargs[f.name] = decode_tree(hints[f.name], data[f.name], error=error)
        return tp(**kwargs)

    if origin in (tuple, list, set, frozenset):
        args = get_args(tp)
        # `tuple[X, ...]` is homogeneous; a fixed-length `tuple[X, Y]` is not. Both appear.
        inner = args[0] if args else Any
        items = [decode_tree(inner, x, error=error) for x in data]
        if origin is tuple:
            return tuple(items)
        if origin is frozenset:
            return frozenset(items)
        if origin is set:
            return set(items)
        return items

    if origin is dict:
        _, vt = get_args(tp) or (Any, Any)
        return {k: decode_tree(vt, v, error=error) for k, v in data.items()}

    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(data["$enum"][1] if isinstance(data, dict) else data)

    if tp is Decimal:
        if not isinstance(data, dict) or "$dec" not in data:
            raise error(f"expected a tagged decimal, got {data!r}")
        return Decimal(str(data["$dec"]))  # never re-quantized

    if tp is date:
        if not isinstance(data, dict) or "$date" not in data:
            raise error(f"expected a tagged date, got {data!r}")
        return date.fromisoformat(str(data["$date"]))

    if tp is Any:
        return decode_scalar(data, {}, error=error) if isinstance(data, dict) else data

    return data
