"""常见凭据脱敏，不能保证识别所有个人信息或任意格式的秘密。"""
import re

PATTERNS=[
    re.compile(r'(?i)\bsk-[a-z0-9_-]{16,}'),
    re.compile(r'(?i)(\bBearer\s+)[a-z0-9._~+/=-]{12,}'),
    re.compile(r'(?i)((?:api[_ -]?key|access[_ -]?token|password|passwd|密钥|密码)\s*(?:[:：=]|是)\s*)[\"\']?[^\s\"\'，。；;]{8,}'),
]

def redact(text):
    for pattern in PATTERNS:
        text=pattern.sub(lambda match:(match.group(1) if match.lastindex else '')+'[已隐藏凭据]',text)
    return text


def clean_record(record):
    return {key:redact(value) if isinstance(value,str) else value for key,value in record.items()}
