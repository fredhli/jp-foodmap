"""Selection stages used by the region loop in project main.py.

Networking/extraction stays in scrape_all; main.py owns stage order and CLI policy.
"""
from __future__ import annotations
import math
import re
from urllib.parse import urlsplit
from tabelog.scrape import scrape_all, scrape_topup
from tabelog.scrape.audit_main_meal_coverage import is_main_meal
MIN_RATING = 3.40
MAIN_MEAL_SCORE_TARGET = 3.50
MAX_LIST_PAGES = 60


def _score(value) -> float | None:
    return scrape_all.parse_detail_rating(value)

def admission_floor(origins):
    """Base/count-based entries keep 3.40; depth-only additions require 3.50."""
    origins = set(origins or ())
    if origins and origins <= {'special', 'main_meal_depth'}:
        return MAIN_MEAL_SCORE_TARGET
    return MIN_RATING


def identity(url: str, region: str | None=None) -> str:
    """Match a real Tabelog restaurant ID without changing the stored URL."""
    parsed = urlsplit((url or '').strip())
    if parsed.scheme.lower() not in ('http', 'https') or (parsed.hostname or '').lower() not in ('tabelog.com', 'www.tabelog.com'):
        return ''
    match = re.fullmatch('/([a-z][a-z0-9_-]*)/(?:A\\d+/){1,2}(\\d{6,10})/?', parsed.path)
    if not match or (region is not None and match.group(1) != region):
        return ''
    return 'tabelog:' + match.group(2)

class _RankedScan:
    """One ordered query, with a cursor independent of historical source_page."""

    def __init__(self, session, region: str, *, start_page: int=1, list_url: str | None=None, require_total: bool=True):
        self.session, self.region = (session, region)
        self.next_page = start_page
        self.list_url = list_url
        self.require_total = require_total
        self.total = None
        self.last_page = 0
        self.pages = 0
        self.status = 'running'
        self.issue = ''
        self.fingerprints: set[tuple[str, ...]] = set()
        self.previous_score = None
        self.page_limit_warning = None

    async def read(self) -> list[dict] | None:
        if self.status != 'running':
            return None
        if self.next_page > MAX_LIST_PAGES:
            self.status = 'page_limit'
            kind = '特殊筛选榜' if self.list_url else '普通评分榜'
            last = f'{self.previous_score:.2f}' if self.previous_score is not None else '未知'
            band_complete = self.previous_score is not None and self.previous_score < MAIN_MEAL_SCORE_TARGET
            coverage = ('已读过3.50分段，但后续页面仍被截断。' if band_complete else
                        '尚未读到低于3.50的分数，3.50同分段可能未完整覆盖。')
            message = (f'[{self.region}] {kind}达到第{MAX_LIST_PAGES}页上限，停止继续翻页；'
                       f'最后有效分数 {last}。{coverage}')
            self.page_limit_warning = {
                'code': 'page_limit', 'region': self.region,
                'query': self.list_url or scrape_all.BASE_TEMPLATE,
                'last_page': self.last_page, 'last_score': self.previous_score,
                'target_band_complete': band_complete, 'message': message,
            }
            print('[WARNING] ' + message)
            return None
        page = self.next_page
        try:
            template = (scrape_topup.append_list_page_url(self.list_url, self.region, page)
                        .replace('{', '{{').replace('}', '}}') if self.list_url else scrape_all.BASE_TEMPLATE)
            checkpoint = getattr(self.session, 'resume_checkpoints', None)
            if checkpoint is None:
                if self.list_url:
                    rows, total = await scrape_all.scrape_list_page(self.session, self.region, page, template)
                else:
                    rows, total = await scrape_all.scrape_list_page(self.session, self.region, page)
            else:
                rows, total = await checkpoint.list_page(self.session, self.region, page, template)
        except Exception as exc:
            self.status, self.issue = ('partial', f'page {page}: {exc}')
            return None
        self.last_page = page
        self.pages += 1
        self.next_page = page + 1
        valid_total = isinstance(total, int) and (not isinstance(total, bool)) and (total >= 0)
        if self.pages == 1:
            self.total = total if valid_total else None
            if self.require_total and (self.total is None or self.total == 0):
                self.status, self.issue = ('partial', 'first page had no valid region total; quota cannot be verified')
                return None
        if not rows:
            if self.total is not None and page > 1 and ((page - 1) * 20 >= self.total):
                self.status = 'exhausted'
            else:
                self.status, self.issue = ('partial', f'page {page} unexpectedly had no cards')
            return None
        keys = [identity(row.get('detail_url', ''), self.region) for row in rows]
        if not all(keys):
            self.status, self.issue = ('partial', f'page {page} had invalid restaurant URLs')
            return None
        fingerprint = tuple(sorted(keys))
        if fingerprint in self.fingerprints:
            self.status, self.issue = ('partial', f'page {page} repeated an earlier page')
            return None
        previous = self.previous_score
        scores = [_score(row.get('rating')) for row in rows]
        if not any((score is not None for score in scores)):
            self.status, self.issue = ('partial', f'page {page} had no usable ratings')
            return None
        for score in scores:
            if score is None:
                continue
            if previous is not None and score > previous + 0.001:
                self.status, self.issue = ('partial', f'page {page} broke descending rating order')
                return None
            previous = score
        self.fingerprints.add(fingerprint)
        self.previous_score = previous
        if checkpoint is not None:
            checkpoint.accept_list_page(self.region, page, template, rows, total)
        return rows

def _main_meal(row: dict, *, require_score: bool=False) -> bool:
    score = _score(row.get('rating'))
    return is_main_meal(row.get('genre') or '') and (not require_score or (score is not None and score >= MIN_RATING))

def _supplement_eligible(row: dict) -> bool:
    prices = {}
    for field in ('dinner_upper', 'lunch_upper'):
        try:
            prices[field] = int(row[field]) if row.get(field) not in (None, '') else None
        except (TypeError, ValueError):
            prices[field] = None
    return is_main_meal(row.get('genre') or '') and (not scrape_all._is_fine_dine(prices)) and (not scrape_topup._is_cheap_eats(prices))

class RegionSelection:
    """State shared across base, special-list and ordinary-supplement stages."""

    def __init__(self, session, region, top_pct, hard_cap, fine_dine_pct, *, existing_rows=(), main_meal_ratio=0.008, main_meal_cap=300, special_lists=True, special_urls=None, special_start_pages=None):
        self.session = session
        self.region = region
        self.top_pct = top_pct
        self.hard_cap = hard_cap
        self.fine_dine_pct = fine_dine_pct
        self.existing_rows = existing_rows
        self.main_meal_ratio = main_meal_ratio
        self.main_meal_cap = main_meal_cap
        self.special_lists = special_lists
        self.special_urls = special_urls or {}
        self.special_start_pages = special_start_pages or {}
        self.known_ids = {identity(row.get('detail_url', '')) for row in self.existing_rows} - {''}
        self.population = {identity(row.get('detail_url', ''), self.region): dict(row) for row in self.existing_rows if (row.get('region') or '').lower() == self.region and identity(row.get('detail_url', ''), self.region)}
        self.old_ids = set(self.population)
        self.meal_ids = {key for key, row in self.population.items() if _main_meal(row)}
        self.observed: dict[str, dict] = {}
        self.score_samples: dict[str, set[float]] = {}
        self.score_sources: dict[str, list[dict]] = {}
        self.selected: dict[str, dict] = {}
        self.origins: dict[str, list[str]] = {}
        self.base_ids: set[str] = set()
        self.normal_ids: set[str] = set()
        self.fine_count = 0
        self.target = self.fine_cap = self.meal_target = None
        self.normal = _RankedScan(self.session, self.region)
        self.tail: list[dict] = []
        self.special_report = None
        self.main_meal_depth_started = False
        self.main_meal_depth_start_min = None
        self.main_meal_depth_ids: set[str] = set()
        self.ordinary_candidates: dict[str, dict] = {}
        self.warnings: list[dict] = []

    def score_conflicts_with(self, key, floor):
        scores = self.score_samples.get(key, set())
        return bool(scores) and min(scores) < floor <= max(scores)

    def update_population(self, key, row):
        merged = dict(self.population.get(key, {}))
        for field, value in row.items():
            if value not in (None, '') and (field != 'rating' or _score(value) is not None):
                merged[field] = value
        self.population[key] = merged
        score_confirmed = _score(row.get('rating')) is not None and (not self.score_conflicts_with(key, MIN_RATING))
        if _main_meal(merged, require_score=key not in self.old_ids or score_confirmed):
            self.meal_ids.add(key)
        else:
            self.meal_ids.discard(key)

    def observe(self, rows):
        for row in rows:
            key = identity(row['detail_url'], self.region)
            score = _score(row.get('rating'))
            if score is not None:
                self.score_samples.setdefault(key, set()).add(score)
                self.score_sources.setdefault(key, []).append({'rating': score, 'source_query': row.get('source_query') or 'rating', 'source_page': row.get('source_page')})
            if key not in self.observed or score is not None:
                self.observed[key] = dict(row)
            if key in self.population:
                self.update_population(key, row)
            if key in self.selected and _score(row.get('rating')) is not None:
                self.selected[key] = dict(row)

    def choose(self, row, origin):
        key = identity(row['detail_url'], self.region)
        self.origins.setdefault(key, [])
        if origin not in self.origins[key]:
            self.origins[key].append(origin)
        self.selected.setdefault(key, dict(row))
        self.update_population(key, row)
        return key

    def missing_old(self):
        return self.old_ids - {key for key, row in self.observed.items() if _score(row.get('rating')) is not None and (not self.score_conflicts_with(key, MIN_RATING))}

    def unseen_old(self):
        return self.old_ids - self.observed.keys()

    def need_main(self):
        return self.meal_target is not None and len(self.meal_ids) < self.meal_target and (len(self.normal_ids) < self.main_meal_cap)

    def population_min_rating(self):
        scores = []
        for key, row in self.population.items():
            score = _score(row.get('rating'))
            if score is None:
                continue
            # A fresh, unambiguous sub-3.40 score already schedules retirement.
            observed_score = _score(self.observed.get(key, {}).get('rating'))
            if (observed_score is not None and observed_score < MIN_RATING
                    and not self.score_conflicts_with(key, MIN_RATING)):
                continue
            scores.append(score)
        return min(scores) if scores else None

    def maybe_start_main_meal_depth(self):
        if (self.main_meal_depth_started or self.need_main()
                or self.main_meal_ratio <= 0 or self.main_meal_cap <= 0
                or self.target is None or len(self.base_ids) < self.target):
            return
        minimum = self.population_min_rating()
        if minimum is not None and minimum > MAIN_MEAL_SCORE_TARGET:
            self.main_meal_depth_started = True
            self.main_meal_depth_start_min = minimum
            print(f'[{self.region}] 正餐数量阶段已结束，但地区最低分仍为 {minimum:.2f}；'
                  '继续正餐补爬至包含3.50同分段或第60页（延伸阶段不受原新增数量上限限制）。')

    def main_meal_depth_complete(self):
        return (self.normal.previous_score is not None
                and self.normal.previous_score < MAIN_MEAL_SCORE_TARGET)

    def need_main_meal_depth(self):
        self.maybe_start_main_meal_depth()
        return self.main_meal_depth_started and not self.main_meal_depth_complete()

    def supplement(self, rows):
        for row in rows:
            self.ordinary_candidates[identity(row['detail_url'], self.region)] = dict(row)
        # Preserve the original count-based layer, including its 300-new-ID cap.
        for row in rows:
            if not self.need_main():
                break
            key = identity(row['detail_url'], self.region)
            row = self.observed.get(key, row)
            score = _score(row.get('rating'))
            if (key in self.known_ids or key in self.selected or score is None
                    or (score < MIN_RATING and not self.score_conflicts_with(key, MIN_RATING))):
                continue
            if _supplement_eligible(row):
                self.normal_ids.add(self.choose(row, 'main_meal'))
        self.maybe_start_main_meal_depth()
        if not self.main_meal_depth_started:
            return
        # Revisit already-read tails/coverage pages once the depth condition is
        # triggered. Processing the complete page keeps all qualifying 3.50 ties.
        for key, cached in self.ordinary_candidates.items():
            row = self.observed.get(key, cached)
            score = _score(row.get('rating'))
            if (key in self.known_ids or key in self.selected or score is None
                    or (score < MAIN_MEAL_SCORE_TARGET
                        and not self.score_conflicts_with(key, MAIN_MEAL_SCORE_TARGET))):
                continue
            if _supplement_eligible(row):
                self.main_meal_depth_ids.add(self.choose(row, 'main_meal_depth'))

    async def collect_base(self):
        """Fill the base quota and observe every card on its final page."""
        while self.normal.status == 'running':
            rows = await self.normal.read()
            if rows is None:
                break
            if self.target is None:
                self.target = min(round(self.normal.total * self.top_pct / 100), self.hard_cap)
                self.fine_cap = max(round(self.normal.total * self.fine_dine_pct / 100), scrape_all.FINE_DINE_MIN_CAP)
                self.meal_target = math.ceil(self.normal.total * self.main_meal_ratio)
            self.observe(rows)
            for index, row in enumerate(rows):
                if len(self.base_ids) >= self.target:
                    self.tail = rows[index:]
                    break
                key = identity(row['detail_url'], self.region)
                score = _score(row.get('rating'))
                if key in self.base_ids or (score is not None and score < MIN_RATING):
                    continue
                if scrape_all._is_fine_dine(row):
                    if self.fine_count >= self.fine_cap:
                        continue
                    self.fine_count += 1
                self.base_ids.add(self.choose(row, 'base'))
            print(f'[{self.region}] base page {self.normal.last_page}: {len(rows)} cards, selected {len(self.base_ids)}/{self.target}, fine dining {self.fine_count}/{self.fine_cap}, old scores {len(self.old_ids) - len(self.missing_old())}/{len(self.old_ids)}')
            if any((_score(row.get('rating')) is not None and _score(row['rating']) < MIN_RATING for row in rows)):
                self.normal.status = 'score_floor'
            if len(self.base_ids) >= self.target:
                break

    async def collect_special(self):
        """Read the approved Tokyo/Osaka query without spending ordinary slots."""
        if self.special_lists and self.region in ('tokyo', 'osaka'):
            url = scrape_topup.TOKYO_APPEND_LIST_URL if self.region == 'tokyo' else scrape_topup.OSAKA_APPEND_LIST_URL
            url = scrape_topup.resolve_list_url_date(self.special_urls.get(self.region) or url)
            start = scrape_topup.append_start_page(url, self.region, self.special_start_pages.get(self.region))
            special = _RankedScan(self.session, self.region, start_page=start, list_url=url, require_total=False)
            before = set(self.selected)
            while special.status == 'running':
                rows = await special.read()
                if rows is None:
                    break
                self.observe(rows)
                for row in rows:
                    key = identity(row['detail_url'], self.region)
                    score = _score(row.get('rating'))
                    if key in self.known_ids or score is None or score < 3.5:
                        continue
                    if _supplement_eligible(row):
                        self.choose(row, 'special')
                print(f'[{self.region}] filtered page {special.last_page}: new candidates {len(set(self.selected) - before)}, old scores {len(self.old_ids) - len(self.missing_old())}/{len(self.old_ids)}')
                if any((_score(row.get('rating')) is not None and _score(row['rating']) < 3.5 for row in rows)):
                    special.status = 'score_floor'
            self.special_report = {
                'start_page': start, 'last_page': special.last_page, 'pages': special.pages,
                'status': special.status, 'issue': special.issue,
                'last_score': special.previous_score,
                'new_candidates': len(set(self.selected) - before),
            }
            if special.page_limit_warning:
                self.warnings.append(special.page_limit_warning)

    async def complete_main_meals_and_coverage(self):
        """Fill the remaining meal deficit and collect still-unseen old scores."""
        self.supplement(self.tail)
        while self.normal.status == 'running' and (self.need_main() or self.need_main_meal_depth() or self.unseen_old()):
            rows = await self.normal.read()
            if rows is None:
                break
            self.observe(rows)
            self.supplement(rows)
            print(f'[{self.region}] coverage page {self.normal.last_page}: main meals {len(self.meal_ids)}/{self.meal_target}, new supplement {len(self.normal_ids)}/{self.main_meal_cap}, depth additions {len(self.main_meal_depth_ids)}, old scores {len(self.old_ids) - len(self.missing_old())}/{len(self.old_ids)}')
            if any((_score(row.get('rating')) is not None and _score(row['rating']) < MIN_RATING for row in rows)):
                self.normal.status = 'score_floor'
        if self.normal.status == 'running':
            capped = self.meal_target is not None and len(self.meal_ids) < self.meal_target and (len(self.normal_ids) >= self.main_meal_cap)
            self.normal.status = ('depth_score_floor' if self.main_meal_depth_started and self.main_meal_depth_complete()
                                  else 'supplement_cap' if capped else 'quota_reached')
        if self.normal.page_limit_warning:
            self.warnings.append(self.normal.page_limit_warning)
        minimum = self.population_min_rating()
        if (self.main_meal_depth_started and minimum is not None and minimum > MAIN_MEAL_SCORE_TARGET
                and (self.main_meal_depth_complete() or self.normal.status == 'exhausted')):
            message = (f'[{self.region}] 已读过3.50分段或读完榜单，但符合当前筛选的餐厅集最低分仍为 '
                       f'{minimum:.2f}；未强行加入不符合正餐/价格条件的餐厅。')
            warning = {'code': 'qualified_min_above_target', 'region': self.region,
                       'minimum': minimum, 'message': message}
            self.warnings.append(warning)
            print('[WARNING] ' + message)

    def result(self, *, append_only=False):
        issues = [scan_issue for scan_issue in (self.normal.issue, (self.special_report or {}).get('issue')) if scan_issue]
        quota_short = self.target is None or len(self.base_ids) < self.target or self.need_main()
        status = 'partial' if issues else 'truncated' if self.normal.status == 'page_limit' and quota_short else self.normal.status
        if append_only:
            status = 'partial' if issues else (self.special_report or {}).get('status', 'skipped')
        conflicts = {}
        for key in self.old_ids | self.selected.keys():
            floor = MIN_RATING if key in self.old_ids else admission_floor(self.origins.get(key))
            if self.score_conflicts_with(key, floor):
                conflicts[key] = {'scores': sorted(self.score_samples[key]), 'threshold': floor, 'observations': self.score_sources[key]}
        stale_main = len(self.meal_ids & self.missing_old())
        selected_rows = [dict(row, rating=None) if key in conflicts else row for key, row in self.selected.items()]
        observed_rows = [dict(row, rating=None) if key in conflicts else row for key, row in self.observed.items()]
        return {
            'rows': selected_rows,
            'observed_rows': observed_rows,
            'score_conflicts': conflicts,
            'selected_origins': self.origins,
            'total': self.normal.total,
            'target': self.target,
            'base_selected': len(self.base_ids),
            'fine_dine_count': self.fine_count,
            'main_meal_target': self.meal_target,
            'main_meal_candidates': len(self.meal_ids),
            'main_meal_new': len(self.normal_ids),
            'main_meal_cap': self.main_meal_cap,
            'main_meal_depth_started': self.main_meal_depth_started,
            'main_meal_depth_start_min': self.main_meal_depth_start_min,
            'main_meal_depth_new': len(self.main_meal_depth_ids),
            'main_meal_depth_complete': self.main_meal_depth_started and self.main_meal_depth_complete(),
            'main_meal_depth_status': ('not_started' if not self.main_meal_depth_started
                                       else 'score_floor' if self.main_meal_depth_complete() else self.normal.status),
            'region_candidate_min_rating': self.population_min_rating(),
            'warnings': self.warnings,
            'unverified_retained_main_meals': stale_main,
            'old_score_covered': len(self.old_ids) - len(self.missing_old()),
            'old_score_missing': len(self.missing_old()),
            'observed_count': len(self.observed),
            'pages': self.normal.pages,
            'plain_status': self.normal.status,
            'special': self.special_report,
            'status': status,
            'issue': '; '.join(issues),
        }
