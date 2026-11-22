"""人工改绑后的最终分流回归；仅内存数据库，不访问真实媒体或网络。"""
import json
import unittest
from unittest.mock import patch

import test_v2551_review_closure as f
from app.api import tasks
from app.models.manual_confirmation import ManualSeasonConfirmation
from app.models.season_mapping import SeasonMapping
from app.core.season_classifier import classify_single_record, get_record_work_context


class ManualMappingTests(unittest.TestCase):
    setUp = f.DatabaseRegression.setUp
    tearDown = f.DatabaseRegression.tearDown
    season = f.DatabaseRegression.season

    def make(self, broadcast='finished', total=3, evidence=None, conflict=None, confirmed_tid=200):
        rec = self.season(tmdb_id='200', status='archive_ready', total_episodes=3,
            local_episode_count=3, recognized_episodes_json='[1,2,3]', duplicate_episodes_json='[]',
            evidence_json=json.dumps(evidence or {
                'meta_status':'metadata_unverified', 'metadata_binding_verified':False,
                'matched_work':{'tmdb_id':100},
                'broadcast_evidence':{'status':'unknown','schedule_verified':False},
                'review_plan_preview':{'can_clean':False}}), conflict_json=json.dumps(conflict or {}))
        self.session.add(SeasonMapping(scope_key=rec.season_path, show_path=rec.show_path,
            season_path=rec.season_path, source_tmdb_id=200, source_season=1, target_tmdb_id=200,
            target_season=1, target_title='Fixture', expected_total_episodes=3,
            mapping_kind='manual_confirm_remap', is_active=True))
        self.session.add(ManualSeasonConfirmation(scope_key=rec.season_path, task_id='t',
            season_record_id=rec.id, show_path=rec.show_path, season_path=rec.season_path,
            tmdb_id=str(confirmed_tid), season_number=1, identity_confirmed=True,
            confirmed_tmdb_id=confirmed_tid, confirmed_season_number=1,
            confirmed_total_episodes=total, broadcast_status=broadcast, is_active=True))
        self.session.commit()
        return rec

    def recalculate(self, rec):
        self.assertTrue(f.immediate(tasks._apply_in_place_recalculate(self.db, rec, fetch_remote=False)))
        self.session.commit()
        self.session.expire_all()
        return f.immediate(get_record_work_context(self.db, rec))['main_category']

    def test_valid_confirmation_clears_stale_metadata_and_persists_mapping(self):
        rec = self.make()
        self.assertEqual(classify_single_record(rec)[0], 'anomaly')
        self.assertEqual(self.recalculate(rec), 'archive')
        ev = json.loads(rec.evidence_json)
        self.assertEqual(ev['meta_status'], 'manual_confirmed')
        self.assertEqual(ev['manual_confirmation']['confirmed_tmdb_id'], 200)
        self.assertEqual(ev['broadcast_evidence']['status'], 'finished')
        self.assertEqual(json.loads(rec.mapping_json)['target_tmdb_id'], 200)
        self.assertFalse(ev['metadata_binding_verified'], '人工确认不能伪装成远程资料已核实')

    def test_confirmed_airing_overrides_mapping_finished_default(self):
        rec = self.make(broadcast='airing')
        self.assertEqual(self.recalculate(rec), 'airing')
        self.assertFalse(rec.season_finished)

    def test_unknown_broadcast_does_not_default_to_finished(self):
        rec = self.make(broadcast='unknown')
        self.assertEqual(self.recalculate(rec), 'anomaly')

    def test_overflow_still_blocks_archive(self):
        rec = self.make(total=2)
        self.assertEqual(self.recalculate(rec), 'anomaly')
        self.assertTrue(json.loads(rec.evidence_json)['manual_confirmation']['contradiction']['has_contradiction'])

    def test_mixed_works_are_not_cleared_by_confirmation_or_mapping(self):
        rec = self.make(conflict={'kind':'mixed_works','blocking':True})
        self.assertEqual(self.recalculate(rec), 'anomaly')
        self.assertEqual(rec.status, 'identity_review')

    def test_explicit_other_work_file_id_still_blocks(self):
        rec = self.make(evidence={'meta_status':'metadata_unverified',
            'release_evidence':{'explicit_file_tmdb_conflicts':[{'file_tmdb_id':'999'}]}})
        self.assertEqual(self.recalculate(rec), 'anomaly')

    def test_different_identity_confirmation_is_not_inherited(self):
        rec = self.make(confirmed_tid=999)
        self.assertEqual(self.recalculate(rec), 'anomaly')
        self.assertNotIn('manual_confirmation', json.loads(rec.evidence_json))

    def test_save_response_uses_same_final_category_as_list(self):
        rec = self.make()
        async def no_file_plan(*args, **kwargs):
            return {'has_file_changes':False}, None
        with patch.object(tasks, '_build_correction_plan_for_record', no_file_plan):
            result = f.immediate(tasks.save_manual_season_confirmation('t', rec.id,
                tasks.ManualConfirmRequest(confirm_current_identity=True, target_tmdb_id=200,
                    total_episodes=3, broadcast_status='finished', apply_file_changes=False), self.db))
        self.assertTrue(result['ok'])
        self.assertEqual(result['target_category'], 'archive')
        self.assertEqual(result['season']['main_category'], 'archive')
        self.assertIn('齐全可归档', result['message'])


if __name__ == '__main__': unittest.main()
