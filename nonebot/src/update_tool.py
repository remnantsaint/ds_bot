"""先暂停接新回复并排空，再执行外部容器更新；新消息继续持久化。"""
import http.cookiejar
import json
import os
import time
import urllib.request


def main():
    jar=http.cookiejar.CookieJar()
    client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base='http://127.0.0.1:'+os.getenv('PORT','8080')
    def request(path,body):
        req=urllib.request.Request(base+'/admin/api/'+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Admin-Request':'1'})
        return json.load(client.open(req,timeout=12))
    request('login',{'password':os.environ['ADMIN_PASSWORD']})
    try:
        deadline=time.monotonic()+190
        while True:
            state=request('maintenance',{'enabled':True})
            if state['pending']==0:
                print('Ready to restart: incoming messages are persisted; restart now or POST maintenance enabled=false to resume.')
                return
            if time.monotonic()>deadline:raise TimeoutError('仍有处理中的消息，本次更新准备已取消')
            time.sleep(1)
    except BaseException:
        request('maintenance',{'enabled':False})
        raise

if __name__=='__main__':main()
