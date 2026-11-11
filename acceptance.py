"""Live acceptance checks. Uses temporary QA accounts, then removes only this run's data.
Requires Python 3.10+ and requests; run while the application is running.
Use --ai to include real, billable DeepSeek calls through the backend.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
import requests

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT.parent / '项目资料' / '07-验收与交付' / '接口验收结果.json'
CHECKS = []


def check(name, value):
    assert value, name
    CHECKS.append(name)
    print('PASS', name, flush=True)


class Client:
    def __init__(self, base):
        self.base = base
        self.session = requests.Session()
        self.session.trust_env = False  # Loopback requests must bypass machine-wide proxies.
        self.csrf = self.call('GET', '/session')['csrf']

    def call(self, method, path, body=None, status=200):
        response = self.session.request(method, self.base + '/api' + path, json=body,
                                        headers={'X-CSRF-Token': getattr(self, 'csrf', '')}, timeout=85)
        data = response.json()
        assert response.status_code == status, (method, path, response.status_code, data)
        if isinstance(data, dict) and 'csrf' in data:
            self.csrf = data['csrf']
        return data

    def login(self, username, password='123456'):
        return self.call('POST', '/login', {'username': username, 'password': password})


def cleanup(prefix):
    """Bounded cleanup in this project database, with credentials never printed."""
    state = json.loads((ROOT / '.local' / 'state.json').read_text(encoding='utf-8-sig'))
    mysql_home = os.environ.get('JUXIA_MYSQL_HOME', r'C:\Program Files\MySQL\MySQL Server 8.0')
    mysql = Path(mysql_home) / 'bin' / 'mysql.exe'
    assert prefix.startswith('qa_') and prefix.replace('_', '').isalnum()
    ids = f"SELECT id FROM users WHERE username LIKE '{prefix}%'"
    story_ids = f'SELECT id FROM stories WHERE author_id IN ({ids})'
    sql = f"""
      DELETE rs FROM reading_steps rs JOIN readings r ON r.id=rs.reading_id WHERE r.user_id IN ({ids});
      DELETE FROM readings WHERE user_id IN ({ids});
      DELETE FROM ai_runs WHERE user_id IN ({ids}) OR story_id IN ({story_ids});
      DELETE FROM feedback WHERE user_id IN ({ids}) OR story_id IN ({story_ids});
      DELETE FROM bookmarks WHERE user_id IN ({ids}) OR story_id IN ({story_ids});
      DELETE FROM reviews WHERE reviewer_id IN ({ids}) OR story_id IN ({story_ids});
      DELETE FROM publications WHERE story_id IN ({story_ids});
      DELETE FROM stories WHERE author_id IN ({ids});
      DELETE FROM users WHERE username LIKE '{prefix}%';
    """
    env = dict(os.environ, MYSQL_PWD=state['rootPassword'])
    result = subprocess.run([str(mysql), '--no-defaults', '--protocol=TCP', '--host=127.0.0.1',
                             f"--port={state['dbPort']}", '--user=root', '--default-character-set=utf8mb4', 'juxia'],
                            input=sql, text=True, encoding='utf-8', capture_output=True, env=env)
    if result.returncode:
        raise RuntimeError('QA cleanup failed; inspect temporary QA records before delivery.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8108')
    parser.add_argument('--ai', action='store_true')
    args = parser.parse_args()
    prefix = 'qa_' + uuid.uuid4().hex[:10] + '_'
    outcome = {'date': time.strftime('%Y-%m-%d %H:%M:%S'), 'realAi': args.ai, 'checks': CHECKS}
    try:
        visitor = Client(args.base)
        check('健康检查连接真实数据库', visitor.call('GET', '/health')['databaseReady'])
        check('故事库至少六部公开故事', len(visitor.call('GET', '/stories')) >= 6)
        visitor.call('GET', '/workspace', status=401)
        check('未登录不能进入工作台', True)
        response = visitor.session.post(args.base + '/api/login', json={'username': 'admin', 'password': '123456'}, timeout=5)
        check('无CSRF的写请求被拒绝', response.status_code == 403)
        admin = Client(args.base); admin.login('admin')
        for role in ['author', 'reader', 'reviewer']:
            admin.call('POST', '/admin/users', {'username': prefix + role, 'displayName': '验收临时' + role,
                                               'role': role, 'password': '123456'})
        admin.call('POST', '/admin/users', {'username': prefix + 'reader', 'displayName': '重复账号',
                                          'role': 'reader', 'password': '123456'}, status=409)
        check('管理员创建三类账号并拒绝重名', True)
        author, reader, reviewer = [Client(args.base) for _ in range(3)]
        for client, role in [(author, 'author'), (reader, 'reader'), (reviewer, 'reviewer')]:
            check('新账号登录:' + role, client.login(prefix + role)['user']['role'] == role)
        reader.call('GET', '/reviews', status=403)
        reader.call('GET', '/admin/users', status=403)
        reader.call('POST', '/workspace', {'title': '不能创建'}, status=403)
        check('读者不能审稿、管理账号或创建作品', True)
        users = admin.call('GET', '/admin/users')
        uid = next(u['id'] for u in users if u['username'] == prefix + 'reader')
        admin.call('POST', f'/admin/users/{uid}/password', {'password': '654321'})
        reset_client = Client(args.base)
        reset_client.call('POST', '/login', {'username': prefix + 'reader', 'password': '123456'}, status=401)
        reset_client.login(prefix + 'reader', '654321')
        check('密码重置后旧密码失效新密码可登录', True)

        sid = author.call('POST', '/workspace', {'title': 'QA临时分支故事'})['id']
        work = author.call('GET', f'/workspace/{sid}')
        check('新作品包含可编辑起点和两个结局', len(work['draft']['nodes']) == 3)
        reader.call('GET', f'/workspace/{sid}', status=404)
        visitor.call('GET', f'/stories/{sid}', status=404)
        check('未公开草稿仅作者可访问', True)
        author.call('POST', f'/workspace/{sid}/submit', {'revision': work['revision']}, status=400)
        check('空白正文不能提交审核', True)
        draft = work['draft']
        for node in draft['nodes']:
            node['body'] = '雨夜中，林打开信封，决定回到车站。' if node['id'] == 'start' else '林平安回家，故事在天亮时结束。'
        draft['world'] = '故事发生在同一个雨夜，主人公名叫林。'
        draft['characters'] = [{'name': '林', 'bio': '正在寻找回家方向的旅人。'}]
        work = author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'], 'draft': draft})
        check('完整图校验通过并保存', not work['issues'])
        author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'] - 1, 'draft': draft}, status=409)
        check('过期草稿版本拒绝覆盖', True)
        bad = dict(draft, genre=None)
        author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'], 'draft': bad}, status=400)
        check('无效输入返回400而非500', True)
        work = author.call('POST', f'/workspace/{sid}/submit', {'revision': work['revision']})
        author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'], 'draft': draft}, status=409)
        reviewer.call('POST', f'/reviews/{sid}', {'revision': work['revision'], 'approve': False, 'note': '请补充起点细节。'})
        work = author.call('GET', f'/workspace/{sid}')
        check('退回意见与审核记录可追溯', work['state'] == 'REJECTED' and len(work['reviews']) == 1)
        work = author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'], 'draft': draft})
        work = author.call('POST', f'/workspace/{sid}/submit', {'revision': work['revision']})
        reviewer.call('POST', f'/reviews/{sid}', {'revision': work['revision'], 'approve': True, 'note': '结构与内容已核对，允许发布。'})
        check('审核通过产生发布版本1', visitor.call('GET', f'/stories/{sid}')['version'] == 1)
        reading = reader.call('POST', f'/stories/{sid}/read', {'restart': False})
        rid = reading['id']; old_body = reading['node']['body']
        reader.call('POST', f'/stories/{sid}/feedback', {'rating': 5, 'content': '尚未读完'}, status=400)
        author.call('GET', f'/reading/{rid}', status=404)
        reader.call('POST', f'/reading/{rid}/choose', {'choiceId': 'not_exists', 'revision': 0}, status=400)
        check('评价门槛、存档归属和合法选项检查生效', True)
        done = reader.call('POST', f'/reading/{rid}/choose', {'choiceId': reading['node']['choices'][0]['id'], 'revision': 0})
        reader.call('POST', f'/reading/{rid}/choose', {'choiceId': reading['node']['choices'][0]['id'], 'revision': 0}, status=409)
        check('选择推进到结局且重复点击不重复推进', done['finished'] and done['revision'] == 1)
        reader.call('POST', f'/stories/{sid}/feedback', {'rating': 5, 'content': '完成结局后记录真实感受。'})
        reader.call('POST', f'/stories/{sid}/bookmark', {'enabled': True})
        check('评价和收藏持久化', len(visitor.call('GET', f'/stories/{sid}')['feedback']) == 1 and
              any(s['id'] == sid for s in reader.call('GET', '/shelf')['bookmarks']))

        work = author.call('POST', f'/workspace/{sid}/revise', {})
        draft = work['draft']; draft['nodes'][0]['body'] = '新版起点：天亮后，林收到另一封信。'
        work = author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'], 'draft': draft})
        work = author.call('POST', f'/workspace/{sid}/submit', {'revision': work['revision']})
        reviewer.call('POST', f'/reviews/{sid}', {'revision': work['revision'], 'approve': True, 'note': '新版本已确认。'})
        check('二次发布产生版本2', visitor.call('GET', f'/stories/{sid}')['version'] == 2)
        check('旧存档仍绑定版本1及原文', reader.call('GET', f'/reading/{rid}')['publicationVersion'] == 1 and
              reader.call('GET', f'/reading/{rid}')['path'][0]['body'] == old_body)
        latest = reader.call('POST', f'/stories/{sid}/read', {'restart': True})
        check('重开使用版本2新正文', latest['publicationVersion'] == 2 and latest['node']['body'] != old_body)

        if args.ai:
            work = author.call('POST', f'/workspace/{sid}/revise', {})
            generated = author.call('POST', f'/stories/{sid}/ai', {'kind': 'continue', 'nodeId': 'start',
                                    'instruction': '保留原意，补充环境与人物动作，返回完整章节正文。'})
            check('真实DeepSeek补全返回结构化草稿', bool(generated['result']['body']))
            check('AI生成不自动覆盖原文', author.call('GET', f'/workspace/{sid}')['revision'] == work['revision'])
            applied = author.call('POST', f'/stories/{sid}/ai/{generated["id"]}/apply', {})
            check('作者主动采纳后正文更新', applied['draft']['nodes'][0]['body'] == generated['result']['body'])
            author.call('POST', f'/stories/{sid}/ai/{generated["id"]}/apply', {}, status=409)
            check('相同AI草稿不能重复采纳', True)
            stale = author.call('POST', f'/stories/{sid}/ai', {'kind': 'continue', 'nodeId': 'start', 'instruction': '更简练一些。'})
            work = author.call('GET', f'/workspace/{sid}')
            author.call('PUT', f'/workspace/{sid}', {'revision': work['revision'], 'draft': work['draft']})
            author.call('POST', f'/stories/{sid}/ai/{stale["id"]}/apply', {}, status=409)
            check('生成后草稿被修改则拒绝AI覆盖', True)
            consistency = author.call('POST', f'/stories/{sid}/ai', {'kind': 'consistency', 'instruction': '检查同一雨夜与天亮的时间关系。'})
            check('真实DeepSeek一致性检查成功', isinstance(consistency['result']['issues'], list))
            recap = reader.call('POST', f'/stories/{sid}/ai', {'kind': 'recap', 'instruction': '只总结已读内容。', 'readingId': rid})
            check('真实DeepSeek已读路径回顾成功', bool(recap['result']['body']))
            reader.call('POST', f'/stories/{sid}/ai', {'kind': 'consistency', 'instruction': ''}, status=404)
            check('读者不能调用他人作者AI上下文', True)
            outcome['aiSamples'] = {'continue': generated['result'], 'consistency': consistency['result'], 'recap': recap['result']}
        outcome['passed'] = True
    except Exception as ex:
        outcome['passed'] = False
        outcome['error'] = str(ex)
        raise
    finally:
        try:
            cleanup(prefix)
            outcome['qaDataRemoved'] = True
        finally:
            outcome['count'] = len(CHECKS)
            REPORT.parent.mkdir(parents=True, exist_ok=True)
            REPORT.write_text(json.dumps(outcome, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Report:', REPORT)


if __name__ == '__main__':
    main()
