// 用 ComfyUI 官方前端(app.loadGraphData + app.graphToPrompt)把 UI 工作流转成 API prompt。
// 只读:拦截所有非 GET 请求(abort),保证不会入队/写 userdata/settings(实测会拦下 ~20 个
// 插件前端的 POST/PATCH,没有一个是 /prompt)。
//   node convert.cjs variants.json <输出目录>
// variants.json:[{name, ui, modes?:{节点id: 0|4}, widgets?:{节点id: {下标或键: 值}}}]
const { chromium } = require(process.env.PLAYWRIGHT_MODULE
  || '/media/heygo/program/projects-code/repos/nous-engine/frontend/node_modules/playwright');
const COMFY = process.env.COMFY_URL || 'http://127.0.0.1:8888/';
const fs = require('fs');
const [,, variantsPath, outDir] = process.argv;
const variants = JSON.parse(fs.readFileSync(variantsPath, 'utf8'));
(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME || '/usr/bin/google-chrome' });
  const page = await browser.newPage();
  const blocked = [];
  await page.route('**/*', (route) => {
    const m = route.request().method();
    if (m !== 'GET' && m !== 'HEAD' && m !== 'OPTIONS') { blocked.push(m + ' ' + route.request().url()); return route.abort(); }
    return route.continue();
  });
  page.on('console', (msg) => { if (msg.type() === 'error') console.error('[page]', msg.text().slice(0, 200)); });
  await page.goto(COMFY, { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => window.app && window.app.graph && typeof window.app.graphToPrompt === 'function', null, { timeout: 60000 });
  const fe = await page.evaluate(() => (window.__COMFYUI_FRONTEND_VERSION__ || null));
  console.log('frontend version:', fe);
  for (const v of variants) {
    const wf = JSON.parse(fs.readFileSync(v.ui, 'utf8'));
    for (const n of wf.nodes) {
      if (v.modes && v.modes[String(n.id)] !== undefined) n.mode = v.modes[String(n.id)];
      if (v.widgets && v.widgets[String(n.id)]) {
        const patch = v.widgets[String(n.id)];
        if (Array.isArray(n.widgets_values)) { for (const [i, val] of Object.entries(patch)) n.widgets_values[Number(i)] = val; }
        else Object.assign(n.widgets_values, patch);
      }
    }
    const res = await page.evaluate(async (wfj) => {
      await window.app.loadGraphData(wfj, true, false, 'nous-convert');
      const p = await window.app.graphToPrompt();
      return p.output;
    }, wf);
    fs.writeFileSync(`${outDir}/${v.name}.api.json`, JSON.stringify(res, null, 2) + '\n');
    console.log(v.name, 'nodes:', Object.keys(res).join(','));
  }
  console.log('blocked non-GET requests:', blocked.length, JSON.stringify(blocked.slice(0, 20)));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
