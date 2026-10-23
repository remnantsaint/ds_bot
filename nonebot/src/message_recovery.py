"""更新期间的持久收件箱与有界历史补拉；不宣称网络发送恰好一次。"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time

from nonebot import logger
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from src.memory_privacy import redact

maintenance=False
recovering=set()
_tasks={}
_store=None


def clean(value):
    if isinstance(value,str):return redact(value)
    if isinstance(value,list):return [clean(item) for item in value]
    if isinstance(value,dict):return {key:clean(item) for key,item in value.items()}
    return value


class Inbox:
    def __init__(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS inbox(
            bot TEXT,grp TEXT,msg TEXT,stamp REAL,payload TEXT,state TEXT,updated REAL,
            PRIMARY KEY(bot,grp,msg));
            CREATE TABLE IF NOT EXISTS groups_seen(bot TEXT,grp TEXT,stamp REAL,PRIMARY KEY(bot,grp));''')
        with self.db:
            self.db.execute("UPDATE inbox SET state='queued' WHERE state='processing'")
            self.db.execute("UPDATE inbox SET state='uncertain' WHERE state='sending'")

    def capture(self,bot,data,state='queued'):
        key=(str(bot),str(data['group_id']),str(data['message_id']))
        stamp=float(data.get('time',time.time()))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?)',
                (*key,stamp,json.dumps(clean(data),ensure_ascii=False),state,time.time()))
            self.db.execute('''INSERT INTO groups_seen VALUES (?,?,?) ON CONFLICT(bot,grp)
                DO UPDATE SET stamp=max(stamp,excluded.stamp)''',(*key[:2],stamp))
            # 已完成去重标记保留7天，未完成与不确定项保留。
            self.db.execute("DELETE FROM inbox WHERE state='done' AND updated<?",(time.time()-7*86400,))
        return key

    def claim(self,key):
        with self.db:
            return self.db.execute("UPDATE inbox SET state='processing',updated=? WHERE bot=? AND grp=? AND msg=? AND state='queued'",(time.time(),*key)).rowcount==1

    def state(self,key):
        row=self.db.execute('SELECT state FROM inbox WHERE bot=? AND grp=? AND msg=?',key).fetchone()
        return row[0] if row else None

    def mark(self,key,state):
        with self.db:self.db.execute('UPDATE inbox SET state=?,updated=? WHERE bot=? AND grp=? AND msg=?',(state,time.time(),*key))

    def finish(self,key,interrupted=False):
        state=self.state(key)
        if state=='sending':self.mark(key,'uncertain')
        elif state=='processing':self.mark(key,'queued' if interrupted else 'done')

    def groups(self,bot):
        return self.db.execute('SELECT grp,stamp FROM groups_seen WHERE bot=? ORDER BY stamp DESC LIMIT 20',(str(bot),)).fetchall()

    def pending(self,bot):
        return self.db.execute("SELECT * FROM inbox WHERE bot=? AND state='queued' ORDER BY stamp,grp,msg",(str(bot),)).fetchall()

    def status(self):
        return {row[0]:row[1] for row in self.db.execute('SELECT state,count(*) FROM inbox GROUP BY state')}

    def seed(self,memory_path):
        if not Path(memory_path).exists():return
        source=sqlite3.connect('file:'+str(memory_path)+'?mode=ro',uri=True)
        try:
            groups=source.execute("SELECT bot,grp,max(stamp) FROM messages WHERE role='user' GROUP BY bot,grp ORDER BY max(stamp) DESC LIMIT 20").fetchall()
            with self.db:
                for bot,group,stamp in groups:
                    self.db.execute('INSERT OR IGNORE INTO groups_seen VALUES (?,?,?)',(str(bot),str(group),stamp))
            rows=source.execute("SELECT bot,grp,message_id,stamp FROM messages WHERE role='user' AND stamp>?",(time.time()-600,)).fetchall()
            for bot,group,msg,stamp in rows:self.capture(bot,{'group_id':group,'message_id':msg,'time':stamp},'done')
        finally:source.close()


def get_inbox():
    global _store
    # 离线测试约定：关闭真实运行存储，测试自行注入临时Inbox。
    if os.getenv('MEMORY_ENABLED','').lower()=='false' or os.getenv('RECOVERY_ENABLED','true').lower()=='false':return None
    if _store is None:
        root=Path(__file__).resolve().parents[1]/'.run'
        _store=Inbox(root/'inbox.sqlite3')
        _store.seed(root/'memory.sqlite3')
    return _store


def key_for(bot,event):return (str(bot.self_id),str(event.group_id),str(event.message_id))


def mark_sending(bot,event):
    store=get_inbox()
    if store:store.mark(key_for(bot,event),'sending')


def mark_sent(bot,event):
    store=get_inbox()
    if store:store.mark(key_for(bot,event),'done')


def history_event(bot_id,group,data):
    if str(data.get('group_id',group))!=str(group):raise ValueError('历史消息群号不符')
    data=dict(data)
    data.update(self_id=int(bot_id),group_id=int(group),post_type='message',message_type='group')
    data.setdefault('sub_type','normal');data.setdefault('font',0)
    data.setdefault('original_message',data['message']);data.setdefault('raw_message','')
    data['to_me']=any(item.get('type')=='at' and str(item.get('data',{}).get('qq'))==str(bot_id) for item in data['message'])
    return GroupMessageEvent.model_validate(data)


async def recover(bot,groups=None):
    bot_id=str(bot.self_id)
    store=get_inbox()
    if not store:return
    recovering.add(bot_id)
    try:
        for group in (groups if groups is not None else store.groups(bot_id)):
            cutoff=max(float(group['stamp']),time.time()-600)
            try:
                response=await asyncio.wait_for(bot.call_api('get_group_msg_history',group_id=int(group['grp']),count=100),timeout=12)
                messages=response.get('messages',[])
                if len(messages)>=100 and min(float(row.get('time',0)) for row in messages)>cutoff:logger.warning('[recovery] 历史达到100条上限，可能存在缺口')
                for item in sorted(messages,key=lambda row:row.get('time',0)):
                    if float(item.get('time',0))<cutoff or str(item.get('user_id'))==bot_id:continue
                    event=history_event(bot_id,group['grp'],item)
                    store.capture(bot_id,event.model_dump(mode='json'))
            except Exception as error:
                logger.warning(f'[recovery] 补拉失败：{type(error).__name__}，不影响已持久化队列')
        from src.plugins.chat import dispatch_saved
        while not maintenance:
            queued=store.pending(bot_id)
            if not queued:break
            for row in queued:
                key=(row['bot'],row['grp'],row['msg'])
                if row['stamp']<time.time()-600:
                    store.mark(key,'expired');continue
                try:
                    event=GroupMessageEvent.model_validate_json(row['payload'])
                    await dispatch_saved(bot,event,replay=True)
                except asyncio.CancelledError:raise
                except Exception as error:
                    logger.warning(f'[recovery] 恢复失败：{type(error).__name__}')
                    return
        logger.info(f'[recovery] 恢复检查完成：{store.status()}')
    finally:recovering.discard(bot_id)


async def connected(bot: Bot):
    if not get_inbox():return
    key=str(bot.self_id)
    if key in _tasks and not _tasks[key].done():return
    snapshot=list(get_inbox().groups(key))
    recovering.add(key)
    task=asyncio.create_task(recover(bot,snapshot));_tasks[key]=task
    def complete(done):
        _tasks.pop(key,None)
        if not done.cancelled() and done.exception():logger.warning(f'[recovery] 任务失败：{type(done.exception()).__name__}')
    task.add_done_callback(complete)


async def stop():
    tasks=list(_tasks.values())
    for task in tasks:task.cancel()
    await asyncio.gather(*tasks,return_exceptions=True)
