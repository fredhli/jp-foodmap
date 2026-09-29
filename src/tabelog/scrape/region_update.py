"""Region data commits shared by normal and bimonth runs.

The caller supplies the selection made by main.py. This module owns detail
refreshes, departure records, and atomic writes; it does not choose restaurants.
"""
from __future__ import annotations
import csv
import hashlib
import io
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tabelog.paths import atomic_write_csv, atomic_write_json, atomic_write_text
from tabelog.scrape import scrape_all
from tabelog.scrape.bimonth_resume import corpus_fingerprint, digest_file, read_report, region_fingerprint
from tabelog.scrape.region_selection import MIN_RATING, _score, identity, _main_meal, admission_floor

LIST_FIELDS = (
    "rank", "name", "rating", "review_count", "save_count", "awards",
    "genre", "station", "station_distance_m", "dinner_upper", "lunch_upper",
    "holiday", "source_page", "source_query",
)

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def read_corpus(csv_path: Path) -> tuple[list[str], list[dict]]:
    if not csv_path.exists():
        return list(scrape_all.FIELDS), []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        if "region" not in fields or "detail_url" not in fields:
            raise ValueError(f"invalid active CSV schema: {csv_path}")
        return fields, list(reader)

def _fields(existing: list[str]) -> list[str]:
    result = list(existing)
    for field in scrape_all.FIELDS + ["rating_check", "score_source_query", "score_source_page"]:
        if field not in result:
            result.append(field)
    return result

def _copy_row(row: dict, fields: list[str]) -> dict:
    return {field: "" if row.get(field) is None else row.get(field, "") for field in fields}

def _patch_list(old: dict, fresh: dict, stamp: str) -> dict:
    updated = dict(old)
    for field in LIST_FIELDS:
        if field not in fresh or field in ("rank", "source_page", "source_query"):
            continue
        value = fresh.get(field)
        if field == "rating" and _score(value) is None:
            continue
        if field == "awards" or value not in (None, ""):
            updated[field] = value
    if _score(fresh.get("rating")) is None:
        updated["rating_check"] = "pending"
    else:
        updated["rating_check"] = "list"
        updated["score_checked_at"] = stamp
        updated["score_source_query"] = fresh.get("source_query") or "rating"
        updated["score_source_page"] = fresh.get("source_page", "")
    return updated

def _detail_into_new(row: dict, detail: dict, stamp: str) -> dict:
    updated = dict(row)
    for key in ("seat_count", "address", "reservation_policy"):
        updated[key] = detail.get(key) or ""
    updated["tabelog_bookable"] = detail.get("tabelog_bookable", False)
    for i, photo in enumerate((detail.get("photos") or [])[:3], 1):
        updated[f"photo{i}_url"] = photo
    updated["operating_status"] = detail.get("operating_status") or "unknown"
    detail_score = _score(detail.get("rating"))
    list_score = _score(row.get("rating"))
    if detail_score is not None:
        updated["rating"] = f"{detail_score:.2f}"
        updated["rating_check"] = "detail"
        updated["score_checked_at"] = stamp
    elif list_score is not None:
        updated["rating_check"] = "list"
        updated["score_checked_at"] = stamp
    else:
        updated["rating"] = ""
        updated["rating_check"] = "pending"
    updated["scraped_at"] = stamp
    updated["details_checked_at"] = stamp
    return updated

def _departure(row: dict, reason: str, stamp: str, run_id: str,
               detail: dict | None = None) -> dict:
    return {"run_id": run_id, "fetched_at": _now(), "reason": reason,
            "verified_rating": (detail or {}).get("rating"),
            "verified_status": (detail or {}).get("operating_status", "unknown"),
            "row": dict(row)}

def _archive_key(entry: dict) -> tuple:
    return (entry.get("run_id"), entry.get("reason"),
            (entry.get("row") or {}).get("detail_url"))

def _append_archive(path: Path, entries: list[dict]) -> None:
    if not entries:
        return
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = {_archive_key(json.loads(line)) for line in previous.splitlines() if line.strip()}
    missing = [entry for entry in entries if _archive_key(entry) not in existing]
    if missing:
        lines = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in missing)
        atomic_write_text(path, previous + lines, keep_prev=True)

def _csv_text(rows: list[dict], fields: list[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()

def _csv_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8-sig")).hexdigest()

def _csv_fingerprint(text: str) -> str:
    # Parse the serialized text so values carry the same string form that
    # read_corpus returns after the write.
    reader = csv.DictReader(io.StringIO(text, newline=""))
    rows = list(reader)
    return corpus_fingerprint(list(reader.fieldnames or []), rows)

def _file_fingerprint(csv_path: Path) -> str | None:
    return corpus_fingerprint(*read_corpus(csv_path)) if csv_path.exists() else None

def recover_prepared(run_path: Path, csv_path: Path, archive_path: Path) -> str:
    """Finish an interrupted commit, or allow a retry if CSV was untouched."""
    report = read_report(run_path)
    if not report or report.get("commit") != "prepared":
        return "none"
    before, after = report.get("csv_before_sha256"), report.get("csv_after_sha256")
    if not after or "csv_before_sha256" not in report:
        raise ValueError(f"{run_path}: legacy prepared commit has no CSV journal; inspect it manually")
    current = digest_file(csv_path)
    # Journals written since content fingerprints were added also survive a
    # map build that filled lat/lon after the interruption.
    content = _file_fingerprint(csv_path) if "csv_after_content" in report else None
    if current == after or (content is not None and content == report["csv_after_content"]):
        _append_archive(archive_path, report.get("departures", []))
        report["commit"] = "complete"
        report["region_after_sha256"] = region_fingerprint(read_corpus(csv_path)[1], report["region"])
        atomic_write_json(run_path, report, indent=2)
        return "committed"
    if current == before or (content is not None and content == report.get("csv_before_content")):
        archived = set()
        if archive_path.exists():
            archived = {_archive_key(json.loads(line)) for line in archive_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        if any(_archive_key(entry) in archived for entry in report.get("departures", [])):
            raise ValueError(f"{run_path}: departure archive exists but CSV is before commit")
        return "retry"
    raise ValueError(f"{run_path}: active CSV changed since prepared commit; refusing to overwrite it")

@contextmanager
def corpus_lock(csv_path: Path):
    lock_path = csv_path.with_name(csv_path.name + ".bimonth.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock:
        if os.name == "nt":
            import msvcrt
            lock.seek(0, 2)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

async def apply_region(session, region: str, csv_path: Path, archive_path: Path,
                       run_path: Path, run_id: str, *, selection: dict,
                       translate: bool = True, full_details: bool = False,
                       append_only: bool = False) -> dict:
    fields, corpus = read_corpus(csv_path)
    fields = _fields(fields)
    old = [row for row in corpus if (row.get("region") or "").lower() == region]
    print(f"[{region}] refreshing {len(old)} active restaurants")
    print(f"[{region}] list ended: {selection['status']} after {selection['pages']} page(s), "
          f"{len(selection['rows'])} selected"
          + (f"; {selection['issue']}" if selection['issue'] else ""))
    observed_rows = selection.get("observed_rows", selection["rows"])
    if not selection["rows"] and not observed_rows:
        raise RuntimeError(f"{region}: no valid rating-list cards ({selection['issue'] or selection['status']})")
    observed = {identity(row["detail_url"], region): row for row in observed_rows}
    selected_origins = selection.get("selected_origins", {})
    old_keys = {identity(row.get("detail_url", ""), region) for row in old}
    global_keys = {identity(row.get("detail_url", "")) for row in corpus}
    stamp = _now()
    retained: dict[int, dict] = {id(row): dict(row) for row in old} if append_only else {}
    departures: list[dict] = []
    pending: list[dict] = []
    selected_score_missing: list[dict] = []
    duplicates: list[str] = []
    counts = {"list_patched": 0, "old_verified": 0, "new_added": 0,
              "removed_low_rating": 0, "removed_closed": 0, "pending": 0,
              "skipped_duplicate": 0}
    detail_attempts = 0
    checkpoint = getattr(session, "resume_checkpoints", None)

    async def fetch_detail(url: str) -> dict:
        return (await checkpoint.detail(session, url) if checkpoint is not None
                else await scrape_all.fetch_detail(session, url))

    def detail_progress(phase: str, row: dict) -> None:
        nonlocal detail_attempts
        detail_attempts += 1
        if detail_attempts == 1 or detail_attempts % 10 == 0:
            print(f"[{region}] detail {detail_attempts} ({phase}): "
                  f"{row.get('name') or row.get('detail_url')}")

    old_detail_count = 0 if append_only else len(old) if full_details else sum(_score(observed.get(identity(row.get("detail_url", ""), region), {}).get("rating")) is None for row in old)
    new_detail_count = sum(identity(row.get("detail_url", ""), region) not in global_keys for row in selection["rows"])
    list_only = 0 if append_only or full_details else len(old) - old_detail_count
    old_reason = "old full refreshes" if full_details else "old scores unresolved"
    print(f"[{region}] detail plan: {new_detail_count} new candidates, {old_detail_count} {old_reason}; "
          f"{list_only} old restaurants updated from lists")

    # Every observed old score is useful, even outside the new-entry selection.
    refreshed_old: list[dict] = []
    for old_row in ([] if append_only else old):
        key = identity(old_row.get("detail_url", ""), region)
        listed = observed.get(key)
        if listed is not None and not full_details:
            row = _patch_list(old_row, listed, stamp)
            score = _score(listed.get("rating"))
            if score is not None and score < MIN_RATING:
                departures.append(_departure(old_row, "rating_below_3.40", stamp, run_id,
                                             {"rating": score, "operating_status": "unknown"}))
                counts["removed_low_rating"] += 1
            else:
                retained[id(old_row)] = row
                counts["list_patched"] += 1
                if score is None:
                    selected_score_missing.append(old_row)
            continue
        # Full mode refreshes every old detail; bimonth reaches only unresolved rows.
        detail_progress("old", old_row)
        try:
            detail = await fetch_detail(old_row.get("detail_url", ""))
        except Exception as exc:
            fresh_score = _score((listed or {}).get("rating"))
            if fresh_score is not None and fresh_score < MIN_RATING:
                departures.append(_departure(old_row, "rating_below_3.40", stamp, run_id,
                                             {"rating": fresh_score, "operating_status": "unknown"}))
                counts["removed_low_rating"] += 1
            else:
                retained[id(old_row)] = (_patch_list(old_row, listed, stamp)
                                        if fresh_score is not None else dict(old_row, rating_check="pending"))
                if fresh_score is not None:
                    counts["list_patched"] += 1
            pending.append({"detail_url": old_row.get("detail_url"), "reason": str(exc)})
            continue
        counts["old_verified"] += 1
        status = detail.get("operating_status", "unknown")
        score = _score(detail.get("rating"))
        score_origin = "detail"
        if full_details and score is None and listed is not None:
            score = _score(listed.get("rating"))
            score_origin = "list"
        evidence = dict(detail, rating=score)
        if status in ("closed", "temporarily_closed"):
            departures.append(_departure(old_row, status, stamp, run_id, evidence))
            counts["removed_closed"] += 1
        elif score is not None and score < MIN_RATING:
            departures.append(_departure(old_row, "rating_below_3.40", stamp, run_id, evidence))
            counts["removed_low_rating"] += 1
        else:
            row = _patch_list(old_row, listed, stamp) if full_details and listed is not None else dict(old_row)
            if full_details and detail.get("address"):
                previous_policy = row.get("reservation_policy")
                for field in ("address", "seat_count", "reservation_policy"):
                    if detail.get(field) not in (None, ""):
                        row[field] = detail[field]
                if "tabelog_bookable" in detail:
                    row["tabelog_bookable"] = detail["tabelog_bookable"]
                for number, photo in enumerate((detail.get("photos") or [])[:3], 1):
                    row[f"photo{number}_url"] = photo
                if row.get("reservation_policy") != previous_policy:
                    row["reservation_policy_chinese"] = ""
                row["scraped_at"] = stamp
                refreshed_old.append(row)
            row["operating_status"] = status
            row["details_checked_at"] = stamp
            if score is not None:
                row["rating"] = f"{score:.2f}"
                row["rating_check"] = score_origin
                row["score_checked_at"] = stamp
            else:
                row["rating_check"] = "pending"
                pending.append({"detail_url": old_row.get("detail_url"), "reason": "detail_score_missing"})
            retained[id(old_row)] = row

    new_rows: list[dict] = []
    for listed in selection["rows"]:
        key = identity(listed.get("detail_url", ""), region)
        if key in old_keys:
            continue
        if key in global_keys:
            counts["skipped_duplicate"] += 1
            duplicates.append(listed["detail_url"])
            continue
        score_floor = admission_floor(selected_origins.get(key))
        if (_score(listed.get("rating")) is not None
                and _score(listed.get("rating")) < score_floor):
            continue
        detail_progress("new", listed)
        try:
            detail = await fetch_detail(listed["detail_url"])
        except Exception as exc:
            pending.append({"detail_url": listed["detail_url"], "reason": str(exc), "new": True})
            continue
        if detail.get("operating_status") in ("closed", "temporarily_closed"):
            continue
        if _score(detail.get("rating")) is not None and _score(detail.get("rating")) < score_floor:
            continue
        row = _detail_into_new(listed, detail, stamp)
        if not row.get("address"):
            pending.append({"detail_url": listed["detail_url"], "reason": "address_missing", "new": True})
            continue
        if row["rating_check"] == "pending":
            pending.append({"detail_url": listed["detail_url"], "reason": "score_missing", "new": True})
            continue
        new_rows.append(row)
        counts["new_added"] += 1

    # A selected card with no score needs a detail check too. Run these at
    # the end of the region, after ordinary new-detail work.
    for old_row in selected_score_missing:
        key = id(old_row)
        detail_progress("selected score missing", old_row)
        try:
            detail = await fetch_detail(old_row.get("detail_url", ""))
        except Exception as exc:
            pending.append({"detail_url": old_row.get("detail_url"), "reason": str(exc)})
            continue
        counts["old_verified"] += 1
        status = detail.get("operating_status", "unknown")
        score = _score(detail.get("rating"))
        if status in ("closed", "temporarily_closed"):
            retained.pop(key, None)
            departures.append(_departure(old_row, status, stamp, run_id, detail))
            counts["removed_closed"] += 1
        elif score is not None and score < MIN_RATING:
            retained.pop(key, None)
            departures.append(_departure(old_row, "rating_below_3.40", stamp, run_id, detail))
            counts["removed_low_rating"] += 1
        else:
            row = retained[key]
            row["operating_status"] = status
            row["details_checked_at"] = stamp
            if score is None:
                pending.append({"detail_url": old_row.get("detail_url"), "reason": "detail_score_missing"})
            else:
                row["rating"] = f"{score:.2f}"
                row["rating_check"] = "detail"
                row["score_checked_at"] = stamp

    if translate and (new_rows or refreshed_old):
        await scrape_all.translate_reservation_policy(new_rows + refreshed_old, run_path.parent / f"{region}_translation.csv")
    counts["pending"] = len(pending)
    report = {"region": region, "at": stamp, "selection": {k: v for k, v in selection.items() if k not in ("rows", "observed_rows", "selected_origins")},
              "counts": counts, "pending": pending, "duplicates": duplicates,
              "departures": departures, "incomplete": selection["status"] == "partial",
              "commit": "prepared", "run_id": run_id}
    # The prepared record contains complete departure rows for recovery if a
    # crash occurs between the active CSV replace and the archive append.
    merged = [_copy_row(retained[id(row)] if id(row) in retained else row, fields)
              for row in corpus
              if (row.get("region") or "").lower() != region or id(row) in retained]
    merged.extend(_copy_row(row, fields) for row in new_rows)
    report["main_meal_final"] = len({identity(row.get("detail_url", ""), region) for row in merged
                                     if (row.get("region") or "").lower() == region and _main_meal(row)
                                     and identity(row.get("detail_url", ""), region)})
    goal = selection.get("main_meal_target")
    report["main_meal_remaining"] = max(0, goal - report["main_meal_final"]) if goal is not None else None
    after_text = _csv_text(merged, fields)
    report["csv_before_sha256"] = digest_file(csv_path)
    report["csv_after_sha256"] = _csv_digest(after_text)
    report["csv_before_content"] = _file_fingerprint(csv_path)
    report["csv_after_content"] = _csv_fingerprint(after_text)
    atomic_write_json(run_path, report, indent=2)
    atomic_write_csv(csv_path, merged, fields, keep_prev=True)
    if digest_file(csv_path) != report["csv_after_sha256"]:
        raise RuntimeError(f"{region}: CSV hash differed after atomic commit")
    _append_archive(archive_path, departures)
    report["commit"] = "complete"
    report["region_after_sha256"] = region_fingerprint(read_corpus(csv_path)[1], region)
    atomic_write_json(run_path, report, indent=2)
    removed = counts["removed_low_rating"] + counts["removed_closed"]
    print(f"[{region}] committed: patched {counts['list_patched']}, "
          f"new {counts['new_added']}, removed {removed}, pending {counts['pending']}; "
          f"list {selection['status']}")
    return report
