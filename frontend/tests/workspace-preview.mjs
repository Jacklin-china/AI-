// Isolated browser QA only: synthetic API responses in memory, no Provider or business database.
// Run after npm run build: node tests/workspace-preview.mjs [--assets]
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve, extname, sep } from 'node:path'
import { createHash } from 'node:crypto'
const root = resolve(fileURLToPath(new URL('../../src/kantoku/shells/web/', import.meta.url)))
const project = { project_id: 'qa-project', title: '树梢上的守望', current_version: 1, director_version: null }
const brief = { version: 1, original_request: '一个少女站在树梢看村庄', hard_constraints: ['少女', '树梢', '村庄'], soft_preferences: ['安静'], creative_freedom: ['构图'] }
const assets = [{ asset_id: 'qa-character', name: '少女', version: 1, pinned_version: 1, state: 'active', details: { kind: 'character', appearance: '东方少女，保持角色身份', clothing: '白色古装' }, fixed_constraints: ['保持角色身份'], reference_artifact_ids: [] }]
const stages = ['creative_understanding', 'visual_direction', 'cinematography', 'director_critic', 'director_assemble']
const runs = []; const tasks = []; const versions = []
const conversations = Array.from({ length: 24 }, (_, index) => ({ id: index ? `qa-chat-${index}` : 'qa-conversation', title: index ? `创作对话 ${index} · 场景与人物` : '树梢上的守望', domain: 'comic', interaction_mode: 'guided', created_at: new Date().toISOString(), updated_at: new Date().toISOString(), messages: [] }))
// 图片工作台要能拿到 project，唯一路径是"对话 → 关联 Run → Run 的 project_id"，
// 所以首条对话必须挂上下面 execute() 生成的 qa-run-1。
conversations[0].related_run_ids = ['qa-run-1']
const port = Number(process.env.KANTOKU_QA_PORT ?? 8765)
const assetMode = process.argv.includes('--assets')
const clipboardMode = process.argv.includes('--clipboard')
const clipboardImage = clipboardMode ? await readFile(new URL('../../assets/reference/environments/fantasy-sky-city.png', import.meta.url)) : null
const clipboardPrompt = { artifact_id: 'qa-prompt-transfer', project_id: 'qa-project', shot_id: 'qa-shot', storyboard_id: 'qa-board',
  version: 1, shot_version: 1, storyboard_version: 1, director_spec_version: 1, creative_brief_version: 1,
  model_target: 'external', context_version: 'image-bibles-1', character_context: '身份：离线演示人物', world_context: '地点：空中城市',
  style_context: '艺术风格：离线演示', shot_context: '动作：抬起左手', positive_prompt: '保持参考图的人物和场景，只抬起左手。', negative_prompt: '文字水印',
  final_prompt: '离线交付验证：请使用所附参考图，保持原人物与场景，只抬起左手。\n\nNegative Constraint / 禁止内容\n文字水印',
  reference_attachments: [{ artifact_id: 'qa-reference-original', filename: 'qa-reference.png',
    sha256: clipboardImage ? createHash('sha256').update(clipboardImage).digest('hex') : '', image: { image_id: 'qa-image', version: 2 } }] }
const navigationMode = process.argv.includes('--work-return')
let navigationCreates = 0
let navigationQueries = 0
const navigationRequests = new Map(), navigationMessages = new Map()
const navigationWorks = [{ work_id: 'qa-return-work', title: '导航恢复验收作品', version: 1,
  conversation_id: 'qa-return-chat', story_conversation_id: 'qa-return-story', tasks: [] }]
const fixtureArtifact = (id, type, source, metadata, location = null) => ({
  id, run_id: 'offline-asset-fixture', node_id: 'fixture', type, source, status: 'ready',
  version: 1, created_at: new Date().toISOString(), location, metadata,
})
const fixtureArtifacts = [
  fixtureArtifact('qa-legacy-image', 'image', 'comic.archive', { request_id: 'qa-legacy-image' }, 'missing-archive.png'),
  fixtureArtifact('qa-video', 'video', 'studio.video', { origin: 'real', filename: 'offline-demo.mp4' }, 'offline-demo.mp4'),
  fixtureArtifact('qa-prompt', 'prompt', 'comic.prompt.compiler', { payload: {
    positive_prompt: Array.from({ length: 30 }, (_, index) => `离线完整提示词第 ${index + 1} 行：窗边的橘猫与雨后城市。`).join('\n'),
    negative_prompt: '不要文字、水印或多余肢体。',
  } }),
  fixtureArtifact('qa-document', 'document', 'studio.document', { payload: {
    title: '离线文档验收', content: '这是完整正文。\n第二段也应保留换行。',
    sections: Array.from({ length: 7 }, (_, index) => ({ title: `第 ${index + 1} 节`, content: `完整文档内容 ${index + 1}` })),
  } }),
  fixtureArtifact('qa-report', 'report', 'studio.report', { payload: {
    summary: '离线报告验收', passed: false, findings: [{ issue: '样例问题', solution: '样例解决方案' }], final_note: '报告末尾应当完整显示。',
  } }),
  fixtureArtifact('qa-mock', 'image', 'commerce.image.mock', { schema_name: 'commerce.product_image', origin: 'mock', payload: { model: 'offline-mock' } }),
  fixtureArtifact('qa-blocked', 'video', 'studio.video', { origin: 'blocked' }),
  fixtureArtifact('qa-missing', 'image', 'studio.image', { origin: 'real' }, 'missing.png'),
  fixtureArtifact('qa-failed', 'image', 'studio.image', { origin: 'real' }, 'unavailable.png'),
  fixtureArtifact('qa-corrupt', 'image', 'studio.image', { origin: 'real' }, 'corrupt.png'),
  fixtureArtifact('qa-empty', 'document', 'studio.document', { payload: {} }),
]
const fixturePng = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVk8AAAAASUVORK5CYII=', 'base64')
// ---- 图片工作台离线数据（只有合成响应，不触 Provider 与业务库）----
// 供 /workspace/comic/images 与「图片工作台」节点做视觉验收：暗色灯箱、标注框、版本条、右侧面板。
const wbImage = {
  image_id: 'qa-workbench-image', version: 1, project_id: project.project_id, conversation_id: 'qa-conversation',
  artifact_id: 'qa-workbench-image', sha256: 'qa-workbench', filename: '废墟茶铺前的机械战马.png', mime_type: 'image/png',
  parent: null, instruction_artifact_id: null, provenance: 'import', created_at: new Date().toISOString(),
  selection: { selected_version: 1, selection_version: 1 }, label: '废墟茶铺前的机械战马.png',
}
const wbResult = {
  ...wbImage, version: 2, artifact_id: 'qa-workbench-result', parent: { image_id: wbImage.image_id, version: 1 },
  filename: '废墟茶铺前的机械战马-v2.png', label: undefined,
}
const wbBasis = {
  source_image: { image_id: wbImage.image_id, version: 1 }, project_id: project.project_id, conversation_id: 'qa-conversation',
  task_context: { task: brief.original_request },
}
const wbAnalysis = {
  artifact_id: 'qa-analysis', version: 1, status: 'completed', advisory: true, run_id: 'qa-run-1', basis: wbBasis,
  output: {
    summary: '离线图片观察：招牌文字与左下橱窗内容需要整理。',
    findings: [
      { finding_id: 'sign', observation: '招牌上「卿辉nb」四字需完整确认', suggested_change: '补全招牌文字并保持原有排版', confidence: 0.72, uncertainty: '离线替身，不证明真实视觉质量', region: [0.34, 0.09, 0.62, 0.26] },
      { finding_id: 'shelf', observation: '左侧破碎玻璃窗内仍贴着一杯奶茶海报', suggested_change: '清理或替换海报内容', confidence: 0.61, uncertainty: '需人工确认', region: [0.06, 0.44, 0.24, 0.62] },
    ],
    protected_features: ['机械战马的姿态与金属质感', '废墟整体色调'],
    limitations: ['离线替身，不证明真实视觉质量'],
  },
}
const wbInstruction = {
  artifact_id: 'qa-edit', version: 1, status: 'completed', run_id: 'qa-run-1',
  basis: { ...wbBasis, constraints: [] }, analysis_artifact_id: wbAnalysis.artifact_id, requires_confirmation: false,
  scope: {
    user_instruction: '补全招牌文字并清理左下橱窗海报', selected_findings: wbAnalysis.output.findings,
    protected_features: wbAnalysis.output.protected_features,
    edit_scope: { mode: 'regions', regions: [{ region_id: 'region:qa-1', label: '人工选区', bounds: [0.34, 0.09, 0.62, 0.26], finding_id: 'sign', instruction: '补全招牌文字' }], whole_finding_ids: [], instruction_target: { mode: 'selected_regions' } },
  },
  output: { positive_prompt: '补全招牌上的文字，保持原有透视与材质；清理左侧玻璃窗内的海报。', negative_prompt: '不要改变机械战马姿态，不要添加人物。', summary: '局部整理：招牌文字 + 左下橱窗' },
}
let workbenchVersion = 0
let wbDraftStored = null
let wbEditCreated = false
const wbWorkbenchState = () => ({ version: workbenchVersion, state: { image: { image_id: wbImage.image_id, version: 1 }, edit_image: { image_id: wbImage.image_id, version: 1 }, auto_analyze: true } })
function newSpec() {
  return { schema_version: 2, spec_id: 'qa-director', project_id: project.project_id, version: versions.length + 1, creative_brief_version: 1, asset_versions: { 'asset:qa-character': 1 }, created_at: new Date().toISOString(),
    creative_decision: { intent_summary: '少女从树梢眺望村庄，表达安静的守望与归属感。', narrative_context: '近处枝叶与远方村落建立人物和家园的联系。', emotional_target: '宁静中带有期待', audience_experience: '跟随少女的视线寻找远方的生活', hard_constraints: brief.hard_constraints, narrative_focus: '人物视线与村庄的关系' },
    director_plan: { visual_strategy: '以人物视线串联树梢和远方村庄。', visual_focus: '少女的姿态与眺望方向', composition_strategy: '树枝形成前景，村庄位于视线延伸处。', color_strategy: '柔和林间绿色与村庄暖色协调', creative_choices: ['让环境说明人物的期待，而非夸张力量。'], continuity_rules: ['保持白色古装与角色身份'] },
    cinematography: { shot_size: '远景', camera_angle: '与树梢平齐', spatial_feel: '枝叶、人物、村庄形成三个层次', lighting: '柔和侧光保留人物和村庄细节', light_direction: '树冠侧面', depth_strategy: '远方村庄保持可识别', material_language: '衣料与枝叶的柔韧质感' },
    critic_result: { verdict: 'pass', public_summary: '意图与视觉表达一致，硬约束保留。', findings: [] } }
}
function execute(body, pending = false, existing = null) {
  const failed = body.task?.includes('失败')
  const spec = failed || pending ? null : body.review_current ? { ...structuredClone(versions.at(-1)), version: versions.length + 1, critic_result: { verdict: 'pass', public_summary: '离线审核替身：当前手工修改已验证', findings: [] }, user_confirmed: false } : { ...newSpec(), user_confirmed: false }
  if (spec) { for (const [stage, patch] of Object.entries(body.stage_edits ?? {})) { const key = { creative_understanding: 'creative_decision', visual_direction: 'director_plan', cinematography: 'cinematography' }[stage]; spec[key] = patch } versions.push(spec); project.director_version = spec.version; project.current_version++ }
  const id = existing?.run_id ?? `qa-run-${runs.length + 1}`
  const run = { id, domain: 'comic', workflow: 'comic.director', status: pending ? 'running' : failed ? 'failed' : 'completed', started_at: existing ? runs.find(run => run.id === id).started_at : new Date().toISOString(), updated_at: new Date().toISOString(), completed_at: pending ? null : new Date().toISOString(), current_node: pending ? 'creative_understanding' : '__end__', nodes: [], cost_fen: 0, error: failed ? '离线测试：DirectorSpec validation failed' : null,
    state: { task: body.task ?? runs.find(run => run.id === body.previous_run_id)?.state.task, project_id: project.project_id, execution_mode: body.creation_mode, conversation_id: body.conversation_id, trace_id: `qa-trace-${id}`, input_versions: { creative_brief: 1, 'asset:qa-character': 1 }, rerun_from: body.rerun_from, error_id: failed ? 'qa-error' : null } }
  const output = { creative_understanding: { creative_decision: spec?.creative_decision }, visual_direction: { director_plan: spec?.director_plan }, cinematography: { cinematography: spec?.cinematography }, director_critic: { critic_result: spec?.critic_result }, director_assemble: { director_spec: spec } }
  const task = { run_id: id, status: run.status, recovery_required: false, director_spec: spec, director_execution_summary: { mode: body.creation_mode, status_label: failed ? '导演方案生成失败' : '导演方案已保存', current_stage: failed ? 'creative_understanding' : 'director_assemble', error_id: run.state.error_id, available_actions: failed ? ['view', 'resume'] : body.creation_mode === 'professional' ? ['view', 'edit_stage', 'rerun_stage'] : ['view'], stages: body.creation_mode === 'professional' ? stages.map(stage => ({ stage, status: failed ? 'pending' : 'completed', input_versions: run.state.input_versions, output_summary: null, output: failed ? {} : output[stage] })) : [], ...(body.creation_mode === 'professional' ? { critic_result: spec?.critic_result } : {}) } }
  if (pending) { task.director_execution_summary.status_label = '离线验收：正在理解创意'; task.director_execution_summary.stages.forEach((stage, index) => { stage.status = index === 0 ? 'running' : 'pending'; stage.output = {} }) }
  if (existing) { Object.assign(runs.find(item => item.id === id), run); Object.assign(existing, task); return existing }
  runs.unshift(run); tasks.unshift(task); return task
}
execute({ task: brief.original_request, creation_mode: 'professional', conversation_id: 'qa-conversation' })
const json = (response, value, status = 200) => { response.writeHead(status, { 'Content-Type': 'application/json' }); response.end(JSON.stringify(value)) }
createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1:8765'); const path = url.pathname
    if (clipboardMode && path === '/qa/paste') {
      response.writeHead(200, { 'Content-Type': 'text/html;charset=utf-8' })
      return response.end(`<!doctype html><html lang="zh"><meta charset="utf-8"><title>离线图文粘贴验证</title><h1>离线粘贴接收区</h1><p>此页面只检查本次粘贴，不发送或保存内容。</p><button>读取本次复制</button><div contenteditable="true" role="textbox" aria-label="粘贴图文" style="border:1px solid #aaa;min-height:100px;padding:20px">请在这里粘贴</div><pre role="status">尚未粘贴</pre><script>
        document.querySelector('button').addEventListener('click', async () => {
          try {
            const result = {types: [], text: '', htmlContainsImage: false, images: []};
            for (const item of await navigator.clipboard.read()) for (const type of item.types) {
              result.types.push(type); const content = await item.getType(type);
              if (type === 'text/plain') result.text = await content.text();
              if (type === 'text/html') result.htmlContainsImage = (await content.text()).includes('<img');
              if (type.startsWith('image/')) { const image = await createImageBitmap(content); result.images.push({type, bytes: content.size, width:image.width, height:image.height}); image.close(); }
            }
            document.querySelector('[role=status]').textContent = JSON.stringify(result, null, 2);
          } catch(error) { document.querySelector('[role=status]').textContent = error.message; }
        });
        document.querySelector('[contenteditable]').addEventListener('paste', async event => {
          event.preventDefault(); const data = event.clipboardData;
          const result = { types: [...data.types], text: data.getData('text/plain'), htmlContainsImage: data.getData('text/html').includes('<img'), images: [] };
          for (const file of data.files) { const image = await createImageBitmap(file); result.images.push({type:file.type,bytes:file.size,width:image.width,height:image.height}); image.close(); }
          document.querySelector('[role=status]').textContent = JSON.stringify(result, null, 2);
        });</script></html>`)
    }
    if (clipboardMode && path === '/api/artifacts/qa-reference-original/content') { response.writeHead(200, { 'Content-Type': 'image/png' }); return response.end(clipboardImage) }
    if (clipboardMode && path === '/api/comic/works') return json(response, { works: [] })
    if (clipboardMode && /^\/api\/comic\/conversations\/[^/]+\/works$/.test(path)) return json(response, { works: [{
      work_id: 'qa-project:brief:1', project_id: 'qa-project', brief_version: 1, title: brief.original_request,
      original_request: brief.original_request, current: true, director_run_id: 'qa-run-1', has_director: true, image_count: 1 }] })
    if (clipboardMode && path.endsWith('/prompt/versions')) return json(response, { versions: [clipboardPrompt] })
    if (clipboardMode && path === '/api/comic/projects/qa-project/prompts') return json(response, { prompts: [clipboardPrompt] })
    let body = {}; if (request.method === 'POST') { let text = ''; for await (const chunk of request) text += chunk; body = JSON.parse(text || '{}') }
    if (navigationMode && path === '/qa/work-return') return json(response, { creates: navigationCreates,
      works: navigationWorks.length, turns: navigationRequests.size, queries: navigationQueries,
      phases: [...navigationRequests.values()].map(item => item.phase) })
    if (navigationMode && path === '/api/comic/works') {
      if (request.method !== 'POST') return json(response, { works: navigationWorks })
      navigationCreates++
      const work = { ...navigationWorks[0], work_id: `qa-created-work-${navigationCreates}`,
        conversation_id: `qa-created-chat-${navigationCreates}`, title: body.title, tasks: [] }
      navigationWorks.unshift(work)
      return json(response, work)
    }
    if (navigationMode && path.startsWith('/api/comic/works/')) {
      const work = navigationWorks.find(item => item.work_id === path.split('/')[4])
      if (!work) return json(response, { error: 'Offline work not found' }, 404)
      if (path.endsWith('/turns') && request.method === 'POST') {
        const turn = { phase: 'submitted', result: null }
        navigationRequests.set(body.request_id, turn)
        const messages = navigationMessages.get(work.work_id) ?? []
        navigationMessages.set(work.work_id, messages)
        const user = { id: `qa-user-${body.request_id}`, role: 'user', content: body.task,
          event_id: `director-request:${body.request_id}`, created_at: new Date().toISOString() }
        messages.push(user)
        // Deliberately finish after navigation, without allocating a task beforehand.
        await new Promise(resolve => setTimeout(resolve, 10000))
        const result = execute({ ...body, conversation_id: work.conversation_id })
        const task = { task_id: 'qa-project', project_id: 'qa-project', work_id: work.work_id,
          conversation_id: work.conversation_id, title: '离线延迟完成的方案', project_version: project.current_version }
        work.tasks.push(task); user.run_id = result.run_id
        messages.push({ id: `qa-answer-${body.request_id}`, role: 'assistant', type: 'text',
          content: '离线验收：离开页面期间任务继续执行，方案已保存。可拖选这段文字并复制到输入框。',
          run_id: result.run_id, created_at: new Date().toISOString() })
        turn.phase = 'completed'; turn.result = { ...result, task_id: task.task_id }
        return json(response, turn.result)
      }
      if (path.includes('/requests/')) {
        navigationQueries++
        return json(response, navigationRequests.get(path.split('/')[6]) ?? { phase: null, result: null })
      }
      if (path.endsWith('/conversation')) return json(response, { conversation_id: work.conversation_id,
        messages: navigationMessages.get(work.work_id) ?? [],
        related_run_ids: runs.filter(run => run.state.conversation_id === work.conversation_id).map(run => run.id) })
      return json(response, work)
    }
    if (path === '/api/session') return json(response, { token: 'offline-qa-only' })
    if (path === '/api/runs') return json(response, { runs: [] })
    if (path === '/api/approvals') return json(response, { approvals: [] })
    if (path === '/api/artifacts') return json(response, { artifacts: assetMode ? fixtureArtifacts : [] })
    if (assetMode && path === '/media/qa-legacy-image') {
      response.writeHead(200, { 'Content-Type': 'image/png' }); return response.end(fixturePng)
    }
    if (assetMode && /^\/api\/artifacts\/[^/]+\/content$/.test(path)) {
      const id = path.split('/')[3]
      if (id === 'qa-video') {
        const video = await readFile(new URL('../../assets/demo/w1-cli-trace.mp4', import.meta.url))
        response.writeHead(200, { 'Content-Type': 'video/mp4' }); return response.end(video)
      }
      if (id === 'qa-corrupt') {
        response.writeHead(200, { 'Content-Type': 'image/png' }); return response.end('Offline corrupt image fixture')
      }
      return json(response, { safe_message: id === 'qa-failed' ? '离线测试：文件服务暂时不可用。' : '媒体文件不存在。',
        error_id: `offline-${id}`, trace_id: 'offline-asset-fixture' }, id === 'qa-failed' ? 503 : 404)
    }
    if (/^\/api\/runs\/[^/]+\/cancel$/.test(path) && request.method === 'POST') {
      const task = tasks.find(task => task.run_id === path.split('/')[3]); const run = runs.find(run => run.id === task.run_id)
      task.status = run.status = 'cancelled'; task.director_execution_summary.status_label = '导演任务已取消'
      return json(response, run)
    }
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) return json(response, { events: [{ id: 1, run_id: path.split('/')[3], sequence: 1, event_type: 'director_spec_created', created_at: new Date().toISOString(), payload: {} }] })
    if (path.startsWith('/api/runs/')) return json(response, runs.find(run => run.id === path.split('/')[3]))
    if (path === '/api/conversations' && request.method === 'POST') { const created = { ...conversations[0], id: `qa-new-${conversations.length}`, title: '新对话', messages: [] }; conversations.unshift(created); return json(response, created) }
    if (path === '/api/conversations') return json(response, { conversations })
    if (path.startsWith('/api/conversations/')) return json(response, conversations.find(item => item.id === path.split('/')[3]))
    if (path === '/api/comic/projects' && request.method === 'POST') { project.title = body.title; brief.original_request = body.brief.original_request; return json(response, { project, creative_brief: brief }) }
    if (path === '/api/comic/projects/qa-project') return json(response, { project, creative_brief: brief })
    if (path.startsWith('/api/') && path.endsWith('/assets')) return json(response, { assets })
    if (path.startsWith('/api/') && path.endsWith('/tasks')) return json(response, { tasks })
    if (path.endsWith('/storyboards')) return json(response, { storyboards: [{ storyboard_id: 'qa-board', title: '树梢守望', version: 1, director_spec_version: 1, status: 'draft' }] })
    if (path.endsWith('/shots')) return json(response, { shots: [{ shot_id: 'qa-shot', storyboard_id: 'qa-board', sequence_number: 1, subject: '少女', purpose: '表现守望', action: '眺望村庄', version: 1, status: 'planned', character_asset_versions: [{ asset_id: 'qa-character', version: 1 }], scene_asset_versions: [] }] })
    if (path.endsWith('/prompt/versions')) return json(response, { versions: [] })
    if (path.endsWith('/director-spec/versions')) return json(response, { versions })
    if (path.endsWith('/director-spec/confirm') && request.method === 'POST') {
      const spec = versions.at(-1)
      if (spec.version !== body.version || spec.critic_result?.verdict !== 'pass') return json(response, { safe_message: '离线测试：请先审核当前版本' }, 400)
      spec.user_confirmed = true
      return json(response, spec)
    }
    if (path.endsWith('/director-spec/restore') && request.method === 'POST') { const spec = structuredClone(versions.find(item => item.version === body.version)); spec.version = versions.length + 1; spec.source = 'restored'; versions.push(spec); project.director_version = spec.version; project.current_version++; return json(response, spec) }
    if (path.endsWith('/director-spec')) {
      if (request.method !== 'POST') return json(response, versions.at(-1))
      if (body.draft || body.revision_instruction) {
        const current = versions.at(-1)
        const spec = { ...structuredClone(body.draft ?? current), spec_id: 'qa-director', project_id: project.project_id, creative_brief_version: brief.version, version: versions.length + 1, critic_result: null, user_confirmed: false }
        if (body.revision_instruction) spec.director_plan.composition_strategy = `离线修改指令：${body.revision_instruction}`
        versions.push(spec); project.director_version = spec.version; project.current_version++
        return json(response, spec)
      }
      // 只在隔离 QA 中按明确测试指令模拟等待，生产代码没有人为延时或虚假状态。
      const slow = body.task?.includes('离线慢任务')
      const task = execute(body, slow)
      if (slow) { await new Promise(resolve => setTimeout(resolve, 10000)); if (task.status !== 'cancelled') execute(body, false, task) }
      if (task.status === 'failed') return json(response, { safe_message: '离线测试：DirectorSpec validation failed', trace_id: `qa-trace-${task.run_id}`, error_id: 'qa-error' }, 500)
      return json(response, task)
    }
    // ---- 图片工作台（/workspace/comic/images）：与真实 core.ts 路由一一对应，全部内存合成 ----
    if (path === '/api/comic/projects/qa-project/images' && request.method === 'GET') return json(response, { images: [wbResult, wbImage] })
    if (path === '/api/comic/projects/qa-project/prompts') return json(response, { prompts: [] })
    if (path === '/api/comic/projects/qa-project/image-workbench') {
      if (request.method !== 'PUT') return json(response, wbWorkbenchState())
      workbenchVersion += 1
      return json(response, { version: workbenchVersion, state: body })
    }
    if (/^\/api\/comic\/images\/[^/]+\/versions$/.test(path)) return json(response, { versions: [wbResult, wbImage], next_before: null, selection: { selected_version: 1, selection_version: 1 } })
    if (/^\/api\/comic\/images\/[^/]+\/review-draft$/.test(path)) {
      if (request.method !== 'PUT') return json(response, wbDraftStored ?? { version: 0, state: null })
      wbDraftStored = { version: (wbDraftStored?.version ?? 0) + 1, state: body }; return json(response, wbDraftStored)
    }
    if (/^\/api\/comic\/images\/[^/]+\/selection$/.test(path) && request.method === 'PUT') return json(response, { ok: true })
    if (path === '/api/comic/projects/qa-project/image-comparisons') return json(response, { artifact_id: 'qa-compare', before: wbImage, after: wbResult, output: { summary: '离线比较：招牌文字已补全。', changes: [{ check_id: 'c1', status: 'satisfied', evidence: '招牌文字完整' }], preservation: [{ check_id: 'p1', status: 'satisfied', evidence: '机械战马姿态未变' }], limitations: ['离线替身，不证明真实视觉质量'] } })
    if (path === '/api/comic/projects/qa-project/image-analyses') return request.method === 'POST' ? json(response, wbAnalysis) : json(response, { items: [wbAnalysis], next_cursor: null })
    if (path.startsWith('/api/comic/projects/qa-project/image-analyses/')) return json(response, wbAnalysis)
    if (path === '/api/comic/projects/qa-project/edit-instructions') {
      if (request.method === 'POST') { wbEditCreated = true; return json(response, { ...wbInstruction, requires_confirmation: true }) }
      return json(response, { items: wbEditCreated ? [wbInstruction] : [], next_cursor: null })
    }
    if (path.endsWith('/confirm')) return json(response, { ok: true })
    if (path.startsWith('/api/comic/projects/qa-project/edit-instructions/')) return json(response, wbInstruction)
    if (/^\/api\/artifacts\/[^/]+\/content$/.test(path) && path.includes('qa-workbench')) {
      const artwork = await readFile(new URL('../../assets/reference/environments/fantasy-sky-city.png', import.meta.url))
      response.writeHead(200, { 'Content-Type': 'image/png' }); return response.end(artwork)
    }
    if (path.startsWith('/api/')) return json(response, { error: 'Offline fixture has no such endpoint' }, 404)
    const target = resolve(root, `.${path}`); if (!target.startsWith(root + sep) && target !== root) return json(response, {}, 403)
    const file = path.startsWith('/assets/') ? target : resolve(root, 'index.html')
    const content = await readFile(file); response.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(file)] ?? 'application/octet-stream' }); response.end(content)
  } catch (error) { json(response, { error: error.message }, 500) }
}).listen(port, '127.0.0.1', () => process.stdout.write(`Offline ${assetMode ? 'Asset' : 'Workspace'} QA: http://127.0.0.1:${port}${assetMode ? '/assets' : '/workspace/comic/run/qa-run-1'}\n`))
