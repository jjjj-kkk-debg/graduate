"""Browser regressions for reused routes and delayed responses.

GETs use the running application. Every write is intercepted: this suite does
not save drafts, advance readings, publish stories, or call a paid AI service.
"""
import argparse
import asyncio
import copy
import json
from pathlib import Path
from playwright.async_api import async_playwright, expect


async def main(base, output):
    output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
            headless=True, args=['--no-proxy-server'])

        async def login(role):
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            await page.goto(base + '/#/login')
            await page.get_by_label('账号', exact=True).fill(role)
            await page.get_by_label('密码', exact=True).fill('123456')
            await page.get_by_role('button', name='进入工作空间').click()
            await expect(page.get_by_role('button', name='退出', exact=True)).to_be_visible()
            return page

        async def get(page, path):
            return await (await page.request.get(base + '/api' + path)).json()

        async def go(page, path):
            await page.evaluate('(path) => { location.hash = path }', path)

        def passed(name):
            checks.append(name)
            print('PASS', name, flush=True)

        author = await login('author')
        works = await get(author, '/workspace')
        a = next(w for w in works if w['state'] in ('DRAFT', 'REJECTED'))
        b = next(w for w in works if w['id'] != a['id'])
        a_path, b_path = '/workspace/' + str(a['id']), '/workspace/' + str(b['id'])
        await go(author, a_path)
        await expect(author.locator('.editor-header h1')).to_contain_text(a['title'])
        await go(author, b_path)
        await expect(author.locator('.editor-header h1')).to_contain_text(b['title'])
        passed('编辑器同组件切换作品重新加载正确草稿')
        await go(author, a_path)
        await expect(author.locator('.chapter-editor')).to_be_editable()
        await author.locator('.chapter-editor').fill('未保存的路由保护测试，不会提交到数据库。')
        author.once('dialog', lambda dialog: dialog.dismiss())
        await go(author, b_path)
        await expect(author).to_have_url(base + '/#' + a_path)
        await expect(author.locator('.chapter-editor')).to_have_value('未保存的路由保护测试，不会提交到数据库。')
        passed('取消切换保留原地址与未保存正文')
        author.once('dialog', lambda dialog: dialog.accept())
        await go(author, b_path)
        await expect(author.locator('.editor-header h1')).to_contain_text(b['title'])
        passed('确认放弃后切换且清空上一部作品的编辑状态')

        # Hold the earlier GET until the later story has already rendered.
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old_draft = await get(author, a_path)
        async def delayed_draft(route):
            entered.set()
            await release.wait()
            await route.fulfill(json=old_draft)
            finished.set()
        await author.route('**/api' + a_path, delayed_draft)
        await go(author, a_path)
        await asyncio.wait_for(entered.wait(), 10)
        await go(author, b_path)
        await expect(author.locator('.editor-header h1')).to_contain_text(b['title'])
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        await author.wait_for_timeout(200)
        await expect(author.locator('.editor-header h1')).to_contain_text(b['title'])
        passed('较晚返回的旧草稿请求不覆盖当前作品')
        await author.unroute('**/api' + a_path, delayed_draft)

        await go(author, a_path)
        await expect(author.locator('.chapter-editor')).to_be_editable()
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def delayed_save(route):
            if route.request.method != 'PUT':
                return await route.continue_()
            entered.set()
            await release.wait()
            await route.fulfill(json=old_draft)
            finished.set()
        await author.route('**/api' + a_path, delayed_save)
        await author.get_by_role('button', name='保存草稿', exact=True).click()
        await asyncio.wait_for(entered.wait(), 10)
        await go(author, b_path)
        await expect(author).to_have_url(base + '/#' + a_path)
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        await expect(author.get_by_role('button', name='保存草稿', exact=True)).to_be_enabled()
        passed('保存进行中阻止切换，响应完成后恢复操作（模拟写入）')
        await author.unroute('**/api' + a_path, delayed_save)

        async def failed_history(route):
            await route.fulfill(status=503, json={'message': '模拟历史服务失败'})
        await author.route('**/api/stories/' + str(a['id']) + '/ai', failed_history)
        await go(author, b_path)
        await expect(author.locator('.editor-header h1')).to_contain_text(b['title'])
        await go(author, a_path)
        await expect(author.locator('.chapter-editor')).to_be_editable()
        await expect(author.get_by_text('AI历史暂时无法加载：模拟历史服务失败')).to_be_visible()
        passed('AI历史失败仍可打开和编辑正文（模拟故障）')
        await author.context.close()

        reader = await login('reader')
        shelf = await get(reader, '/shelf')
        ra = next((r for r in shelf['readings'] if not r['finished']), shelf['readings'][0])
        rb = next(r for r in shelf['readings'] if r['id'] != ra['id'])
        read_a, read_b = await get(reader, '/reading/' + str(ra['id'])), await get(reader, '/reading/' + str(rb['id']))
        read_a['node']['title'] = '本次旧存档'
        read_b['node']['title'] = '本次目标存档'
        pa, pb = '/reading/' + str(ra['id']), '/reading/' + str(rb['id'])
        async def fixture(route):
            await route.fulfill(json=read_b if route.request.url.endswith(pb) else read_a)
        await reader.route('**/api/reading/*', fixture)
        await go(reader, pa)
        await expect(reader.locator('.reading-paper h1')).to_have_text('本次旧存档')

        # Simulate successful old choose/fork/restart/feedback after a route change.
        for operation in ('choose', 'fork', 'restart', 'feedback'):
            current = copy.deepcopy(read_a)
            current['finished'] = operation in ('restart', 'feedback')
            current['node']['choices'] = [{'id': 'qa', 'label': '测试选项', 'available': True, 'missingClues': []}]
            current['path'] = [dict(current['path'][0]), dict(current['path'][0])]
            async def current_fixture(route):
                await route.fulfill(json=current)
            await reader.route('**/api' + pa, current_fixture)
            await go(reader, pb)
            await expect(reader.locator('.reading-paper h1')).to_have_text('本次目标存档')
            await go(reader, pa)
            await expect(reader.locator('.reading-paper h1')).to_have_text('本次旧存档')
            entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            pattern = ('**/api' + pa + '/' + operation if operation in ('choose', 'fork')
                       else '**/api/stories/' + str(current['storyId']) + ('/read' if operation == 'restart' else '/feedback'))
            async def delayed_write(route):
                entered.set()
                await release.wait()
                await route.fulfill(json=current)
                finished.set()
            await reader.route(pattern, delayed_write)
            if operation == 'choose':
                await reader.get_by_role('button', name='测试选项').click()
            elif operation == 'fork':
                reader.once('dialog', lambda dialog: dialog.accept())
                await reader.locator('.branch-point').first.click()
            elif operation == 'restart':
                await reader.get_by_role('button', name='再读一次，探索其他选择').click()
            else:
                await reader.get_by_label('评价', exact=True).fill('模拟评价，不会保存。')
                await reader.get_by_role('button', name='保存评价').click()
            await asyncio.wait_for(entered.wait(), 10)
            await go(reader, pb)
            await expect(reader.locator('.reading-paper h1')).to_have_text('本次目标存档')
            release.set()
            await asyncio.wait_for(finished.wait(), 10)
            await reader.wait_for_timeout(200)
            await expect(reader).to_have_url(base + '/#' + pb)
            await expect(reader.locator('.reading-paper h1')).to_have_text('本次目标存档')
            await expect(reader.locator('.success-note').filter(has_text='评价已保存')).to_have_count(0)
            passed('切换存档后忽略旧' + operation + '响应（模拟写入）')
            await reader.unroute(pattern, delayed_write)
            await reader.unroute('**/api' + pa, current_fixture)
        await reader.unroute('**/api/reading/*', fixture)

        stories = await get(reader, '/stories')
        sa, sb = stories[:2]
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        detail = await get(reader, '/stories/' + str(sa['id']))
        async def delayed_detail(route):
            entered.set()
            await release.wait()
            await route.fulfill(json=detail)
            finished.set()
        await reader.route('**/api/stories/' + str(sa['id']), delayed_detail)
        await go(reader, '/stories/' + str(sa['id']))
        await asyncio.wait_for(entered.wait(), 10)
        await go(reader, '/stories/' + str(sb['id']))
        await expect(reader.locator('.story-detail h1')).to_have_text(sb['title'])
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        await reader.wait_for_timeout(200)
        await expect(reader.locator('.story-detail h1')).to_have_text(sb['title'])
        passed('较晚返回的旧作品详情不覆盖当前详情')

        async def failed_shelf(route):
            await route.fulfill(status=503, json={'message': '模拟书架故障'})
        await reader.route('**/api/shelf', failed_shelf)
        await go(reader, '/')
        await expect(reader.locator('.story-card')).to_have_count(len(stories))
        passed('个人书架失败仍显示公开故事库（模拟故障）')
        await go(reader, '/stories/' + str(sb['id']))
        await expect(reader.locator('.story-detail h1')).to_have_text(sb['title'])
        await expect(reader.get_by_role('button', name='开始阅读', exact=True)).to_be_enabled()
        passed('个人书架失败仍显示公开作品详情（模拟故障）')
        for width in (1440, 768, 390):
            await reader.set_viewport_size({'width': width, 'height': 900})
            assert await reader.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        passed('作品详情在桌面、平板、手机无横向溢出')
        assert not errors, errors
        await browser.close()
    report = {'passed': True, 'date': '2026-11-04', 'base': base, 'checks': checks,
              'pageErrors': errors, 'writeRequests': 'mocked; no business data changed', 'realAi': False}
    (output / '导航与异步回归.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', default='http://127.0.0.1:8108')
    parser.add_argument('--out', type=Path, default=Path(__file__).resolve().parents[1] / '.local/navigation-check')
    args = parser.parse_args()
    asyncio.run(main(args.base.rstrip('/'), args.out))
