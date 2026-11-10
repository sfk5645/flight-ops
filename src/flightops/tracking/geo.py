"""Geometry helpers that build DuckDB SQL expressions."""

EARTH_RADIUS_M = 6_371_000


def distance_m(lat1: str, lon1: str, lat2: str, lon2: str) -> str:
    """SQL for the great-circle (haversine) distance in metres between two columns' points."""
    return f"""(2 * {EARTH_RADIUS_M} * asin(sqrt(
        pow(sin(radians({lat2} - {lat1}) / 2), 2)
        + cos(radians({lat1})) * cos(radians({lat2}))
        * pow(sin(radians({lon2} - {lon1}) / 2), 2)
    )))"""
