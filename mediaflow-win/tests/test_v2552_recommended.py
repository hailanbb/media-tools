"""推荐处理收敛与豆瓣检索通道回归；内存数据库、模拟云盘/HTTP。"""
import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from test_v2551_review_closure import (
    Disk, file, immediate, archive, DirectoryGroup,
    generate_review_clean_plan, classify_content_unit,
)
import test_v2551_review_closure as fixtures
from app.core import inspector, directory_merge
from app.core.douban_lookup import search_items, search_page_candidates, subject_page_detail
from app.core.identity_resolver import save_verified_metadata_snapshot, get_verified_metadata_snapshot


async def gather_now(*coros, **kwargs):
    return [await c for c in coros]


async def wait_now(coro, timeout=None):
    return await coro


class Tree(Disk):
    def __init__(self, tree):
        super().__init__([])
        self.tree = tree
        self.reads = []

    async def list_files(self, path, **kwargs):
        self.reads.append((path, kwargs.get('force_refresh')))
        if path not in self.tree:
            raise OSError('missing fixture directory: ' + path)
        return [dict(f) for f in self.tree[path]]

    async def delete_file(self, path):
        self.deleted.append(path)
        self.tree.pop(path, None)
        for key in self.tree:
            self.tree[key] = [f for f in self.tree[key] if f['path'] != path]
        return {'success': True}


def folder(path):
    return {'name':path.rsplit('/', 1)[-1], 'path':path, 'id':path, 'size':0, 'isDirectory':True}


class PlanRegression(unittest.TestCase):
    def test_protected_overflow_video_keeps_its_attachments(self):
        files = [file('Show.S01E01.mkv'), file('Show.S01E02.mkv'), file('Show.S01E02.nfo'), file('Show.S01E02-thumb.jpg')]
        plan = generate_review_clean_plan(files, season_number=1, total_episodes=1)
        self.assertFalse(plan['can_clean'])
        self.assertEqual(plan['to_delete'], [])

    def test_derivatives_stay_independent_from_regular_and_each_other(self):
        labels = ['正片', '专访.Interview', '直播.Live', '电台.Radio', '连麦.Hangout', '陪看.Watch']
        keys = {classify_content_unit('Show.S01E01.' + label + '.mkv')['content_unit_key'] for label in labels}
        self.assertEqual(len(keys), len(labels))
        plan = generate_review_clean_plan([file('Show.S01E01.' + label + '.mkv') for label in labels], 1, 1)
        self.assertEqual(plan['to_delete'], [])


class BatchRegression(unittest.TestCase):
    setUp = fixtures.DatabaseRegression.setUp
    tearDown = fixtures.DatabaseRegression.tearDown
    season = fixtures.DatabaseRegression.season

    def test_directory_alignment_and_cleanup_converge_in_one_batch(self):
        show = '/src/Show (2026) [tmdbid=1]'
        arc = '/arc/Show (2026) [tmdbid=1]'
        sp = show + '/Season 1'
        rec = self.season(show_path=show, season_path=sp, tmdb_id='1', evidence_json=json.dumps({
            'directory_group':{'id':'g','requires_confirm':True}, 'review_plan_preview':{'can_clean':True},
            'period_evidence':{'is_segmented':True, 'source_status':'period_ready'}}))
        entries = [{'side':'source','norm_path':show,'raw_name':show.rsplit('/',1)[-1],'seasons':[1]},
                   {'side':'archive','norm_path':arc,'raw_name':arc.rsplit('/',1)[-1],'seasons':[1]}]
        group = DirectoryGroup(id='g',task_id='t',tmdb_id='1',category_key='cat:_root_',status='pending_confirm',
            paths_json=json.dumps(entries),evidence_json=json.dumps({'canonical_tmdb_title':'Show','canonical_tmdb_year':'2026'}))
        self.session.add(group); self.session.commit()
        files = [dict(file(n), path=sp+'/'+n) for n in ('Show.S01E01.720p.mkv','Show.S01E01.2160p.mkv')]
        disk = Tree({'/src':[folder(show)], '/arc':[folder(arc)], show:[folder(sp)], sp:files, arc:[folder(arc+'/Season 1')]})
        settings = SimpleNamespace(source_root='/src',archive_root='/arc', enable_multi_dir_governance=True, mp_url='', mp_token='')
        from app.core import directory_governance
        with ExitStack() as stack:
            for module in (directory_merge,directory_governance,archive):
                stack.enter_context(patch.object(module,'get_settings',return_value=settings))
            stack.enter_context(patch.object(archive,'CloudDrive2Client',return_value=disk))
            stack.enter_context(patch.object(archive.asyncio,'gather',gather_now))
            stack.enter_context(patch.object(archive.asyncio,'wait_for',wait_now))
            preview = immediate(archive.build_recommended_batch_plan(archive.RecommendedPlanRequest(task_id='t'),self.db))
            self.assertEqual(len(preview['merge_items']),1,preview)
            self.assertTrue(preview['merge_items'][0]['align_only'])
            self.assertEqual(len(preview['clean_items']),1,preview)
            result = immediate(archive._run_recommended_batch_plan(archive.RecommendedExecuteRequest(task_id='t',plan_id=preview['plan_id']),self.db))
            self.assertEqual(result['failed_steps'],[])
            self.assertTrue(result['fully_resolved'],result)
            again = immediate(archive.build_recommended_batch_plan(archive.RecommendedPlanRequest(task_id='t'),self.db))
            self.assertEqual(again['merge_items']+again['clean_items']+again['redundant_source_items'],[])
        self.assertEqual(group.status,'confirmed')
        self.assertEqual(len(disk.deleted),1)

    def test_empty_old_directory_is_actionable_and_force_read(self):
        old='/src/Old (2026) [tmdbid=1]'; target='/src/New (2026) [tmdbid=1]'
        group=DirectoryGroup(id='g',task_id='t',tmdb_id='1',category_key='cat:_root_',status='pending_confirm',
            paths_json=json.dumps([{'side':'source','norm_path':p,'raw_name':p.rsplit('/',1)[-1],'seasons':[6,7]} for p in (old,target)]))
        self.session.add(group);self.session.commit()
        disk=Tree({old:[],target:[]})
        plan=immediate(directory_merge.build_directory_merge_plan(self.db,'t','g',canonical_title='New',first_air_year='2026',all_seasons=True,auto_canonical_target=True,cd2_client=disk))
        self.assertTrue(plan['can_execute'],plan)
        self.assertEqual(plan['affected_seasons'],[6,7])
        self.assertIn(old,plan['directories_to_clean'])
        self.assertTrue(all(refresh for _,refresh in disk.reads))

    def test_read_failure_never_becomes_empty_directory_confirmation(self):
        group=DirectoryGroup(id='g',task_id='t',tmdb_id='1',category_key='cat:_root_',
            paths_json=json.dumps([{'side':'source','norm_path':'/src/Old','raw_name':'Old'}]))
        self.session.add(group);self.session.commit()
        with self.assertRaisesRegex(RuntimeError,'读取目录失败'):
            immediate(directory_merge.build_directory_merge_plan(self.db,'t','g',canonical_title='New',all_seasons=True,auto_canonical_target=True,cd2_client=Tree({})))
        self.assertNotEqual(group.status,'confirmed')

    def test_archive_check_allows_local_duplicates_but_identity_conflict_does_not(self):
        rec=self.season(status='archive_check')
        disk=Disk([file('Show.S01E01.720p.mkv'),file('Show.S01E01.2160p.mkv')])
        res=immediate(archive._clean_duplicates_impl(archive._ArchiveRequest(season_record_ids=['s']),self.db,
            cd2_client=disk,active_dl_paths_override=set(),skip_summary_refresh=True))
        self.assertTrue(res.ok,res.message)
        self.assertEqual(rec.status,'archive_check')
        rec.conflict_json='{"blocking":true}'
        self.assertIn('身份冲突',archive._duplicate_cleanup_block_reason(rec))

    def test_failed_lookup_does_not_overwrite_successful_binding(self):
        immediate(save_verified_metadata_snapshot(self.db,'douban_binding:1:S1','douban_binding','1',1,{'douban_id':'12345'}))
        immediate(save_verified_metadata_snapshot(self.db,'douban_binding:1:S1','douban_binding','1',1,{}))
        self.session.commit()
        self.assertEqual(immediate(get_verified_metadata_snapshot(self.db,'douban_binding:1:S1'))['douban_id'],'12345')

    def discard_fixture(self):
        from app.api import source_discard
        rec=self.season(status='download_needed',total_episodes=2,local_episode_count=1,
            missing_episodes_json='[2]',duplicate_episodes_json='[]',evidence_json='{}')
        disk=Tree({'/src/Show':[folder('/src/Show/Season 1'),folder('/src/Show/Season 2')],
                   '/src/Show/Season 1':[file('Show.S01E01.mkv')], '/src/Show/Season 2':[file('Other.S02E01.mkv')]})
        stack=ExitStack()
        stack.enter_context(patch.object(source_discard,'CloudDrive2Client',return_value=disk))
        stack.enter_context(patch.object(source_discard,'get_settings',return_value=SimpleNamespace(
            source_root='/src',archive_root='/archive',mp_url='',mp_token='')))
        self.addCleanup(stack.close)
        return source_discard,rec,disk

    def test_discard_only_confirmed_season_and_repeat_is_idempotent(self):
        api,rec,disk=self.discard_fixture()
        preview=immediate(api.preview(api.PreviewRequest(season_record_id='s'),self.db))['data']
        self.assertFalse(preview['will_delete_show_dir'])
        body=api.ExecuteRequest(season_record_id='s',preview_token=preview['preview_token'],confirm_abandon=True)
        result=immediate(api.execute(body,self.db))
        self.assertTrue(result['ok'])
        self.assertEqual(disk.deleted,['/src/Show/Season 1'])
        self.assertIn('/src/Show/Season 2',disk.tree)
        self.assertEqual(rec.status,'source_absent')
        self.assertTrue(immediate(api.execute(body,self.db))['ok'])
        self.assertEqual(len(disk.deleted),1)

    def test_discard_rejects_added_file_after_preview(self):
        api,rec,disk=self.discard_fixture()
        token=immediate(api.preview(api.PreviewRequest(season_record_id='s'),self.db))['data']['preview_token']
        disk.tree[rec.season_path].append(file('Show.S01E02.mkv'))
        with self.assertRaises(archive.HTTPException) as error:
            immediate(api.execute(api.ExecuteRequest(season_record_id='s',preview_token=token,confirm_abandon=True),self.db))
        self.assertIn('发生变化',error.exception.detail)
        self.assertEqual(disk.deleted,[])

    def test_discard_requires_explicit_confirmation_and_rejects_flat_show_root(self):
        api,rec,disk=self.discard_fixture()
        token=immediate(api.preview(api.PreviewRequest(season_record_id='s'),self.db))['data']['preview_token']
        with self.assertRaises(archive.HTTPException):
            immediate(api.execute(api.ExecuteRequest(season_record_id='s',preview_token=token),self.db))
        rec.season_path=rec.show_path
        with self.assertRaises(archive.HTTPException) as error:
            immediate(api.preview(api.PreviewRequest(season_record_id='s'),self.db))
        self.assertIn('独立的本季目录',error.exception.detail)
        self.assertEqual(disk.deleted,[])

    def test_discard_blocks_inflight_ledger_from_another_task(self):
        api,rec,disk=self.discard_fixture()
        self.session.add(api.GapFillEpisode(task_id='other-task',season_record_id='other',season_path=rec.season_path,
            season_number=1,identity_key='tmdb:1:S1',episode_number=2,state='submitted_unverified'))
        self.session.commit()
        with self.assertRaises(archive.HTTPException) as error:
            immediate(api.preview(api.PreviewRequest(season_record_id='s'),self.db))
        self.assertIn('在途',error.exception.detail)
        self.assertEqual(disk.deleted,[])

    def test_discard_does_not_report_success_before_parent_verification(self):
        api,rec,disk=self.discard_fixture()
        token=immediate(api.preview(api.PreviewRequest(season_record_id='s'),self.db))['data']['preview_token']
        async def no_effect(path):return {'success':True}
        with patch.object(disk,'delete_file',no_effect),self.assertRaises(archive.HTTPException):
            immediate(api.execute(api.ExecuteRequest(season_record_id='s',preview_token=token,confirm_abandon=True),self.db))
        self.assertEqual(rec.status,'download_needed')

    def test_no_progress_batch_is_not_partial_success(self):
        self.season()
        pid='plan_rec_123451234512'
        archive._RECOMMENDED_BATCH_PLANS[pid]=dict(task_id='t',scope_record_ids=['s'],clean_items=[
            {'season_record_id':'s','show_name':'Show','season_number':1,'season_path':'/src/Show/Season 1'}])
        async def fail(*a,**kw):return SimpleNamespace(ok=False,message='fixture failure')
        with patch.object(archive,'CloudDrive2Client',return_value=Disk([])),patch.object(archive,'_clean_duplicates_impl',fail):
            result=immediate(archive._run_recommended_batch_plan(archive.RecommendedExecuteRequest(task_id='t',plan_id=pid),self.db))
        self.assertTrue(result['no_progress'])
        self.assertFalse(result['partial_success'])
        self.assertFalse(result['can_refresh_remaining'])


class DoubanRegression(unittest.TestCase):
    def test_response_wrappers_and_dynamic_page_are_distinguished(self):
        self.assertEqual(search_items({'data':{'results':[{'id':'12345'}]}}),[{'id':'12345'}])
        with self.assertRaises(ValueError):search_items({'message':'failed'})
        self.assertEqual(search_page_candidates('<div id="root"></div>')[1],'unavailable')
        self.assertEqual(search_page_candidates('<div>验证码</div>')[1],'blocked')
        self.assertEqual(search_page_candidates('<div>没有找到相关结果</div>')[1],'empty')

    def test_subject_page_counts_do_not_prove_finished(self):
        html='<span property="v:itemreviewed">一路繁花</span><div id="info"><span>首播:</span> 2025-01-11<br><span>集数:</span> 10</div>'
        detail=subject_page_detail(html,'12345')
        self.assertEqual(detail['number_of_episodes'],10)
        self.assertEqual(detail['status'],'')

    def lookup(self, mp, http, **kwargs):
        diagnostics=[]
        with patch.object(inspector.httpx,'AsyncClient',return_value=http),patch.object(inspector.asyncio,'gather',gather_now),patch.object(inspector.asyncio,'wait_for',wait_now):
            result=immediate(inspector._fetch_seasons_via_douban(mp,'一路繁花 (2025) [tmdbid=278324]',
                needed_seasons={1},tmdb_id=278324,diagnostics=diagnostics,**kwargs))
        return result,diagnostics

    def test_empty_apis_fall_back_to_user_search_page(self):
        class MP:
            async def search_media(self,*a,**kw):return []
            async def get_douban_media_info(self,did,**kw):return {'title':'一路繁花','year':'2025','number_of_episodes':10}
        class HTTP:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def get(self,url,**kw):
                return SimpleNamespace(status_code=200,json=lambda:[],text='<a href="https://movie.douban.com/subject/12345/">一路繁花</a>')
        (did,seasons),diag=self.lookup(MP(),HTTP())
        self.assertEqual(did,'12345')
        self.assertFalse(seasons[1]['finished'])
        self.assertTrue(any(d['source']=='douban_search_page' and d['status']=='returned' for d in diag))

    def test_wrong_year_candidate_does_not_prevent_web_fallback(self):
        class MP:
            async def search_media(self,*a,**kw):return [{'douban_id':'11111','title':'一路繁花'}]
            async def get_douban_media_info(self,did,**kw):return {'title':'一路繁花','year':'2024' if did=='11111' else '2025','number_of_episodes':10}
        class HTTP:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def get(self,url,**kw):return SimpleNamespace(status_code=200,json=lambda:[],text='<a href="https://movie.douban.com/subject/22222/">一路繁花</a>')
        (did,_),diag=self.lookup(MP(),HTTP())
        self.assertEqual(did,'22222')
        self.assertTrue(any(d.get('subject_id')=='11111' and d['status']=='rejected' for d in diag))

    def test_dynamic_search_shell_falls_back_to_classic_search(self):
        class MP:
            async def search_media(self,*a,**kw):return []
            async def get_douban_media_info(self,did,**kw):return {'title':'一路繁花','year':'2025','number_of_episodes':10}
        class HTTP:
            async def __aenter__(self):return self
            async def __aexit__(self,*a):pass
            async def get(self,url,**kw):
                html='<a href="https://movie.douban.com/subject/12345/">一路繁花</a>' if 'www.douban.com/search' in url else '<div id="root"></div>'
                return SimpleNamespace(status_code=200,json=lambda:[],text=html)
        (did,_),diag=self.lookup(MP(),HTTP())
        self.assertEqual(did,'12345')
        self.assertTrue(any(d['source']=='douban_search_page' and d['status']=='unavailable' for d in diag))
        self.assertTrue(any(d['source']=='douban_classic_search' and d['status']=='returned' for d in diag))

    def test_known_binding_bypasses_search_and_raw_detail_survives_media_failure(self):
        class MP:
            async def search_media(self,*a,**kw):raise AssertionError('should not search')
            async def get_douban_media_info(self,*a,**kw):raise OSError('temporary failure')
            async def get_douban_detail(self,did):return {'data':{'title':'一路繁花','year':'2025','episodes':10}}
        class HTTP:
            def __init__(self):raise AssertionError('should not access web')
        (did,_),diag=self.lookup(MP(),SimpleNamespace(),known_bindings={1:'12345'})
        self.assertEqual(did,'12345')
        self.assertEqual(inspector._douban_lookup_status(diag,True),'adopted')


if __name__ == '__main__':
    unittest.main(verbosity=2)
