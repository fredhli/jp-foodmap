#!/usr/bin/env python3
"""Pipeline regression tests — M-019 / M-020 / M-094 / M-096 / M-172.

    uv run python tests/pipeline/run.py            # all
    uv run python tests/pipeline/run.py t_merge    # one test by name

Imports the REAL modules (tabelog.scrape.scrape_all, tabelog.scrape.map) —
never a copy — so the assertions track the shipped code. Everything is
written under a per-run temp directory; the master CSV and the real geocode
cache are only ever READ, and only when they happen to exist (a fresh clone
has neither, in which case those tests are skipped, not failed).

No network. No Playwright. Runs in about a second.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from tabelog.paths import TABELOG_CSV, GEOCODE_CACHE  # noqa: E402
from tabelog.scrape import scrape_all  # noqa: E402
from tabelog import atomic  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="tabelog-pipeline-"))

_registry: list = []
_skips: list[str] = []


def test(fn):
    _registry.append(fn)
    return fn


class Skip(Exception):
    pass


def eq(actual, expected, what: str) -> None:
    if actual != expected:
        raise AssertionError(f"{what}: expected {expected!r}, got {actual!r}")


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def master_head(n: int) -> tuple[list[str], list[dict]]:
    """First n rows of the REAL master CSV, read-only. Skips when the file
    isn't present (fresh clone — data/ is gitignored)."""
    if not TABELOG_CSV.exists():
        raise Skip(f"{TABELOG_CSV} not present (data/ is gitignored)")
    with TABELOG_CSV.open(encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        fields = list(r.fieldnames or [])
        rows = []
        for row in r:
            rows.append(row)
            if len(rows) >= n:
                break
    return fields, rows


def phase1_row(old: dict) -> dict:
    """What scrape_list_page produces for an already-known restaurant: list
    columns only. lat/lon/photos/address/policy are structurally absent."""
    return {
        "region": old.get("region", ""),
        "rank": old.get("rank", ""),
        "name": old.get("name", ""),
        "rating": old.get("rating", ""),
        "review_count": old.get("review_count", ""),
        "save_count": old.get("save_count", ""),
        "awards": old.get("awards", ""),
        "genre": old.get("genre", ""),
        "station": old.get("station", ""),
        "station_distance_m": old.get("station_distance_m", ""),
        "dinner_upper": old.get("dinner_upper", ""),
        "lunch_upper": old.get("lunch_upper", ""),
        "holiday": old.get("holiday", ""),
        "seat_count": "", "address": "", "reservation_policy": "",
        "reservation_policy_chinese": "", "tabelog_bookable": "",
        "detail_url": old.get("detail_url", ""),
        "source_page": old.get("source_page", ""),
        "scraped_at": "2026-09-05T00:00:00Z",
    }


# --------------------------------------------------------------------------
# (a) M-019: a new row whose address came back empty must not clobber the old
# --------------------------------------------------------------------------
@test
def t_empty_address_keeps_old_row():
    fields, rows = master_head(4)
    work = TMP / "a"
    work.mkdir(parents=True, exist_ok=True)
    csv_path = work / "tabelog.csv"
    write_csv(csv_path, fields, rows)

    # Every detail page failed => phase-1 rows with a blank address.
    new_rows = [phase1_row(r) for r in rows]
    ledger = work / "failed.jsonl"
    orig_ledger = scrape_all.SCRAPE_FAILED_LEDGER
    scrape_all.SCRAPE_FAILED_LEDGER = ledger
    try:
        # 100% empty is over the 5% gate, so raise the gate for THIS test —
        # we are asserting the per-row rule, not the batch rule (t_gate does).
        scrape_all.append_and_dedupe(new_rows, csv_path, max_empty_address_pct=100.0)
    finally:
        scrape_all.SCRAPE_FAILED_LEDGER = orig_ledger

    _, after = read_csv(csv_path)
    eq(len(after), len(rows), "row count unchanged")
    for before_row, after_row in zip(rows, after):
        eq(after_row["address"], before_row["address"], "address preserved")
        eq(after_row["lat"], before_row["lat"], "lat preserved")
        eq(after_row["photo1_url"], before_row["photo1_url"], "photo preserved")
    entries = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    eq(len(entries), len(rows), "ledger entries")
    eq(entries[0]["reason"], "empty_address", "ledger reason")


# --------------------------------------------------------------------------
# (b) M-172: empty lat/lon/photo* keep the old value; awards can go empty
# --------------------------------------------------------------------------
@test
def t_whitelist_merge():
    fields, rows = master_head(4)
    work = TMP / "b"
    work.mkdir(parents=True, exist_ok=True)
    csv_path = work / "tabelog.csv"
    # Force a known non-empty award on the first row so "cleared" is meaningful.
    rows[0]["awards"] = '["\\u767e\\u540d\\u5e97"]'
    write_csv(csv_path, fields, rows)

    new_rows = []
    for r in rows:
        n = phase1_row(r)
        n["address"] = r["address"] or "東京都千代田区1-1-1"  # detail page OK
        n["awards"] = ""                                      # award lapsed
        new_rows.append(n)
    scrape_all.append_and_dedupe(new_rows, csv_path)

    _, after = read_csv(csv_path)
    eq(len(after), len(rows), "row count unchanged")
    for before_row, after_row in zip(rows, after):
        eq(after_row["lat"], before_row["lat"], "lat preserved (M-172)")
        eq(after_row["lon"], before_row["lon"], "lon preserved (M-172)")
        for k in ("photo1_url", "photo2_url", "photo3_url"):
            eq(after_row[k], before_row[k], f"{k} preserved")
        eq(after_row["reservation_policy_chinese"],
           before_row["reservation_policy_chinese"], "policy_zh preserved")
        eq(after_row["awards"], "", "awards CLEARED (not blanket-merged)")
    eq(after[0]["scraped_at"], "2026-09-05T00:00:00Z", "scraped_at written (M-096)")


# --------------------------------------------------------------------------
# (c) M-019: 6% empty-address batch is refused, master CSV untouched, exit!=0
# --------------------------------------------------------------------------
@test
def t_empty_address_batch_gate():
    work = TMP / "c"
    work.mkdir(parents=True, exist_ok=True)
    csv_path = work / "tabelog.csv"
    fields = list(scrape_all.FIELDS)
    old = [{k: "" for k in fields} | {"detail_url": f"https://x/{i}",
                                      "name": f"old{i}",
                                      "address": "東京都千代田区1-1-1"}
           for i in range(100)]
    write_csv(csv_path, fields, old)
    before = csv_path.read_bytes()

    new_rows = []
    for i in range(100):
        n = {k: "" for k in fields}
        n["detail_url"] = f"https://x/{i}"
        n["name"] = f"new{i}"
        n["address"] = "" if i < 6 else "東京都港区2-2-2"   # exactly 6%
        new_rows.append(n)

    ledger = work / "failed.jsonl"
    orig_ledger = scrape_all.SCRAPE_FAILED_LEDGER
    scrape_all.SCRAPE_FAILED_LEDGER = ledger
    try:
        scrape_all.append_and_dedupe(new_rows, csv_path)
    except SystemExit as e:
        code = e.code
    else:
        raise AssertionError("6% empty addresses did NOT raise SystemExit")
    finally:
        scrape_all.SCRAPE_FAILED_LEDGER = orig_ledger

    if not (isinstance(code, str) and "REFUSING" in code):
        raise AssertionError(f"expected a REFUSING message, got {code!r}")
    eq(csv_path.read_bytes(), before, "master CSV byte-identical after refusal")
    eq((work / "tabelog.csv.prev").exists(), False, "no .prev written on refusal")

    # 5% (the threshold itself) still goes through — the gate is "> pct".
    ok_rows = []
    for i in range(100):
        n = {k: "" for k in fields}
        n["detail_url"] = f"https://x/{i}"
        n["name"] = f"ok{i}"
        n["address"] = "" if i < 5 else "東京都港区2-2-2"
        ok_rows.append(n)
    scrape_all.SCRAPE_FAILED_LEDGER = ledger
    try:
        scrape_all.append_and_dedupe(ok_rows, csv_path)
    finally:
        scrape_all.SCRAPE_FAILED_LEDGER = orig_ledger
    _, after = read_csv(csv_path)
    eq(len(after), 100, "row count unchanged at 5%")
    eq(after[0]["name"], "old0", "the 5 rejected rows kept their old value")
    eq(after[5]["name"], "ok5", "the accepted rows were updated")


# --------------------------------------------------------------------------
# (d) M-020: an exception mid-write leaves the original intact + .prev exists
# --------------------------------------------------------------------------
@test
def t_atomic_write_interrupted():
    work = TMP / "d"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "tabelog.csv"
    target.write_text("original,contents\n1,2\n", encoding="utf-8-sig")
    before = target.read_bytes()

    class Boom(Exception):
        pass

    def exploding_rows():
        yield {"a": "1"}
        raise Boom("simulated crash halfway through serialisation")

    try:
        atomic.atomic_write_csv(target, exploding_rows(), ["a"], keep_prev=True)
    except Boom:
        pass
    else:
        raise AssertionError("expected the generator to blow up")
    eq(target.read_bytes(), before, "original untouched after a mid-write crash")
    eq((work / "tabelog.csv.tmp").exists(), False, "no stray .tmp left behind")

    # Now a successful write: content replaced, .prev holds the old bytes.
    atomic.atomic_write_csv(target, [{"a": "9"}], ["a"], keep_prev=True)
    eq((work / "tabelog.csv.prev").read_bytes(), before, ".prev == previous content")
    if b"9" not in target.read_bytes():
        raise AssertionError("new content not written")


# --------------------------------------------------------------------------
# (e) M-094: the legacy cache format must not trigger a re-geocode
# --------------------------------------------------------------------------
@test
def t_legacy_geocode_cache_compat():
    from tabelog.scrape import map as mapmod

    legacy = {
        # pre-2.0 positive entry: no "status" key at all
        "東京都千代田区丸の内1-1-1": {"lat": 35.68, "lon": 139.76,
                                      "matched_query": "x", "display": "丸の内一丁目"},
        # pre-2.0 negative entry: bare null
        "沖縄県国頭郡": None,
    }
    resolved, loc = mapmod._cache_lookup(legacy, "東京都千代田区丸の内1-1-1")
    eq(resolved, True, "legacy hit resolves from cache (no GSI call)")
    eq(loc["lat"], 35.68, "legacy hit returns coords")
    resolved, loc = mapmod._cache_lookup(legacy, "沖縄県国頭郡")
    eq((resolved, loc), (True, None), "legacy null resolves as a miss (no GSI call)")
    resolved, _ = mapmod._cache_lookup(legacy, "not in cache")
    eq(resolved, False, "unknown address is a cache fault")

    # New-format entries round-trip.
    fresh: dict = {}
    mapmod._cache_store_hit(fresh, "A", {"lat": 1.0, "lon": 2.0})
    mapmod._cache_store_miss(fresh, "B")
    eq(mapmod._cache_lookup(fresh, "A")[1]["lon"], 2.0, "new hit round-trips")
    eq(mapmod._cache_lookup(fresh, "B"), (True, None), "new miss round-trips")
    eq(fresh["A"]["status"], "hit", "new hit is tagged")

    # An "error" entry (which we never write, but a hand-edited cache might
    # contain) must NOT be treated as resolved.
    eq(mapmod._cache_lookup({"C": {"status": "error"}}, "C")[0], False,
       "cached error re-queries")

    # And against the REAL cache: every entry must resolve without a GSI call.
    if not GEOCODE_CACHE.exists():
        raise Skip(f"{GEOCODE_CACHE} not present (data/ is gitignored)")
    real = json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
    faults = [k for k in real if not mapmod._cache_lookup(real, k)[0]]
    eq(faults, [], f"real cache: {len(faults)} of {len(real)} entries would re-query GSI")
    hits = sum(1 for k in real if mapmod._cache_lookup(real, k)[1] is not None)
    print(f"      real cache: {len(real)} entries, {hits} hits, "
          f"{len(real) - hits} misses, 0 re-queries")


# --------------------------------------------------------------------------
# (f) M-020: load_cache refuses to continue on a corrupt cache
# --------------------------------------------------------------------------
@test
def t_load_cache_refuses_corrupt():
    from tabelog.scrape import map as mapmod

    work = TMP / "f"
    work.mkdir(parents=True, exist_ok=True)
    bad = work / "geocode_cache.json"
    bad.write_text('{"a": {"lat": 1, "lo', encoding="utf-8")  # truncated
    orig = mapmod.CACHE_PATH
    mapmod.CACHE_PATH = bad
    try:
        mapmod.load_cache()
    except SystemExit as e:
        if "not valid JSON" not in str(e.code):
            raise AssertionError(f"unexpected message: {e.code!r}")
    else:
        raise AssertionError("corrupt cache did not raise SystemExit")
    finally:
        mapmod.CACHE_PATH = orig


# --------------------------------------------------------------------------
# (g) M-096: scraped_at is in FIELDS and old rows survive without it
# --------------------------------------------------------------------------
@test
def t_scraped_at_column():
    if "scraped_at" not in scrape_all.FIELDS:
        raise AssertionError("scraped_at missing from FIELDS")
    work = TMP / "g"
    work.mkdir(parents=True, exist_ok=True)
    csv_path = work / "tabelog.csv"
    old_fields = [f for f in scrape_all.FIELDS if f != "scraped_at"]
    write_csv(csv_path, old_fields,
              [{k: "" for k in old_fields} | {"detail_url": "https://x/1",
                                              "name": "old",
                                              "address": "東京都1-1"}])
    new = {k: "" for k in scrape_all.FIELDS}
    new["detail_url"] = "https://x/2"
    new["name"] = "new"
    new["address"] = "東京都2-2"
    new["scraped_at"] = "2026-09-05T12:00:00Z"
    scrape_all.append_and_dedupe([new], csv_path)
    fields, rows = read_csv(csv_path)
    if "scraped_at" not in fields:
        raise AssertionError("scraped_at column not added to the CSV header")
    eq(rows[0]["scraped_at"], "", "pre-existing row keeps an empty stamp")
    eq(rows[1]["scraped_at"], "2026-09-05T12:00:00Z", "new row carries the stamp")

    # And the age printer tolerates both.
    from tabelog.scrape import map as mapmod
    mapmod.print_corpus_age(rows)          # must not raise
    mapmod.print_corpus_age([{"scraped_at": ""}])
    mapmod.print_corpus_age([{"scraped_at": "garbage"}])


# --------------------------------------------------------------------------
# (h) M-029: policy_struct never invents a field
# --------------------------------------------------------------------------
@test
def t_policy_struct():
    from tabelog.scrape import map as mapmod

    ps, pb = mapmod.policy_struct, mapmod.policy_b

    # b comes from the Japanese head word only, and 完全予約制 wins over the
    # 予約可 prefix test.
    eq(pb("完全予約制 …"), 2, "完全予約制 -> 2")
    eq(pb("予約不可"), 0, "予約不可 -> 0")
    eq(pb("予約可 …"), 1, "予約可 -> 1")
    eq(pb("★インターネット予約"), None, "unknown head -> no verdict")
    eq(pb(""), None, "empty -> no verdict")

    # --- JA ---------------------------------------------------------------
    eq(ps("予約可", "ja", 1), {"b": 1}, "head word alone leaves nothing to say")
    d = ps("予約可 ご予約のキャンセルは1週間前までにお願いします。", "ja", 1)
    eq(set(d), {"b", "cancel"}, "JA cancellation sentence -> cancel only")
    eq("キャンセル" in d["cancel"], True, "JA cancel keeps the original wording")
    d = ps("予約可 ご予約は3日前までにお願いします。", "ja", 1)
    eq(set(d), {"b", "lead"}, "JA lead-time sentence -> lead only")

    # --- ZH ---------------------------------------------------------------
    eq(ps("可预订", "zh-CN", 1), {"b": 1}, "SC head word alone")
    d = ps("接受预订。请提前3天预订。取消需在前一天告知。", "zh-CN", 1)
    eq(set(d), {"b", "lead", "cancel"}, "SC splits lead and cancel")
    eq(d["lead"], "请提前3天预订", "SC lead is verbatim")
    # The Traditional variant uses the same keyword table (it carries both
    # spellings), so the s2twp text classifies identically.
    d = ps("接受預訂。請提前3天預訂。取消需在前一天告知。", "zh-TW", 1)
    eq(set(d), {"b", "lead", "cancel"}, "TC splits lead and cancel too")

    # --- EN ---------------------------------------------------------------
    eq(ps("Reservations accepted", "en", 1), {"b": 1}, "EN head word alone")
    d = ps(
        "Reservations accepted. Please book 3 days in advance. "
        "Cancellations must be made the day before.",
        "en",
        1,
    )
    eq(set(d), {"b", "lead", "cancel"}, "EN splits lead and cancel")
    # An ASCII full stop only ends a sentence when whitespace follows, and
    # a handful of abbreviations keep theirs — otherwise "2.5 hours" and
    # "etc. by the day before" each split into fragments.
    d = ps(
        "Reservations accepted. The course is 2.5 hours. Please change the "
        "date, time, etc. by the day before.",
        "en",
        1,
    )
    eq(d["note"], "The course is 2.5 hours", "a decimal point is not a sentence end")
    eq(
        d["lead"],
        "Please change the date, time, etc. by the day before",
        "'etc.' keeps its full stop",
    )

    # --- nothing to say ---------------------------------------------------
    eq(ps("", "ja", None), None, "no text and no verdict -> no slot at all")
    eq(ps("   ", "en", None), None, "blank text -> no slot")
    eq(ps("", "ja", 0), {"b": 0}, "verdict with no prose is still a slot")

    # Bullet lists have no terminal punctuation; the marker is a boundary,
    # and a compound word's middle dot is not.
    d = ps(
        "完全予約制 ・キャンセルは前日までにご連絡ください ・コースは一斉スタートです",
        "ja",
        2,
    )
    eq(d["cancel"], "キャンセルは前日までにご連絡ください", "bullet ends the sentence")
    eq(d["note"], "コースは一斉スタートです", "and the next bullet is its own sentence")
    eq(
        ps("予約可 ソフト・ドリンクは無料です。", "ja", 1)["note"],
        "ソフト・ドリンクは無料です",
        "a compound word's middle dot is not a boundary",
    )

    # A sentence matching BOTH keyword families is a cancellation rule.
    d = ps("予約可 当日キャンセルはコース代金を頂戴します。", "ja", 1)
    eq(set(d), {"b", "cancel"}, "cancel wins over lead when both match")

    # note is capped, and the cap is visible.
    long_note = "予約可 " + ("あ" * 400) + "。"
    d = ps(long_note, "ja", 1)
    eq(len(d["note"]), 201, "note truncated to 200 chars + ellipsis")
    eq(d["note"].endswith("…"), True, "truncated note ends with an ellipsis")

    # Every emitted string is a substring of the input (nothing invented).
    src = "予約可 ご予約は3日前までに。キャンセルは前日まで無料です。当店は禁煙です。"
    d = ps(src, "ja", 1)
    for key in ("lead", "cancel", "note"):
        if key in d:
            for part in d[key].split("。"):
                if part and part not in src:
                    raise AssertionError(f"{key} fragment {part!r} is not in the source text")


# --------------------------------------------------------------------------
# (i) M-030: holiday / station-distance slots reject Tabelog's placeholders
# --------------------------------------------------------------------------
@test
def t_holiday_slot():
    from tabelog.scrape import map as mapmod

    hs, sm = mapmod.holiday_slot, mapmod.station_m_slot

    # 2,578 corpus rows carry the literal '-'; rendering them would put
    # "休 -" on a quarter of the cards.
    eq(hs("-"), None, "'-' is not a closing day")
    eq(hs(""), None, "empty is not a closing day")
    eq(hs(None), None, "None is not a closing day")
    eq(hs("年中無休です"), "年中無休です", "無休 kept verbatim")
    eq(hs("月曜日"), "月曜日", "曜 token kept verbatim")
    eq(hs("不定休"), "不定休", "不定休 kept verbatim")
    eq(hs("祝日の翌日"), "祝日の翌日", "祝 token kept verbatim")
    eq(hs("お問い合わせください"), None, "prose with no day token is dropped")

    eq(sm("-"), None, "'-' is not a distance")
    eq(sm(""), None, "empty is not a distance")
    eq(sm("abc"), None, "junk is not a distance")
    eq(sm("520"), 520, "plain metres")
    eq(sm("520.0"), 520, "float-ish metres are floored to int")
    eq(sm(-5), None, "negative metres are rejected")


# --------------------------------------------------------------------------
# (j) M-023: prefecture_index is an integer key, never a substring match
# --------------------------------------------------------------------------
@test
def t_prefecture_index():
    from tabelog.scrape import map as mapmod

    pi = mapmod.prefecture_index
    eq(len(mapmod._PREF_EN), 47, "_PREF_EN has one name per prefecture")
    eq(pi("東京都渋谷区神南1-1-1"), 13, "東京都 is index 13")
    eq(pi("〒150-0001 東京都渋谷区神南1-1-1"), 13, "postcode prefix is stripped")
    eq(pi("〒6008216京都府京都市下京区"), 25, "postcode without a hyphen too")
    # The reason this is an integer and not a substring test: 京都 occurs
    # inside 東京都.
    eq(pi("京都府京都市下京区"), 25, "京都府 is index 25, not 東京都")
    eq(pi("北海道札幌市中央区"), 0, "北海道 is index 0")
    eq(pi("沖縄県那覇市"), 46, "沖縄県 is index 46")
    eq(pi("Somewhere else"), None, "no prefecture -> no index")
    eq(pi(""), None, "empty -> no index")
    eq(pi(None), None, "None -> no index")

    table = mapmod.pref_l10n_table({13: 936})
    eq(len(table), 47, "PREFS table has 47 entries")
    eq(table[13]["ja"], "東京都", "PREFS is index-aligned with _PREFECTURES")
    eq(table[13]["en"], "Tokyo", "PREFS carries the romanization")
    eq(table[13]["n"], 936, "PREFS carries this build's row count")
    eq(table[2]["tc"], "岩手縣", "岩手 is not over-converted to 巖手")
    for i, entry in enumerate(table):
        if set(entry) != {"ja", "sc", "tc", "en", "n"}:
            raise AssertionError(f"PREFS[{i}] keys are {sorted(entry)}")


def main(argv: list[str]) -> int:
    wanted = argv[1:] or None
    n_pass = n_fail = n_skip = 0
    for fn in _registry:
        if wanted and fn.__name__ not in wanted:
            continue
        try:
            fn()
        except Skip as e:
            n_skip += 1
            print(f"  SKIP  {fn.__name__}: {e}")
        except Exception:
            n_fail += 1
            print(f"  FAIL  {fn.__name__}")
            traceback.print_exc()
        else:
            n_pass += 1
            print(f"  ok    {fn.__name__}")
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\npipeline: {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
