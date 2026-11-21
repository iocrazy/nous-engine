const main = document.querySelector('#main')
const nav = document.querySelector('#nav')
const dialog = document.querySelector('#dialog')
const upload = document.querySelector('#upload')
const toast = document.querySelector('#toast')

const tabs = [
  ['dataset', '数据集'],
  ['models', '模型与目录'],
  ['training', '训练任务'],
  ['results', '结果与发布'],
]
const samples = [
  ['01_山间薄雾', '山脉倒映在平静的湖面，蓝色晨雾压低了远景。'],
  ['02_高山雪线', '岩壁与雪线形成斜向结构，远山笼罩在冷色云层中。'],
  ['03_湖面余晖', '日落照亮湖面与群山，前景带有温暖的反光。'],
  ['04_森林光束', '日光穿过密林，照亮潮湿的绿色植被。'],
  ['05_旷野日落', ''],
  ['06_海浪', ''],
  ['07_草原', ''],
  ['08_远山', ''],
].map(([name, caption], i) => ({ id: i + 1, name: name + '.jpg', caption, src: 'photo-' + (i + 1) + '.webp' }))
const models = [
  { id: 'qwen', modes: ['image', 'edit'], name: 'Qwen-Image-2.1', source: 'Qwen/Qwen-Image-2.1', size: '30.86 GiB', family: 'qwen2.1', status: 'missing', components: ['兼容 DiffSynth 的分片权重'] },
  { id: 'moss-base', modes: ['image'], name: 'MOSS-Image-Base', source: 'openmoss/MOSS-Image-Base', size: '25.36 GiB', family: 'moss', status: 'ready', components: [] },
  { id: 'moss-sft', modes: ['image'], name: 'MOSS-Image-SFT', source: 'openmoss/MOSS-Image-SFT', size: '25.36 GiB', family: 'moss', status: 'absent', components: ['模型权重', '文本编码器', 'VAE'] },
]
const editSamples = [
  { id: 1, refs: [1, 2], target: 3, caption: '保留第一张的群山，把湖面调成清透的青绿色。' },
  { id: 2, refs: [4, 5], target: 8, caption: '保留前景线条，将环境改为暖色远山与开阔天空。' },
  { id: 3, refs: [6], target: 7, caption: '' },
]
const managedTrainer = '/models/training/DiffSynth-Studio'
const modeNames = { image: '文生图', edit: '图像编辑' }
const cards = { ready: ['可训练', 'ok'], missing: ['训练组件待补齐', 'warn'], absent: ['未下载', ''], downloading: ['下载中', ''], checking: ['校验中', ''] }
const state = {
  tab: 'dataset', filter: 'all', layout: 'grid', query: '', modelFilter: 'all', focus: 1,
  added: samples.slice(0, 6), selected: new Set([1, 2, 3, 4, 5, 6]),
  mode: 'image', editFocus: 1, selectedEdits: new Set([1, 2, 3]),
  trainerPath: managedTrainer, trainerRecognized: false, trainerDiagnostic: '',
  model: 'qwen', taskName: '风景摄影 · 光线与色彩', gpu: '1', rank: 32, steps: 1200,
  rate: '0.0001', interval: 400, resolution: 1024, preset: 'standard', task: null, checkpoint: '1200', split: 50,
  gradientCheckpoint: true, cpuOffload: false,
  tested: false, published: '', allowEmpty: false, ranCheck: false, advanced: false, verifiedEnvironment: false,
}
let messageTimer
let nextMediaId = 20

function escape(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char])
}

function button(action, label, options = {}) {
  const { kind = '', value, disabled = false, pressed } = options
  return `<button type="button" class="${kind}" data-action="${action}"${value === undefined ? '' : ` data-value="${escape(value)}"`}${disabled ? ' disabled' : ''}${pressed === undefined ? '' : ` aria-pressed="${pressed}"`}>${label}</button>`
}

function heading(title, description, actions = '') {
  return `<header class="heading"><div><h1 tabindex="-1">${title}</h1><p>${description}</p></div><div class="actions wrap">${actions}</div></header>`
}

function chip(label, variant = '') {
  return `<span class="chip ${variant}">${label}</span>`
}

function tell(message) {
  clearTimeout(messageTimer)
  toast.textContent = message
  toast.hidden = false
  messageTimer = setTimeout(() => { toast.hidden = true }, 3000)
}

function navigate(tab) {
  state.tab = tab
  render()
  document.querySelector('h1').focus({ preventScroll: true })
  window.scrollTo(0, 0)
}

function model() {
  return models.find((item) => item.id === state.model)
}

function focusSample() {
  return state.added.find((sample) => sample.id === state.focus)
}

function visibleSamples() {
  return state.added.filter((sample) => {
    const matches = sample.name.toLowerCase().includes(state.query.toLowerCase())
    return matches && (state.filter === 'all' || (state.filter === 'done' ? sample.caption : !sample.caption))
  })
}

function labeledCount() {
  return state.added.filter((sample) => state.selected.has(sample.id) && sample.caption).length
}

function showDialog(title, content) {
  dialog.innerHTML = `<div class="panel-head row between"><h2 id="dialog-heading">${title}</h2>${button('close-dialog', '关闭', { kind: 'quiet' })}</div><div class="section-block stack">${content}</div>`
  dialog.setAttribute('aria-labelledby', 'dialog-heading')
  if (!dialog.open) dialog.showModal()
}

function modeButtons(action) {
  return `<div class="filters" aria-label="训练类型">${Object.entries(modeNames).map(([mode, label]) =>
    button(action, label, { value: mode, pressed: state.mode === mode })).join('')}</div>`
}

function editTiles(sample) {
  const tiles = sample.refs.map((id, index) => [id, '输入 ' + (index + 1)])
  tiles.push([sample.target, '目标'])
  return `<div class="edit-tiles">${tiles.map(([id, label]) =>
    `<figure><img src="${samples.find((item) => item.id === id).src}" alt="${label}"><figcaption>${label}</figcaption></figure>`).join('')}</div>`
}

function editDatasetPage() {
  const sample = editSamples.find((item) => item.id === state.editFocus)
  return heading('风景摄影 · 编辑样本', '每组使用有顺序的参考图、目标图和一条编辑指令。', modeButtons('set-mode')) + `
    <div class="workspace">
      <section class="panel">
        <header class="panel-head"><h2>样本组</h2></header>
        <div class="section-block stack">
          ${editSamples.map((item) => `<article class="edit-group stack">
            <div class="row between wrap">
              <label class="checkline"><input type="checkbox" data-edit-sample="${item.id}" ${state.selectedEdits.has(item.id) ? 'checked' : ''}> 样本 ${item.id}</label>
              ${item.caption ? chip('配对完整', 'ok') : chip('待补编辑指令', 'warn')}
              ${button('edit-sample', '查看样本 ' + item.id, { kind: 'quiet', value: item.id })}
            </div>
            ${editTiles(item)}
          </article>`).join('')}
        </div>
        <footer class="toolbar-note"><small>已选 ${state.selectedEdits.size} 组</small>${button('train-selection', '训练所选样本', { kind: 'primary', disabled: !state.selectedEdits.size })}</footer>
      </section>
      <aside class="panel">
        <header class="panel-head"><h2>样本 ${sample.id} · 配对检查</h2></header>
        <div class="section-block stack">${editTiles(sample)}
          ${sample.refs.length > 1 ? button('swap-references', '交换输入 1、2', { kind: 'quiet' }) : ''}
          <label class="field">编辑指令<textarea id="edit-caption" placeholder="描述如何从参考图生成目标图">${escape(sample.caption)}</textarea></label>
          ${button('save-edit-caption', '保存编辑指令')}
        </div>
      </aside>
    </div>`
}

function datasetPage() {
  if (state.mode === 'edit') return editDatasetPage()
  const sample = focusSample()
  const visible = visibleSamples()
  const pending = state.added.filter((item) => !item.caption).length
  const brief = `${state.added.length} 张素材 · ${state.added.length - pending} 张已标注 · 文生图 LoRA`
  const importer = modeButtons('set-mode') + button('gallery', '从图库选择', { kind: 'quiet' }) + button('upload', '上传图片')
  const filters = [['all', '全部 · ' + state.added.length], ['pending', '待标注 · ' + pending], ['done', '已标注 · ' + (state.added.length - pending)]]
  return heading('风景摄影 · 光线与色彩', brief, importer) + `
    <div class="workspace">
      <section class="panel" aria-label="素材">
        <header class="panel-head stack">
          <div class="row between wrap"><h2>素材</h2><input class="inline-search" type="search" data-field="query" aria-label="搜索素材" placeholder="搜索素材文件名" value="${escape(state.query)}"></div>
          <div class="row between wrap">
            <div class="filters">${filters.map(([id, label]) => button('filter', label, { value: id, pressed: state.filter === id })).join('')}</div>
            <div class="filters">${button('layout', '网格', { value: 'grid', pressed: state.layout === 'grid' })}${button('layout', '列表', { value: 'list', pressed: state.layout === 'list' })}</div>
          </div>
        </header>
        <div class="media-${state.layout}">
          ${visible.map((item) => `
            <article class="media-card" data-selected="${state.selected.has(item.id)}">
              <button type="button" class="media-open" data-action="focus-media" data-value="${item.id}">
                <img src="${escape(item.src)}" alt="">
                <span class="media-info"><strong>${escape(item.name)}</strong><small class="caption-status${item.caption ? '' : ' missing'}">${item.caption ? '已标注' : '待标注'}</small></span>
              </button>
              <label class="check"><input type="checkbox" aria-label="选择 ${escape(item.name)}" data-media="${item.id}" ${state.selected.has(item.id) ? 'checked' : ''}></label>
            </article>`).join('') || '<p class="empty">没有符合筛选条件的素材。</p>'}
        </div>
        <footer class="toolbar-note">
          <div class="actions wrap">${button('select-all', '全选当前', { kind: 'quiet' })}${button('clear', '清空选择', { kind: 'quiet' })}<small>已选 ${state.selected.size} 张</small></div>
          ${button('train-selection', '训练所选素材 →', { kind: 'primary', disabled: !state.selected.size })}
        </footer>
      </section>
      <aside class="stack" aria-label="标注">
        ${sample ? `<section class="panel">
          <div class="panel-head"><h2>素材标注</h2></div>
          <div class="section-block stack">
            <img class="preview-thumb" src="${escape(sample.src)}" alt="">
            <div class="row between"><h3>${escape(sample.name)}</h3>${sample.caption ? chip('已标注', 'ok') : chip('待标注', 'warn')}</div>
            <label class="field">描述<textarea id="caption" placeholder="写下画面主体、光线和构图">${escape(sample.caption)}</textarea></label>
            ${button('save-caption', '保存描述')}
          </div>
        </section>` : ''}
        <section class="panel"><div class="section-block stack">
          <h2>批量 AI 反推</h2>
          <label class="field">视觉模型<select aria-label="视觉模型"><option>本地 Qwen · 视觉</option></select></label>
          <label class="checkline"><input type="checkbox" id="overwrite"> 重做已有描述</label>
          ${button('caption', `反推所选 ${state.selected.size} 张`, { disabled: !state.selected.size })}
        </div></section>
      </aside>
    </div>`
}

function modelsPage() {
  const filters = [['all', '全部'], ['ready', '可训练'], ['missing', '需补齐'], ['absent', '未下载']]
  const visible = models.filter((entry) => state.modelFilter === 'all' || state.modelFilter === entry.status)
  return heading('模型与目录', '下载或扫描已有权重，校验后关联 DiffSynth 训练配方。', button('scan', '扫描本地模型', { kind: 'primary' })) + `
    <section class="panel">
      <div class="panel-head row between wrap"><h2>训练模型目录</h2><div class="filters">${filters.map(([id, label]) => button('filter-model', label, { value: id, pressed: state.modelFilter === id })).join('')}</div></div>
      <div class="section-block card-grid">
        ${visible.map((entry) => {
          const status = cards[entry.status]
          const downloading = ['downloading', 'checking'].includes(entry.status)
          const primary = entry.status === 'ready' ? button('choose-model', '配置训练 →', { kind: 'primary', value: entry.id })
            : downloading ? `<progress value="${entry.progress}" max="100" aria-label="下载进度"></progress>`
              : button('download-dialog', entry.status === 'missing' ? '补齐缺失组件' : '下载模型', { kind: 'primary', value: entry.id })
          return `<article class="panel model-card">
            <div class="row between"><span class="model-symbol ${entry.family === 'moss' ? 'moss' : ''}" aria-hidden="true">${entry.family === 'moss' ? 'M' : 'Q'}</span>${chip(...status)}</div>
            <div><h3>${entry.name}</h3><small class="mono">${entry.source}</small></div>
            <small>${entry.size} · DiffSynth · ${entry.modes.map((mode) => modeNames[mode]).join(' / ')} ${entry.id === 'qwen' ? '· 推理可用' : ''}</small>
            ${entry.downloadSource ? `<small>下载来源 · ${escape(entry.downloadSource)}</small>` : ''}
            ${entry.localPath ? `<small class="mono">${escape(entry.localPath)}</small>` : ''}
            <div class="actions wrap">${primary}${button('components', '查看组件', { kind: 'quiet', value: entry.id })}${button('match-directory', '关联本地目录', { kind: 'quiet', value: entry.id, disabled: downloading })}</div>
          </article>`
        }).join('') || '<p class="empty">这个状态暂无模型。</p>'}
      </div>
    </section>
    <section class="panel gap-section"><div class="section-block">
      <h2>存储设置</h2>
      <dl class="model-details">
        <div><dt>模型根目录</dt><dd class="mono">/models/training</dd></div>
        <div><dt>LoRA 发布目录</dt><dd class="mono">ComfyUI / models / loras</dd></div>
        <div><dt>磁盘可用</dt><dd>183 GiB</dd></div>
      </dl>
    </div></section>`
}

function taskProgress() {
  const task = state.task
  if (!task) return ''
  const percent = task.percent
  const done = percent === 100
  return `<section class="panel gap-section" aria-label="训练记录">
    <header class="panel-head row between wrap"><div class="stack"><h2>训练记录</h2><small>${escape(task.name)} · ${escape(task.modelName)} · GPU ${task.gpu}</small></div>${chip(done ? '已完成' : task.cancelled ? '已取消' : percent < 10 ? '准备数据' : '训练中', done ? 'ok' : '')}</header>
    <div class="section-block stack"><div class="row between"><small>步骤 ${Math.floor(percent * task.steps / 100)} / ${task.steps}</small><b>${percent}%</b></div><progress value="${percent}" max="100" aria-label="训练进度"></progress><pre class="logs">${done ? '✓ Checkpoint 与样图已写入\n✓ 可进入结果页试载' : task.cancelled ? '任务已取消。可返回配置重新提交。' : '✓ 训练环境、数据集和模型校验通过\n→ DiffSynth 正在处理示例训练任务'}</pre>
    ${done ? button('navigate', '查看训练结果 →', { kind: 'primary', value: 'results' }) : task.cancelled ? '' : button('cancel-training', '取消训练', { kind: 'quiet' })}</div></section>`
}

function selectedEditSamples() {
  return editSamples.filter((item) => state.selectedEdits.has(item.id))
}

function selectionReady() {
  return state.mode === 'edit'
    ? state.selectedEdits.size > 0 && selectedEditSamples().every((item) => item.caption)
    : state.selected.size > 0 && (state.allowEmpty || labeledCount() === state.selected.size)
}

function environmentDialog() {
  const status = state.verifiedEnvironment ? '已验证' : state.trainerRecognized ? '已识别 · 待验证' : '待识别'
  showDialog('引擎与环境', `
    <div class="row between wrap"><h3>DiffSynth-Studio</h3>${chip(status, state.verifiedEnvironment ? 'ok' : '')}</div>
    <label class="field">训练器目录<input id="trainer-path" value="${escape(state.trainerPath)}" placeholder="/models/training/DiffSynth-Studio"></label>
    <div class="row wrap">${button('use-managed-trainer', '使用托管目录', { kind: 'quiet' })}${button('recognize-environment', '识别并保存环境', { kind: 'primary' })}</div>
    ${state.trainerRecognized ? `<div class="kv"><span>Python</span><b class="mono">${escape(state.trainerPath)}/.venv/bin/python</b></div>` : ''}
    ${state.trainerDiagnostic ? `<details open><summary>诊断日志</summary><pre class="logs">${escape(state.trainerDiagnostic)}</pre></details>` : ''}
    ${button('verify-environment', '验证环境', { disabled: !state.trainerRecognized })}
  `)
}

function trainingPage() {
  const selected = model()
  const busy = state.task && !state.task.cancelled && state.task.percent < 100
  const missing = state.selected.size - labeledCount()
  const eligible = selected.status === 'ready' && selectionReady() && state.verifiedEnvironment
  const editing = state.mode === 'edit'
  const preview = editing ? selectedEditSamples() : state.added.filter((item) => state.selected.has(item.id))
  return heading('发起训练', '选择数据集、基础模型与 GPU。提交时保存这些配置的快照。', button('navigate', '查看模型目录', { kind: 'quiet', value: 'models' })) + `
    <div class="workspace">
      <section class="panel">
        <header class="panel-head row between wrap"><h2>训练配置</h2>${modeButtons('set-mode')}</header>
        <div class="section-block stack">
          <label class="field">任务名称<input data-field="taskName" value="${escape(state.taskName)}" aria-label="任务名称"></label>
          <div class="row between wrap"><div class="stack"><h3>${editing ? '风景摄影 · 编辑样本' : '风景摄影 · 光线与色彩'}</h3><small>${editing ? '已选 ' + preview.length + ' 组 · 配对完整 ' + preview.filter((item) => item.caption).length + ' 组' : '已选 ' + state.selected.size + ' 张 · 已标注 ' + labeledCount() + ' 张'}</small></div>${button('navigate', '修改所选素材', { kind: 'quiet', value: 'dataset' })}</div>
          ${!editing && missing ? `<label class="checkline"><input type="checkbox" data-field="allowEmpty" ${state.allowEmpty ? 'checked' : ''}> 允许 ${missing} 张已选素材使用空描述</label>` : ''}
          <div class="form-grid">
            <label class="field">基础模型<select data-field="model">${models.filter((entry) => entry.modes.includes(state.mode)).map((entry) => `<option value="${entry.id}" ${entry.id === state.model ? 'selected' : ''}>${entry.name} · ${cards[entry.status][0]}</option>`).join('')}</select></label>
            <label class="field">GPU<select data-field="gpu"><option value="1" ${state.gpu === '1' ? 'selected' : ''}>GPU 1 · RTX PRO 6000</option><option value="0" ${state.gpu === '0' ? 'selected' : ''}>GPU 0 · RTX PRO 5000</option></select></label>
          </div>
          ${selected.status === 'ready' ? '' : `<div class="notice warn row between wrap"><span>${selected.name} 训练权重待准备</span>${button('navigate', '打开模型与目录', { kind: 'link', value: 'models' })}</div>`}
          <label class="field">训练预设<select data-field="preset"><option value="standard" ${state.preset === 'standard' ? 'selected' : ''}>标准 · 风格 LoRA</option><option value="short" ${state.preset === 'short' ? 'selected' : ''}>快速验证 · 少步数</option></select></label>
          <details id="advanced" ${state.advanced ? 'open' : ''}><summary>高级参数</summary><div class="form-grid">
            ${[['steps','训练步数'],['rank','LoRA Rank'],['rate','学习率'],['resolution','训练分辨率'],['interval','Checkpoint 保存间隔']].map(([key, label]) => `<label class="field">${label}<input type="number" step="${key === 'rate' ? 'any' : '1'}" min="0" data-field="${key}" value="${escape(state[key])}"></label>`).join('')}
          </div><div class="row wrap">
            <label class="checkline"><input type="checkbox" data-field="gradientCheckpoint" ${state.gradientCheckpoint ? 'checked' : ''}> 梯度检查点</label>
            <label class="checkline"><input type="checkbox" data-field="cpuOffload" ${state.cpuOffload ? 'checked' : ''}> 模型 CPU 卸载</label>
          </div></details>
        </div>
        <footer class="toolbar-note">
          <small>${busy ? '已有训练任务正在运行' : 'DiffSynth-Studio · 独立环境'}</small>
          <div class="actions wrap">${button('preflight', '检查准备情况', { kind: 'quiet' })}${button('start-training', '开始训练', { kind: 'primary', disabled: !eligible || busy })}</div>
        </footer>
      </section>
      <aside class="panel">
        <header class="panel-head"><h2>训练预检</h2></header>
        <div class="section-block stack">
          <div class="kv"><span>数据集快照</span><b>${preview.length} ${editing ? '组选中' : '张选中'}</b></div>
          <div class="kv"><span>模型组件</span>${chip(...cards[selected.status])}</div>
          <div class="kv"><span>当前可用显存（示例）</span><b>${state.gpu === '1' ? 54 : 48} GiB</b></div>
          <div class="kv"><span>配方显存参考（示例）</span><b>${state.cpuOffload ? 'CPU 卸载' : selected.id === 'qwen' ? '约 38 GiB' : '约 31 GiB'}</b></div>
          <div class="kv"><span>DiffSynth 环境</span>${chip(state.verifiedEnvironment ? '验证通过' : '待检查', state.verifiedEnvironment ? 'ok' : '')}</div>
          ${state.verifiedEnvironment ? '' : button('environment', '配置训练环境', { kind: 'quiet' })}
          ${state.ranCheck ? `<div class="notice ${eligible ? '' : 'warn'}">${eligible ? '预检通过，可以提交训练。' : '请检查训练环境、模型组件和素材标注。'}</div>` : ''}
        </div>
        <div class="panel-head"><h2>所选素材预览</h2></div>
        <div class="section-block stack training-previews">
          ${preview.slice(0, 8).map((item) => editing ? editTiles(item) : `<img src="${escape(item.src)}" alt="${escape(item.name)}">`).join('') || '<p class="muted">暂无所选素材。</p>'}
        </div>
      </aside>
    </div>
    ${taskProgress()}`
}

function resultsPage() {
  const task = state.task
  if (!task || task.cancelled || task.percent < 100) {
    return heading('结果与发布', '选择训练产物，在同一提示词与种子下对比后发布。') + `<section class="panel section-block stack"><h2>等待训练结果</h2><p class="muted">训练完成后，样图与 checkpoint 会在这里出现。</p><div>${button('navigate', '查看训练任务', { value: 'training' })}</div></section>`
  }
  const grade = state.checkpoint === '1200' ? 'contrast(1.13) saturate(1.2)' : state.checkpoint === '800' ? 'contrast(1.08) saturate(1.13)' : 'contrast(1.04) saturate(1.07)'
  return heading('结果与发布', `${escape(task.name)} · ${escape(task.modelName)} · 已完成`, button('navigate', '返回训练记录', { kind: 'quiet', value: 'training' })) + `
    <div class="workspace">
      <section class="panel">
        <header class="panel-head row between wrap"><h2>样图对比</h2><div class="filters">${['400','800','1200'].map((checkpoint) => button('checkpoint', 'Step ' + Math.round(task.steps * Number(checkpoint) / 1200), { value: checkpoint, pressed: state.checkpoint === checkpoint })).join('')}</div></header>
        <div class="section-block stack">
          <div class="compare" style="--split:${state.split}%;--grade:${grade}">
            <img src="photo-1.webp" alt="示例基础模型样图"><img src="photo-1.webp" alt="示例 checkpoint 样图" class="after">
            <span class="compare-label">基础模型</span><span class="compare-label right">训练样图</span>
            <span class="compare-line"><span class="compare-handle" aria-hidden="true">↔</span></span>
            <input class="compare-control" type="range" min="0" max="100" value="${state.split}" aria-label="对比滑块">
          </div>
          <div class="row between wrap"><small>同一提示词 · 山湖、冷色晨雾</small><small class="mono">Seed 18472 · 1024 × 768</small></div>
          <div><h3>训练 Loss</h3><div class="loss-chart" role="img" aria-label="示例训练 Loss 趋势下降">${[93,81,88,66,74,61,62,48,52,43,42,37,39,32,29,28].map((value) => `<i style="height:${value}%"></i>`).join('')}</div></div>
        </div>
      </section>
      <aside class="panel">
        <header class="panel-head"><h2>发布到 LoRA 目录</h2></header>
        <div class="section-block stack">
          <div class="kv"><span>目标家族</span><b>${task.family}</b></div>
          <div class="kv"><span>ComfyUI 试载</span>${chip(state.tested ? '验证通过' : '待试载', state.tested ? 'ok' : '')}</div>
          ${button('test-lora', '试载此 checkpoint')}
          ${button('publish-dialog', '发布 LoRA →', { kind: 'primary', disabled: !state.tested })}
          ${state.published ? `<div class="notice">已发布并同步 · ${escape(state.published)}</div>` : ''}
        </div>
      </aside>
    </div>`
}

function render() {
  nav.innerHTML = tabs.map(([id, label]) => `<button type="button" data-action="navigate" data-value="${id}" ${id === state.tab ? 'aria-current="page"' : ''}>${label}<span aria-hidden="true">${id === state.tab ? '›' : ''}</span></button>`).join('')
  const pages = { dataset: datasetPage, models: modelsPage, training: trainingPage, results: resultsPage }
  main.innerHTML = pages[state.tab]()
  document.querySelector('#advanced')?.addEventListener('toggle', (event) => { state.advanced = event.target.open })
}

function runTraining() {
  const activeModel = model()
  const params = [state.steps, state.rank, state.rate, state.interval, state.resolution].map(Number)
  if (!state.taskName.trim() || params.some((value) => !Number.isFinite(value) || value <= 0) || Number(state.interval) > Number(state.steps)) {
    tell('请输入任务名称和有效的训练参数。保存间隔不能超过训练步数。')
    return
  }
  const task = {
    name: state.taskName.trim(), gpu: state.gpu, steps: Number(state.steps),
    modelName: activeModel.name, family: activeModel.family, percent: 0,
    mode: state.mode, gradientCheckpoint: state.gradientCheckpoint, cpuOffload: state.cpuOffload,
    dataset: state.mode === 'edit' ? selectedEditSamples().map((item) => ({ ...item, refs: [...item.refs] })) : state.added.filter((item) => state.selected.has(item.id)).map(({ id, caption }) => ({ id, caption })),
  }
  state.task = task
  state.checkpoint = '1200'
  state.split = 50
  state.tested = false
  state.published = ''
  state.ranCheck = true
  render()
  document.querySelector('[aria-label="训练记录"]').scrollIntoView({ block: 'nearest' })
  const timer = setInterval(() => {
    if (task.cancelled) {
      clearInterval(timer)
      return
    }
    task.percent = Math.min(100, task.percent + 10)
    if (state.tab === 'training') {
      const previous = document.querySelector('[aria-label="训练记录"]')
      if (previous) previous.outerHTML = taskProgress()
    }
    if (task.percent === 100) {
      clearInterval(timer)
      render()
      tell('示例训练完成，可查看样图与 checkpoint。')
    }
  }, 310)
}

function runDownload(entry) {
  entry.downloadSource = dialog.querySelector('[aria-label="下载来源"]').value
  entry.status = 'downloading'
  entry.progress = 0
  state.modelFilter = 'all'
  dialog.close()
  render()
  const timer = setInterval(() => {
    entry.progress = Math.min(100, entry.progress + 20)
    if (entry.progress === 100) {
      clearInterval(timer)
      entry.status = 'checking'
      setTimeout(() => {
        entry.status = 'ready'
        entry.components = []
        if (state.tab === 'training' || state.tab === 'models') render()
        tell(entry.name + ' 已关联训练配方。')
      }, 400)
    }
    if (state.tab === 'models') render()
  }, 240)
}

document.addEventListener('click', (event) => {
  const target = event.target.closest('[data-action]')
  if (!target) return
  const { action, value } = target.dataset
  const entry = models.find((item) => item.id === value)

  switch (action) {
    case 'navigate': navigate(value); return
    case 'close-dialog': dialog.close(); return
    case 'environment': environmentDialog(); return
    case 'use-managed-trainer':
      document.querySelector('#trainer-path').value = managedTrainer
      return
    case 'recognize-environment': {
      state.trainerPath = document.querySelector('#trainer-path').value.trim().replace(/\/+$/, '')
      state.trainerRecognized = state.trainerPath === managedTrainer
      state.verifiedEnvironment = false
      state.ranCheck = false
      state.trainerDiagnostic = state.trainerRecognized
        ? '✓ 识别到示例 DiffSynth 训练入口和独立 .venv'
        : '未找到示例环境。可选择「使用托管目录」后重新识别。'
      render()
      environmentDialog()
      return
    }
    case 'verify-environment':
      if (!state.trainerRecognized) return
      state.verifiedEnvironment = true
      state.trainerDiagnostic = '✓ DiffSynth 入口\n✓ 独立 Python\n✓ CUDA 和目录访问（示例）'
      render()
      environmentDialog()
      return
    case 'set-mode':
      state.mode = value
      if (!model().modes.includes(value)) state.model = models.find((item) => item.modes.includes(value)).id
      state.ranCheck = false
      break
    case 'edit-sample': state.editFocus = Number(value); break
    case 'swap-references': {
      const item = editSamples.find((entry) => entry.id === state.editFocus)
      item.caption = document.querySelector('#edit-caption').value.trim()
      item.refs.reverse()
      break
    }
    case 'save-edit-caption':
      editSamples.find((item) => item.id === state.editFocus).caption = document.querySelector('#edit-caption').value.trim()
      state.ranCheck = false
      tell('编辑指令已保存。')
      break
    case 'filter': state.filter = value; break
    case 'layout': state.layout = value; break
    case 'focus-media': state.focus = Number(value); break
    case 'select-all': visibleSamples().forEach((item) => state.selected.add(item.id)); break
    case 'clear': state.selected.clear(); break
    case 'upload': upload.click(); return
    case 'gallery': {
      const available = samples.filter((item) => !state.added.some(({ id }) => id === item.id))
      const choices = available.map((item) => `<label class="checkline"><input type="checkbox" name="gallery-item" value="${item.id}" checked><img src="${item.src}" width="45" height="35" alt="" style="object-fit:cover;border-radius:4px"> ${escape(item.name)}</label>`).join('')
      showDialog('从图库选择', choices ? choices + button('add-gallery', '加入数据集', { kind: 'primary' }) : '<p class="muted">示例图库内的素材已全部加入。</p>')
      return
    }
    case 'add-gallery': {
      const selected = [...dialog.querySelectorAll('[name="gallery-item"]:checked')].map((item) => Number(item.value))
      state.added.push(...samples.filter((item) => selected.includes(item.id)))
      selected.forEach((id) => state.selected.add(id))
      if (selected.length) state.focus = selected[0]
      dialog.close()
      tell('已加入 ' + selected.length + ' 张素材。')
      break
    }
    case 'save-caption':
      focusSample().caption = document.querySelector('#caption').value.trim()
      tell('描述已保存。')
      break
    case 'caption': {
      const overwrite = document.querySelector('#overwrite').checked
      const pending = state.added.filter((item) => state.selected.has(item.id) && (overwrite || !item.caption))
      if (!pending.length) {
        tell('所选素材已有描述。勾选「重做已有描述」可重新生成。')
        return
      }
      if (overwrite) {
        showDialog('重做已有描述', '<p>此操作会替换选中素材的当前描述。</p>' + button('confirm-caption', '确认重做', { kind: 'primary' }))
        return
      }
      applyCaptions(pending)
      break
    }
    case 'confirm-caption':
      applyCaptions(state.added.filter((item) => state.selected.has(item.id)))
      dialog.close()
      break
    case 'train-selection': navigate('training'); return
    case 'filter-model': state.modelFilter = value; break
    case 'scan':
      tell('示例扫描完成。' + models.filter((item) => item.status === 'ready').length + ' 个模型可训练，' + models.filter((item) => item.status === 'missing').length + ' 个模型需补齐。')
      break
    case 'match-directory': {
      const examplePath = '/models/training/' + entry.name
      showDialog(entry.name + ' · 关联本地目录', `<label class="field">本地模型目录<input id="model-directory" value="${escape(entry.localPath ?? examplePath)}"></label>
        <p class="muted">示例目录 ${examplePath}。匹配后检查基础权重、文本编码器和 VAE。</p>
        <div id="directory-status" role="status"></div>
        ${button('confirm-match', '识别并关联', { kind: 'primary', value: entry.id })}`)
      return
    }
    case 'confirm-match': {
      const path = document.querySelector('#model-directory').value.trim().replace(/\/+$/, '')
      if (path !== '/models/training/' + entry.name) {
        document.querySelector('#directory-status').textContent = '未找到匹配的示例组件。请检查目录后重试。'
        return
      }
      entry.localPath = path
      entry.components = []
      entry.status = 'ready'
      state.ranCheck = false
      dialog.close()
      tell('已关联本地组件和 DiffSynth 训练配方。')
      break
    }
    case 'components': {
      const components = entry.components.length ? entry.components.map((name) => `<div class="kv"><span>${name}</span>${chip('缺失', 'warn')}</div>`).join('')
        : '<p class="notice">基础模型、文本编码器和 VAE 均已通过示例校验。</p>'
      showDialog(entry.name + ' · 训练组件', components)
      return
    }
    case 'download-dialog':
      showDialog(entry.name, `<label class="field">下载来源<select aria-label="下载来源"><option>ModelScope</option><option>Hugging Face</option></select></label>
        <p class="muted mono">${entry.source}</p><div class="kv"><span>模型体积</span><b>${entry.size}</b></div>
        <div class="kv"><span>待准备组件</span><b>${entry.components.join('、')}</b></div>
        <div class="kv"><span>目标目录</span><b class="mono">/models/training</b></div>
        ${button('start-download', '确认下载', { kind: 'primary', value: entry.id })}`)
      return
    case 'start-download': runDownload(entry); return
    case 'choose-model':
      state.model = value
      if (!entry.modes.includes(state.mode)) state.mode = entry.modes[0]
      state.ranCheck = false
      navigate('training')
      return
    case 'preflight': state.ranCheck = true; break
    case 'start-training': runTraining(); return
    case 'cancel-training': state.task.cancelled = true; tell('任务已取消。'); break
    case 'checkpoint': state.checkpoint = value; state.tested = false; break
    case 'test-lora': state.tested = true; tell('示例 checkpoint 试载通过。'); break
    case 'publish-dialog':
      showDialog('发布 LoRA', `<label class="field">文件名<input id="weight-name" value="landscape-light.safetensors"></label>
        <div class="kv"><span>目标目录</span><b class="mono">ComfyUI / models / loras / ${state.task.family}</b></div>
        ${button('publish', '确认发布并同步', { kind: 'primary' })}`)
      return
    case 'publish': {
      const name = document.querySelector('#weight-name').value.trim()
      if (!name.endsWith('.safetensors') || /[\\/:*?"<>|]/.test(name)) {
        tell('请输入有效的 .safetensors 文件名。')
        return
      }
      const path = state.task.family + '/' + name
      if (state.published === path) {
        showDialog('文件已存在', '<p>请修改名称后重新发布。</p>' + button('publish-dialog', '修改文件名', { kind: 'primary' }))
        return
      }
      state.published = path
      dialog.close()
      tell('LoRA 已发布并同步（示例）。')
      break
    }
    default: return
  }
  render()
})

function applyCaptions(items) {
  const captions = {
    5: '荒野中的低矮植被延伸至地平线，斜射的日光带来柔和暖色。',
    6: '青蓝色海浪涌向镜头，细碎白色浪花和阴影交叠。',
    7: '宽阔草原在远处缓缓起伏，阳光为绿色草叶勾勒明暗层次。',
    8: '低饱和的远山叠在天际线下，天空和地形形成横向分层。',
  }
  items.forEach((item) => {
    item.caption = captions[item.id] || '示例标注：描述画面主体、光线、颜色与构图。'
  })
  tell('已生成 ' + items.length + ' 条示例描述。')
}

upload.addEventListener('change', () => {
  const imported = [...upload.files].filter((file) => file.type.startsWith('image/')).map((file) => ({
    id: nextMediaId++, name: file.name, caption: '', src: URL.createObjectURL(file),
  }))
  state.added.push(...imported)
  imported.forEach(({ id }) => state.selected.add(id))
  if (imported.length) {
    state.focus = imported[0].id
    state.filter = 'all'
    tell('已加入 ' + imported.length + ' 张图片。')
    render()
  }
  upload.value = ''
})

main.addEventListener('input', (event) => {
  const input = event.target
  if (input.matches('.compare-control')) {
    state.split = Number(input.value)
    input.closest('.compare').style.setProperty('--split', state.split + '%')
    return
  }
  if (input.dataset.field === 'query') {
    state.query = input.value
    const caret = input.value.length
    render()
    const field = document.querySelector('[data-field="query"]')
    field.focus()
    field.setSelectionRange(caret, caret)
  }
  if (input.dataset.field === 'taskName' || input.type === 'number') state[input.dataset.field] = input.value
})

main.addEventListener('change', (event) => {
  const input = event.target
  if (input.dataset.editSample) {
    const id = Number(input.dataset.editSample)
    if (input.checked) state.selectedEdits.add(id)
    else state.selectedEdits.delete(id)
    state.ranCheck = false
    render()
    document.querySelector('[data-edit-sample="' + id + '"]')?.focus()
    return
  }
  if (input.dataset.media) {
    const id = Number(input.dataset.media)
    if (input.checked) state.selected.add(id)
    else state.selected.delete(id)
    render()
    document.querySelector('[data-media="' + id + '"]')?.focus()
    return
  }
  const { field } = input.dataset
  if (!['model', 'gpu', 'preset', 'allowEmpty', 'gradientCheckpoint', 'cpuOffload'].includes(field)) return
  state[field] = input.type === 'checkbox' ? input.checked : input.value
  state.ranCheck = false
  if (field === 'preset') {
    state.steps = input.value === 'short' ? 300 : 1200
    state.interval = input.value === 'short' ? 100 : 400
    state.rank = input.value === 'short' ? 16 : 32
  }
  render()
  document.querySelector('[data-field="' + field + '"]')?.focus()
})

render()
