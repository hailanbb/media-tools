"""真实文件名快照 + 隔离数据库/云盘回归，不读取生产库、不操作媒体。"""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import test_v2551_review_closure as f
import test_v2552_recommended as v52
from app.core import directory_merge as merge
from app.core.identity_resolver import analyze_season_release_signals, _cluster_release_titles
from app.core.episode_parser import parse_episode, is_video_file, classify_content_unit
from app.core.review_engine import generate_review_clean_plan
from app.core.season_classifier import classify_single_record
from app.core.douban_lookup import lookup_error_reason, normalize_detail

CASES = json.loads((Path(__file__).parent/'fixtures/v2553_variety_cases.json').read_text(encoding='utf-8'))


class ContentRegression(unittest.TestCase):
    def test_real_special_member_diary_files_are_all_retained(self):
        case=next(c for c in CASES if c['tmdb_id']=='222808')
        plan=generate_review_clean_plan(case['files'],2,20)
        self.assertFalse(plan['can_clean'])
        self.assertEqual(plan['to_delete'],[])
        self.assertTrue(plan['all_distinct_retained'])
        special=next(x for x in case['files'] if '特别节目' in x['name'] and is_video_file(x['name']))
        self.assertEqual(classify_content_unit(special['name'])['content_kind'],'special')

    def test_real_pinyin_and_english_release_names_are_one_work(self):
        case=next(c for c in CASES if c['tmdb_id']=='296217')
        parsed=[parse_episode(x['name'],x['path']) for x in case['files'] if is_video_file(x['name'])]
        result=analyze_season_release_signals(case['files'],parsed,case['show_name'],case['matched_work'])
        self.assertFalse(result['mixed_works'],result)
        self.assertFalse(result['incompatible_with_primary'],result)

    def test_bracketed_original_title_links_season_release_alias(self):
        self.assertEqual(len(_cluster_release_titles(["[萌探2024 先导片] The Detectives' Adventures",'萌探2024 Funny Team'],set())),1)
        self.assertEqual(len(_cluster_release_titles(['[先导片] Show One','[先导片] Show Two'],set())),2)

    def test_explicit_different_ids_still_block_even_with_equal_titles(self):
        files=[f.file('一饭封神.S01E01.[tmdbid=296217].mkv'),f.file('一饭封神.S01E01.[tmdbid=123].mkv')]
        result=analyze_season_release_signals(files,[parse_episode(x['name'],x['path']) for x in files],'一饭封神',{'tmdb_id':296217,'name':'一饭封神'})
        self.assertTrue(result['mixed_works'])
        self.assertTrue(result['explicit_file_tmdb_conflicts'])

    def test_verified_complete_season_keeps_adjacent_extra_without_anomaly(self):
        rec={'status':'review_needed','total_episodes':20,'season_finished':True,'recognized_episodes':list(range(1,22)),
             'evidence':{'metadata_binding_verified':True,'broadcast_evidence':{'status':'finished'},'review_plan_preview':{'can_clean':False}}}
        self.assertEqual(classify_single_record(rec)[0],'archive')
        rec['evidence']['review_plan_preview']['can_clean']=True
        self.assertEqual(classify_single_record(rec)[0],'review')
        rec['recognized_episodes']=list(range(1,20))+[21]
        self.assertEqual(classify_single_record(rec)[0],'anomaly')

    def test_unknown_period_or_large_overflow_is_not_accepted(self):
        rec={'status':'review_needed','total_episodes':20,'season_finished':True,'recognized_episodes':list(range(1,28)),
             'evidence':{'metadata_binding_verified':True,'broadcast_evidence':{'status':'finished'},'review_plan_preview':{'can_clean':False}}}
        self.assertEqual(classify_single_record(rec)[0],'anomaly')
        rec['recognized_episodes']=list(range(1,22));rec['evidence']['broadcast_evidence']['status']='unknown'
        self.assertEqual(classify_single_record(rec)[0],'anomaly')

    def test_lookup_errors_are_readable_and_response_wrappers_supported(self):
        self.assertEqual(lookup_error_reason(ValueError('详情页未返回可读取的作品资料')),'详情页未返回可读取的作品资料')
        self.assertNotIn('secret',lookup_error_reason(ValueError('secret credential')))
        self.assertEqual(normalize_detail({'data':{'media_info':{'name':'Show','episode_count':'20'}}})['number_of_episodes'],20)


class DirectoryRegression(unittest.TestCase):
    setUp=f.DatabaseRegression.setUp
    tearDown=f.DatabaseRegression.tearDown
    season=f.DatabaseRegression.season

    def setup_tree(self):
        old='/src/Old (2026) [tmdbid=1]';target='/src/Show (2026) [tmdbid=1]'
        oldseason=old+'/Season 1';newseason=target+'/Season 1'
        entries=[{'side':'source','norm_path':p,'seasons':[1]} for p in (old,target)]
        group=f.DirectoryGroup(id='g',task_id='t',tmdb_id='1',category_key='cat:_root_',status='pending_confirm',
            paths_json=json.dumps(entries),evidence_json=json.dumps({'canonical_tmdb_title':'Show','canonical_tmdb_year':'2026'}))
        self.session.add(group);self.season(show_path=target,season_path=newseason,tmdb_id='1')
        self.session.commit()
        def asset(path):return dict(f.file(path.rsplit('/',1)[-1]),path=path)
        tree=v52.Tree({'/src':[v52.folder(old),v52.folder(target)],old:[v52.folder(oldseason)],target:[v52.folder(newseason)],
            oldseason:[asset(oldseason+'/season.nfo'),asset(oldseason+'/poster.jpg')],newseason:[asset(newseason+'/season.nfo')]})
        settings=SimpleNamespace(source_root='/src',archive_root='/arc')
        return old,target,oldseason,newseason,group,tree,settings

    def test_season_assets_move_verify_then_empty_root_removed(self):
        old,target,oldseason,newseason,group,tree,settings=self.setup_tree()
        async def move(cd,src,dest,name,cache):
            if dest not in tree.tree:tree.tree[dest]=[]
            parent=src.rsplit('/',1)[0];item=next(x for x in tree.tree[parent] if x['path']==src)
            tree.tree[parent].remove(item);tree.tree[dest].append(dict(item,path=dest+'/'+name,name=name))
            return dest+'/'+name
        with patch.object(merge,'get_settings',return_value=settings),patch.object(merge,'move_and_rename_file_robust',move):
            plan=f.immediate(merge.build_directory_merge_plan(self.db,'t','g',all_seasons=True,auto_canonical_target=True,cd2_client=tree))
            self.assertTrue(plan['can_execute']);self.assertEqual(len(plan['assets_to_preserve']),2)
            self.assertEqual(plan['isolated_conflicts'],[])
            result=f.immediate(merge.execute_directory_merge(self.db,'t','g',plan_id=plan['plan_id'],cd2_client=tree,skip_summary_refresh=True))
        self.assertEqual(group.status,'confirmed')
        self.assertNotIn(old,tree.tree)
        self.assertEqual(tree.tree[newseason][0]['name'],'season.nfo')
        for asset in plan['assets_to_preserve']:
            self.assertTrue(any(x['path']==asset['target_path'] for x in tree.tree[asset['target_dir']]))

    def test_false_move_success_does_not_delete_old_directory(self):
        old,target,oldseason,newseason,group,tree,settings=self.setup_tree()
        async def no_effect(*a,**kw):return '/ignored'
        with patch.object(merge,'get_settings',return_value=settings),patch.object(merge,'move_and_rename_file_robust',no_effect):
            plan=f.immediate(merge.build_directory_merge_plan(self.db,'t','g',all_seasons=True,auto_canonical_target=True,cd2_client=tree))
            with self.assertRaisesRegex(RuntimeError,'附件保全未完成'):
                f.immediate(merge.execute_directory_merge(self.db,'t','g',plan_id=plan['plan_id'],cd2_client=tree,skip_summary_refresh=True))
        self.assertFalse(tree.deleted);self.assertEqual(group.status,'pending_confirm')

    def test_missing_client_cannot_claim_cloud_directory_empty(self):
        with self.assertRaisesRegex(RuntimeError,'未提供云盘'):
            f.immediate(merge.list_files_robust(None,'/not-a-real-mount/Show'))
        class MissingList:
            async def list_files(self,*a,**kw):return None
        with self.assertRaisesRegex(RuntimeError,'读取目录失败'):
            f.immediate(merge.list_files_robust(MissingList(),'/not-a-real-mount/Show'))


class DoubanRegression(unittest.TestCase):
    lookup=v52.DoubanRegression.lookup

    def test_empty_default_movie_payload_still_uses_real_detail(self):
        class MP:
            async def get_douban_media_info(self,*a):return {'title':None,'type':'电影','number_of_episodes':None}
            async def get_douban_detail(self,*a):return {'title':'一路繁花','type':'电视剧','year':'2025','number_of_episodes':10}
        (did,seasons),diag=self.lookup(MP(),object(),known_bindings={1:'12345'})
        self.assertEqual(did,'12345');self.assertEqual(seasons[1]['total'],10)

    def test_real_movie_is_rejected_without_unnecessary_subject_request(self):
        class MP:
            async def search_media(self,*a,**kw):return [{'douban_id':'12345','title':'#活着'}]
            async def get_douban_media_info(self,*a):return {'title':None,'type':'电影'}
            async def get_douban_detail(self,*a):return {'title':'#活着','type':'电影','year':'2020'}
        calls=[]
        class HTTP:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def get(self,url,**kw):
                calls.append(url)
                return SimpleNamespace(status_code=200,json=lambda:[],text='没有找到')
        (did,seasons),diag=self.lookup(MP(),HTTP())
        self.assertIsNone(did)
        self.assertTrue(any(x.get('reason')=='候选是电影，未采纳' for x in diag))
        self.assertFalse(any('/subject/' in u for u in calls))


if __name__ == '__main__':
    unittest.main(verbosity=2)
