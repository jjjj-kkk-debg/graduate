"""Compact a private v4 fixture; prove edits, old saves and publications survive."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
import requests
import bcrypt
from fresh_catalog_smoke import ROOT, LOCAL, JAR, mysql_command


def main():
    state = json.loads((LOCAL / 'state.json').read_text(encoding='utf-8-sig'))
    mysql = Path(os.environ.get('JUXIA_MYSQL_HOME', r'C:\Program Files\MySQL\MySQL Server 8.0')) / 'bin'
    java = Path(os.environ.get('JUXIA_JAVA_HOME', r'C:\Users\EchosPhd\scoop\apps\temurin17-jdk\current')) / 'bin/java.exe'
    env = dict(os.environ, MYSQL_PWD=state['rootPassword'])
    suffix = uuid.uuid4().hex[:10]
    schema = 'juxia_expand_' + suffix
    user = 'jxexpand_' + suffix
    password = uuid.uuid4().hex
    app = None
    log = (LOCAL / 'expanded-upgrade.log').open('w', encoding='utf-8')
    cwd = LOCAL / 'expanded-upgrade-cwd'; cwd.mkdir(exist_ok=True)
    http = requests.Session(); http.trust_env = False
    checks = []
    result = {'passed': False, 'checks': checks}

    def sql(statement):
        return mysql_command(mysql, state['dbPort'], 'root', schema, statement, env)

    def start(seed):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        app_env = dict(os.environ, SERVER_PORT=str(port),
                       DB_URL=f'jdbc:mysql://127.0.0.1:{state["dbPort"]}/{schema}?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai',
                       DB_USER=user, DB_PASSWORD=password, SEED_ENABLED=str(seed).lower())
        process = subprocess.Popen([str(java), '-Dfile.encoding=UTF-8', '-jar', str(JAR)], cwd=cwd,
                                   env=app_env, stdout=log, stderr=subprocess.STDOUT)
        base = f'http://127.0.0.1:{port}'
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError('Isolated catalog upgrade did not start.')
            try:
                ready = http.get(base + '/api/health', timeout=2)
                if ready.ok and (not seed or ready.json().get('databaseReady')):
                    if not seed or sql("SELECT COUNT(*) FROM demo_catalog WHERE catalog_key='interactive-stories-v5';") == '1':
                        return process, base
            except requests.RequestException:
                pass
            time.sleep(.5)
        process.terminate(); process.wait(timeout=20)
        raise RuntimeError('Isolated catalog upgrade timed out.')

    def check(name, condition):
        assert condition, name
        checks.append(name); print('PASS', name, flush=True)

    try:
        mysql_command(mysql, state['dbPort'], 'root', 'mysql',
                      f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                      f"CREATE USER '{user}'@'127.0.0.1' IDENTIFIED BY '{password}'; "
                      f"GRANT ALL PRIVILEGES ON `{schema}`.* TO '{user}'@'127.0.0.1';", env)
        app, _ = start(False)
        app.terminate(); app.wait(timeout=20); app = None
        sql("INSERT INTO users(username,password_hash,display_name,role) VALUES "
            "('reader','unused','旧读者','reader'),('author','unused','沈知遥','author'),"
            "('author2','unused','陆听澜','author'),('reviewer','unused','编辑','reviewer'),('admin','unused','管理员','admin');")
        password_hash = bcrypt.hashpw(b'123456', bcrypt.gensalt()).hex()
        sql(f"UPDATE users SET password_hash=CONVERT(0x{password_hash} USING utf8mb4);")
        catalog = json.loads((ROOT / 'backend/src/main/resources/demo-stories-v4.json').read_text(encoding='utf-8-sig'))
        for index, document in enumerate(catalog):
            draft = json.loads(json.dumps(document, ensure_ascii=False))
            if index == 0:
                draft['nodes'][0]['body'] += '\n学生自己补写的情节，必须保留。'
            payload = json.dumps(draft, ensure_ascii=False).encode('utf-8').hex()
            original = json.dumps(document, ensure_ascii=False).encode('utf-8').hex()
            status = 'PUBLISHED' if index < 12 else 'DRAFT' if index == 12 else 'SUBMITTED'
            # Index 1 is untouched but has revision 2 from a previous upgrade.
            story = sql(f"INSERT INTO stories(author_id,state,revision,draft) VALUES ({2 + index % 2},'{status}',{2 if index == 1 else 1},CONVERT(0x{payload} USING utf8mb4)); SELECT LAST_INSERT_ID();")
            if index < 12:
                if index == 2:
                    publication = json.loads(json.dumps(document, ensure_ascii=False))
                    publication['nodes'][0]['body'] += '\n已经发布的学生修订版，必须保留。'
                    original = json.dumps(publication, ensure_ascii=False).encode('utf-8').hex()
                pub = sql(f"INSERT INTO publications(story_id,version,document) VALUES ({story},1,CONVERT(0x{original} USING utf8mb4)); SELECT LAST_INSERT_ID();")
                sql(f"UPDATE stories SET published_id={pub} WHERE id={story};")
                if index == 1:
                    first_choice = next(c['id'] for c in document['nodes'][0]['choices'] if c['target'] == 'path_a')
                    sql(f"INSERT INTO readings(user_id,story_id,publication_id,current_node,revision) VALUES (1,{story},{pub},'path_a',1); "
                        f"INSERT INTO reading_steps(reading_id,step_no,node_id,choice_id) VALUES (LAST_INSERT_ID(),0,'start','{first_choice}'),(LAST_INSERT_ID(),1,'path_a',NULL);")
        sql("INSERT INTO demo_catalog(catalog_key) VALUES ('interactive-stories-v3'),('interactive-stories-v4');")
        original_readings = sql('SELECT id,story_id,publication_id,current_node,revision,clues FROM readings;')
        original_steps = sql('SELECT reading_id,step_no,node_id,choice_id FROM reading_steps ORDER BY id;')
        original_publications = sql('SELECT id,MD5(CAST(document AS CHAR)) FROM publications ORDER BY id;')
        protected = sql('SELECT id,revision,state,MD5(CAST(draft AS CHAR)),published_id FROM stories WHERE id IN (1,3) ORDER BY id;')

        app, base = start(True)
        check('学生编辑的草稿及发布快照完全保留', sql('SELECT id,revision,state,MD5(CAST(draft AS CHAR)),published_id FROM stories WHERE id IN (1,3) ORDER BY id;') == protected)
        check('未修改的12部作品精简为8章10个选择，包括两份创作示例', sql("SELECT COUNT(*) FROM stories WHERE JSON_LENGTH(JSON_EXTRACT(draft,'$.nodes'))=8;") == '12')
        check('已是修订2的原样种子可继续升级', sql('SELECT CONCAT(revision,\':\',JSON_LENGTH(JSON_EXTRACT(draft,\'$.nodes\'))) FROM stories WHERE id=2;') == '3:8')
        check('旧存档与逐步选择原样保留', sql('SELECT id,story_id,publication_id,current_node,revision,clues FROM readings;') == original_readings and sql('SELECT reading_id,step_no,node_id,choice_id FROM reading_steps ORDER BY id;') == original_steps)
        check('全部旧发布快照不可变', sql('SELECT id,MD5(CAST(document AS CHAR)) FROM publications WHERE id<=12 ORDER BY id;') == original_publications)
        check('新发布版本与旧存档分离', sql('SELECT CONCAT(p.version,\':\',JSON_LENGTH(JSON_EXTRACT(p.document,\'$.nodes\'))) FROM stories s JOIN publications p ON p.id=s.published_id WHERE s.id=2;') == '2:8')
        check('未新建阅读记录、未重复添加作品', sql('SELECT CONCAT((SELECT COUNT(*) FROM stories),\':\',(SELECT COUNT(*) FROM readings));') == '14:1')
        fingerprint = sql('SELECT id,revision,published_id,MD5(CAST(draft AS CHAR)) FROM stories ORDER BY id;')
        pub_count = sql('SELECT COUNT(*) FROM publications;')
        app.terminate(); app.wait(timeout=20); app = None
        app, base = start(True)
        check('再次启动不重复升级或增加发布版本', sql('SELECT id,revision,published_id,MD5(CAST(draft AS CHAR)) FROM stories ORDER BY id;') == fingerprint and sql('SELECT COUNT(*) FROM publications;') == pub_count)
        from playwright.sync_api import sync_playwright, expect
        screenshots = LOCAL / 'compact-ui'; screenshots.mkdir(exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                                                  headless=True, args=['--no-proxy-server'])
            errors = []

            def login(role):
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/#/login', wait_until='networkidle')
                page.get_by_label('账号', exact=True).fill(role)
                page.get_by_label('密码', exact=True).fill('123456')
                page.get_by_role('button', name='进入工作空间').click()
                expect(page.get_by_role('button', name='退出', exact=True)).to_be_visible()
                return page

            reader = login('reader')
            reader.goto(base + '/#/stories/2')
            expect(reader.get_by_role('button', name='从最新版重新开始')).to_be_visible()
            expect(reader.get_by_text('这本书已更新内容。继续阅读可保留原版进度，重新开始会新建最新版存档。')).to_be_visible()
            reader.get_by_role('button', name='继续上次阅读').click()
            expect(reader.locator('.reading-top')).to_contain_text('版本 1')
            reader.goto(base + '/#/stories/2')
            reader.get_by_role('button', name='从最新版重新开始').click()
            expect(reader.locator('.reading-top')).to_contain_text('版本 2')
            expect(reader.locator('.reading-text')).to_be_visible()
            reader.screenshot(path=str(screenshots / 'latest-reading.png'), full_page=True)
            check('页面继续原版及重开最新版两个入口均可用', sql('SELECT publication_id,current_node,revision FROM readings WHERE id=1;').endswith('\tpath_a\t1'))

            author = login('author')
            author.goto(base + '/#/workspace/13')
            author.get_by_role('button', name='分支图', exact=True).click()
            expect(author.locator('.graph-node')).to_have_count(8)
            author.get_by_role('button', name='全图', exact=True).click()
            author.screenshot(path=str(screenshots / 'expanded-tree-desktop.png'), full_page=True)
            author.get_by_role('button', name='放大分支图', exact=True).click()
            chapter = author.locator('.graph-node').nth(1).get_attribute('aria-label').removeprefix('编辑章节：')
            author.locator('.graph-node').nth(1).press('Enter')
            expect(author.locator('.chapter-title-field input')).to_have_value(chapter)
            author.get_by_role('button', name='分支图', exact=True).click()
            expect(author.locator('.graph-node').nth(1)).to_have_attribute('aria-pressed', 'true')
            author.get_by_role('button', name='当前章', exact=True).click()
            for page in (reader, author):
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.screenshot(path=str(screenshots / ('expanded-tree-mobile.png' if page == author else 'latest-reading-mobile.png')), full_page=True)
            check('8章故事树可缩放定位键盘选章，手机阅读与树图不溢出', not errors)
            browser.close()

        csrf = http.get(base + '/api/session').json()['csrf']
        signed_in = http.post(base + '/api/login', json={'username': 'reader', 'password': '123456'}, headers={'X-CSRF-Token': csrf})
        headers = {'X-CSRF-Token': signed_in.json()['csrf']}
        latest = json.loads((ROOT / 'backend/src/main/resources/demo-stories.json').read_text(encoding='utf-8'))[1]
        by_id = {node['id']: node for node in latest['nodes']}
        pending = [('start', frozenset(), [])]; seen = set(); routes = {}
        while pending:
            node_id, held, route = pending.pop(0)
            node = by_id[node_id]; held = held | frozenset(node['grantsClues'])
            if (node_id, held) in seen:
                continue
            seen.add((node_id, held))
            if node['ending']:
                routes.setdefault(node_id, route); continue
            for choice in node['choices']:
                if set(choice['requiresClues']) <= held:
                    pending.append((choice['target'], held | frozenset(choice['grantsClues']), route + [choice['id']]))
        for ending, route in routes.items():
            reading = http.post(base + '/api/stories/2/read', json={'restart': True}, headers=headers).json()
            for choice in route:
                response = http.post(base + f'/api/reading/{reading["id"]}/choose', json={'choiceId': choice, 'revision': reading['revision']}, headers=headers)
                assert response.ok, response.status_code
                reading = response.json()
            assert reading['finished'] and reading['node']['id'] == ending
        check('新版三种结局均通过实际阅读API到达，包括有线索门槛的路线', len(routes) == 3 and reading['exploration']['total'] == 3)
        result['passed'] = True
    finally:
        if app:
            app.terminate(); app.wait(timeout=20)
        log.close()
        mysql_command(mysql, state['dbPort'], 'root', 'mysql',
                      f"DROP DATABASE IF EXISTS `{schema}`; DROP USER IF EXISTS '{user}'@'127.0.0.1';", env)
        result['isolatedSchemaRemoved'] = True
        (LOCAL / 'expanded-upgrade-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
