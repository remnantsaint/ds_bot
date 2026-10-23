"""官方分词数据只作为数据读取，不执行下载包中的Python代码。"""
import hashlib
import io
from pathlib import Path
import urllib.request
import zipfile

TOKENIZER_SHA256 = '89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614'
TOKENIZER_URL = 'https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip'
_tokenizer = None


def data_path():
    return Path(__file__).resolve().parents[1]/'.run'/'deepseek-tokenizer.json'


def install_tokenizer():
    with urllib.request.urlopen(TOKENIZER_URL, timeout=60) as response:
        content=response.read(20*1024*1024+1)
    if len(content)>20*1024*1024:
        raise ValueError('分词包过大')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        info=archive.getinfo('deepseek_v4_tokenizer/tokenizer.json')
        if info.file_size>20*1024*1024:
            raise ValueError('分词数据过大')
        data=archive.read(info)
    if hashlib.sha256(data).hexdigest()!=TOKENIZER_SHA256:
        raise ValueError('官方分词数据版本变化，需人工核对后更新摘要')
    path=data_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def tokenizer():
    global _tokenizer
    if _tokenizer is None and data_path().exists():
        from tokenizers import Tokenizer
        data=data_path().read_bytes()
        if hashlib.sha256(data).hexdigest()!=TOKENIZER_SHA256:
            raise ValueError('分词数据校验失败')
        _tokenizer=Tokenizer.from_str(data.decode())
    return _tokenizer


def mode():
    try:
        return 'deepseek_official' if tokenizer() else 'utf8_conservative_estimate'
    except (ImportError,ValueError,OSError):
        return 'utf8_conservative_estimate'


def count_tokens(text):
    try:
        engine=tokenizer()
        if engine is not None:
            return len(engine.encode(text,add_special_tokens=False).ids)
    except (ImportError,ValueError,OSError):
        pass
    # 未安装时使用保守上界近似，明确不冒充真实Token计费数。
    return len(text.encode('utf-8'))
