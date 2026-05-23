"""One-shot: fill the 10 bookmarks.json entries that Wikidata couldn't
resolve. Translations are LLM-curated based on knowledge of the actual
places (Wikipedia titles use disambig parens, compound names, or
slightly different forms than the user's input).

Match key is name_src exactly. Re-running is safe: it only overwrites
entries where the four translation fields are still empty.

Run:  uv run python src/tabelog/scrape/fill_remaining_misses.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from tabelog.paths import BOOKMARKS_JSON


# name_src → {sc, tc, jp, en}
FILLS: dict[str, dict[str, str]] = {
    "梅田スカイビル 空中庭園展望台": {
        "sc": "梅田蓝天大厦 空中庭园展望台",
        "tc": "梅田藍天大廈 空中庭園展望台",
        "jp": "梅田スカイビル 空中庭園展望台",
        "en": "Umeda Sky Building Kuchu Teien Observatory",
    },
    "大山 鳥取県": {
        "sc": "大山（鸟取县）",
        "tc": "大山（鳥取縣）",
        "jp": "大山（鳥取県）",
        "en": "Mount Daisen (Tottori)",
    },
    "大山寺 鳥取県": {
        "sc": "大山寺（鸟取县）",
        "tc": "大山寺（鳥取縣）",
        "jp": "大山寺（鳥取県）",
        "en": "Daisen-ji Temple (Tottori)",
    },
    "鍵掛峠 大山": {
        "sc": "钥挂垭口（大山）",
        "tc": "鑰掛峠（大山）",
        "jp": "鍵掛峠（大山）",
        "en": "Kagikake Pass (Daisen)",
    },
    "桝水フィールドステーション": {
        "sc": "桝水原野外活动中心",
        "tc": "桝水原野外活動中心",
        "jp": "桝水フィールドステーション",
        "en": "Masumizu Field Station",
    },
    "三徳山三佛寺": {
        "sc": "三德山三佛寺",
        "tc": "三德山三佛寺",
        "jp": "三徳山三佛寺",
        "en": "Sanbutsu-ji Temple (Mt. Mitoku)",
    },
    "美山かやぶきの里": {
        "sc": "美山茅葺之里",
        "tc": "美山茅葺之里",
        "jp": "美山かやぶきの里",
        "en": "Miyama Kayabuki-no-Sato (Thatched Roof Village)",
    },
    "瑠璃渓 京都府": {
        "sc": "琉璃溪（京都府）",
        "tc": "瑠璃溪（京都府）",
        "jp": "瑠璃渓（京都府）",
        "en": "Rurikei Gorge (Kyoto)",
    },
    "浅草仲見世通り": {
        "sc": "浅草仲见世通",
        "tc": "淺草仲見世通",
        "jp": "浅草仲見世通り",
        "en": "Asakusa Nakamise-dōri",
    },
    "東京都庁展望室": {
        "sc": "东京都厅展望室",
        "tc": "東京都廳展望室",
        "jp": "東京都庁展望室",
        "en": "Tokyo Metropolitan Government Building Observation Deck",
    },
}


def main() -> None:
    entries = json.loads(BOOKMARKS_JSON.read_text(encoding="utf-8"))

    filled = 0
    skipped_not_found = []
    skipped_already_filled = []

    src_to_idx = {e.get("name_src"): i for i, e in enumerate(entries)
                  if isinstance(e, dict) and e.get("name_src")}

    for src, names in FILLS.items():
        if src not in src_to_idx:
            skipped_not_found.append(src)
            continue
        e = entries[src_to_idx[src]]
        if any(e.get(f) for f in ("name_sc", "name_tc", "name_jp", "name_en")):
            skipped_already_filled.append(src)
            continue
        e["name_sc"] = names["sc"]
        e["name_tc"] = names["tc"]
        e["name_jp"] = names["jp"]
        e["name_en"] = names["en"]
        filled += 1
        print(f"  filled: {src}")
        print(f"      sc={names['sc']}")
        print(f"      tc={names['tc']}")
        print(f"      jp={names['jp']}")
        print(f"      en={names['en']}")

    BOOKMARKS_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print()
    print(f"[fill] filled {filled} entries")
    if skipped_not_found:
        print(f"[fill] {len(skipped_not_found)} name_src not in bookmarks.json:")
        for s in skipped_not_found:
            print(f"  - {s}")
    if skipped_already_filled:
        print(f"[fill] {len(skipped_already_filled)} already had translations "
              f"(left unchanged):")
        for s in skipped_already_filled:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
