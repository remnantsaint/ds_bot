"""有来源的事实条目，不因周期摘要而覆盖或删除。"""
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict


class Fact(BaseModel):
    model_config=ConfigDict(extra='forbid')
    subject: str=Field(min_length=1,max_length=80)
    subject_user_id: str | None=Field(default=None,pattern=r'^[0-9]{1,20}$')
    statement_type: Literal['self_report','hearsay','inference','correction','unknown']='unknown'
    relation: str=Field(min_length=1,max_length=80)
    content: str=Field(min_length=1,max_length=500)
    status: Literal['confirmed','tentative','cancelled','uncertain']='uncertain'
    event_time: str=Field(default='',max_length=80)
    source_ids: list[int]=Field(min_length=1,max_length=8)


class SummaryEnvelope(BaseModel):
    model_config=ConfigDict(extra='forbid')
    summary: str=Field(min_length=1)
    facts: list[Fact]=Field(default_factory=list,max_length=12)


class SummaryText(str):
    def __new__(cls,text,facts):
        instance=super().__new__(cls,text)
        instance.facts=facts
        return instance


def attribute_fact(fact, records):
    """发言人只取原文元数据；身份不匹配时保守降级，不猜测昵称对应QQ。"""
    sources=[{'record_id':record_id,'user_id':str(record['user_id']) if record.get('user_id') is not None else None,
              'role':record.get('role','unknown')} for record_id,record in records]
    speakers=sorted({source['user_id'] for source in sources if source['role']=='user' and source['user_id']})
    known=set(speakers)
    for _,record in records:
        known.update(str(value) for value in record.get('mentioned_user_ids',[]) if str(value).isascii() and str(value).isdigit())
    subject=fact['subject_user_id']
    if subject is not None and subject not in known:
        fact['subject_user_id']=None
        fact['statement_type']='unknown'
    if fact['statement_type']=='self_report' and (not subject or speakers!=[subject]):
        fact['statement_type']='hearsay' if fact['subject_user_id'] else 'unknown'
    if (not speakers or fact['statement_type'] in ('hearsay','inference','unknown')
            or fact['subject_user_id'] is None or speakers!=[fact['subject_user_id']]):
        fact['status']='uncertain'
    return {**fact,'speaker_user_ids':speakers,'source_speakers':sources}
