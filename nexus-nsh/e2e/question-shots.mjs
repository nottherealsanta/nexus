import { chromium } from 'playwright';
import path from 'node:path';
const out = path.resolve('../reports/question-readability');
const browser = await chromium.launch({executablePath:'/Users/santa/Library/Caches/ms-playwright/chromium-1200/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'});
const page = await browser.newPage({viewport:{width:1024,height:768},colorScheme:'dark'});
await page.goto(process.env.QUESTION_SHOT_URL ?? 'http://127.0.0.1:48732');
if (!process.env.QUESTION_SHOT_SESSION) throw new Error('Set QUESTION_SHOT_SESSION to the seeded readability session id');
await page.getByTestId(`session-${process.env.QUESTION_SHOT_SESSION}`).click();
await page.getByTestId('waiting-scroll').waitFor();
for (const [name,width,height] of [['short',1024,480],['narrow',640,480]]) {
 await page.setViewportSize({width,height}); await page.waitForTimeout(800);
 await page.screenshot({path:`${out}/nsh-${name}.png`});
 const scroll=page.getByTestId('waiting-scroll');
 const hintBox=await page.getByTestId('waiting-reading-hint').boundingBox();
 const scrollBox=await scroll.boundingBox();
 if (!hintBox || !scrollBox || hintBox.y + hintBox.height > scrollBox.y + 1) throw new Error(`${name}: reading hint overlaps scroll viewport`);
 await scroll.evaluate(el=>{el.scrollTop=el.scrollHeight;el.dispatchEvent(new Event('scroll'));});
 await page.waitForTimeout(400); await page.screenshot({path:`${out}/nsh-${name}-end.png`});
 await scroll.evaluate(el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll'));});
}
for (const [name,width,height] of [['short',1008,486],['narrow',672,432],['narrow-end',672,432]]) {
 await page.setViewportSize({width,height});
 await page.goto(`file://${out}/tui-${name}.svg`); await page.screenshot({path:`${out}/tui-${name}.png`});
}
await browser.close();
