"""中文二字片段＋词串检索，BM25排序；轻量词表扩展并非向量语义模型。"""
import math
import re
from collections import Counter

ALIASES = [('聚餐','吃饭','用餐','饭局'),('改期','延期','改到','改为'),
           ('地点','地址','哪里','哪儿'),('预算','费用','价格','多少钱'),
           ('过敏','忌口'),('订位','订座','预约')]
STOP={'记错','错了','是不是','不是','没有','怎么','什么','记得','之前','以前'}


def searchable(record):
    return ' '.join(str(record.get(key,'')) for key in ('nickname','user_id','content'))


def tokens(text):
    result=[]
    for word in re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]+',text.lower()):
        if re.fullmatch(r'[\u4e00-\u9fff]+',word):
            result.extend(word[i:i+2] for i in range(len(word)-1) if word[i:i+2] not in STOP)
        elif len(word)>1:
            result.append(word)
    return result


def query_terms(query):
    terms=set(tokens(query[:200]))
    for family in ALIASES:
        if any(word in query for word in family):
            for word in family:
                terms.update(tokens(word))
    return sorted(terms)[:80]


def bm25_rank(query, texts):
    terms=query_terms(query)
    docs=[Counter(tokens(text)) for text in texts]
    lengths=[sum(doc.values()) for doc in docs]
    average=sum(lengths)/max(1,len(docs)) or 1
    scores=[0.0]*len(docs)
    for term in terms:
        frequency=sum(term in doc for doc in docs)
        idf=math.log(1+(len(docs)-frequency+.5)/(frequency+.5))
        for i,doc in enumerate(docs):
            tf=doc[term]
            scores[i]+=idf*tf*2.2/(tf+1.2*(.25+.75*lengths[i]/average)) if tf else 0
    return scores
