"""Render actual workflow results in Chrome; UI requests reuse prior real model samples."""
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]

def verify(base,sid,rid,completed_id,generated,report,reviewed):
    out=ROOT/'.local/ai-workflow-ui';out.mkdir(exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=r'C:/Program Files/Google/Chrome/Application/chrome.exe',headless=True,args=['--no-proxy-server'])
        def login(role):
            page=browser.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.goto(base+'/#/login',wait_until='networkidle')
            page.get_by_label('账号',exact=True).fill(role);page.get_by_label('密码',exact=True).fill('123456')
            page.get_by_role('button',name='进入工作空间').click();expect(page.get_by_role('button',name='退出',exact=True)).to_be_visible()
            return page
        def capture(page,name):
            for width in [1440,390]:
                page.set_viewport_size({'width':width,'height':1000 if width==1440 else 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1'),name
                page.screenshot(path=str(out/f'{name}-{width}.png'),full_page=True)
        reader=login('reader');reader.goto(base+f'/#/reading/{rid}',wait_until='networkidle')
        reader.locator('.assistant-toggle').click()
        reader.get_by_label('想聊些什么？').select_option('dialogue')
        expect(reader.locator('.dialogue-turn')).to_have_count(2)
        capture(reader,'reader-dialogue')
        reader.get_by_label('想聊些什么？').select_option('hint')
        reader.locator('.ai-history summary').click()
        reader.locator('.assistant-record button').filter(has_text='线索推理').first.click()
        expect(reader.locator('.assistant-result')).to_be_visible()
        capture(reader,'reader-hint')
        reader.reload(wait_until='networkidle');reader.locator('.assistant-toggle').click()
        reader.get_by_label('想聊些什么？').select_option('dialogue')
        expect(reader.locator('.dialogue-turn')).to_have_count(2)
        reader.goto(base+f'/#/reading/{completed_id}',wait_until='networkidle');reader.locator('.assistant-toggle').click()
        reader.get_by_label('想聊些什么？').select_option('compare')
        expect(reader.get_by_label('另一份已完成存档')).to_be_enabled()
        reader.locator('.ai-history summary').click()
        reader.locator('.assistant-record button').filter(has_text='路线比较').first.click()
        expect(reader.locator('.assistant-result')).to_be_visible();capture(reader,'reader-comparison')
        author=login('author');author.goto(base+f'/#/workspace/{sid}',wait_until='networkidle')
        author.locator('.ai-history summary').first.click()
        author.locator('.history-item').filter(has_text='生成故事结构').first.click()
        author.get_by_role('button',name='预览生成的故事树',exact=True).click()
        expect(author.locator('.structure-preview .graph-node')).to_have_count(8)
        capture(author,'structure-preview')
        author.locator('.history-item').filter(has_text='分支体验检查').first.click()
        expect(author.locator('.playtest-summary')).to_contain_text('可达 3 个结局')
        capture(author,'writer-playtest')
        author.get_by_role('button',name='创建修订',exact=True).click()
        author.get_by_role('button',name='提交审核',exact=True).click()
        expect(author.get_by_text('审核中',exact=True)).to_be_visible()
        reviewer=login('reviewer');reviewer.goto(base+'/#/reviews',wait_until='networkidle')
        title=generated['result']['story']['title']
        reviewer.locator('.review-row').filter(has=reviewer.get_by_role('heading',name=title,exact=True)).get_by_role('button',name='查看并审核').click()
        # Use the preceding real model answer to verify display without charging twice.
        reviewer.route('**/api/stories/*/ai',lambda route:route.fulfill(json=reviewed))
        reviewer.get_by_role('button',name='AI辅助检查',exact=True).click()
        expect(reviewer.locator('.playtest-summary')).to_be_visible()
        reviewer.get_by_role('button',name='将摘要填入审核意见').click()
        assert reviewer.get_by_label('审核意见',exact=True).input_value().strip()
        capture(reviewer,'review-assistant')
        assert not errors,errors
        browser.close()
