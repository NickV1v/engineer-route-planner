"""Explicit, conservative pushing-bike policy for a separate local OSM graph."""

from pathlib import Path

DISMOUNT_POLICY = {
    "version": "public_footways_dismount_v1",
    "highways": ["footway", "pedestrian"],
    "access": ["yes", "permissive", "designated"],
}


def dismount_tags(tags: dict[str, str]) -> dict[str, str]:
    """Infer pushing only on public footways without explicit bicycle rules.

    This is a local routing assumption, never an edit to OpenStreetMap. Keep
    restrictions, conditional/directional rules, private ways and indoor paths.
    The engine still checks barriers, turn restrictions and network connectivity.
    """
    if tags.get("highway") not in DISMOUNT_POLICY["highways"]:
        return tags
    if any(k == "bicycle" or k.startswith("bicycle:") for k in tags):
        return tags
    for key in ("access", "foot", "vehicle"):
        if key in tags and tags[key] not in DISMOUNT_POLICY["access"]:
            return tags
        if any(k.startswith(key + ":") for k in tags):
            return tags
    if (
        tags.get("indoor", "no") != "no"
        or tags.get("conveying", "no") != "no"
        or any(k in tags for k in ("construction", "proposed", "opening_hours"))
    ):
        return tags
    return {**tags, "bicycle": "dismount"}


def prepare_dismount_pbf(source: Path, destination: Path) -> int:
    """Stream all OSM objects unchanged except inferred way-level dismount tags."""
    import osmium

    changed = 0
    with osmium.SimpleWriter(str(destination), overwrite=False) as output:
        # Native filtering forwards millions of unaffected nodes/ways/relations
        # directly to the writer, retaining their order and metadata.
        objects = (
            osmium.FileProcessor(str(source))
            .with_filter(
                osmium.filter.TagFilter(*[("highway", v) for v in DISMOUNT_POLICY["highways"]])
            )
            .handler_for_filtered(output)
        )
        for obj in objects:
            if isinstance(obj, osmium.osm.Way):
                original = dict(obj.tags)
                updated = dismount_tags(original)
                if updated != original:
                    changed += 1
                    obj = obj.replace(tags=updated)
            output.add(obj)
    return changed
