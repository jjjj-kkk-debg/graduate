"""Verify the v1 catalog upgrades by stable title without replacing student edits."""
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid
import requests

from fresh_catalog_smoke import LOCAL, JAR, mysql_command

ROOT = Path(__file__).resolve().parents[1]


def main():
    state = json.loads((LOCAL / 'state.json').read_text(encoding='utf-8-sig'))
    mysql_bin = Path(os.environ.get('JUXIA_MYSQL_HOME', r'C:\Program Files\MySQL\MySQL Server 8.0')) / 'bin'
    java = Path(os.environ.get('JUXIA_JAVA_HOME', r'C:\Users\EchosPhd\scoop\apps\temurin17-jdk\current')) / 'bin/java.exe'
    old_catalog = json.loads((ROOT / 'backend/src/main/resources/demo-stories-v1.json').read_text(encoding='utf-8-sig'))
    suffix = uuid.uuid4().hex[:10]
    schema = 'juxia_upgrade_' + suffix
    db_user = 'jxupgrade_' + suffix
    db_password = uuid.uuid4().hex
    root_env = dict(os.environ, MYSQL_PWD=state['rootPassword'])
    app = None
    port = 0
    session = requests.Session(); session.trust_env = False
    cwd = LOCAL / 'upgrade-cwd'; cwd.mkdir(exist_ok=True)
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        mysql_command(mysql_bin, state['dbPort'], 'root', 'mysql',
                      f"CREATE DATABASE `{schema}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; "
                      f"CREATE USER '{db_user}'@'127.0.0.1' IDENTIFIED BY '{db_password}'; "
                      f"GRANT ALL PRIVILEGES ON `{schema}`.* TO '{db_user}'@'127.0.0.1';", root_env)
        old_schema = (ROOT / 'backend/src/main/resources/db/migration/V1__juxia.sql').read_text(encoding='utf-8-sig')
        mysql_command(mysql_bin, state['dbPort'], 'root', schema, old_schema, root_env)

        inserts = ["INSERT INTO users(username,password_hash,display_name,role) VALUES "
                   "('reader','not-used','旧版读者','reader'),('author','not-used','沈知遥','author'),"
                   "('author2','not-used','陆听澜','author'),('reviewer','not-used','内容编辑','reviewer'),"
                   "('admin','not-used','平台管理员','admin');"]
        mysql_command(mysql_bin, state['dbPort'], 'root', schema, inserts[0], root_env)
        user_ids = {username: int(user_id) for user_id, username in
                    (line.split('\t') for line in mysql_command(mysql_bin, state['dbPort'], 'root', schema,
                             'SELECT id,username FROM users;', root_env).splitlines())}

        for index, document in enumerate(old_catalog):
            if index == 0:
                document = json.loads(json.dumps(document, ensure_ascii=False))
                document['nodes'][0]['body'] += '\n学生已编辑的内容。'
            payload = json.dumps(document, ensure_ascii=False, separators=(',', ':')).encode('utf-8').hex()
            author = user_ids['author'] if index % 2 == 0 else user_ids['author2']
            story_id = mysql_command(mysql_bin, state['dbPort'], 'root', schema,
                f"INSERT INTO stories(author_id,state,draft) VALUES ({author},'PUBLISHED',CONVERT(0x{payload} USING utf8mb4)); "
                "SELECT LAST_INSERT_ID();", root_env)
            publication_id = mysql_command(mysql_bin, state['dbPort'], 'root', schema,
                f"INSERT INTO publications(story_id,version,document) "
                f"SELECT {story_id},1,draft FROM stories WHERE id={story_id}; SELECT LAST_INSERT_ID();", root_env)
            mysql_command(mysql_bin, state['dbPort'], 'root', schema,
                f"UPDATE stories SET published_id={publication_id} WHERE id={story_id};", root_env)
            if index == 0:
                mysql_command(mysql_bin, state['dbPort'], 'root', schema,
                    f"INSERT INTO readings(user_id,story_id,publication_id,current_node) "
                    f"VALUES ({user_ids['reader']},{story_id},{publication_id},'start');", root_env)

        env = dict(os.environ, SERVER_PORT=str(port),
                   DB_URL=f'jdbc:mysql://127.0.0.1:{state["dbPort"]}/{schema}?useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai',
                   DB_USER=db_user, DB_PASSWORD=db_password, SEED_ENABLED='true',
                   SPRING_FLYWAY_BASELINE_ON_MIGRATE='true')
        log_file = (LOCAL / 'upgrade-smoke.log').open('w', encoding='utf-8')
        app = subprocess.Popen([str(java), '-Dfile.encoding=UTF-8', '-jar', str(JAR)], cwd=cwd, env=env,
                               stdout=log_file, stderr=subprocess.STDOUT)
        base = f'http://127.0.0.1:{port}'
        for _ in range(80):
            if app.poll() is not None:
                raise RuntimeError('Packaged JAR failed while upgrading the isolated v1 database.')
            try:
                health = session.get(base + '/api/health', timeout=2)
                if health.ok and health.json().get('databaseReady'):
                    break
            except requests.RequestException:
                pass
            time.sleep(.5)
        else:
            raise RuntimeError('Isolated v1 database upgrade did not become ready.')

        stories = []
        for _ in range(80):
            stories = session.get(base + '/api/stories', timeout=5).json()
            if len(stories) == 14:
                break
            time.sleep(.5)
        titles = {story['title'] for story in stories}
        assert len(stories) == 14 and '雾港来信' in titles and '消失的放映室' in titles, sorted(titles)
        edited = mysql_command(mysql_bin, state['dbPort'], 'root', schema,
            "SELECT JSON_UNQUOTE(JSON_EXTRACT(draft,'$.nodes[0].body')) FROM stories "
            "WHERE JSON_UNQUOTE(JSON_EXTRACT(draft,'$.title'))='末班列车的第七站';", root_env)
        assert '学生已编辑的内容。' in edited
        old_reading = mysql_command(mysql_bin, state['dbPort'], 'root', schema,
            "SELECT CONCAT(p.version,':',JSON_UNQUOTE(JSON_EXTRACT(p.document,'$.title'))) "
            "FROM readings r JOIN publications p ON p.id=r.publication_id ORDER BY r.id LIMIT 1;", root_env)
        assert old_reading == '1:末班列车的第七站', old_reading
        upgraded_version = mysql_command(mysql_bin, state['dbPort'], 'root', schema,
            "SELECT MAX(p.version) FROM stories s JOIN publications p ON p.id=s.published_id "
            "WHERE JSON_UNQUOTE(JSON_EXTRACT(p.document,'$.title'))='雾港来信';", root_env)
        assert upgraded_version == '4', upgraded_version
        print('PASS 旧目录按故事标题映射到新目录，14部作品均可浏览')
        print('PASS 已修改草稿不被覆盖，未修改作品逐级升级到版本4')
        print('PASS 旧阅读存档仍绑定版本1原作品')
    finally:
        if app is not None:
            app.terminate()
            app.wait(timeout=15)
        mysql_command(mysql_bin, state['dbPort'], 'root', 'mysql',
                      f"DROP DATABASE IF EXISTS `{schema}`; DROP USER IF EXISTS '{db_user}'@'127.0.0.1';", root_env)


if __name__ == '__main__':
    main()
