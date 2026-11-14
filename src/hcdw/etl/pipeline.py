"""End-to-end ETL: extract -> clean -> aggregate -> load -> quality. Always builds the LOCAL file;
use `hcdw push` (or `hcdw etl --push`) to publish it to MotherDuck.

The warehouse is built in a side file (<dw>.building) and swapped in only after every step has
succeeded and every data-quality check has passed, so a failed, interrupted or failing-DQ run never
replaces the live warehouse. dq_report.json is written either way (`published` says which).
"""
from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from hcdw.config import get_settings
from hcdw.db import get_conn
from hcdw.etl.aggregate import aggregate
from hcdw.etl.clean import clean
from hcdw.etl.extract import extract
from hcdw.etl.load import load
from hcdw.etl.quality import run_checks

log = logging.getLogger(__name__)


class WarehouseLockedError(RuntimeError):
    """The finished warehouse could not replace the old file (another process has it open)."""


class DataQualityError(RuntimeError):
    """Data-quality checks failed: the new build was discarded and the live warehouse kept.

    `report` is the run report (also written to dq_report.json) with the failing checks."""

    def __init__(self, report: dict, report_path: Path):
        self.report = report
        failed = [c for c in report["data_quality"]["checks"] if not c["passed"]]
        lines = "\n".join(f"  {c['name']}: {c['detail']}" for c in failed)
        super().__init__(f"{len(failed)} data-quality check(s) failed; the warehouse was not replaced "
                         f"(old {Path(report['dw_path']).name} kept). Report: {report_path}\n{lines}")


def _remove(*paths: Path) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


@contextmanager
def _step(name: str, timings: dict[str, float]):
    log.info("== %s ...", name)
    t0 = time.perf_counter()
    yield
    timings[name] = round(time.perf_counter() - t0, 2)
    log.info("== %s done in %.2fs", name, timings[name])


def run(source_dir: Path, dw_path: Path | None = None, extra_source_dirs: list[Path] | tuple[Path, ...] = ()) -> dict:
    """Build the warehouse from source_dir; optional sources may also live in extra_source_dirs."""
    settings = get_settings()
    dw_path = Path(dw_path or settings.dw_path)
    building = dw_path.with_name(dw_path.name + ".building")
    _remove(building, Path(f"{building}.wal"))
    timings: dict[str, float] = {}
    con = get_conn("local", path=building)
    # Row order of staging tables does not matter; dropping the guarantee saves memory on 27M-row files.
    con.execute("SET preserve_insertion_order = false")
    try:
        with _step("extract", timings):
            extracted = extract(con, source_dir, extra_source_dirs)
        with _step("clean", timings):
            cleaned = clean(con, settings)
        with _step("aggregate", timings):
            aggregated = aggregate(con, settings)
        with _step("load", timings):
            loaded = load(con, settings)
        with _step("quality", timings):
            dq = run_checks(con, settings)
        con.execute("CHECKPOINT")
    except BaseException:
        con.close()
        _remove(building, Path(f"{building}.wal"))
        raise
    con.close()

    report = {
        "run_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": str(source_dir),
        "dw_path": str(dw_path),
        "timings_sec": timings,
        "extracted_rows": extracted,
        "cleaned_applications": cleaned,
        "aggregated_rows": aggregated,
        "loaded_rows": loaded,
        "data_quality": dq,
        "published": bool(dq["passed"]),
    }
    report_path = dw_path.parent / settings.dq_report.name
    if not dq["passed"]:
        # Quality gate: keep the report as evidence, discard the build, leave the live warehouse alone.
        _remove(building, Path(f"{building}.wal"))
        _write_report(report, report_path)
        raise DataQualityError(report, report_path)

    try:
        _remove(Path(f"{dw_path}.wal"))  # a stale WAL must never be replayed onto the new file
        os.replace(building, dw_path)
    except PermissionError as exc:
        raise WarehouseLockedError(
            f"{dw_path.name} is open in another process (dashboard?). Close it and run the ETL again; "
            f"the old warehouse is unchanged."
        ) from exc
    _write_report(report, report_path)
    return report


def _write_report(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    log.info("report written to %s", path)
