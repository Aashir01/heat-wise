"""US city resolution. Returns every match for an ambiguous name; never picks one silently.

python -m heatwise.geo Portland
"""

from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass
from functools import lru_cache

from .gazetteer_data import (
    CITIES,
    CITY_TZ,
    NON_US_COUNTRIES,
    NON_US_PLACES,
    STATE_NAMES,
    STATE_TZ,
)


class GeoError(Exception):
    """Base class. `user_message` is safe to show to an end user."""

    @property
    def user_message(self) -> str:
        return str(self)


class NonUSLocation(GeoError):
    def __init__(self, query: str) -> None:
        super().__init__(
            f"'{query}' looks like a place outside the United States. "
            "FortyGuard data and this tool cover US locations only."
        )


class CityNotFound(GeoError):
    def __init__(self, query: str, suggestions: list[str]) -> None:
        self.suggestions = suggestions
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(
            f"I don't have '{query}' in my US city list.{hint} "
            "You can also give latitude and longitude."
        )


@dataclass(frozen=True)
class City:
    name: str
    state: str
    lat: float
    lon: float
    tz: str

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}"


_ALIASES = {
    "nyc": "new york",
    "la": "los angeles",
    "sf": "san francisco",
    "vegas": "las vegas",
    "washington dc": "washington",
    "washington d c": "washington",
    "dc": "washington",
}


def _norm(text: str) -> str:
    t = text.lower().replace("&", " and ")
    t = re.sub(r"\bst\.?\b", "saint", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


@lru_cache(maxsize=1)
def _all() -> tuple[City, ...]:
    return tuple(
        City(n, s, lat, lon, CITY_TZ.get((n, s), STATE_TZ[s])) for n, s, lat, lon in CITIES
    )


def _split_state(norm_query: str) -> tuple[str, str | None]:
    """Split a trailing state (abbreviation or full name) off a normalised query."""
    abbrevs = {a.lower(): a for a in STATE_TZ}
    for name, abbr in sorted(STATE_NAMES.items(), key=lambda kv: -len(kv[0])):
        n = _norm(name)
        if norm_query.endswith(" " + n):
            return norm_query[: -len(n)].strip(), abbr
    parts = norm_query.rsplit(" ", 1)
    if len(parts) == 2 and parts[1] in abbrevs and parts[0]:
        return parts[0], abbrevs[parts[1]]
    return norm_query, None


def resolve(query: str) -> list[City]:
    """All US cities matching `query`. Raises NonUSLocation or CityNotFound; never guesses."""
    raw = query.strip()
    if not raw:
        raise CityNotFound(query, [])
    comma_parts = [p.strip() for p in raw.split(",")]
    if len(comma_parts) > 1 and _norm(comma_parts[-1]) in NON_US_COUNTRIES:
        raise NonUSLocation(raw)
    norm = _norm(raw.replace(",", " "))
    norm = _ALIASES.get(norm, norm)
    if norm in NON_US_PLACES:
        raise NonUSLocation(raw)

    city_part, state = _split_state(norm)
    city_part = _ALIASES.get(city_part, city_part)
    matches = [
        c for c in _all() if _norm(c.name) == city_part and (state is None or c.state == state)
    ]
    if matches:
        return matches
    if city_part in NON_US_PLACES:
        raise NonUSLocation(raw)
    pool = {_norm(c.name): c.name for c in _all()}
    close = difflib.get_close_matches(city_part, list(pool), n=3, cutoff=0.75)
    suggestions = [pool[k] for k in close]
    if state is not None and any(_norm(c.name) == city_part for c in _all()):
        suggestions = [c.label for c in _all() if _norm(c.name) == city_part]
    raise CityNotFound(raw, suggestions)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("usage: python -m heatwise.geo <city name>")
        return 2
    query = " ".join(args)
    try:
        cities = resolve(query)
    except GeoError as exc:
        print(exc.user_message)
        return 1
    if len(cities) > 1:
        print(f"'{query}' is ambiguous: {len(cities)} US matches")
    for c in cities:
        print(f"  {c.label}: {c.lat}, {c.lon} ({c.tz})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
