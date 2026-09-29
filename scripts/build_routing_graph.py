"""Build a new local Valhalla road graph from an explicitly supplied OSM PBF."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from dispatch.routing.bicycle import DISMOUNT_POLICY, prepare_dismount_pbf
from dispatch.routing.contracts import content_hash


def main():
    import valhalla

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pbf", type=Path)
    parser.add_argument("output", type=Path, help="Новая, ещё не существующая директория")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--bicycle-dismount", action="store_true", help="Отдельный велограф с проходами пешком"
    )
    args = parser.parse_args()
    if not 1 <= args.threads <= 16:
        parser.error("--threads должен быть от 1 до 16")
    if args.pbf.suffix != ".pbf":
        parser.error("Нужен завершённый файл .pbf, не частичная загрузка .part")
    with args.pbf.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    args.output.mkdir(parents=True, exist_ok=False)
    build_source = args.pbf
    dismount = None
    if args.bicycle_dismount:
        build_source = args.output / "dismount.osm.pbf"
        count = prepare_dismount_pbf(args.pbf, build_source)
        with build_source.open("rb") as source:
            prepared_hash = hashlib.file_digest(source, "sha256").hexdigest()
        dismount = {"policy": DISMOUNT_POLICY, "changed_ways": count, "pbf_sha256": prepared_hash}
        print(f"Проходы со спешиванием: {count}", flush=True)
    tiles = (args.output / "tiles").resolve()
    tiles.mkdir()
    config = valhalla.get_config(tile_dir=tiles, tile_extract="", verbose=True)
    for key in (
        "traffic_extract",
        "admin",
        "landmarks",
        "timezone",
        "transit_dir",
        "transit_feeds_dir",
    ):
        config["mjolnir"][key] = ""
    config["mjolnir"]["data_processing"]["use_admin_db"] = False
    config["mjolnir"]["data_processing"]["apply_country_overrides"] = False
    config_path = args.output / "valhalla.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    binary = Path(valhalla.__file__).parent / "bin/valhalla_build_tiles"
    print(f"Сборка графа; журнал: {args.output / 'build.log'}", flush=True)
    with (args.output / "build.log").open("w") as log:
        subprocess.run(
            [
                str(binary),
                "-c",
                str(config_path.resolve()),
                "-j",
                str(args.threads),
                str(build_source.resolve()),
            ],
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    if not any(tiles.rglob("*.gph")):
        raise RuntimeError("Сборка завершилась без дорожных тайлов")
    manifest = {
        "schema_version": "valhalla_static_v1",
        "engine": valhalla.__version__,
        "pbf_sha256": digest,
        "config_hash": content_hash(config),
        "assumptions": [
            "Без пробок, базы часовых поясов и региональных скоростных переопределений."
        ],
        "attribution": "© OpenStreetMap contributors, ODbL",
    }
    if dismount:
        manifest["bicycle_dismount"] = dismount
        manifest["assumptions"].append(
            "Общедоступные footway/pedestrian без явных правил bicycle доступны со спешиванием; "
            "это допущение локальной модели. Явные ограничения доступа сохранены."
        )
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    )
    if dismount:
        build_source.unlink()
    print(f"Граф готов: {args.output}; {content_hash(manifest)}")


if __name__ == "__main__":
    main()
