"""Create HanLane collections and indexes. Safe to run repeatedly.

    python db/init.py          # create missing, update TTLs that changed in config
    python db/init.py --check  # report only, no writes

Never drops anything: a collection of the wrong kind is reported as an error.
"""
from __future__ import annotations

import argparse
import sys

from pymongo.database import Database
from pymongo.errors import OperationFailure

from common.mongo import get_db
from db.schema import CollectionSpec, IndexSpec, specs


def _norm_keys(keys) -> list[tuple[str, object]]:
    return [(k, int(v) if isinstance(v, (int, float)) else v) for k, v in keys]


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.errors = 0

    def add(self, target: str, status: str, detail: str = "") -> None:
        self.rows.append((target, status, detail))
        if status == "ERROR":
            self.errors += 1

    def print(self) -> None:
        w = max(len(r[0]) for r in self.rows) if self.rows else 10
        for target, status, detail in self.rows:
            print(f"{target:<{w}}  {status:<10} {detail}")


def ensure_collection(db: Database, spec: CollectionSpec, check: bool, rep: Report) -> bool:
    info = next(db.list_collections(filter={"name": spec.name}), None)
    if info is None:
        if check:
            rep.add(spec.name, "MISSING")
            return False
        opts: dict = {}
        if spec.timeseries:
            opts["timeseries"] = spec.timeseries
            if spec.ttl is not None:
                opts["expireAfterSeconds"] = spec.ttl
        db.create_collection(spec.name, **opts)
        kind = "timeseries" if spec.timeseries else "collection"
        rep.add(spec.name, "created", f"{kind}" + (f", ttl={spec.ttl}s" if spec.timeseries else ""))
        return True

    is_ts = info.get("type") == "timeseries"
    if bool(spec.timeseries) != is_ts:
        want = "timeseries" if spec.timeseries else "regular collection"
        rep.add(spec.name, "ERROR", f"exists but is not a {want}; fix by hand, not dropping")
        return False
    if is_ts:
        ts_opts = info["options"].get("timeseries", {})
        for k in ("timeField", "metaField", "granularity"):
            if ts_opts.get(k) != spec.timeseries.get(k):  # type: ignore[union-attr]
                rep.add(spec.name, "ERROR", f"timeseries.{k}={ts_opts.get(k)!r}, want {spec.timeseries[k]!r}")  # type: ignore[index]
                return False
        current_ttl = info["options"].get("expireAfterSeconds")
        if current_ttl != spec.ttl:
            if check:
                rep.add(spec.name, "DRIFT", f"ttl={current_ttl}, config wants {spec.ttl}")
            else:
                db.command("collMod", spec.name, expireAfterSeconds=spec.ttl)
                rep.add(spec.name, "updated", f"ttl {current_ttl} -> {spec.ttl}s")
            return True
        rep.add(spec.name, "unchanged", f"timeseries, ttl={current_ttl}s")
    else:
        rep.add(spec.name, "unchanged", "collection")
    return True


def ensure_index(db: Database, coll: str, spec: IndexSpec, check: bool, rep: Report) -> None:
    target = f"{coll}.{spec.name}"
    existing = db[coll].index_information()
    want_keys = _norm_keys(spec.keys)
    match_name = existing.get(spec.name)
    same_keys = next((n for n, i in existing.items() if _norm_keys(i["key"]) == want_keys), None)

    if match_name is None and same_keys is None:
        if check:
            rep.add(target, "MISSING")
            return
        kwargs: dict = {"name": spec.name}
        if spec.unique:
            kwargs["unique"] = True
        if spec.ttl is not None:
            kwargs["expireAfterSeconds"] = spec.ttl
        db[coll].create_index(spec.keys, **kwargs)
        rep.add(target, "created", _describe(spec))
        return

    name = spec.name if match_name is not None else same_keys
    info = existing[name]  # type: ignore[index]
    if _norm_keys(info["key"]) != want_keys:
        rep.add(target, "ERROR", f"name in use with keys {info['key']}")
        return
    if bool(info.get("unique")) != spec.unique:
        rep.add(target, "ERROR", f"unique={info.get('unique', False)}, want {spec.unique}; drop by hand")
        return
    current_ttl = info.get("expireAfterSeconds")
    if current_ttl != spec.ttl:
        if spec.ttl is None or current_ttl is None:
            rep.add(target, "ERROR", f"ttl={current_ttl}, want {spec.ttl}; cannot convert, drop by hand")
            return
        if check:
            rep.add(target, "DRIFT", f"ttl={current_ttl}, config wants {spec.ttl}")
            return
        db.command("collMod", coll, index={"name": name, "expireAfterSeconds": spec.ttl})
        rep.add(target, "updated", f"ttl {current_ttl} -> {spec.ttl}s")
        return
    rep.add(target, "unchanged", _describe(spec) + (f" (named {name})" if name != spec.name else ""))


def _describe(spec: IndexSpec) -> str:
    keys = ", ".join(f"{k}:{v}" for k, v in spec.keys)
    extra = []
    if spec.unique:
        extra.append("unique")
    if spec.ttl is not None:
        extra.append(f"ttl={spec.ttl}s")
    return "{" + keys + "}" + (" " + " ".join(extra) if extra else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report state only, no writes")
    args = ap.parse_args()

    db = get_db("hanlane-init")
    db.client.admin.command("ping")
    print(f"database: {db.name}  mode: {'check' if args.check else 'apply'}")
    rep = Report()
    try:
        for spec in specs():
            if ensure_collection(db, spec, args.check, rep):
                for idx in spec.indexes:
                    ensure_index(db, spec.name, idx, args.check, rep)
    except OperationFailure as e:
        rep.add("mongodb", "ERROR", str(e))
    rep.print()
    if rep.errors:
        print(f"\n{rep.errors} error(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
