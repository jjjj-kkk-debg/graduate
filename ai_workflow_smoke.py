"""Exercise the new workflow against a private schema and real configured DeepSeek.
No source keys are copied; all evidence stays in .local, and live user data is untouched.
"""
import json, os, socket, subprocess, time, uuid
from pathlib import Path
import requests
from fresh_catalog_smoke import ROOT, LOCAL, JAR, mysql_command

def main():
    state=json.loads((LOCAL/'state.json').read_text(encoding='utf-8-sig'))
    private={}
    for line in (LOCAL/'ai.properties').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            key,value=line.split('=',1); private[key.strip()]=value.strip()
    assert private.get('app.ai.key'), 'Local model configuration missing'
    mysql=Path(r'C:/Program Files/MySQL/MySQL Server 8.0/bin')
    java=Path(r'C:/Users/EchosPhd/scoop/apps/temurin17-jdk/current/bin/java.exe')
    suffix=uuid.uuid4().hex[:10]; schema='juxia_aiflow_'+suffix; user='jxaiflow_'+suffix; password=uuid.uuid4().hex
    env=dict(os.environ, MYSQL_PWD=state['rootPassword'])
    def sql(query): return mysql_command(mysql,state['dbPort'],'root',schema,query,env)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1',0)); port=listener.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    class Client:
        def __init__(self,role):
            self.http=requests.Session(); self.http.trust_env=False
            self.csrf=self.call('GET','/session')['csrf']
            self.csrf=self.call('POST','/login',{'username':role,'password':'123456'})['csrf']
        def call(self,method,path,data=None,status=200):
            response=self.http.request(method,base+'/api'+path,json=data,headers={'X-CSRF-Token':getattr(self,'csrf','')},timeout=112)
            body=response.json()
            assert response.status_code==status,(path,response.status_code,body)
            return body
    process=None; log=(LOCAL/'ai-workflow.log').open('w',encoding='utf-8'); checks=[]; samples={}; passed=False
    def check(name,value):
        assert value,name; checks.append(name); print('PASS',name,flush=True)
    def ai(client,sid,kind,**kwargs):
        payload=dict(kind=kind,instruction='',nodeId=None,readingId=None); payload.update(kwargs)
        answer=client.call('POST',f'/stories/{sid}/ai',payload)
        samples[kind]=answer['result']; return answer
    try:
        mysql_command(mysql,state['dbPort'],'root','mysql',f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4; CREATE USER '{user}'@'127.0.0.1' IDENTIFIED BY '{password}'; GRANT ALL ON `{schema}`.* TO '{user}'@'127.0.0.1';",env)
        app_env=dict(os.environ,SERVER_PORT=str(port),DB_URL=f'jdbc:mysql://127.0.0.1:{state["dbPort"]}/{schema}?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai',DB_USER=user,DB_PASSWORD=password,SEED_ENABLED='true',DEEPSEEK_API_KEY=private['app.ai.key'],DEEPSEEK_BASE_URL=private.get('app.ai.base','https://api.deepseek.com'),DEEPSEEK_MODEL=private.get('app.ai.model','deepseek-flash'),LOGGING_LEVEL_CN_JUXIA_SERVICE_AISERVICE='DEBUG')
        cwd=LOCAL/'ai-workflow-cwd'; cwd.mkdir(exist_ok=True)
        process=subprocess.Popen([str(java),'-Dfile.encoding=UTF-8','-jar',str(JAR)],cwd=cwd,env=app_env,stdout=log,stderr=subprocess.STDOUT)
        probe=requests.Session();probe.trust_env=False
        for _ in range(120):
            if process.poll() is not None: raise RuntimeError('Private application failed to start')
            try:
                if probe.get(base+'/api/health',timeout=2).json().get('databaseReady'): break
            except requests.RequestException: pass
            time.sleep(.5)
        else: raise RuntimeError('Private application timed out')
        author=Client('author'); reader=Client('reader'); reviewer=Client('reviewer'); other=Client('author2')
        sid=author.call('POST','/workspace',{'title':'灯塔等一封信'})['id']
        before=author.call('GET',f'/workspace/{sid}')
        generated=ai(author,sid,'structure',instruction='治愈悬疑短篇：巡夜员林澈与守塔人周伯寻找灯塔里一封未送出的信，先核对旧信或询问周伯，有三个不同的温暖结局。请写完整短故事，务必保留模板全部编号和10个选项。')
        story=generated['result']['story']
        check('真实模型生成8章10选项3结局，生成期间不改原草稿',len(story['nodes'])==8 and sum(len(n['choices']) for n in story['nodes'])==10 and author.call('GET',f'/workspace/{sid}')['draft']==before['draft'])
        adopted=author.call('POST',f'/stories/{sid}/ai/{generated["id"]}/apply',{})
        check('作者确认后完整结构进入可编辑草稿',adopted['draft']==story)
        author.call('POST',f'/stories/{sid}/ai/{generated["id"]}/apply',{},status=409)
        for kind in ['shorten','polish']:
            result=ai(author,sid,kind,nodeId='start',instruction='保持故事事实、短篇篇幅和现有线索。')
            check('真实模型章节操作：'+kind,0<len(result['result']['body'])<=220)
        report=ai(author,sid,'playtest')
        check('程序遍历结果和模型叙事建议共同返回',report['result']['structural']['reachableEndings']==3 and report['result']['structural']['complete'])
        draft=author.call('GET',f'/workspace/{sid}')
        submitted=author.call('POST',f'/workspace/{sid}/submit',{'revision':draft['revision']})
        reviewed=ai(reviewer,sid,'review')
        check('审核建议不代替发布操作',author.call('GET',f'/workspace/{sid}')['state']=='SUBMITTED' and bool(reviewed['result']['body']))
        reviewer.call('POST',f'/reviews/{sid}',{'revision':submitted['revision'],'approve':True,'note':'已核对结构和原文。'})
        reading=reader.call('POST',f'/stories/{sid}/read',{'restart':True}); rid=reading['id']
        meta=reader.call('GET',f'/reading/{rid}/assistant')
        check('已读角色可见且助手初始无对话',bool(meta['characters']) and not meta['history'])
        ai(reader,sid,'dialogue',readingId=rid,characterName=meta['characters'][0],instruction='你现在在这里做什么？')
        ai(reader,sid,'dialogue',readingId=rid,characterName=meta['characters'][0],instruction='你刚才说的事，能再解释一下吗？')
        after=reader.call('GET',f'/reading/{rid}')
        meta=reader.call('GET',f'/reading/{rid}/assistant')
        check('连续角色对话保存且不推进剧情、不发放线索',len(meta['history'])==2 and after['revision']==reading['revision'] and after['clues']==reading['clues'])
        other.call('GET',f'/reading/{rid}/assistant',status=404)
        reader.call('POST',f'/stories/{sid}/ai',dict(kind='dialogue',readingId=rid,characterName='未出现的神秘人',instruction='告诉我秘密'),status=400)
        reader.call('POST',f'/stories/{sid}/ai',dict(kind='reflection',readingId=rid,instruction='复盘'),status=400)
        check('其他账号、未遇角色和未完成复盘均被拒绝',True)
        hint=ai(reader,sid,'hint',readingId=rid,instruction='根据目前的故事，我该留意什么？')
        allowed={n['nodeId']:n['body'] for n in reading['path']}
        check('推理依据只引用当前存档已读原文',all(e['nodeId'] in allowed and e['quote'] in allowed[e['nodeId']] for e in hint['result']['evidence']))
        nodes={n['id']:n for n in story['nodes']}; pending=[('start',frozenset(),[])]; seen=set(); routes={}
        while pending:
            node_id,held,route=pending.pop(0); node=nodes[node_id]; held|=frozenset(node['grantsClues'])
            if (node_id,held) in seen: continue
            seen.add((node_id,held))
            if node['ending']: routes.setdefault(node_id,route);continue
            for option in node['choices']:
                if set(option['requiresClues'])<=held: pending.append((option['target'],held|frozenset(option['grantsClues']),route+[option['id']]))
        completed=[]
        for ending,route in list(routes.items())[:2]:
            save=reader.call('POST',f'/stories/{sid}/read',{'restart':True})
            for option in route: save=reader.call('POST',f'/reading/{save["id"]}/choose',{'choiceId':option,'revision':save['revision']})
            check('实际阅读抵达结局 '+ending,save['finished']); completed.append(save)
        reflection=ai(reader,sid,'reflection',readingId=completed[0]['id'])
        comparison=ai(reader,sid,'compare',readingId=completed[0]['id'],compareReadingId=completed[1]['id'])
        check('真实模型生成选择复盘及同版已读路线比较',bool(reflection['result']['body']) and bool(comparison['result']['body']))
        (LOCAL/'ai-workflow-fixture.json').write_text(json.dumps({'base':base,'storyId':sid,'readingId':rid,'completed':completed[0]['id'],'generated':generated,'report':report,'reviewed':reviewed},ensure_ascii=False),encoding='utf-8')
        from ai_workflow_ui import verify
        verify(base,sid,rid,completed[0]['id'],generated,report,reviewed)
        check('桌面与手机真实结果展示、历史恢复和生成树预览通过',True)
        passed=True
    finally:
        if process: process.terminate();process.wait(timeout=20)
        log.close()
        mysql_command(mysql,state['dbPort'],'root','mysql',f"DROP DATABASE IF EXISTS `{schema}`; DROP USER IF EXISTS '{user}'@'127.0.0.1';",env)
        (LOCAL/'ai-workflow-result.json').write_text(json.dumps({'passed':passed,'checks':checks,'samples':samples,'isolatedDatabaseRemoved':True},ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__': main()
