// Isolated browser QA only: synthetic API responses in memory, no Provider or business database.
// Run after npm run build: node tests/workspace-preview.mjs
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve, extname, sep } from 'node:path'
const root = resolve(fileURLToPath(new URL('../../src/kantoku/shells/web/', import.meta.url)))
const project = { project_id: 'qa-project', title: '山海经穷奇观察人类文明', current_version: 1, director_version: null }
const brief = { version: 1, original_request: project.title, hard_constraints: ['穷奇', '悬崖'], soft_preferences: ['孤独感'], creative_freedom: ['构图'] }
const assets = [{ asset_id: 'qa-character', name: '穷奇', version: 1, pinned_version: 1, state: 'active', details: { kind: 'character', appearance: '远古异兽，保留翼与角', clothing: '无' }, fixed_constraints: ['保持异兽身份'], reference_artifact_ids: [] }]
const stages = ['creative_understanding', 'visual_direction', 'cinematography', 'director_critic', 'director_assemble']
const runs = []; const tasks = []; const versions = []
function newSpec() {
  return { schema_version: 2, spec_id: 'qa-director', project_id: project.project_id, version: versions.length + 1, creative_brief_version: 1, asset_versions: { 'asset:qa-character': 1 }, created_at: new Date().toISOString(),
    creative_decision: { intent_summary: '表现异兽观察人类文明的孤独感，而非战斗威力。', narrative_context: '悬崖与远方城市形成时间和尺度的对比。', emotional_target: '孤独而克制', audience_experience: '远古生命的距离感', hard_constraints: brief.hard_constraints, narrative_focus: '观察而非攻击' },
    director_plan: { visual_strategy: '让环境与主体共同表达隔阂。', visual_focus: '穷奇安静的姿态', composition_strategy: '悬崖占近景，城市退到远处。', color_strategy: '自然岩色与城市暖光对比', creative_choices: ['不依赖低机位夸张力量。'], continuity_rules: ['保持翼与角'] },
    cinematography: { shot_size: '远景', camera_angle: '平视', spatial_feel: '远近空间分层', lighting: '侧逆光强调翼的轮廓', light_direction: '城市方向', depth_strategy: '城市保持可识别', material_language: '岩石与毛发形成触感对照' },
    critic_result: { verdict: 'pass', public_summary: '意图与视觉表达一致，硬约束保留。', findings: [] } }
}
function execute(body, pending = false, existing = null) {
  const failed = body.task?.includes('失败')
  const spec = failed || pending ? null : newSpec()
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
    let body = {}; if (request.method === 'POST') { let text = ''; for await (const chunk of request) text += chunk; body = JSON.parse(text || '{}') }
    if (path === '/api/session') return json(response, { token: 'offline-qa-only' })
    if (path === '/api/runs') return json(response, { runs: [] })
    if (path === '/api/approvals') return json(response, { approvals: [] })
    if (path === '/api/artifacts') return json(response, { artifacts: [] })
    if (/^\/api\/runs\/[^/]+\/cancel$/.test(path) && request.method === 'POST') {
      const task = tasks.find(task => task.run_id === path.split('/')[3]); const run = runs.find(run => run.id === task.run_id)
      task.status = run.status = 'cancelled'; task.director_execution_summary.status_label = '导演任务已取消'
      return json(response, run)
    }
    if (/^\/api\/runs\/[^/]+\/events$/.test(path)) return json(response, { events: [{ id: 1, run_id: path.split('/')[3], sequence: 1, event_type: 'director_spec_created', created_at: new Date().toISOString(), payload: {} }] })
    if (path.startsWith('/api/runs/')) return json(response, runs.find(run => run.id === path.split('/')[3]))
    if (path === '/api/conversations' && request.method === 'POST') return json(response, { id: 'qa-conversation' })
    if (path === '/api/comic/projects' && request.method === 'POST') { project.title = body.title; brief.original_request = body.brief.original_request; return json(response, { project, creative_brief: brief }) }
    if (path === '/api/comic/projects/qa-project') return json(response, { project, creative_brief: brief })
    if (path.endsWith('/assets')) return json(response, { assets })
    if (path.endsWith('/tasks')) return json(response, { tasks })
    if (path.endsWith('/storyboards')) return json(response, { storyboards: [{ storyboard_id: 'qa-board', title: '悬崖观察', version: 1, director_spec_version: 1, status: 'draft' }] })
    if (path.endsWith('/shots')) return json(response, { shots: [{ shot_id: 'qa-shot', sequence_number: 1, subject: '穷奇', purpose: '表现隔阂', action: '静静观察', version: 1, status: 'planned', character_asset_versions: [{ asset_id: 'qa-character', version: 1 }], scene_asset_versions: [] }] })
    if (path.endsWith('/prompt/versions')) return json(response, { versions: [] })
    if (path.endsWith('/director-spec/versions')) return json(response, { versions })
    if (path.endsWith('/director-spec/restore') && request.method === 'POST') { const spec = structuredClone(versions.find(item => item.version === body.version)); spec.version = versions.length + 1; spec.source = 'restored'; versions.push(spec); project.director_version = spec.version; project.current_version++; return json(response, spec) }
    if (path.endsWith('/director-spec')) {
      if (request.method !== 'POST') return json(response, versions.at(-1))
      // 只在隔离 QA 中按明确测试指令模拟等待，生产代码没有人为延时或虚假状态。
      const slow = body.task?.includes('离线慢任务')
      const task = execute(body, slow)
      if (slow) { await new Promise(resolve => setTimeout(resolve, 10000)); if (task.status !== 'cancelled') execute(body, false, task) }
      if (task.status === 'failed') return json(response, { safe_message: '离线测试：DirectorSpec validation failed', trace_id: `qa-trace-${task.run_id}`, error_id: 'qa-error' }, 500)
      return json(response, task)
    }
    if (path.startsWith('/api/')) return json(response, { error: 'Offline fixture has no such endpoint' }, 404)
    const target = resolve(root, `.${path}`); if (!target.startsWith(root + sep) && target !== root) return json(response, {}, 403)
    const file = path.startsWith('/assets/') ? target : resolve(root, 'index.html')
    const content = await readFile(file); response.writeHead(200, { 'Content-Type': ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(file)] ?? 'application/octet-stream' }); response.end(content)
  } catch (error) { json(response, { error: error.message }, 500) }
}).listen(8765, '127.0.0.1', () => process.stdout.write('Offline Workspace QA: http://127.0.0.1:8765/workspace/comic/run/qa-run-1\n'))
