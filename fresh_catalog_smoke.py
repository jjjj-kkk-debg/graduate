"""Start the packaged JAR against an isolated empty MySQL schema and exercise clue gates.
The schema, account, app process and QA users created here are always cleaned up.
"""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid
import requests

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'
JAR = ROOT / 'backend/target/juxia-1.0.0.jar'
REPORT = LOCAL / 'catalog-acceptance.json'
CHECKS = []


def check(name, condition):
    assert condition, name
    CHECKS.append(name)
    print('PASS', name, flush=True)


def mysql_command(bin_path, port, user, database, sql, env):
    command = [str(bin_path / 'mysql.exe'), '--no-defaults', '--protocol=TCP', '--host=127.0.0.1',
               f'--port={port}', f'--user={user}', '--default-character-set=utf8mb4', '--batch', '--skip-column-names']
    if database:
        command.append(database)
    result = subprocess.run(command, input=sql, capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
    if result.returncode:
        raise RuntimeError('Isolated MySQL smoke-test setup failed.')
    return result.stdout.strip()


def main():
    state = json.loads((LOCAL / 'state.json').read_text(encoding='utf-8-sig'))
    mysql_home = Path(os.environ.get('JUXIA_MYSQL_HOME', r'C:\Program Files\MySQL\MySQL Server 8.0'))
    mysql_bin = mysql_home / 'bin'
    assert (mysql_bin / 'mysql.exe').is_file() and JAR.is_file()
    root_env = dict(os.environ, MYSQL_PWD=state['rootPassword'])
    suffix = uuid.uuid4().hex[:10]
    schema = 'juxia_smoke_' + suffix
    db_user = 'jxsmoke_' + suffix
    db_password = uuid.uuid4().hex
    port = 0
    app = None
    smoke_cwd = LOCAL / 'smoke-cwd'
    smoke_cwd.mkdir(exist_ok=True)
    session = requests.Session(); session.trust_env = False
    outcome = {'checks': CHECKS, 'passed': False}
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        check('只分配本次随机隔离端口与数据库名', bool(re.fullmatch(r'juxia_smoke_[0-9a-f]{10}', schema)))
        mysql_command(mysql_bin, state['dbPort'], 'root', 'mysql',
                      f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                      f"CREATE USER '{db_user}'@'127.0.0.1' IDENTIFIED BY '{db_password}'; "
                      f"GRANT ALL PRIVILEGES ON `{schema}`.* TO '{db_user}'@'127.0.0.1';", root_env)
        app_env = dict(os.environ, SERVER_PORT=str(port),
                       DB_URL=f'jdbc:mysql://127.0.0.1:{state["dbPort"]}/{schema}?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai',
                       DB_USER=db_user, DB_PASSWORD=db_password, SEED_ENABLED='true')
        log_file = (LOCAL / 'catalog-smoke.log').open('w', encoding='utf-8')
        app = subprocess.Popen([str(Path(os.environ.get('JUXIA_JAVA_HOME', r'C:\Users\EchosPhd\scoop\apps\temurin17-jdk\current')) / 'bin/java.exe'),
                                '-Dfile.encoding=UTF-8', '-jar', str(JAR)], cwd=smoke_cwd, env=app_env,
                               stdout=log_file, stderr=subprocess.STDOUT)
        base = f'http://127.0.0.1:{port}'
        for _ in range(80):
            if app.poll() is not None:
                raise RuntimeError('Packaged JAR failed against the isolated empty schema.')
            try:
                health = session.get(base + '/api/health', timeout=2)
                if health.ok and health.json().get('databaseReady'):
                    break
            except requests.RequestException:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError('Isolated packaged application did not become ready.')
        stories = session.get(base + '/api/stories', timeout=5).json()
        check('空库初始化生成12部公开故事和2份创作流程示例', len(stories) == 12)
        catalog_sizes = {s['title']: (s['nodeCount'], s['choiceCount'], s['endingCount'], s['clueCount']) for s in stories}
        check('每部新故事含8章、10项选择、3个结局、2条线索',
              all(size == (8, 10, 3, 2) for size in catalog_sizes.values()))
        totals = mysql_command(mysql_bin, state['dbPort'], 'root', schema,
                               "SELECT CONCAT((SELECT COUNT(*) FROM stories), ':', "
                               "(SELECT COUNT(*) FROM publications), ':', "
                               "(SELECT COUNT(*) FROM readings WHERE is_demo=TRUE), ':', "
                               "(SELECT COUNT(DISTINCT DATE(created_at)) FROM readings WHERE is_demo=TRUE));", root_env)
        check('种子共14部作品（含2份创作流程示例）、12部发布作品和84份七日阅读记录', totals == '14:12:84:7')

        csrf = session.get(base + '/api/session').json()['csrf']
        write_headers = {'X-CSRF-Token': csrf}
        login = session.post(base + '/api/login', json={'username': 'admin', 'password': '123456'}, headers=write_headers)
        check('空库初始管理员可登录', login.ok and login.json()['user']['role'] == 'admin')
        csrf = login.json()['csrf']; write_headers['X-CSRF-Token'] = csrf
        prefix = 'qa_' + uuid.uuid4().hex[:10] + '_'
        for role in ('author', 'reader'):
            created = session.post(base + '/api/admin/users', json={'username': prefix + role, 'displayName': '玩法验收' + role,
                                       'role': role, 'password': '123456'}, headers=write_headers)
            check('临时创建测试角色:' + role, created.ok)
        author = requests.Session(); author.trust_env = False
        csrf = author.get(base + '/api/session').json()['csrf']
        login = author.post(base + '/api/login', json={'username': prefix + 'author', 'password': '123456'},
                            headers={'X-CSRF-Token': csrf})
        check('临时作者账号登录', login.ok)
        author_headers = {'X-CSRF-Token': login.json()['csrf']}
        story_id = author.post(base + '/api/workspace', json={'title': 'QA线索解锁流程'}, headers=author_headers).json()['id']
        work = author.get(base + f'/api/workspace/{story_id}').json()
        doc = work['draft']
        doc['clues'] = [
            {'id': 'secret', 'title': '修船匠的记号', 'description': '在检修记录里找到的旧标记。'},
            {'id': 'receipt', 'title': '查阅凭证', 'description': '选择查看维修记录后获得。'},
            {'id': 'late', 'title': '结局证据', 'description': '仅在结局获得。'}]
        doc['world'] = '维修记录提供可核实的线索，章节选择决定调查方向。'
        start = next(n for n in doc['nodes'] if n['id'] == 'start')
        start['body'] = '站台边的维修登记本缺了最后一页。'
        start['choices'] = [
            {'id': 'search', 'label': '先查看维修记录', 'target': 'path', 'requiresClues': [], 'grantsClues': ['receipt']},
            {'id': 'secret', 'label': '没有线索也强行追问', 'target': 'end_b', 'requiresClues': ['secret'], 'grantsClues': []},
            {'id': 'rush', 'label': '不再追查，先回家', 'target': 'end_a', 'requiresClues': [], 'grantsClues': []}]
        path = {'id': 'path', 'title': '维修记录', 'body': '旧记录角落有一行被涂改的符号。', 'ending': False,
                'endingTitle': '', 'grantsClues': ['secret'], 'choices': [
                    {'id': 'accept', 'label': '接受记录给出的答案', 'target': 'end_a', 'requiresClues': [], 'grantsClues': []},
                    {'id': 'solve', 'label': '用标记追问到底', 'target': 'end_b', 'requiresClues': ['secret'], 'grantsClues': []}]}
        doc['nodes'].append(path)
        for node in doc['nodes']:
            if node['id'].startswith('end_'):
                node['body'] = '调查在这里暂时告一段落。'
            if node['id'] == 'end_b':
                node['grantsClues'] = ['late']
        saved = author.put(base + f'/api/workspace/{story_id}', json={'revision': work['revision'], 'draft': doc}, headers=author_headers)
        check('作者可以配置线索、章节奖励和锁定路线', saved.ok and not saved.json()['issues'])
        from editor_preview import verify
        verify(base, prefix + 'author', story_id)
        check('桌面平板手机草稿试玩遵守线索门槛，新增章节与选项正常', True)
        submitted = author.post(base + f'/api/workspace/{story_id}/submit', json={'revision': saved.json()['revision']}, headers=author_headers)
        reviewer = requests.Session(); reviewer.trust_env = False
        csrf = reviewer.get(base + '/api/session').json()['csrf']
        login = reviewer.post(base + '/api/login', json={'username': 'reviewer', 'password': '123456'}, headers={'X-CSRF-Token': csrf})
        published = reviewer.post(base + f'/api/reviews/{story_id}', json={'revision': submitted.json()['revision'], 'approve': True,
                                    'note': '线索触发与分支目标已检查。'}, headers={'X-CSRF-Token': login.json()['csrf']})
        check('特殊分支经审核后发布', published.ok)
        reader = requests.Session(); reader.trust_env = False
        csrf = reader.get(base + '/api/session').json()['csrf']
        login = reader.post(base + '/api/login', json={'username': prefix + 'reader', 'password': '123456'}, headers={'X-CSRF-Token': csrf})
        reader_headers = {'X-CSRF-Token': login.json()['csrf']}
        reading = reader.post(base + f'/api/stories/{story_id}/read', json={'restart': True}, headers=reader_headers).json()
        blocked = next(option for option in reading['node']['choices'] if option['id'] == 'secret')
        check('未发现线索时特殊选项显示锁定及线索名称', not blocked['available'] and blocked['missingClues'][0]['title'] == '修船匠的记号')
        denied = reader.post(base + f'/api/reading/{reading["id"]}/choose', json={'choiceId': 'secret', 'revision': 0}, headers=reader_headers)
        check('伪造请求不能绕过线索锁', denied.status_code == 400)
        reading = reader.post(base + f'/api/reading/{reading["id"]}/choose', json={'choiceId': 'search', 'revision': 0}, headers=reader_headers).json()
        option = next(choice for choice in reading['node']['choices'] if choice['id'] == 'solve')
        check('进入线索章节后采集记录且解锁路线', {clue['id'] for clue in reading['clues']} == {'secret', 'receipt'} and option['available'])
        ending = reader.post(base + f'/api/reading/{reading["id"]}/choose', json={'choiceId': 'solve', 'revision': 1}, headers=reader_headers).json()
        check('解锁选择能到达新的结局', ending['finished'] and ending['node']['endingTitle'] == '另一种可能')
        check('结局收集仅展示已经抵达的结局', len(ending['exploration']['discovered']) == 1
              and ending['exploration']['discovered'][0]['id'] == 'end_b')
        fork_url = base + f'/api/reading/{ending["id"]}/fork'
        forked = reader.post(fork_url, json={'stepNo': 1, 'revision': 2}, headers=reader_headers).json()
        check('历史章节分叉保留前置选择与章节线索，清除后续奖励',
              forked['id'] != ending['id'] and forked['node']['id'] == 'path'
              and {clue['id'] for clue in forked['clues']} == {'secret', 'receipt'}
              and forked['path'][-1]['choice'] == '' and forked['revision'] == 1)
        original = reader.get(base + f'/api/reading/{ending["id"]}').json()
        check('另开分支不修改原存档', original['node']['id'] == 'end_b' and original['revision'] == 2
              and {clue['id'] for clue in original['clues']} == {'secret', 'receipt', 'late'})
        fork_start = reader.post(fork_url, json={'stepNo': 0, 'revision': 2}, headers=reader_headers).json()
        check('从开头分叉不会携带未来线索', not fork_start['clues'] and not fork_start['node']['choices'][1]['available'])
        stale = reader.post(fork_url, json={'stepNo': 0, 'revision': 1}, headers=reader_headers)
        future = reader.post(fork_url, json={'stepNo': 3, 'revision': 2}, headers=reader_headers)
        reviewer_csrf = reviewer.get(base + '/api/session').json()['csrf']
        foreign = reviewer.post(fork_url, json={'stepNo': 0, 'revision': 2}, headers={'X-CSRF-Token': reviewer_csrf})
        check('过期进度、未来章节及他人存档分叉被拒绝', stale.status_code == 409 and future.status_code == 400 and foreign.status_code == 404)
        other_ending = reader.post(base + f'/api/reading/{forked["id"]}/choose', json={'choiceId': 'accept', 'revision': 1}, headers=reader_headers).json()
        check('分支可独立抵达其他结局并累计收集', other_ending['finished'] and len(other_ending['exploration']['discovered']) == 2)
        work = author.post(base + f'/api/workspace/{story_id}/revise', json={}, headers=author_headers).json()
        doc['world'] += ' 第二版补充设定。'
        saved = author.put(base + f'/api/workspace/{story_id}', json={'revision': work['revision'], 'draft': doc}, headers=author_headers).json()
        submitted = author.post(base + f'/api/workspace/{story_id}/submit', json={'revision': saved['revision']}, headers=author_headers).json()
        published = reviewer.post(base + f'/api/reviews/{story_id}', json={'revision': submitted['revision'], 'approve': True,
                                  'note': '第二版可发布。'}, headers={'X-CSRF-Token': reviewer_csrf})
        check('作者修改后可以发布第二版', published.ok)
        newest = reader.post(base + f'/api/stories/{story_id}/read', json={'restart': True}, headers=reader_headers).json()
        old_fork = reader.post(fork_url, json={'stepNo': 1, 'revision': 2}, headers=reader_headers).json()
        check('分叉沿用原发布版本，结局收集不跨版本泄露', old_fork['publicationVersion'] == 1
              and newest['publicationVersion'] == 2 and not newest['exploration']['discovered'])
        outcome['passed'] = True
    finally:
        if app is not None:
            app.terminate()
            try: app.wait(timeout=15)
            except subprocess.TimeoutExpired: raise RuntimeError('Isolated QA process did not stop.')
        try:
            mysql_command(mysql_bin, state['dbPort'], 'root', 'mysql',
                          f"DROP DATABASE IF EXISTS `{schema}`; DROP USER IF EXISTS '{db_user}'@'127.0.0.1';", root_env)
            outcome['isolatedSchemaRemoved'] = True
        finally:
            outcome['checkCount'] = len(CHECKS)
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Report:', REPORT)


if __name__ == '__main__':
    main()
