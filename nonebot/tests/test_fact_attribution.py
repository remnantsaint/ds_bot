import unittest
from src.memory_facts import Fact, attribute_fact

class FactAttributionTests(unittest.TestCase):
    def fact(self,subject='202',kind='self_report'):
        return Fact(subject='偏好',subject_user_id=subject,statement_type=kind,relation='过敏',content='花生过敏',status='confirmed',source_ids=[1]).model_dump()

    def test_self_report_has_program_derived_speaker(self):
        result=attribute_fact(self.fact(),[(1,{'role':'user','user_id':202})])
        self.assertEqual(result['speaker_user_ids'],['202'])
        self.assertEqual(result['status'],'confirmed')
        self.assertEqual(result['source_speakers'][0]['record_id'],1)

    def test_other_person_cannot_be_self_report(self):
        result=attribute_fact(self.fact('303'),[(1,{'role':'user','user_id':202,'mentioned_user_ids':['303']})])
        self.assertEqual(result['subject_user_id'],'303')
        self.assertEqual(result['statement_type'],'hearsay')
        self.assertEqual(result['status'],'uncertain')
        self.assertEqual(result['speaker_user_ids'],['202'])

    def test_unknown_identity_is_not_guessed(self):
        result=attribute_fact(self.fact('999'),[(1,{'role':'user','user_id':202,'nickname':'小林'})])
        self.assertIsNone(result['subject_user_id'])
        self.assertEqual(result['statement_type'],'unknown')
        self.assertEqual(result['status'],'uncertain')

    def test_inference_is_uncertain_even_about_self(self):
        result=attribute_fact(self.fact(kind='inference'),[(1,{'role':'user','user_id':202})])
        self.assertEqual(result['status'],'uncertain')

    def test_correction_preserves_source_and_type(self):
        result=attribute_fact(self.fact(kind='correction'),[(1,{'role':'user','user_id':202})])
        self.assertEqual(result['statement_type'],'correction')
        self.assertEqual(result['speaker_user_ids'],['202'])

    def test_assistant_does_not_confirm_user_fact(self):
        result=attribute_fact(self.fact(),[(1,{'role':'assistant','user_id':202})])
        self.assertEqual(result['speaker_user_ids'],[])
        self.assertEqual(result['status'],'uncertain')
