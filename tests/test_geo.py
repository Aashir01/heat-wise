import pytest

from heatwise.gazetteer_data import CITIES, STATE_TZ
from heatwise.geo import CityNotFound, NonUSLocation, resolve


def labels(q: str) -> set[str]:
    return {c.label for c in resolve(q)}


def test_portland_is_ambiguous() -> None:
    assert labels("Portland") == {"Portland, OR", "Portland, ME"}


def test_springfield_has_at_least_three_matches() -> None:
    found = labels("springfield")
    assert len(found) >= 3
    assert {"Springfield, IL", "Springfield, MO", "Springfield, MA"} <= found


@pytest.mark.parametrize(
    "q", ["Columbus", "Kansas City", "Aurora", "Richmond", "Arlington", "Salem"]
)
def test_other_known_ambiguous_names(q: str) -> None:
    assert len(resolve(q)) >= 2


def test_austin_is_unique() -> None:
    (c,) = resolve("Austin")
    assert c.label == "Austin, TX" and c.tz == "America/Chicago"


@pytest.mark.parametrize("q", ["Portland, ME", "portland maine", "Portland ME"])
def test_state_disambiguates(q: str) -> None:
    (c,) = resolve(q)
    assert c.label == "Portland, ME"


def test_full_state_name_with_spaces() -> None:
    (c,) = resolve("Charleston, West Virginia")
    assert c.label == "Charleston, WV"


@pytest.mark.parametrize("q", ["St. Louis", "Saint Louis", "st louis"])
def test_saint_variants(q: str) -> None:
    assert labels(q) == {"St. Louis, MO"}


def test_non_us_is_rejected_not_guessed() -> None:
    with pytest.raises(NonUSLocation):
        resolve("London")
    with pytest.raises(NonUSLocation):
        resolve("Toronto, Canada")
    with pytest.raises(NonUSLocation):
        resolve("Paris, France")


def test_typo_gives_suggestions_not_a_silent_pick() -> None:
    with pytest.raises(CityNotFound) as ei:
        resolve("Housten")
    assert "Houston" in ei.value.suggestions
    with pytest.raises(CityNotFound) as ei2:
        resolve("Pheonix")
    assert "Phoenix" in ei2.value.suggestions


def test_unknown_city_and_empty_input() -> None:
    with pytest.raises(CityNotFound):
        resolve("Zzyzx Springs")
    with pytest.raises(CityNotFound):
        resolve("   ")


def test_valid_name_wrong_state_lists_real_options() -> None:
    with pytest.raises(CityNotFound) as ei:
        resolve("Austin, CA")
    assert "Austin, TX" in ei.value.suggestions


def test_washington_dc_alias() -> None:
    assert labels("Washington DC") == {"Washington, DC"}


def test_data_sanity() -> None:
    assert len(CITIES) >= 100
    for name, state, lat, lon in CITIES:
        assert state in STATE_TZ, (name, state)
        assert 18 < lat < 72 and -170 < lon < -66, (name, lat, lon)
    keys = [(n, s) for n, s, *_ in CITIES]
    assert len(keys) == len(set(keys))
