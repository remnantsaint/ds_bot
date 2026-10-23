import unittest
from pathlib import Path
from unittest.mock import patch
from src import token_budget
from src.memory_facts import SummaryEnvelope
from pydantic import ValidationError

class TokenBudgetTests(unittest.TestCase):
    def test_missing_data_uses_conservative_bytes(self):
        with patch.object(token_budget,'_tokenizer',None), patch.object(token_budget,'data_path',return_value=Path('/not-present-tokenizer.json')):
            self.assertEqual(token_budget.count_tokens('中文abc'),9)
            self.assertEqual(token_budget.mode(),'utf8_conservative_estimate')

    def test_invalid_data_falls_back(self):
        with patch.object(token_budget,'tokenizer',side_effect=ValueError('bad data')):
            self.assertEqual(token_budget.count_tokens('abc'),3)
            self.assertEqual(token_budget.mode(),'utf8_conservative_estimate')

    def test_fact_requires_sources_and_valid_status(self):
        with self.assertRaises(ValidationError):
            SummaryEnvelope.model_validate({'summary':'s','facts':[{'subject':'202','relation':'food','content':'x','source_ids':[],'status':'certain'}]})
