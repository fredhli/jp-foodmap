"""Offline selection/coverage regressions. Only temporary CSVs are writable."""
import contextlib
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import main as entry
from tabelog.scrape import region_selection as update, region_update, scrape_all


def record(n, *, region='aichi', genre='寿司', rating='3.60', dinner=9999, lunch=4999):
    code = {'aichi':'23','tokyo':'13','osaka':'27'}[region]
    return {'region':region, 'detail_url':f'https://tabelog.com/{region}/A{code}01/A{code}0101/{int(code)*1000000+n}/',
            'name':f'restaurant {n}', 'genre':genre, 'rating':rating,
            'dinner_upper':dinner, 'lunch_upper':lunch, 'rank':'99', 'source_page':'42',
            'source_query':'rating', 'address':'existing address', 'seat_count':'12',
            'photo1_url':'https://example.test/photo.jpg', 'scraped_at':'2026-01-01T00:00:00Z'}


def fetched(row):
    return dict(row, address='', seat_count='', photo1_url='', rank='1', source_page=1)


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory(prefix='bimonth-selection-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.csv = self.root/'corpus.csv'

    def save(self, rows):
        fields=list(dict.fromkeys(scrape_all.FIELDS + [key for row in rows for key in row]))
        with self.csv.open('w',encoding='utf-8-sig',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

    def load(self):
        with self.csv.open(encoding='utf-8-sig',newline='') as f:
            return list(csv.DictReader(f))

    async def collect(self, pages, *, existing=(), total=1000, cap=2, meal_cap=300, region='aichi'):
        calls=[]
        async def fetch(_session, _region, page):
            calls.append(page)
            if page not in pages: raise AssertionError(f'unexpected page {page}')
            return [fetched(r) for r in pages[page]], total
        with patch.object(scrape_all,'scrape_list_page',side_effect=fetch):
            result=await entry.collect_region(None,region,1,cap,0.1,
                existing_rows=existing,main_meal_cap=meal_cap,special_lists=False)
        return result,calls

    async def test_aichi_613_old_restaurants_are_covered_past_the_480_quota(self):
        # At 3.50 the new depth condition is already satisfied; this case
        # isolates old-score coverage beyond the base selection quota.
        old=[record(n,rating='3.50') for n in range(1,614)]
        cards=[record(n,rating='3.50') for n in range(1,641)]
        pages={p:cards[(p-1)*20:p*20] for p in range(1,33)}
        selection,calls=await self.collect(pages,existing=old,total=48000,cap=500)
        self.assertEqual(selection['base_selected'],480)
        self.assertEqual(selection['old_score_covered'],613)
        self.assertEqual(selection['old_score_missing'],0)
        self.assertEqual(calls,list(range(1,32)))
        self.assertEqual(len(selection['rows']),480)
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(side_effect=AssertionError('old detail must not be opened'))) as detail:
            report=await region_update.apply_region(None,'aichi',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=selection,translate=False)
        self.assertEqual(detail.await_count,0)
        self.assertEqual(report['counts']['list_patched'],613)
        self.assertEqual(len(self.load()),613)

    async def test_same_page_tail_supplements_and_second_run_does_not_inflate(self):
        old=[record(1,genre='喫茶店',rating='3.50'),record(2,genre='喫茶店',rating='3.50'),record(3,rating='3.50')]
        page=old+[record(n,rating='3.50') for n in range(4,15)]
        first,calls=await self.collect({1:page},existing=old)
        self.assertEqual(calls,[1])
        self.assertEqual(first['main_meal_target'],8)
        self.assertEqual(first['main_meal_new'],7)
        self.assertEqual(first['main_meal_candidates'],8)
        new=[r for r in first['rows'] if update.identity(r['detail_url']) not in {update.identity(x['detail_url']) for x in old}]
        second,_=await self.collect({1:page},existing=old+new)
        self.assertEqual(second['main_meal_new'],0)
        self.assertEqual(len(second['rows']),2)

    async def test_cap_counts_only_new_ids_and_coverage_continues_after_cap(self):
        old=[record(1,genre='喫茶店',rating='3.50'),record(2,genre='喫茶店',rating='3.50'),record(30,genre='喫茶店',rating='3.50')]
        rows=[record(n,genre='喫茶店' if n in (1,2,30) else '寿司',rating='3.50') for n in range(1,41)]
        result,calls=await self.collect({1:rows[:20],2:rows[20:]},existing=old,meal_cap=1)
        self.assertEqual(calls,[1,2])
        self.assertEqual(result['main_meal_new'],1)
        self.assertEqual(result['old_score_missing'],0)
        self.assertEqual(result['status'],'supplement_cap')
        self.assertEqual(len(result['rows']),3)

    async def test_count_cap_is_300_when_region_minimum_is_already_350(self):
        rows=[record(n,genre='喫茶店' if n==1 else '寿司',rating='3.50') for n in range(1,401)]
        pages={p:rows[(p-1)*20:p*20] for p in range(1,21)}
        result,calls=await self.collect(pages,total=100000,cap=1)
        self.assertEqual(result['main_meal_new'],300)
        self.assertEqual(len(result['rows']),301)
        self.assertEqual(calls[-1],16)
        self.assertEqual(result['status'],'supplement_cap')

    async def test_fine_cap_does_not_discard_observed_old_score(self):
        old=[record(n,dinner=29999 if n<=6 else 9999,rating='3.50') for n in range(1,12)]
        result,_=await self.collect({1:old},existing=old,cap=10)
        self.assertEqual(result['fine_dine_count'],5)
        self.assertEqual(len(result['rows']),10)
        self.assertEqual(result['observed_count'],11)
        self.assertEqual(result['old_score_covered'],11)
        self.assertEqual(result['status'],'quota_reached')

    async def test_floor_340_keeps_boundary_and_records_low_old_score(self):
        old=[record(1,rating='3.40'),record(2,rating='3.39')]
        result,calls=await self.collect({1:old},existing=old,total=100,cap=1)
        self.assertEqual(calls,[1])
        self.assertEqual(result['status'],'score_floor')
        self.assertEqual(len(result['rows']),1)
        self.assertEqual(len(result['observed_rows']),2)
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(side_effect=AssertionError('already observed'))):
            report=await region_update.apply_region(None,'aichi',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=result,translate=False)
        self.assertEqual(len(self.load()),1)
        self.assertEqual(report['counts']['removed_low_rating'],1)

    async def test_special_list_observes_old_even_if_price_and_genre_fail(self):
        old=[record(1,region='tokyo',genre='喫茶店'),record(2,region='tokyo',genre='うどん')]
        new=record(3,region='tokyo',rating='3.50')
        special_old=record(2,region='tokyo',genre='うどん',rating='3.49',dinner=999,lunch=None)
        calls=[]
        async def fetch(_session,_region,page,template=None):
            calls.append((page,bool(template)))
            if template:
                return [dict(fetched(new),source_query='filtered'),dict(fetched(special_old),source_query='filtered')],5000
            return [dict(fetched(old[0]),rating='4.00')],100
        with patch.object(scrape_all,'scrape_list_page',side_effect=fetch):
            result=await entry.collect_region(None,'tokyo',1,1,.1,existing_rows=old)
        self.assertEqual(calls,[(1,False),(10,True)])
        self.assertEqual(result['old_score_covered'],2)
        self.assertEqual(result['main_meal_new'],0)
        self.assertEqual(result['special']['status'],'score_floor')
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(return_value={'rating':3.50,'address':'new address'})) as detail:
            report=await region_update.apply_region(None,'tokyo',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=result,translate=False)
        detail.assert_awaited_once_with(None,new['detail_url'])
        saved={r['name']:r for r in self.load()}
        self.assertEqual(saved['restaurant 2']['rating'],'3.49')
        self.assertEqual(saved['restaurant 2']['rank'],'99')
        self.assertEqual(saved['restaurant 2']['source_page'],'42')
        self.assertEqual(saved['restaurant 2']['source_query'],'rating')
        self.assertEqual(saved['restaurant 2']['score_source_query'],'filtered')
        self.assertEqual(report['counts']['new_added'],1)

    async def test_osaka_special_starts_at_8_and_can_stop_at_60(self):
        old=[record(1,region='osaka',genre='喫茶店')]
        calls=[]
        async def fetch(_session,_region,page,template=None):
            calls.append((page,bool(template)))
            if template:
                return [fetched(record(100+page,region='osaka'))],5000
            if page == 1:
                return [fetched(old[0])],100
            return [fetched(record(999,region='osaka',genre='喫茶店',rating='3.49'))],100
        with patch.object(scrape_all,'scrape_list_page',side_effect=fetch):
            result=await entry.collect_region(None,'osaka',1,1,.1,existing_rows=old)
        self.assertEqual([p for p,special in calls if special],list(range(8,61)))
        self.assertEqual(result['special']['status'],'page_limit')
        self.assertNotIn(result['status'],('partial','truncated'))
        self.assertEqual(result['main_meal_new'],0)
        self.assertEqual(result['old_score_covered'],1)

    def test_current_genres_and_original_unknown_price_behavior(self):
        for genre in ('うどん','そば','ラーメン','イタリアン','フレンチ','居酒屋','カフェ'):
            self.assertFalse(update._supplement_eligible(record(1,genre=genre)),genre)
        self.assertTrue(update._supplement_eligible(record(1,genre='寿司',dinner=None,lunch=None)))
        self.assertTrue(update._supplement_eligible(record(1,dinner=None,lunch=3999)))
        self.assertFalse(update._supplement_eligible(record(1,dinner=3000)))
        self.assertFalse(update._supplement_eligible(record(1,dinner=9999,lunch=20000)))

    async def test_observed_missing_score_uses_detail_without_scanning_to_60(self):
        old=[record(1,rating='3.50'),record(2,rating='3.50')]
        mixed=[dict(old[0],rating=''),old[1]]
        result,calls=await self.collect({1:mixed},existing=old,total=100,cap=1)
        self.assertEqual(calls,[1])
        self.assertEqual(result['old_score_missing'],1)
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(return_value={'rating':3.55})) as detail:
            await region_update.apply_region(None,'aichi',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=result,translate=False)
        detail.assert_awaited_once_with(None,old[0]['detail_url'])

    async def test_cross_query_boundary_conflict_requires_detail_before_removal(self):
        old=[record(1,region='tokyo',rating='3.60')]
        async def fetch(_session,_region,page,template=None):
            return ([fetched(dict(old[0],rating='3.39',source_query='filtered'))],100) if template else ([fetched(dict(old[0],rating='3.40'))],100)
        with patch.object(scrape_all,'scrape_list_page',side_effect=fetch):
            selection=await entry.collect_region(None,'tokyo',1,1,.1,existing_rows=old)
        key=update.identity(old[0]['detail_url'])
        self.assertEqual(selection['score_conflicts'][key]['scores'],[3.39,3.4])
        self.assertIsNone(selection['observed_rows'][0]['rating'])
        self.assertEqual(selection['old_score_missing'],1)
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(side_effect=RuntimeError('unavailable'))) as detail:
            report=await region_update.apply_region(None,'tokyo',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=selection,translate=False)
        detail.assert_awaited_once_with(None,old[0]['detail_url'])
        self.assertEqual(self.load()[0]['rating'],'3.60')
        self.assertFalse((self.root/'archive.jsonl').exists())
        self.assertEqual(report['counts']['pending'],1)

    async def test_special_new_score_conflict_is_verified_not_silently_dropped(self):
        old=[record(1,region='tokyo',genre='喫茶店',rating='3.80'),record(2,region='tokyo',genre='喫茶店')]
        candidate=record(3,region='tokyo',rating='3.50')
        async def fetch(_session,_region,page,template=None):
            if template:
                return [fetched(candidate),fetched(record(4,region='tokyo',rating='3.49',genre='喫茶店'))],100
            if page==1:return [fetched(old[0])],100
            return [fetched(dict(candidate,rating='3.49')),fetched(dict(old[1],rating='3.48'))],100
        with patch.object(scrape_all,'scrape_list_page',side_effect=fetch):
            selection=await entry.collect_region(None,'tokyo',1,1,.1,existing_rows=old)
        key=update.identity(candidate['detail_url'])
        self.assertEqual(selection['score_conflicts'][key]['threshold'],3.50)
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(return_value={'rating':3.50,'address':'new address'})) as detail:
            report=await region_update.apply_region(None,'tokyo',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=selection,translate=False)
        detail.assert_awaited_once_with(None,candidate['detail_url'])
        self.assertEqual(report['counts']['new_added'],1)

    async def test_unverified_low_history_counts_while_retained(self):
        old=[record(1,genre='喫茶店'),record(2,rating='3.39')]
        pages={1:[old[0],record(3,genre='喫茶店',rating='3.38')]}
        result,_=await self.collect(pages,existing=old,total=100,cap=1)
        self.assertEqual(result['main_meal_candidates'],1)
        self.assertEqual(result['unverified_retained_main_meals'],1)
        self.assertEqual(result['main_meal_new'],0)
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(side_effect=RuntimeError('unavailable'))):
            report=await region_update.apply_region(None,'aichi',self.csv,self.root/'archive.jsonl',self.root/'report.json',
                'fixture',selection=result,translate=False)
        self.assertEqual(report['main_meal_final'],1)
        self.assertEqual(report['main_meal_remaining'],0)

    async def test_base_quota_exact_rounding_allows_zero_and_python_half_even(self):
        card=record(1)
        async def first_page(_session, _region, page):
            self.assertEqual(page,1)
            return [fetched(card)],49
        with patch.object(scrape_all,'scrape_list_page',side_effect=first_page):
            zero=await entry.collect_region(None,'aichi',1,500,.1,main_meal_ratio=0,special_lists=False)
        self.assertEqual(zero['target'],0)
        self.assertEqual(zero['base_selected'],0)
        self.assertEqual(zero['rows'],[])

        cards=[record(n) for n in (1,2,3)]
        async def two_and_half(_session, _region, page):
            self.assertEqual(page,1)
            return [fetched(card) for card in cards],250
        with patch.object(scrape_all,'scrape_list_page',side_effect=two_and_half):
            rounded=await entry.collect_region(None,'aichi',1,500,.1,main_meal_ratio=0,special_lists=False)
        self.assertEqual(rounded['target'],2)  # Python round(2.5) is 2.
        self.assertEqual(rounded['base_selected'],2)
        self.assertEqual(len(rounded['rows']),2)

    async def test_final_main_meal_count_uses_id_union_not_area_alias_rows(self):
        first=record(1)
        alias=dict(first,detail_url=first['detail_url'].replace('A230101/','A230102/'))
        self.save([first,alias])
        selection={'rows':[], 'observed_rows':[fetched(first)], 'status':'quota_reached',
                   'issue':'', 'pages':1, 'main_meal_target':2}
        with patch.object(scrape_all,'fetch_detail',AsyncMock(side_effect=AssertionError('old score already observed'))) as detail:
            report=await region_update.apply_region(None,'aichi',self.csv,
                self.root/'alias-archive.jsonl',self.root/'alias-report.json','fixture',
                selection=selection,translate=False)
        detail.assert_not_awaited()
        self.assertEqual(len(self.load()),2)
        self.assertEqual(report['main_meal_final'],1)
        self.assertEqual(report['main_meal_remaining'],1)

    async def test_cached_tail_uses_later_observation_before_selecting_candidate(self):
        old=[record(1,region='tokyo',genre='喫茶店',rating='4.00')]
        candidate=record(2,region='tokyo',rating='3.40')
        async def fetch(_session,_region,page,template=None):
            if template:return [fetched(dict(candidate,rating='3.39'))],100
            if page==1:return [fetched(old[0]),fetched(candidate)],100
            return [fetched(record(3,region='tokyo',rating='3.38',genre='喫茶店'))],100
        with patch.object(scrape_all,'scrape_list_page',side_effect=fetch):
            result=await entry.collect_region(None,'tokyo',1,1,.1,existing_rows=old)
        self.assertEqual(result['main_meal_new'],1)
        self.assertEqual(len(result['rows']),2)
        key=update.identity(candidate['detail_url'])
        self.assertEqual(result['score_conflicts'][key]['threshold'],3.40)
        self.assertIsNone(next(r for r in result['rows'] if update.identity(r['detail_url'])==key)['rating'])
        self.save(old)
        with patch.object(scrape_all,'fetch_detail',AsyncMock(return_value={'rating':3.40,'address':'new address'})) as detail:
            report=await region_update.apply_region(None,'tokyo',self.csv,
                self.root/'tail-archive.jsonl',self.root/'tail-report.json','fixture',
                selection=result,translate=False)
        detail.assert_awaited_once_with(None,candidate['detail_url'])
        self.assertEqual(report['counts']['new_added'],1)

    def test_cli_defaults_enable_both_supplement_rules(self):
        args=entry.parse_args(['aichi','--dry-run'])
        self.assertEqual(args.main_meal_ratio,.008)
        self.assertEqual(args.main_meal_cap,300)
        self.assertTrue(args.special_lists)

if __name__=='__main__':unittest.main()
