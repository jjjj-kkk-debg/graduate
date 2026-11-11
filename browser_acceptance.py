"""Read-only role navigation, native dialog and layout checks; --ai makes two real AI calls."""
import argparse
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.local' / 'ui-check'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ai', action='store_true')
    args = parser.parse_args()
    report = {'viewports': [1440, 390], 'checks': [], 'consoleErrors': [], 'realAi': args.ai}
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                                    headless=True, args=['--no-proxy-server'])

        def page_for(role=None):
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page.on('pageerror', lambda error: report['consoleErrors'].append(str(error)))
            page.goto('http://127.0.0.1:8108/#/login' if role else 'http://127.0.0.1:8108', wait_until='networkidle')
            if role:
                page.get_by_label('账号', exact=True).fill(role)
                page.get_by_label('密码', exact=True).fill('123456')
                page.get_by_role('button', name='进入工作空间').click()
                expect(page.get_by_role('button', name='退出', exact=True)).to_be_visible()
            return page

        def capture(page, filename, selector):
            page.wait_for_selector(selector)
            page.screenshot(path=str(OUT / filename), full_page=True)

        def mobile(page, name):
            page.set_viewport_size({'width': 390, 'height': 844})
            overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
            assert not overflow, name + ' has horizontal overflow'
            report['checks'].append(name + '手机宽度390px无横向溢出')
            page.set_viewport_size({'width': 1440, 'height': 1000})

        public = page_for()
        capture(public, '01-故事库.png', '.story-card')
        public.set_viewport_size({'width': 390, 'height': 844})
        capture(public, '02-手机故事库.png', '.story-card')
        mobile(public, '故事库')
        public.get_by_placeholder('搜索故事或关键词').fill('月亮')
        expect(public.locator('.story-card')).to_have_count(1)
        public.get_by_role('button', name='治愈', exact=True).click()
        expect(public.get_by_text('还没有找到这样的故事')).to_be_visible()
        report['checks'].append('关键词、题材与无结果状态')
        public.goto('http://127.0.0.1:8108/#/login')
        capture(public, '03-登录.png', '.login-panel')
        mobile(public, '登录页')

        reader = page_for('reader')
        reader.goto('http://127.0.0.1:8108/#/stories/1')
        capture(reader, '04-作品详情.png', '.story-detail')
        mobile(reader, '作品详情')
        reader.goto('http://127.0.0.1:8108/#/shelf')
        capture(reader, '05-我的书架.png', '.shelf-row')
        mobile(reader, '我的书架')
        shelf = reader.evaluate("async()=>await (await fetch('/api/shelf')).json()")
        rid = next((r['id'] for r in shelf['readings'] if not r['finished']), shelf['readings'][0]['id'])
        reader.goto(f'http://127.0.0.1:8108/#/reading/{rid}')
        capture(reader, '06-互动阅读.png', '.reading-text')
        mobile(reader, '互动阅读')
        if args.ai:
            reader.get_by_role('button', name='故事助手 展开').click()
            reader.get_by_label('想聊些什么？').select_option('recap')
            reader.get_by_role('button', name='路径回顾', exact=True).click()
            expect(reader.locator('.assistant-result')).to_be_visible(timeout=80000)
            capture(reader, '07-AI已读回顾.png', '.assistant-result')
            report['checks'].append('阅读页真实AI回顾按钮和结果展示')

        author = page_for('author')
        author.goto('http://127.0.0.1:8108/#/workspace')
        capture(author, '08-创作工作台.png', '.work-card')
        mobile(author, '创作工作台')
        works = author.evaluate("async()=>await (await fetch('/api/workspace')).json()")
        work = max(works, key=lambda item: (item['state'] == 'DRAFT', item['nodeCount']))
        work_id = work['id']
        author.goto(f'http://127.0.0.1:8108/#/workspace/{work_id}')
        capture(author, '09-章节编辑.png', '.chapter-editor')
        mobile(author, '章节编辑')
        author.get_by_role('button', name='分支图', exact=True).click()
        expect(author.locator('.graph-node')).to_have_count(work['nodeCount'])
        author.get_by_role('button', name='全图', exact=True).click()
        capture(author, '10-故事分支图.png', '.graph-wrap')
        author.get_by_role('button', name='放大分支图', exact=True).click()
        chapter = author.locator('.graph-node').nth(1).get_attribute('aria-label').removeprefix('编辑章节：')
        author.locator('.graph-node').nth(1).press('Enter')
        expect(author.locator('.chapter-title-field input')).to_have_value(chapter)
        author.get_by_role('button', name='分支图', exact=True).click()
        expect(author.locator('.graph-node').nth(1)).to_have_attribute('aria-pressed', 'true')
        author.get_by_role('button', name='当前章', exact=True).click()
        report['checks'].append('完整故事树可缩放、定位并通过键盘切换章节')
        mobile(author, '分支图')
        author.get_by_role('button', name='试玩草稿', exact=True).click()
        expect(author.locator('dialog')).to_be_visible()
        author.keyboard.press('Tab')
        assert author.evaluate('document.querySelector("dialog").contains(document.activeElement)')
        author.keyboard.press('Escape')
        expect(author.locator('dialog')).not_to_be_visible()
        report['checks'].append('草稿试玩弹窗可键盘操作并按ESC退出')
        if args.ai:
            author.get_by_label('这次需要什么帮助？').select_option('consistency')
            author.get_by_role('button', name='设定检查', exact=True).click()
            expect(author.locator('.ai-draft')).to_be_visible(timeout=80000)
            capture(author, '11-AI一致性检查.png', '.ai-draft')
            mobile(author, 'AI建议')
            report['checks'].append('创作页真实AI一致性检查按钮和历史展示')

        reviewer = page_for('reviewer')
        reviewer.goto('http://127.0.0.1:8108/#/reviews')
        reviewer.get_by_role('button', name='查看并审核').first.click()
        capture(reviewer, '12-内容审核.png', '.review-decision')
        mobile(reviewer, '内容审核')
        admin = page_for('admin')
        admin.goto('http://127.0.0.1:8108/#/admin')
        capture(admin, '13-账号管理.png', 'table')
        mobile(admin, '账号管理')
        assert not report['consoleErrors'], report['consoleErrors']
        report['checks'].append('四角色页面无JavaScript未捕获异常')
        browser.close()
    report['passed'] = True
    (OUT / '浏览器验收结果.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
