"""Run against the disposable story created by fresh_catalog_smoke, with no UI writes."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect


def verify(base, username, story_id):
    output = Path(__file__).resolve().parents[1] / '.local/preview-check'
    output.mkdir(exist_ok=True)
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                                    headless=True, args=['--no-proxy-server'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/#/login', wait_until='networkidle')
        page.get_by_label('账号', exact=True).fill(username)
        page.get_by_label('密码', exact=True).fill('123456')
        page.get_by_role('button', name='进入工作空间', exact=True).click()
        page.goto(base + f'/#/workspace/{story_id}', wait_until='networkidle')
        expect(page.locator('.chapter-editor')).to_be_visible()
        for width in [1440, 768, 390]:
            page.set_viewport_size({'width': width, 'height': 1000 if width > 390 else 844})
            page.get_by_role('button', name='试玩草稿', exact=True).click()
            dialog = page.get_by_role('dialog')
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role('button', name='没有线索也强行追问')).to_be_disabled()
            dialog.get_by_role('button', name='先查看维修记录', exact=True).click()
            expect(dialog.get_by_role('heading', name='维修记录', exact=True)).to_be_visible()
            expect(dialog.get_by_role('button', name='用标记追问到底', exact=True)).to_be_enabled()
            expect(dialog.get_by_text('本局线索：修船匠的记号、查阅凭证', exact=True)).to_be_visible()
            dialog.get_by_role('button', name='用标记追问到底', exact=True).click()
            expect(dialog.get_by_text('结局：另一种可能', exact=True)).to_be_visible()
            page.screenshot(path=str(output / f'preview-{width}.png'))
            dialog.get_by_role('button', name='回到故事起点', exact=True).click()
            expect(dialog.get_by_role('button', name='没有线索也强行追问')).to_be_disabled()
            page.keyboard.press('Escape')
            expect(dialog).not_to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
        page.get_by_role('button', name='新增章节').click()
        expect(page.get_by_label('章节标题', exact=True)).to_have_value('新章节')
        page.get_by_role('button', name='添加选项').click()
        page.locator('.choice-editor details').first.locator('summary').click()
        expect(page.locator('.choice-editor').get_by_label('需要：修船匠的记号')).to_be_visible()
        assert not errors, errors
        browser.close()
