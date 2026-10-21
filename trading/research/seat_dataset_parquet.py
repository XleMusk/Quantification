"""Export source-verified public A-share listings, partitioned by year."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

import pandas as pd

from .seat_dataset import DEFAULT_DATASET, stamp
from .seats import save_json


def write_table(db, table, destination):
    import pyarrow as pa
    import pyarrow.parquet as pq

    if table not in {"stock_disclosures", "seat_observations"}:
        raise ValueError("Only public fact tables can be exported")
    fields = db.execute(f"PRAGMA table_info({table})").fetchall()
    types = {"TEXT": pa.string(), "REAL": pa.float64(), "INTEGER": pa.int64()}
    schema = pa.schema([(row[1], types[row[2]]) for row in fields], metadata={
        b"dataset_semantics": b"Public LHB listings, not private trades or person-level accounts",
        b"missing_amounts": b"Null is unknown, not zero",
        b"amount_unit": b"CNY, A shares only"})
    view = "a_share_" + table
    years = [row[0] for row in db.execute(f"SELECT DISTINCT substr(trade_date,1,4) FROM {view} ORDER BY 1")]
    files = []
    for year in years:
        if not year.isdigit() or len(year) != 4:
            raise ValueError("Unexpected disclosure year")
        path = Path(destination) / table / f"year={year}" / "part-00000.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        rows = 0
        with pq.ParquetWriter(temporary, schema, compression="zstd") as writer:
            query = f"""SELECT e.* FROM {view} e
                WHERE e.trade_date>=? AND e.trade_date<? ORDER BY e.symbol,e.trade_date,e.raw_id"""
            for frame in pd.read_sql_query(query, db, params=(f"{year}-01-01", f"{int(year)+1}-01-01"), chunksize=50000):
                writer.write_table(pa.Table.from_pandas(frame, schema=schema, preserve_index=False))
                rows += len(frame)
        if pq.ParquetFile(temporary).metadata.num_rows != rows:
            raise ValueError("Parquet row count mismatch")
        temporary.replace(path)
        files.append({"path": str(path), "year": year, "rows": rows})
    return files


def export(root=DEFAULT_DATASET):
    root = Path(root)
    status = json.loads((root / "status.json").read_text(encoding="utf-8"))
    if status["status"] != "source_snapshot_complete":
        raise ValueError("Partial backfills cannot be published as a complete research export")
    for name in ("summary_integrity", "seat_integrity"):
        result = json.loads((root / f"{name}.json").read_text(encoding="utf-8"))
        if result["status"] != "source_pagination_verified":
            raise ValueError("Complete, checked source pages are required before publishing Parquet")
    destination = root / "parquet"
    destination.mkdir(exist_ok=True)
    manifest = {"created_at": stamp(), "source_root": str(root), "format": "parquet",
                "private_trade_history": False, "tables": {}}
    with closing(sqlite3.connect(f"file:{(root / 'disclosures.sqlite3').as_posix()}?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        for table in ("stock_disclosures", "seat_observations"):
            files = write_table(db, table, destination)
            expected = db.execute(f"SELECT COUNT(*) FROM a_share_{table}").fetchone()[0]
            if sum(file["rows"] for file in files) != expected:
                raise ValueError("Export does not match database A-share count")
            manifest["tables"][table] = {"rows": expected, "files": files}
    save_json(destination / "manifest.json", manifest)
    print(json.dumps({"path": str(destination), "tables": {k: v["rows"] for k, v in manifest["tables"].items()}}, indent=2))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_DATASET)
    export(parser.parse_args().root)
