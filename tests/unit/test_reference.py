import pyarrow as pa

from flightops.silver.reference import build_airlines, build_airports


def test_airports_get_time_zones_and_one_row_per_code() -> None:
    raw = pa.table(
        {
            "ident": ["KJFK", "KXYZ", "PHNL"],
            "type": ["large_airport", "closed", "large_airport"],
            "name": ["John F Kennedy Intl", "Old JFK strip", "Honolulu Intl"],
            "latitude_deg": ["40.6398", "40.6", "21.3187"],
            "longitude_deg": ["-73.7789", "-73.7", "-157.9225"],
            "elevation_ft": ["13", "10", "13"],
            "iso_country": ["US", "US", "US"],
            "iso_region": ["US-NY", "US-NY", "US-HI"],
            "municipality": ["New York", "New York", "Honolulu"],
            "scheduled_service": ["yes", "no", "yes"],
            "icao_code": ["KJFK", "", "PHNL"],
            "iata_code": ["JFK", "JFK", "HNL"],
        }
    )
    airports = {row["iata"]: row for row in build_airports(raw).to_pylist()}
    assert set(airports) == {"JFK", "HNL"}
    assert airports["JFK"]["name"] == "John F Kennedy Intl"  # open airport beats the closed one
    assert airports["JFK"]["tz"] == "America/New_York"
    assert airports["HNL"]["tz"] == "Pacific/Honolulu"


def test_airlines_clean_placeholder_codes() -> None:
    raw = pa.table(
        {
            "airline_id": ["-1", "2009", "9999"],
            "name": ["Unknown", "Delta Air Lines", "Ghost Air"],
            "alias": [None, None, None],
            "iata": ["-", "DL", "-"],
            "icao": ["N/A", "DAL", ""],
            "callsign": [None, "DELTA", ""],
            "country": [None, "United States", "Nowhere"],
            "active": ["Y", "Y", "N"],
        }
    )
    airlines = build_airlines(raw).to_pylist()
    assert [a["name"] for a in airlines] == ["Delta Air Lines", "Ghost Air"]
    assert airlines[0]["icao"] == "DAL"
    assert airlines[1]["iata"] is None
    assert airlines[1]["active"] is False
