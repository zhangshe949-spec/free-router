/**
 * free-router 控制台 — 单页瀑布流应用逻辑
 * 赛博朋克风格，全功能内联，无侧边栏
 */

console.log('[free-router] app.js loaded');

const API_BASE = '/admin';

// ============ 状态管理 ============
const state = {
  dashboard: {},
  models: [],
  providers: [],
  quota: {},
  cooldowns: {},
  cache: {},
  policy: {},
  settings: {},
  theme: localStorage.getItem('theme') || 'dark',
  currentView: 'table',
  modelFilters: { search: '', type: '', provider: '', capability: '' },
  modelSortBy: 'favorite',
  favorites: new Set(JSON.parse(localStorage.getItem('fr_favorites') || '[]')),
  providerSections: {}, // 折叠状态
  keysInfo: {}, // 各提供商 API Key 信息（来自 /admin/keys）
  probe: {}, // 各提供商连通性/延迟探测结果（来自 /admin/status）
  notifications: { unread: 0, events: [] }, // 模型更新通知（来自 /admin/notifications）
  catalogKind: '',        // 目录筛选：'' | cn | intl | local
  catalogHideProxy: false,
  catalogOnlyFree: false,
  _cache: new Map(),
  _cacheTTL: 5000,
};

// 提供商元数据：中文名、说明、免费额度规则、是否需代理、认证方式
const PROVIDER_META = {
  'amd-radeon': { 
    nameCn: 'AMD Radeon', nameEn: 'AMD Radeon',
    desc: 'AMD官方免费推理API，需注册开发者账号获取rc-xxx密钥',
    freeQuota: '每日额度较高，速率限制宽松，RPM~60',
    recovery: '额度实时恢复，无固定重置时间',
    needProxy: false,
    auth: '开发者门户注册，获取rc-xxx API Key',
    payment: '完全免费',
  },
  'modelscope': { 
    nameCn: '魔搭社区', nameEn: 'ModelScope',
    desc: '阿里云魔搭社区推理服务，支持大量开源模型',
    freeQuota: '免费模型调用，日额度视模型而定',
    recovery: '每日0点重置',
    needProxy: false,
    auth: 'ModelScope账号登录，获取Access Token',
    payment: '免费模型永久免费，付费模型按量计费',
  },
  'kilogw': { 
    nameCn: 'Kilo Code', nameEn: 'Kilo Code',
    desc: 'Kilo Code聚合网关，免Key匿名访问，支持免费模型池',
    freeQuota: '仅限:free后缀模型，日额度不限',
    recovery: '无额度限制',
    needProxy: false,
    auth: '免Key，匿名访问（Authorization留空）',
    payment: '仅免费模型',
  },
  'zhipu': { 
    nameCn: '智谱 GLM', nameEn: 'Zhipu GLM',
    desc: '智谱AI开放平台，中文能力强，GLM-4系列免费',
    freeQuota: 'glm-4-flash/glm-4.7-flash永久免费，每日大额度',
    recovery: '每日0点重置',
    needProxy: false,
    auth: '智谱开放平台注册，获取ZAI_API_KEY',
    payment: '免费模型永久免费，高阶模型按量计费',
  },
  'siliconflow': { 
    nameCn: '硅基流动', nameEn: 'SiliconFlow',
    desc: '硅基流动推理平台，9B以下模型永久免费',
    freeQuota: '9B以下模型无限量免费，大模型有免费额度',
    recovery: '每日0点重置',
    needProxy: false,
    auth: '硅基流动控制台获取API Key',
    payment: '小模型永久免费，大模型按Token计费',
  },
  'qianfan': { 
    nameCn: '百度千帆', nameEn: 'Baidu Qianfan',
    desc: '百度千帆大模型平台，ernie-speed/lite永久免费',
    freeQuota: 'ernie-speed-8k/ernie-lite-8k永久免费',
    recovery: '每日0点重置',
    needProxy: false,
    auth: '百度智能云控制台获取API Key',
    payment: '免费模型永久免费，专业版按量计费',
  },
  'hunyuan': { 
    nameCn: '腾讯混元', nameEn: 'Tencent Hunyuan',
    desc: '腾讯混元大模型平台，hunyuan-lite免费',
    freeQuota: 'hunyuan-lite永久免费，日额度充足',
    recovery: '每日0点重置',
    needProxy: false,
    auth: '腾讯云控制台获取HUNYUAN_API_KEY',
    payment: '免费模型永久免费，专业版按量计费',
  },
  'spark': { 
    nameCn: '讯飞星火', nameEn: 'iFlytek Spark',
    desc: '科大讯飞星火大模型，spark-lite免费',
    freeQuota: 'spark-lite永久免费，日额度充足',
    recovery: '每日0点重置',
    needProxy: false,
    auth: '讯飞开放平台获取SPARK_API_KEY',
    payment: '免费模型永久免费，专业版按量计费',
  },
  'dashscope': { 
    nameCn: '阿里百炼', nameEn: 'Alibaba DashScope',
    desc: '阿里云百炼平台，qwen-plus/turbo每模型100万token免费',
    freeQuota: '每模型100万token免费额度，用完计费',
    recovery: '额度不自动恢复，需人工充值或申请',
    needProxy: false,
    auth: '阿里云百炼控制台获取DASHSCOPE_API_KEY',
    payment: '免费额度用完后按Token计费',
  },
  'ark': { 
    nameCn: '火山方舟', nameEn: 'VolcEngine Ark',
    desc: '字节跳动火山方舟，需加入「协作奖励计划」获免费额度',
    freeQuota: '加入计划后每日200万token免费，否则按量计费',
    recovery: '每日0点重置（需加入计划）',
    needProxy: false,
    auth: '火山方舟控制台获取ARK_API_KEY，需加入协作奖励计划',
    payment: '未加入计划全量计费，加入后每日免费额度',
  },
  // ===== 国内主流（OpenAI 兼容，可一键接入）=====
  'deepseek': {
    nameCn: '深度求索', nameEn: 'DeepSeek', kind: 'cn',
    baseUrl: 'https://api.deepseek.com/v1', keyEnv: 'DEEPSEEK_API_KEY', compat: 'openai',
    desc: '深度求索官方API，deepseek-chat / reasoner 性价比极高',
    freeQuota: '无免费额度，但价格极低（约 ¥0.5/百万 token 起）',
    recovery: '按量计费，充值即用',
    needProxy: false, auth: 'DeepSeek 开放平台注册获取 API Key',
    payment: '按 Token 计费，无免费额度',
  },
  'moonshot': {
    nameCn: '月之暗面 Kimi', nameEn: 'Moonshot AI', kind: 'cn',
    baseUrl: 'https://api.moonshot.cn/v1', keyEnv: 'MOONSHOT_API_KEY', compat: 'openai',
    desc: 'Kimi 大模型，超长上下文（128K+），中文写作与长文总结强',
    freeQuota: '新用户赠送额度，无长期免费模型',
    recovery: '赠送额度用完即止',
    needProxy: false, auth: 'Moonshot 开放平台获取 API Key',
    payment: '按 Token 计费',
  },
  'minimax': {
    nameCn: '稀宇 MiniMax', nameEn: 'MiniMax', kind: 'cn',
    baseUrl: 'https://api.minimax.chat/v1', keyEnv: 'MINIMAX_API_KEY', compat: 'openai',
    desc: 'MiniMax abab 系列，语音与长文本能力突出',
    freeQuota: '新用户赠送额度',
    recovery: '额度用完即止',
    needProxy: false, auth: 'MiniMax 开放平台获取 API Key',
    payment: '按量计费',
  },
  'stepfun': {
    nameCn: '阶跃星辰', nameEn: 'StepFun', kind: 'cn',
    baseUrl: 'https://api.stepfun.com/v1', keyEnv: 'STEPFUN_API_KEY', compat: 'openai',
    desc: '阶跃星辰 Step 系列，多模态与推理均衡',
    freeQuota: '部分模型限时免费',
    recovery: '每日 0 点重置',
    needProxy: false, auth: '阶跃星辰开放平台获取 API Key',
    payment: '免费模型 + 按量计费',
  },
  'baichuan': {
    nameCn: '百川智能', nameEn: 'Baichuan', kind: 'cn',
    baseUrl: 'https://api.baichuan-ai.com/v1', keyEnv: 'BAICHUAN_API_KEY', compat: 'openai',
    desc: '百川 Baichuan 系列，中文知识问答见长',
    freeQuota: '赠送额度',
    recovery: '额度用完即止',
    needProxy: false, auth: '百川智能开放平台获取 API Key',
    payment: '按量计费',
  },
  'sensetime': {
    nameCn: '商汤日日新', nameEn: 'SenseTime SenseNova', kind: 'cn',
    baseUrl: 'https://api.sensenova.cn/compatible-mode/v1', keyEnv: 'SENSENOVA_API_KEY', compat: 'openai',
    desc: '商汤日日新 SenseNova，视觉与多模态见长',
    freeQuota: '赠送额度',
    recovery: '额度用完即止',
    needProxy: false, auth: '商汤大装置获取 API Key',
    payment: '按量计费',
  },
  'ollama': {
    nameCn: '本地 Ollama', nameEn: 'Ollama (Local)', kind: 'local',
    baseUrl: 'http://127.0.0.1:11434/v1', keyEnv: 'OLLAMA_API_KEY', compat: 'openai',
    desc: '本机运行的 Ollama，完全离线、零成本、无隐私外泄',
    freeQuota: '不限量（受本机算力限制）',
    recovery: '无额度概念',
    needProxy: false, auth: '无需 Key（本地服务）',
    payment: '完全免费（消耗本机算力）',
  },
  // ===== 国外主流（多数需代理）=====
  'openai': {
    nameCn: 'OpenAI', nameEn: 'OpenAI (GPT)', kind: 'intl',
    baseUrl: 'https://api.openai.com/v1', keyEnv: 'OPENAI_API_KEY', compat: 'openai',
    desc: 'GPT-4o / 4.1 / o 系列，通用能力标杆',
    freeQuota: '无免费额度（新账号偶有赠送）',
    recovery: '按量计费，充值即用',
    needProxy: true, auth: '海外手机号注册 + 绑定外卡',
    payment: '按 Token 计费，需外币卡',
  },
  'anthropic': {
    nameCn: 'Anthropic Claude', nameEn: 'Anthropic', kind: 'intl',
    baseUrl: 'https://api.anthropic.com/v1', keyEnv: 'ANTHROPIC_API_KEY', compat: 'native',
    desc: 'Claude 系列，长文写作 / 代码 / 安全对齐强',
    freeQuota: '无免费额度',
    recovery: '按量计费',
    needProxy: true, auth: '海外注册 + 外卡；原生协议非 OpenAI 兼容，需网关转接',
    payment: '按 Token 计费',
  },
  'gemini': {
    nameCn: '谷歌 Gemini', nameEn: 'Google Gemini', kind: 'intl',
    baseUrl: 'https://generativelanguage.googleapis.com/v1beta/openai/', keyEnv: 'GEMINI_API_KEY', compat: 'openai',
    desc: 'Gemini 系列，超长上下文与多模态，有免费额度',
    freeQuota: '免费层：Flash 系列每分钟 / 每日有限免费额度',
    recovery: '配额按分钟 / 日滚动恢复',
    needProxy: true, auth: 'Google AI Studio 获取 API Key',
    payment: '免费层 + 付费层',
  },
  'xai': {
    nameCn: 'xAI Grok', nameEn: 'xAI', kind: 'intl',
    baseUrl: 'https://api.x.ai/v1', keyEnv: 'XAI_API_KEY', compat: 'openai',
    desc: 'Grok 系列，实时信息与推理',
    freeQuota: '偶有免费额度（与 X 订阅相关）',
    recovery: '按订阅周期',
    needProxy: true, auth: 'xAI 控制台获取 API Key',
    payment: '按 Token 计费',
  },
  'mistral': {
    nameCn: 'Mistral AI', nameEn: 'Mistral', kind: 'intl',
    baseUrl: 'https://api.mistral.ai/v1', keyEnv: 'MISTRAL_API_KEY', compat: 'openai',
    desc: '欧洲开源系，轻量高效，代码与多语言强',
    freeQuota: 'La Plateforme 免费层（限速）',
    recovery: '限速按分钟恢复',
    needProxy: true, auth: 'Mistral 控制台获取 API Key',
    payment: '免费层 + 按量计费',
  },
  'groq': {
    nameCn: 'Groq', nameEn: 'Groq', kind: 'intl',
    baseUrl: 'https://api.groq.com/openai/v1', keyEnv: 'GROQ_API_KEY', compat: 'openai',
    desc: 'LPU 极速推理，开源模型延迟极低',
    freeQuota: '免费层：限速但额度充足',
    recovery: '限速按分钟 / 日恢复',
    needProxy: true, auth: 'Groq Console 获取 API Key',
    payment: '免费层 + 按量计费',
  },
  'together': {
    nameCn: 'Together AI', nameEn: 'Together', kind: 'intl',
    baseUrl: 'https://api.together.xyz/v1', keyEnv: 'TOGETHER_API_KEY', compat: 'openai',
    desc: '聚合大量开源模型（Llama / Qwen / DeepSeek）',
    freeQuota: '部分模型免费，新账号赠送额度',
    recovery: '额度用完即止',
    needProxy: true, auth: 'Together 控制台获取 API Key',
    payment: '按量计费',
  },
  'openrouter': {
    nameCn: 'OpenRouter', nameEn: 'OpenRouter', kind: 'intl',
    baseUrl: 'https://openrouter.ai/api/v1', keyEnv: 'OPENROUTER_API_KEY', compat: 'openai',
    desc: '网关型聚合商，含大量 :free 模型，一个 Key 通吃',
    freeQuota: ':free 后缀模型免费，每日限次',
    recovery: '每日 0 点（UTC）重置',
    needProxy: true, auth: 'OpenRouter 注册获取 API Key',
    payment: '免费模型 + 付费模型',
  },
  'deepinfra': {
    nameCn: 'DeepInfra', nameEn: 'DeepInfra', kind: 'intl',
    baseUrl: 'https://api.deepinfra.com/v1/openai', keyEnv: 'DEEPINFRA_API_KEY', compat: 'openai',
    desc: '开源模型托管，价格低，OpenAI 兼容',
    freeQuota: '新账号赠送额度',
    recovery: '额度用完即止',
    needProxy: true, auth: 'DeepInfra 控制台获取 API Key',
    payment: '按量计费',
  },
  'cerebras': {
    nameCn: 'Cerebras', nameEn: 'Cerebras', kind: 'intl',
    baseUrl: 'https://api.cerebras.ai/v1', keyEnv: 'CEREBRAS_API_KEY', compat: 'openai',
    desc: '晶圆级芯片推理，速度极快，有免费层',
    freeQuota: '免费层：每日请求 / token 额度',
    recovery: '每日重置',
    needProxy: true, auth: 'Cerebras Cloud 获取 API Key',
    payment: '免费层 + 按量计费',
  },
  'nvidia': {
    nameCn: '英伟达 NIM', nameEn: 'NVIDIA NIM', kind: 'intl',
    baseUrl: 'https://integrate.api.nvidia.com/v1', keyEnv: 'NVIDIA_API_KEY', compat: 'openai',
    desc: 'NVIDIA 托管的开源模型，注册赠免费 credits',
    freeQuota: '注册赠送 1000 credits',
    recovery: 'credits 用完即止',
    needProxy: true, auth: 'NVIDIA NGC 获取 API Key',
    payment: 'credits + 按量计费',
  },
  'github-models': {
    nameCn: 'GitHub 模型', nameEn: 'GitHub Models', kind: 'intl',
    baseUrl: 'https://models.inference.ai.azure.com', keyEnv: 'GITHUB_TOKEN', compat: 'openai',
    desc: 'GitHub 提供的模型试用端点，含 GPT / Llama 等',
    freeQuota: '免费层：限速 + 每日额度',
    recovery: '每日重置',
    needProxy: true, auth: 'GitHub PAT（需 models:read 权限）',
    payment: '免费层 + 按量计费',
  },
  'perplexity': {
    nameCn: 'Perplexity', nameEn: 'Perplexity', kind: 'intl',
    baseUrl: 'https://api.perplexity.ai', keyEnv: 'PERPLEXITY_API_KEY', compat: 'openai',
    desc: 'Sonar 系列，联网检索问答',
    freeQuota: '无免费额度（Pro 订阅赠送额度）',
    recovery: '按订阅周期',
    needProxy: true, auth: 'Perplexity 控制台获取 API Key',
    payment: '按 Token 计费（含检索费）',
  },
  // ===== 2026-10 补充（用户选定）=====
  'sambanova': {
    nameCn: 'SambaNova 云', nameEn: 'SambaNova Cloud', kind: 'intl',
    baseUrl: 'https://api.sambanova.ai/v1', keyEnv: 'SAMBANOVA_API_KEY', compat: 'openai',
    desc: 'SambaNova 云推理，开源模型高速推理（Llama / DeepSeek / gpt-oss）',
    freeQuota: '免费层：限速但长期有效，无需绑卡',
    recovery: '限速按分钟恢复',
    needProxy: true, auth: 'cloud.sambanova.ai 注册获取 API Key（免卡免手机号）',
    payment: '免费层 + 按量计费',
    sampleModels: ['DeepSeek-V3.1', 'Meta-Llama-3.3-70B-Instruct', 'gpt-oss-120b'],
  },
  'ollama-cloud': {
    nameCn: 'Ollama 云', nameEn: 'Ollama Cloud', kind: 'intl',
    baseUrl: 'https://ollama.com/v1', keyEnv: 'OLLAMA_API_KEY', compat: 'openai',
    desc: 'Ollama 官方云托管，开源模型即开即用（与本地 Ollama 是两回事）',
    freeQuota: '$0 免费计划：每月赠送少量额度，限部分起步模型',
    recovery: '按周期重置',
    needProxy: true, auth: 'ollama.com 注册获取 API Key',
    payment: '免费额度 + 购买额度',
  },
  'dashscope-intl': {
    nameCn: '阿里百炼（国际版）', nameEn: 'Alibaba Model Studio (Intl)', kind: 'intl',
    baseUrl: 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    keyEnv: 'DASHSCOPE_INTL_API_KEY', compat: 'openai',
    desc: '阿里云百炼国际站（新加坡节点），账号与 Key 跟国内版独立',
    freeQuota: '新用户每模型约 100 万 token 免费额度（90 天）',
    recovery: '额度用完即止',
    needProxy: false, auth: '阿里云国际站开通百炼 → 获取 DASHSCOPE_INTL_API_KEY',
    payment: '免费额度 + 按量计费',
  },
  'nous': {
    nameCn: 'Nous Portal', nameEn: 'Nous Research', kind: 'intl',
    baseUrl: 'https://inference-api.nousresearch.com/v1', keyEnv: 'NOUS_API_KEY', compat: 'openai',
    desc: 'Nous Research 官方网关（Hermes 的出品方），聚合 420+ 模型，其中 11 个永久免费',
    freeQuota: '11 个永久免费模型：ling-3.1-flash、step-5-preview(:free)、longcat-2.5-preview、solar-mini4、laguna-s/xs、ling-3.0-fin/sante 等',
    recovery: '免费模型不限额度（按限速）',
    needProxy: true, auth: 'portal.nousresearch.com 注册获取 NOUS_API_KEY',
    payment: '免费模型 + 按量计费',
    sampleModels: ['stepfun/step-5-preview:free', 'inclusionai/ling-3.1-flash',
                   'meituan/longcat-2.5-preview:free', 'upstage/solar-mini4:free'],
  },
};

// 提供商归属：cn=国内直连 / intl=国外（多需代理）/ gw=网关 / local=本地
const PROVIDER_KIND = {
  'modelscope': 'cn', 'zhipu': 'cn', 'siliconflow': 'cn', 'qianfan': 'cn',
  'hunyuan': 'cn', 'spark': 'cn', 'dashscope': 'cn', 'ark': 'cn',
  'amd-radeon': 'intl', 'kilogw': 'gw',
};
function providerKind(p) {
  const m = PROVIDER_META[p];
  return (m && m.kind) || PROVIDER_KIND[p] || 'cn';
}
function providerDisplayName(provider) {
  const m = PROVIDER_META[provider];
  if (!m) return provider;
  const cn = m.nameCn || provider, en = m.nameEn || '';
  if (!en || en === cn) return cn;
  // 去掉英文名里冗余的括号说明（如 "Ollama (Local)" -> "Ollama"）
  const enClean = (en.replace(/\s*\([^)]*\)\s*$/, '').trim()) || en;
  if (enClean === cn) return cn;
  // 中文名已包含英文名时（如 "谷歌 Gemini" / "Gemini"），只显示中文名
  if (cn.toLowerCase().includes(enClean.toLowerCase())) return cn;
  return `${cn} (${enClean})`;
}

// ============ 模型能力画像（悬停说明）============
const CAP_RULES = [
  { key: 'vision',  label: '👁 视觉理解', re: /(^|[-_/])(vl|vision|4v|omni|multimodal|image|visual)([-_/.]|$)|(internvl|llava|gemma-3|minicpm-v)/i,
    tasks: '看图问答、OCR 文字识别、图表/文档理解、截图分析、多模态推理' },
  { key: 'audio',   label: '🔊 语音音频', re: /(audio|speech|tts|asr|whisper|voice|realtime)/i,
    tasks: '语音转写、音频理解、语音合成、实时对话' },
  { key: 'embed',   label: '🧬 向量嵌入', re: /(embed|bge|gte|m3e|text-embedding)/i,
    tasks: '文本向量化、语义检索、RAG 召回、聚类' },
  { key: 'rerank',  label: '📐 重排序', re: /(rerank|reranker)/i,
    tasks: '检索结果重排序、提升 RAG 精度' },
  { key: 'code',    label: '💻 代码', re: /(coder|code|devstral|starcoder|codegen|sql)/i,
    tasks: '代码生成与补全、调试改错、跨语言转换、单元测试编写' },
  { key: 'math',    label: '➗ 数学', re: /(math|qwen.*math|deepseek.*math)/i,
    tasks: '数学解题、公式推导、数值计算' },
  { key: 'reason',  label: '🧠 深度推理', re: /(^|[-_/])(r1|reason|reasoner|think|qwq|o1|o3|o4|zeta)([-_/.]|$)/i,
    tasks: '复杂逻辑推理、多步规划、数学证明、难题攻坚（较慢但更准）' },
  { key: 'long',    label: '📚 长上下文', re: /(128k|200k|256k|512k|1m|long|32k)/i,
    tasks: '长文档总结、整本书/代码库分析、长对话记忆' },
  { key: 'fast',    label: '⚡ 轻量快速', re: /(flash|lite|turbo|mini|small|air|speed|nano|tiny|-1\.5b|-3b|-4b|-7b|-8b|-9b)/i,
    tasks: '高并发问答、分类抽取、摘要改写（快且省）' },
  { key: 'flagship',label: '🏆 旗舰高能', re: /(max|pro|plus|ultra|large|-70b|-72b|-235b|-480b|-671b|opus|sonnet|-gpt-4)/i,
    tasks: '通用复杂任务、写作、综合问答、Agent 主控' },
];

function modelCapabilities(m) {
  const hay = `${m.model} ${(m.tags || []).join(' ')}`;
  const caps = [];
  const labels = new Set((m.tags || []).map(t => t.toLowerCase()));
  CAP_RULES.forEach(r => {
    if (r.re.test(hay) || [...labels].some(l => l.includes(r.key))) caps.push(r);
  });
  return caps;
}

function buildModelTooltip(m) {
  const caps = modelCapabilities(m);
  const meta = PROVIDER_META[m.provider] || {};
  const lines = [];
  lines.push(`📦 ${m.model}`);
  lines.push(`🏢 ${providerDisplayName(m.provider)}`);
  lines.push(`💰 ${m.type === 'free' ? '免费模型' : '付费模型'} · Tier ${getTierStars(m.tier)} · 优先级 ${m.priority || 0}`);
  if (caps.length) {
    lines.push('');
    lines.push('【主要能力】' + caps.map(c => c.label).join(' · '));
    lines.push('【擅长任务】');
    caps.forEach(c => lines.push(`  · ${c.label}：${c.tasks}`));
  } else {
    lines.push('');
    lines.push('【主要能力】💬 通用对话');
    lines.push('【擅长任务】日常问答、文本生成、摘要改写等通用任务');
  }
  if (m.disabled) lines.push('');
  if (m.disabled) lines.push('⚠️ 已下线（disabled）');
  if (meta.needProxy) lines.push('🌐 该提供商需代理访问');
  return lines.join('\n');
}

// ============ 免费额度 / 恢复速度评级（供排序）============
// q = 免费额度充裕度(0-5)，r = 额度恢复速度(0-5，越大越快)
const PROVIDER_QUOTA = {
  'siliconflow':   { q: 5, r: 5 },  // 9B 以下永久免费、不限量
  'zhipu':         { q: 5, r: 5 },  // glm-4-flash 永久免费，每日大额度
  'qianfan':       { q: 5, r: 5 },  // ernie-speed/lite 永久免费
  'kilogw':        { q: 5, r: 5 },  // 网关聚合 :free，日额度不限
  'ollama':        { q: 5, r: 5 },  // 本地不限量
  'hunyuan':       { q: 4, r: 5 },  // hunyuan-lite 永久免费
  'spark':         { q: 4, r: 5 },  // spark-lite 永久免费
  'modelscope':    { q: 4, r: 4 },  // 大量免费模型，日额度视模型
  'amd-radeon':    { q: 3, r: 5 },  // 额度高、速率宽松、实时恢复
  'ark':           { q: 3, r: 3 },  // 协作计划每日 200 万 token
  'dashscope':     { q: 3, r: 1 },  // 每模型 100 万 token，用完不自动恢复
  'gemini':        { q: 3, r: 4 },  // Flash 免费层，按分钟/日滚动
  'groq':          { q: 3, r: 5 },  // 免费层限速充足，按分钟恢复
  'cerebras':      { q: 3, r: 5 },  // 免费层每日重置
  'openrouter':    { q: 3, r: 3 },  // :free 每日限次
  'stepfun':       { q: 2, r: 4 },  // 部分限时免费
  'mistral':       { q: 2, r: 4 },  // 免费层限速
  'together':      { q: 2, r: 3 },  // 部分免费 + 赠额
  'nvidia':        { q: 2, r: 3 },  // 注册赠 1000 credits
  'github-models': { q: 2, r: 3 },  // 免费层每日额度
  'deepseek':      { q: 1, r: 3 },  // 无免费，价格极低
  'deepinfra':     { q: 1, r: 3 },  // 赠额
  'moonshot':      { q: 1, r: 1 },  // 仅新用户赠额
  'minimax':       { q: 1, r: 1 },
  'baichuan':      { q: 1, r: 1 },
  'sensetime':     { q: 1, r: 1 },
  'openai':        { q: 1, r: 3 },
  'xai':           { q: 0, r: 3 },
  'perplexity':    { q: 0, r: 2 },
  'anthropic':     { q: 0, r: 2 },
  'sambanova':     { q: 3, r: 5 },   // 长期限速免费层
  'ollama-cloud':  { q: 3, r: 3 },   // 每月赠送额度
  'dashscope-intl':{ q: 3, r: 1 },   // 每模型 100 万 token，用完即止
  'nous':          { q: 4, r: 4 },   // 11 个永久免费模型
};
function quotaRank(p) { return PROVIDER_QUOTA[p] || { q: 0, r: 0 }; }

// ============ 工具函数 ============
function $(sel, ctx = document) { return ctx.querySelector(sel); }
function $$(sel, ctx = document) { 
  const nodes = ctx.querySelectorAll(sel);
  // Use Array.from for better compatibility (NodeList may not be iterable in some environments)
  return Array.from(nodes); 
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k, v]) => {
    if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === 'class') e.className = v;
    else if (k === 'style') {
      // 支持字符串样式（如 'display: none'）与对象样式
      if (typeof v === 'string') {
        e.setAttribute('style', v);
      } else if (v && typeof v === 'object') {
        Object.entries(v).forEach(([sk, sv]) => {
          if (isNaN(sk)) e.style[sk] = sv;
        });
      }
    } else e.setAttribute(k, v);
  });
  const flat = (arr) => arr.flatMap(c => Array.isArray(c) ? flat(c) : c);
  flat(children).forEach(c => e.append(c instanceof Node ? c : document.createTextNode(String(c))));
  return e;
}

function showToast(msg, type = 'success', duration = 5000) {
  console.log('[free-router] Toast:', msg, type);
  const layer = $('#toastLayer');
  if (!layer) return;
  const toast = el('div', { class: `toast ${type}` }, [
    el('span', { class: 'toast-icon' }, type === 'success' ? '✅' : type === 'danger' ? '❌' : type === 'warning' ? '⚠️' : 'ℹ️'),
    el('span', { class: 'toast-message' }, msg),
    el('button', { class: 'toast-close', onclick: () => toast.remove() }, '✕')
  ]);
  layer.appendChild(toast);
  setTimeout(() => toast.remove(), duration);
}

function showModal(content) {
  console.log('[free-router] Show modal');
  const layer = $('#modalLayer');
  const wrapper = $('#modalWrapper');
  if (!layer || !wrapper) { console.warn('Modal elements not found'); return; }
  wrapper.innerHTML = '';
  wrapper.appendChild(content);
  layer.hidden = false;
  document.body.style.overflow = 'hidden';
}

function hideModal() {
  const layer = $('#modalLayer');
  const wrapper = $('#modalWrapper');
  if (!layer || !wrapper) return;
  layer.hidden = true;
  wrapper.innerHTML = '';
  document.body.style.overflow = '';
}

// ============ 自定义悬停提示（即时、可换行、富文本）============
function ensureTipEl() {
  let tip = document.getElementById('frTip');
  if (!tip) {
    tip = el('div', { id: 'frTip', class: 'fr-tip' });
    document.body.appendChild(tip);
  }
  return tip;
}
function positionTip(target) {
  const tip = document.getElementById('frTip');
  if (!tip) return;
  const r = target.getBoundingClientRect();
  tip.style.visibility = 'hidden';
  tip.classList.add('show');
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - tw - 12));
  let top = r.bottom + 8;
  if (top + th > window.innerHeight - 8) top = Math.max(8, r.top - th - 8);
  tip.style.left = left + 'px';
  tip.style.top = top + 'px';
  tip.style.visibility = '';
}
function showTip(target) {
  const text = target.getAttribute && target.getAttribute('data-tip');
  if (!text) return;
  const tip = ensureTipEl();
  tip.textContent = text;
  positionTip(target);
}
function hideTip() {
  const tip = document.getElementById('frTip');
  if (tip) tip.classList.remove('show');
}
function initTips() {
  document.addEventListener('mouseover', e => {
    const t = e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
    if (t) showTip(t);
  });
  document.addEventListener('mouseout', e => {
    const t = e.target && e.target.closest ? e.target.closest('[data-tip]') : null;
    if (t && !(e.relatedTarget && t.contains(e.relatedTarget))) hideTip();
  });
  document.addEventListener('scroll', hideTip, true);
  document.addEventListener('click', hideTip, true);
}

function debounce(fn, ms = 300) {
  let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function formatDuration(sec) {
  if (sec >= 1e9) return '永久';
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.round(sec/60)}m`;
  if (sec < 86400) return `${Math.round(sec/3600)}h`;
  return `${Math.round(sec/86400)}d`;
}

function getTierStars(tier) {
  const map = { high: '★★★', medium: '★★☆', low: '★☆☆' };
  return map[tier] || '☆☆☆';
}

// 折叠面板切换 - 表格视图
function toggleProviderSection(id) {
  const content = $('#' + id);
  const toggle = document.querySelector(`[onclick="toggleProviderSection('${id}')"] .provider-section-toggle`);
  if (!content) return;
  const isOpen = content.style.display !== 'none';
  content.style.display = isOpen ? 'none' : 'block';
  if (toggle) toggle.textContent = isOpen ? '▼' : '▲';
  
  // 保存状态
  const provider = id.replace('-models', '').replace('-quota', '');
  state.providerSections[provider] = !isOpen;
}

// 滚动辅助
function scrollToTop() { window.scrollTo({ top: 0, behavior: 'smooth' }); }
function scrollToBottom() { window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); }

// 初始化滚动按钮
function initScrollButtons() {
  if ($('#scrollTopBtn')) return; // 已存在
  const btnTop = el('button', { class: 'scroll-btn scroll-top', id: 'scrollTopBtn', onclick: scrollToTop, title: '回到顶部' }, '⬆');
  const btnBottom = el('button', { class: 'scroll-btn scroll-bottom', id: 'scrollBottomBtn', onclick: scrollToBottom, title: '跳到底部' }, '⬇');
  document.body.appendChild(btnTop);
  document.body.appendChild(btnBottom);
  
  // 滚动时显示/隐藏 - 使用 CSS class
  window.addEventListener('scroll', () => {
    const st = window.scrollY;
    btnTop.classList.toggle('visible', st > 200);
    btnBottom.classList.toggle('visible', st < document.body.scrollHeight - window.innerHeight - 200);
  });
}

function getTagClass(tag) {
  const t = tag.toLowerCase();
  if (["vision", "vl", "visual", "image"].includes(t)) return "vision";
  if (["reasoning", "reason", "think", "cot"].includes(t)) return "reasoning";
  if (["code", "coding", "program"].includes(t)) return "code";
  if (["long", "context", "32k", "128k", "200k"].includes(t)) return "long";
  return "";
}
function getTagDescription(tag) {
  const desc = {
    'vision': '支持图像输入/视觉理解',
    'reasoning': '支持推理/思维链',
    'code': '擅长代码生成',
    'long': '长上下文支持',
    'chat': '通用对话',
    'embedding': '向量嵌入',
    'audio': '音频处理',
  };
  return desc[tag.toLowerCase()] || '';
}

function buildProviderTooltip(provider) {
  const meta = PROVIDER_META[provider];
  if (!meta) return '';
  const info = state.keysInfo[provider];
  const apiKeyStatus = info
    ? (info.count > 0 ? `已配置 ${info.count} 个` : '未配置')
    : '未知';
  const kindLabel = { cn: '🇨🇳 国内直连', intl: '🌍 国外（多需代理）', gw: '🔀 网关聚合', local: '💻 本地部署' }[providerKind(provider)] || '';
  return `🏢 ${providerDisplayName(provider)}
${kindLabel}
──────────────
${meta.desc}
免费额度: ${meta.freeQuota}
恢复方式: ${meta.recovery}
认证方式: ${meta.auth}
计费模式: ${meta.payment}${meta.sampleModels ? '\n代表模型: ' + meta.sampleModels.slice(0, 4).join('、') : ''}
${meta.needProxy ? '⚠️ 需要代理访问' : '✅ 可直连'}
API Key: ${apiKeyStatus}
（点击标题栏 🔑 可管理多个账号的 Key）`;
}

async function loadKeys() {
  try {
    const r = await api('/keys');
    state.keysInfo = r.providers || {};
  } catch (e) {
    console.warn('[free-router] 加载 API Key 信息失败:', e);
  }
}

// 打开某提供商的 API Key 管理弹窗（支持多账号 / 多 Key）
async function openApiKeyModal(provider) {
  const meta = PROVIDER_META[provider] || {};
  const titleName = meta.nameCn ? `${meta.nameCn} (${meta.nameEn})` : provider;
  const listWrap = el('div', { class: 'apikey-list' });
  const metaBox = el('div', { class: 'apikey-meta' });
  const input = el('input', { type: 'text', id: 'apiKeyInput', placeholder: '粘贴一个新的 API Key（回车或点「添加」）' });

  async function refreshLocal() {
    try {
      const r = await api('/keys');
      state.keysInfo = r.providers || {};
    } catch (e) { /* 保留旧值 */ }
    renderList();
  }

  function renderList() {
    const info = state.keysInfo[provider] || { masked: [], count: 0, key_env: [] };
    metaBox.textContent = `已配置 ${info.count || 0} 个 Key · 环境变量：${(info.key_env || []).join(', ') || '-'}（保存后自动热重载，无需重启）`;
    listWrap.innerHTML = '';
    const masked = info.masked || [];
    if (!masked.length) {
      listWrap.appendChild(el('div', { class: 'apikey-empty' }, '暂无 Key —— 在下方粘贴添加（同一提供商有多个账号就添加多个）'));
    }
    masked.forEach((m, i) => {
      listWrap.appendChild(el('div', { class: 'apikey-row' }, [
        el('span', { class: 'apikey-idx' }, `#${i + 1}`),
        el('code', { class: 'apikey-val' }, m),
        el('button', { class: 'btn ghost danger', title: '删除此 Key', onclick: async () => {
          try {
            await api('/keys', { method: 'POST', body: { provider, action: 'remove', index: i } });
            await refreshLocal(); renderModelsView();
            showToast('已删除该 Key', 'success');
          } catch (e) { showToast(e.message, 'danger'); }
        } }, '🗑️'),
      ]));
    });
  }

  async function addKey() {
    const v = input.value.trim();
    if (!v) { showToast('请输入 API Key', 'warning'); return; }
    try {
      await api('/keys', { method: 'POST', body: { provider, action: 'add', key: v } });
      input.value = '';
      await refreshLocal(); renderModelsView();
      showToast('已添加 Key', 'success');
    } catch (e) { showToast(e.message, 'danger'); }
  }

  async function clearKeys() {
    if (!confirm(`确定清空 ${provider} 的全部 API Key？`)) return;
    try {
      await api('/keys', { method: 'POST', body: { provider, action: 'clear' } });
      await refreshLocal(); renderModelsView();
      showToast('已清空', 'success');
    } catch (e) { showToast(e.message, 'danger'); }
  }

  input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addKey(); } });

  const modal = el('div', { class: 'modal' }, [
    el('div', { class: 'modal-header' }, [
      el('h3', { class: 'modal-title' }, `API Key 管理 · ${titleName}`),
      el('button', { class: 'modal-close', onclick: hideModal }, '✕'),
    ]),
    el('div', { class: 'modal-body' }, [
      metaBox,
      listWrap,
      el('div', { class: 'apikey-add' }, [input, el('button', { class: 'btn primary', onclick: addKey }, '＋ 添加')]),
    ]),
    el('div', { class: 'modal-footer' }, [
      el('button', { class: 'btn danger', onclick: clearKeys }, '清空全部'),
      el('button', { class: 'btn outline', onclick: hideModal }, '关闭'),
    ]),
  ]);
  showModal(modal);
  renderList();
}



// ============ API 请求封装 ============
async function api(path, options = {}) {
  const url = `${API_BASE}${path}`;
  const opts = {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  };
  if (options.body && typeof options.body === 'object') opts.body = JSON.stringify(options.body);
  try {
    const res = await fetch(url, opts);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (e) {
    console.error(`API ${path} 失败:`, e);
    throw e;
  }
}

async function apiCached(path, ttl = 5000) {
  const now = Date.now();
  const cached = state._cache.get(path);
  if (cached && now - cached.time < ttl) return cached.data;
  const data = await api(path);
  state._cache.set(path, { data, time: now });
  return data;
}

function invalidateCache(path) { state._cache.delete(path); }

// ============ 数据加载 ============
async function loadDashboard() {
  console.log('[free-router] Loading dashboard...');
  state.dashboard = await apiCached('/status');
  console.log('[free-router] Dashboard loaded:', state.dashboard.models?.total, 'models');
  console.log('[free-router] by_provider:', state.dashboard.models?.by_provider);
  if (state.dashboard.models?.by_provider) {
    state.providers = Object.entries(state.dashboard.models.by_provider)
      .map(([provider, count]) => ({ provider, modelCount: count }));
    console.log('[free-router] state.providers set to:', state.providers);
  }
  state.quota = state.dashboard.quota || {};
  state.probe = (state.dashboard.probe && Object.keys(state.dashboard.probe).length)
    ? state.dashboard.probe : (state.probe || {});
  state.cooldowns = state.dashboard.cooldowns || {};
  state.cache = state.dashboard.cache || {};
  state.policy = state.dashboard.policy || {};
  state.settings = state.dashboard.config || {};
  $('#lastUpdate').textContent = '刚刚更新';
}

async function loadModels() {
  console.log('[free-router] Loading models...');
  const res = await apiCached('/models');
  state.models = res.models || [];
  console.log('[free-router] Models loaded:', state.models.length);
  // 更新提供商筛选选项
  updateProviderFilter();
}

async function loadPolicy() {
  state.policy = await apiCached('/policy');
}

async function loadCache() {
  state.cache = await apiCached('/cache');
}

function updateProviderFilter() {
  const select = $('#modelFilterProvider');
  if (!select) return;
  const providers = [...new Set(state.models.map(m => m.provider))].sort();
  select.innerHTML = '<option value="">全部提供商</option>' +
    providers.map(p => `<option value="${p}">${p}</option>`).join('');
}

// ============ 渲染入口 ============
function renderAll() {
  console.log('[free-router] Rendering all sections');
  renderOverview();
  renderModels();
  renderProviders();
  renderQuota();
  renderCooldown();
  renderCache();
  renderPolicy();
  renderSettings();
  initParticles();
  initViewToggle();
}

function initViewToggle() {
  $$('.view-toggle [role="tab"]').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.view-toggle [role="tab"]').forEach(b => { b.classList.remove('active'); b.setAttribute('aria-selected', 'false'); });
      btn.classList.add('active');
      btn.setAttribute('aria-selected', 'true');
      state.currentView = btn.dataset.view;
      renderModelsView();
    });
  });
}

// ============ Section 1: 系统概览 ============
function renderOverview() {
  const d = state.dashboard;
  const m = d.models || {};
  const q = d.quota || {};
  const c = d.cooldowns || {};

  // 指标卡片
  const grid = $('#metricGrid');
  if (grid) {
    const kpis = [
      { label: '模型总数', value: m.total || 0, icon: '🤖', class: 'cyan' },
      { label: '活跃模型', value: m.active || 0, icon: '✅', class: 'green' },
      { label: '免费模型', value: m.by_type?.free || 0, icon: '🆓', class: 'cyan' },
      { label: '付费模型', value: m.by_type?.paid || 0, icon: '💰', class: 'amber' },
      { label: '提供商数', value: Object.keys(m.by_provider || {}).length, icon: '🏢', class: 'violet' },
      { label: '冷却中', value: Object.keys(c).length || 0, icon: '❄️', class: 'red' },
    ];
    grid.innerHTML = '';
    kpis.forEach(k => grid.appendChild(el('div', { class: 'metric-card' }, [
      el('div', { class: `metric-icon ${k.class}` }, k.icon),
      el('div', { class: 'metric-value' }, k.value),
      el('div', { class: 'metric-label' }, k.label),
    ])));
  }

  // 图表
  if (m.by_provider) initProviderChart(m.by_provider);
  if (q.usage) initQuotaChart(q.usage);
}

function initProviderChart(data) {
  const dom = $('#cProvider'); if (!dom) return;
  if (!window._echartsLoaded) { console.warn('ECharts 未加载，跳过图表渲染'); return; }
  if (dom._chart) { dom._chart.dispose(); }
  const chart = echarts.init(dom); dom._chart = chart;
  const providers = Object.keys(data);
  const colors = { 'amd-radeon': '#ff6b6b', 'modelscope': '#4ecdc4', 'kilogw': '#45b7d1', 'zhipu': '#96ceb4', 'siliconflow': '#ffa502', 'qianfan': '#feca57', 'hunyuan': '#ff9ff3', 'spark': '#54a0ff', 'dashscope': '#5f27cd', 'ark': '#00c9a7' };
  const option = {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)', backgroundColor: 'rgba(10,15,26,.9)', borderColor: '#22d3ee', borderWidth: 1 },
    legend: { orient: 'vertical', left: 'left', top: 'middle', data: providers, textStyle: { color: '#94a3b8', fontSize: 12 } },
    series: [
      {
        name: '模型数',
        type: 'pie',
        radius: ['40%', '70%'],
        avoidLabelOverlap: false,
        itemStyle: { borderRadius: 8, borderColor: '#030712', borderWidth: 2 },
        label: { show: false },
        emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } },
        labelLine: { show: false },
        data: providers.map(p => ({ value: data[p], name: p, itemStyle: { color: colors[p] || '#64748b' } }))
      }
    ]
  };
  chart.setOption(option);
  window.addEventListener('resize', () => chart.resize());
}

function initQuotaChart(data) {
  const dom = $('#cQuota'); if (!dom) return;
  if (!window._echartsLoaded) { console.warn('ECharts 未加载，跳过图表渲染'); return; }
  if (dom._chart) { dom._chart.dispose(); }
  const chart = echarts.init(dom); dom._chart = chart;
  const entries = Object.entries(data).map(([k, v]) => ({ model: k, rpd: v.rpd || 0, rpm: v.rpm || 0, tpd: v.tpd || 0 })).sort((a, b) => b.rpd - a.rpd).slice(0, 10);
  const option = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, backgroundColor: 'rgba(10,15,26,.9)', borderColor: '#22d3ee', borderWidth: 1 },
    grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
    xAxis: { type: 'value', axisLabel: { color: '#94a3b8' }, axisLine: { show: false }, splitLine: { lineStyle: { color: '#1e293b' } } },
    yAxis: { type: 'category', data: entries.map(e => e.model.split('/').pop()), axisLabel: { color: '#94a3b8', interval: 0 }, axisLine: { show: false }, splitLine: { show: false } },
    series: [{ name: '日请求数', type: 'bar', barWidth: '60%', itemStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [{ offset: 0, color: '#06b6d4' }, { offset: 1, color: '#a855f7' }] } }, data: entries.map(e => e.rpd) }]
  };
  chart.setOption(option);
  window.addEventListener('resize', () => chart.resize());
}

// ============ Section 2: 模型矩阵 ============
function renderModels() {
  // 绑定搜索筛选
  $('#modelSearch')?.addEventListener('input', debounce(() => { state.modelFilters.search = $('#modelSearch').value; renderModelsView(); }, 300));
  $('#modelFilterType')?.addEventListener('change', () => { state.modelFilters.type = $('#modelFilterType').value; renderModelsView(); });
  $('#modelFilterProvider')?.addEventListener('change', () => { state.modelFilters.provider = $('#modelFilterProvider').value; renderModelsView(); });
  $('#modelFilterCapability')?.addEventListener('change', () => { state.modelFilters.capability = $('#modelFilterCapability').value; renderModelsView(); });
  $('#modelSortBy')?.addEventListener('change', () => { state.modelSortBy = $('#modelSortBy').value; renderModelsView(); });
  $('#btnAddModel')?.addEventListener('click', showAddModelModal);
  
  // 填充提供商下拉
    const provSelect = $('#modelFilterProvider');
    if (provSelect && state.providers.length) {
      state.providers.forEach(p => {
        if (![...provSelect.options].some(o => o.value === p.provider)) {
          provSelect.appendChild(el('option', { value: p.provider }, p.provider));
        }
      });
    }
  
  // 更新元信息
  updateModelsMeta();
  
  renderModelsView();
}

function getFilteredModels() {
  const { search, type, provider, capability } = state.modelFilters;
  const showDisabled = false;
  return state.models.filter(m => {
    if (!showDisabled && m.disabled) return false;
    if (type && m.type !== type) return false;
    if (provider && m.provider !== provider) return false;
    if (capability && !(m.tags || []).some(t => t.toLowerCase().includes(capability.toLowerCase()))) return false;
    if (search) {
      const hay = `${m.provider} ${m.model} ${(m.tags || []).join(' ')}`.toLowerCase();
      if (!hay.includes(search.toLowerCase())) return false;
    }
    return true;
  });
}

function sortModels(models) {
  const tierOrder = { high: 3, medium: 2, low: 1 };
  const isFav = m => state.favorites.has(`${m.provider}/${m.model}`);
  
  return [...models].sort((a, b) => {
    const fa = isFav(a), fb = isFav(b);
    if (fa !== fb) return fb - fa; // 收藏优先
    
    switch (state.modelSortBy) {
      case 'score':
        return (getProviderScore(b.provider) || 0) - (getProviderScore(a.provider) || 0);
      case 'tier':
        return (tierOrder[b.tier] || 0) - (tierOrder[a.tier] || 0);
      case 'quota': {
        const qa = quotaRank(a.provider).q, qb = quotaRank(b.provider).q;
        return qb - qa || (tierOrder[b.tier] || 0) - (tierOrder[a.tier] || 0);
      }
      case 'recovery': {
        const ra = quotaRank(a.provider).r, rb = quotaRank(b.provider).r;
        return rb - ra || (tierOrder[b.tier] || 0) - (tierOrder[a.tier] || 0);
      }
      case 'provider':
        return a.provider.localeCompare(b.provider) || a.model.localeCompare(b.model);
      case 'name':
        return a.model.localeCompare(b.model);
      default: // favorite
        return 0;
    }
  });
}

function getProviderScore(provider) {
  // 基于：免费模型数、日额度、恢复速度、健康度
  const models = state.models.filter(m => m.provider === provider && m.type === 'free' && !m.disabled);
  if (!models.length) return 0;
  
  const freeCount = models.length;
  const hasVision = models.some(m => (m.tags || []).includes('vision'));
  const hasReasoning = models.some(m => (m.tags || []).includes('reasoning'));
  const hasCode = models.some(m => (m.tags || []).includes('code'));
  const highTier = models.filter(m => m.tier === 'high').length;
  
  // 额度数据
  let totalRpd = 0, healthy = 0;
  models.forEach(m => {
    const key = `${m.provider}/${m.model}`;
    const usage = state.quota.usage?.[key];
    if (usage) {
      totalRpd += usage.rpd || 0;
      if (!state.cooldowns[key] && !state.quota.predicted_exhausted?.[key]) healthy++;
    }
  });
  
  // 评分公式：免费模型数*10 + 高Tier*5 + 能力加成 + 健康度*2 + 日额度/100
  return freeCount * 10 + highTier * 5 + (hasVision ? 8 : 0) + (hasReasoning ? 5 : 0) + (hasCode ? 5 : 0) + healthy * 2 + Math.min(totalRpd / 100, 50);
}

function getModelStatus(m) {
  if (m.disabled) return { class: 'danger', text: '已禁用', badge: 'danger' };
  const cdKey = `${m.provider}/${m.model}`;
  if (state.cooldowns[cdKey]) return { class: 'warning', text: '冷却中', badge: 'warning' };
  if (state.quota.predicted_exhausted?.[cdKey]) return { class: 'warning', text: '预冷却', badge: 'warning' };
  return { class: 'success', text: '正常', badge: 'success' };
}

function toggleFavorite(m) {
  const key = `${m.provider}/${m.model}`;
  if (state.favorites.has(key)) state.favorites.delete(key);
  else state.favorites.add(key);
  localStorage.setItem('fr_favorites', JSON.stringify([...state.favorites]));
  saveFavorites();     // 持久化到后端：换浏览器/多 Agent 都保留，并影响路由优先级
  renderModelsView();
  showToast(state.favorites.has(key) ? `⭐ 已收藏 ${key}（路由优先）` : `已取消收藏 ${key}`, 'success', 2500);
}

async function saveFavorites() {
  try {
    await api('/favorites', { method: 'POST', body: { favorites: [...state.favorites] } });
  } catch (e) {
    console.warn('[free-router] 保存收藏失败:', e);
    showToast('收藏保存失败：' + e.message, 'danger');
  }
}

async function loadFavorites() {
  let local = [];
  try { local = JSON.parse(localStorage.getItem('fr_favorites') || '[]'); } catch (e) {}
  try {
    const r = await api('/favorites');
    let favs = Array.isArray(r.favorites) ? r.favorites : [];
    if (!favs.length && local.length) {      // 首次迁移：把旧的本地收藏推到后端
      favs = local;
      await api('/favorites', { method: 'POST', body: { favorites: favs } });
    }
    state.favorites = new Set(favs);
    localStorage.setItem('fr_favorites', JSON.stringify(favs));
  } catch (e) {
    console.warn('[free-router] 加载收藏失败，回退本地:', e);
    state.favorites = new Set(local);
  }
}

function updateModelsMeta() {
  const meta = $('#modelsMeta');
  if (!meta) return;
  const total = state.models.length;
  const free = state.models.filter(m => m.type === 'free').length;
  const paid = total - free;
  const favCount = state.favorites.size;
  meta.innerHTML = `共 ${total} 个模型（免费 ${free} · 付费 ${paid}） · ⭐ 收藏 ${favCount} 个`;
}

function renderModelsView() {
  const container = $('#modelsContainer');
  if (!container) return;
  let models = getFilteredModels();
  models = sortModels(models);
  
  if (state.currentView === 'table') renderModelsTable(models, container);
  else if (state.currentView === 'cards') renderModelsCards(models, container);
  else if (state.currentView === 'tree') renderModelsTree(models, container);
  else if (state.currentView === 'scorecard') renderProviderScorecard(container);
}

function renderModelsCards(models, container) {
  container.innerHTML = '';
  if (models.length === 0) { container.innerHTML = '<div class="empty-state">🤖<br>暂无匹配的模型</div>'; return; }
  
  const grid = el('div', { class: 'models-grid' });
  models.forEach(m => {
    const st = getModelStatus(m);
    const isFav = state.favorites.has(`${m.provider}/${m.model}`);
    grid.appendChild(el('div', { class: `model-card ${st.class}` }, [
      el('div', { class: 'model-card-header' }, [
        el('strong', { class: 'model-card-title', 'data-tip': buildModelTooltip(m) }, m.model),
        el('span', { class: `status-badge ${st.badge}` }, st.text),
      ]),
      el('div', { class: 'model-card-meta' }, [
        el('span', {}, `类型: ${m.type === 'free' ? '免费' : '付费'}`),
        el('span', {}, `Tier: ${getTierStars(m.tier)}`),
        el('span', {}, `优先级: ${m.priority || 0}`),
      ]),
      el('div', { class: 'model-card-tags' }, (m.tags || []).map(t => el('span', { class: `tag ${getTagClass(t)}`, title: getTagDescription(t) }, t))),
      el('div', { class: 'model-card-actions' }, [
        el('button', { class: `btn-fav ${isFav ? 'active' : ''}`, onclick: (e) => { e.stopPropagation(); toggleFavorite(m); }, title: isFav ? '取消收藏' : '收藏此模型' }, isFav ? '★' : '☆'),
        el('button', { class: 'btn ghost', onclick: () => showEditModelModal(m), title: '编辑模型' }, '✏️'),
        el('button', { class: 'btn ghost danger', onclick: () => deleteModel(m.provider, m.model), title: '删除模型' }, '🗑️'),
        el('button', { class: 'btn ghost', onclick: () => forceSwitchModel(m.provider, m.model), title: '强制切换到此模型' }, '⚡'),
      ]),
    ]));
  });
  container.appendChild(grid);
}

function renderProviderScorecard(container) {
  container.innerHTML = '';
  
  // 计算每个提供商评分
  const scores = state.providers.map(item => {
    const p = item.provider || item;
    return {
      provider: p,
      score: getProviderScore(p),
      freeModels: state.models.filter(m => m.provider === p && m.type === 'free' && !m.disabled).length,
      totalModels: state.models.filter(m => m.provider === p && !m.disabled).length,
      hasVision: state.models.some(m => m.provider === p && (m.tags || []).includes('vision')),
      hasReasoning: state.models.some(m => m.provider === p && (m.tags || []).includes('reasoning')),
      hasCode: state.models.some(m => m.provider === p && (m.tags || []).includes('code')),
    };
  }).filter(s => s.freeModels > 0).sort((a, b) => b.score - a.score);
  
  if (!scores.length) {
    container.innerHTML = '<div class="empty-state">📊<br>暂无免费模型提供商</div>';
    return;
  }
  
  const grid = el('div', { class: 'scorecard-grid' });
  scores.forEach((s, idx) => {
    const rankClass = idx === 0 ? 'gold' : idx === 1 ? 'silver' : idx === 2 ? 'bronze' : '';
    grid.appendChild(el('div', { class: `scorecard ${rankClass}` }, [
      el('div', { class: 'scorecard-rank' }, `#${idx + 1}`),
      el('div', { class: 'scorecard-header' }, [
        el('div', { class: 'scorecard-title', 'data-tip': buildProviderTooltip(s.provider) }, providerDisplayName(s.provider)),
        el('div', { class: `scorecard-score ${rankClass}` }, Math.round(s.score)),
      ]),
      el('div', { class: 'scorecard-meta' }, [
        el('span', { class: 'scorecard-tag' }, `${s.freeModels} 免费模型`),
        s.hasVision && el('span', { class: 'scorecard-tag vision' }, '👁 视觉'),
        s.hasReasoning && el('span', { class: 'scorecard-tag reasoning' }, '🧠 推理'),
        s.hasCode && el('span', { class: 'scorecard-tag code' }, '💻 代码'),
      ].filter(Boolean)),
      el('div', { class: 'scorecard-action' }, [
        el('button', { class: 'btn outline', onclick: () => { state.modelFilters.provider = s.provider; state.currentView = 'table'; renderModels(); switchTab('table'); } }, '查看模型'),
      ])
    ]));
  });
  container.appendChild(grid);
}

function renderModelsTable(models, container) {
  container.innerHTML = '';
  if (models.length === 0) { container.innerHTML = '<div class="empty-state">🤖<br>暂无匹配的模型</div>'; return; }

  // 按提供商分组
  const grouped = {};
  models.forEach(m => {
    if (!grouped[m.provider]) grouped[m.provider] = [];
    grouped[m.provider].push(m);
  });

  const wrapper = el('div', { class: 'models-table-wrapper' });
  const providers = Object.keys(grouped).sort();
  
  providers.forEach((provider, pi) => {
    const providerModels = grouped[provider];
    const meta = PROVIDER_META[provider] || {};
    const displayName = providerDisplayName(provider);
    const isOpen = state.providerSections[provider] === true;
    
    // 初始化折叠状态为默认收起
    if (state.providerSections[provider] === undefined) {
      state.providerSections[provider] = false;
    }
    
    const section = el('div', { class: 'provider-section' }, [
      el('div', { class: 'provider-section-header', onclick: () => toggleProviderSection(provider + '-models'), title: buildProviderTooltip(provider) }, [
        el('span', { class: 'provider-section-title' }, `${displayName} (${providerModels.length} 个模型)`),
        el('div', { class: 'provider-section-actions' }, [
          el('span', { class: `provider-section-toggle ${isOpen ? 'open' : ''}` }, isOpen ? '▲' : '▼'),
          el('button', { class: 'btn ghost api-key-btn', onclick: (e) => { e.stopPropagation(); openApiKeyModal(provider); }, title: '管理 API Key（支持多账号）' }, '🔑'),
        ]),
      ]),
      el('div', { class: 'provider-section-content', id: provider + '-models', style: isOpen ? '' : 'display: none' }, [
        el('div', { class: 'models-table-wrapper' }, [
          el('table', { class: 'models-table' }, [
            el('thead', {}, el('tr', {}, [
              el('th', {}, '⭐'), el('th', {}, '模型'), el('th', {}, '类型'), el('th', {}, 'Tier'), el('th', {}, '标签'), el('th', {}, '能力'), el('th', {}, '优先级'), el('th', {}, '状态'), el('th', {}, '操作')
            ])),
            el('tbody', {}, providerModels.map(m => {
              const st = getModelStatus(m);
              const isFav = state.favorites.has(`${m.provider}/${m.model}`);
              return el('tr', { class: `model-row ${st.class}`, 'data-model-id': `${m.provider}/${m.model}` }, [
                el('td', { class: 'fav-cell' }, el('button', { class: `btn-fav ${isFav ? 'active' : ''}`, onclick: (e) => { e.stopPropagation(); toggleFavorite(m); }, title: isFav ? '取消收藏' : '收藏此模型' }, isFav ? '★' : '☆')),
                el('td', { style: 'font-family: var(--font-mono)', 'data-tip': buildModelTooltip(m) }, m.model),
                el('td', {}, el('span', { class: `status-badge ${m.type === 'free' ? 'success' : 'warning'}` }, m.type === 'free' ? '免费' : '付费')),
                el('td', {}, getTierStars(m.tier)),
                el('td', {}, el('div', { class: 'tags' }, (m.tags || []).map(t => el('span', { class: `tag ${getTagClass(t)}`, title: getTagDescription(t) }, t)))),
                el('td', {}, el('div', { class: 'caps' }, (modelCapabilities(m).length ? modelCapabilities(m) : [{ key: 'chat', label: '💬 通用' }]).slice(0, 3).map(c => el('span', { class: `cap-badge cap-${c.key}`, 'data-tip': c.tasks ? `${c.label}：${c.tasks}` : '' }, c.label)))),
                el('td', {}, m.priority || 0),
                el('td', {}, el('span', { class: `status-badge ${st.badge}` }, st.text)),
                el('td', {}, el('div', { class: 'action-buttons' }, [
                  el('button', { class: 'btn ghost', onclick: () => showEditModelModal(m), title: '编辑模型' }, '✏️'),
                  el('button', { class: 'btn ghost danger', onclick: () => deleteModel(m.provider, m.model), title: '删除模型' }, '🗑️'),
                  el('button', { class: 'btn ghost', onclick: () => forceSwitchModel(m.provider, m.model), title: '强制切换到此模型' }, '⚡'),
                ]))
              ]);
            }))
          ])
        ])
      ])
    ]);
    wrapper.appendChild(section);
  });

  container.appendChild(wrapper);
}

function renderModelsTree(models, container) {
  container.innerHTML = '';
  if (models.length === 0) { container.innerHTML = '<div class="empty-state">🤖<br>暂无匹配的模型</div>'; return; }

  const grouped = {};
  models.forEach(m => { if (!grouped[m.provider]) grouped[m.provider] = []; grouped[m.provider].push(m); });

  Object.entries(grouped).forEach(([provider, ms]) => {
    const healthy = ms.filter(m => !m.disabled && !state.cooldowns[`${m.provider}/${m.model}`]).length;
    
    // 初始化折叠状态为默认收起
    if (state.providerSections[provider] === undefined) {
      state.providerSections[provider] = false;
    }
    const isOpen = state.providerSections[provider] === true;
    
    const group = el('div', { class: `provider-group ${isOpen ? 'open' : ''}` }, [
      el('div', { class: 'provider-header', onclick: e => { if (e.target === e.currentTarget || e.target.closest('.provider-toggle')) toggleProviderGroup(e.currentTarget, provider); } }, [
        el('div', { class: 'provider-info' }, [
          el('span', { class: 'provider-name', 'data-tip': buildProviderTooltip(provider) }, providerDisplayName(provider)),
          el('span', { class: 'provider-count' }, `${healthy}/${ms.length} 可用`),
        ]),
        el('span', { class: 'provider-toggle' }, isOpen ? '▲' : '▼'),
      ]),
      el('div', { class: 'provider-models', style: isOpen ? '' : 'display: none' }, ms.map(m => {
        const st = getModelStatus(m);
        return el('div', { class: `model-item ${st.class}`, 'data-model-id': `${m.provider}/${m.model}` }, [
          el('label', { class: 'model-item-label' }, [
            el('input', { type: 'checkbox', class: 'model-select', onchange: updateBulkDeleteBtn }),
            el('span', { 'data-tip': buildModelTooltip(m) }, m.model),
            el('span', { class: `status-badge ${st.badge}` }, st.text),
          ]),
          el('div', { class: 'model-details' }, [
            el('span', {}, getTierStars(m.tier)),
            ...(m.tags || []).map(t => el('span', { class: `tag ${getTagClass(t)}` }, t)),
          ]),
        ]);
      })),
    ]);
    container.appendChild(group);
  });
}

function toggleProviderGroup(header, provider) {
  const group = header.closest('.provider-group');
  group.classList.toggle('open');
  if (provider) {
    state.providerSections[provider] = group.classList.contains('open');
  }
}

function updateBulkDeleteBtn() {
  const btn = $('#btnBulkDelete'); // 如果有批量删除按钮
  if (btn) {
    const count = $$('.model-select:checked').length;
    btn.disabled = count === 0;
    btn.textContent = `删除选中 (${count})`;
  }
}

// ============ Section 3: 提供商阵列 ============
function renderProviders() {
  const grid = $('#providerGrid');
  if (!grid) return;
  grid.innerHTML = '';
  if (!state.providers.length) {
    grid.innerHTML = '<div class="empty-state">🏢<br>暂无已接入的提供商</div>';
  } else {
    const ordered = [...state.providers].sort((a, b) => {
      const rank = k => ({ cn: 0, local: 1, gw: 2, intl: 3 }[providerKind(k)] ?? 9);
      return rank(a.provider) - rank(b.provider) || (b.modelCount || 0) - (a.modelCount || 0);
    });
    ordered.forEach(p => {
      const name = p.provider || p;
      const isHealthy = !Object.keys(state.cooldowns).some(k => k.startsWith(name + '/'));
      const kind = providerKind(name);
      const kindLabel = { cn: '国内', intl: '国外', gw: '网关', local: '本地' }[kind] || '';
      grid.appendChild(el('div', { class: 'provider-card', 'data-tip': buildProviderTooltip(name), onclick: () => showProviderModels(name) }, [
        el('div', { class: 'provider-card-header' }, [
          providerNameEl(name),
          el('span', { class: `provider-card-status ${isHealthy ? 'healthy' : 'warning'}` }, isHealthy ? '● 正常' : '● 有冷却'),
        ]),
        el('div', { class: 'provider-card-stats' }, [
          el('span', { class: `kind-badge kind-${kind}` }, kindLabel),
          el('span', {}, `${p.modelCount} 个模型`),
          keyBadgeEl(name),
          latencyBadgeEl(name),
        ]),
        probeReasonEl(name),
        el('div', { class: 'provider-card-actions' }, [
          el('button', { class: 'btn outline', title: '测试连通性与延迟', onclick: e => { e.stopPropagation(); probeProvider(name); } }, '📡 测速'),
          el('button', { class: 'btn outline', title: '重新拉取该提供商的模型列表', onclick: e => { e.stopPropagation(); refreshProvider(name); } }, '🔄 刷新'),
          el('button', { class: 'btn primary', onclick: e => { e.stopPropagation(); showProviderModels(name); } }, '查看模型'),
        ]),
      ]));
    });
  }
  renderProviderCatalog();
}

// 未接入的主流提供商目录
function buildCatalogTooltip(name, meta) {
  const kind = providerKind(name);
  const kindLabel = { cn: '🇨🇳 国内直连', intl: '🌍 国外（多需代理）', gw: '🔀 网关聚合', local: '💻 本地部署' }[kind] || '';
  return `🏢 ${providerDisplayName(name)}   （未接入）
${kindLabel}
──────────────
${meta.desc}
免费额度: ${meta.freeQuota}
恢复方式: ${meta.recovery}
认证方式: ${meta.auth}
计费模式: ${meta.payment}${meta.sampleModels ? '\n代表模型: ' + meta.sampleModels.slice(0, 4).join('、') : ''}
${meta.needProxy ? '⚠️ 需要代理访问' : '✅ 可直连'}
${meta.compat === 'openai' ? '✅ 支持一键接入（OpenAI 兼容）' : '⚠️ 非 OpenAI 兼容协议，需网关转接'}`;
}

// ---- 卡片标题：中文为主（第一行）· 英文为辅（第二行）----
function providerNameEl(name) {
  const m = PROVIDER_META[name];
  const cn = (m && m.nameCn) || name;
  let en = (m && m.nameEn) || '';
  if (en) en = en.replace(/\s*\([^)]*\)\s*$/, '').trim();
  const kids = [el('span', { class: 'pc-cn' }, cn)];
  if (en && en.toLowerCase() !== cn.toLowerCase() && !cn.toLowerCase().includes(en.toLowerCase())) {
    kids.push(el('span', { class: 'pc-en' }, en));
  }
  return el('span', { class: 'provider-card-name' }, kids);
}

// ---- 统一的 API Key 管理入口（替代卡片上的 🔑 按钮）----
function openKeyManager() {
  const list = el('div', { class: 'apikey-list' });
  const names = [...new Set([
    ...state.providers.map(p => p.provider || p),
    ...Object.keys(state.keysInfo),
  ])].sort();
  if (!names.length) {
    list.appendChild(el('div', { class: 'apikey-empty' }, '暂无提供商'));
  }
  names.forEach(n => {
    const info = state.keysInfo[n] || { count: 0 };
    list.appendChild(el('div', { class: 'apikey-row' }, [
      el('span', { style: 'flex:1; min-width:0' }, providerDisplayName(n)),
      el('span', { class: `mini-badge key-${info.count > 0 ? 'ok' : 'missing'}` },
        info.count > 0 ? `已配置 ${info.count} 个` : '未配置'),
      el('button', { class: 'btn outline', onclick: () => { hideModal(); openApiKeyModal(n); } }, '🔑 管理'),
    ]));
  });
  showModal(el('div', { class: 'modal' }, [
    el('div', { class: 'modal-header' }, [
      el('h3', { class: 'modal-title' }, '🔑 API Key 管理'),
      el('button', { class: 'modal-close', onclick: hideModal }, '✕'),
    ]),
    el('div', { class: 'modal-body' }, [
      el('div', { class: 'apikey-meta' }, '同一提供商有多个账号时可添加多个 Key（顺序故障转移）。保存后自动热重载，无需重启。'),
      list,
    ]),
    el('div', { class: 'modal-footer' }, [
      el('button', { class: 'btn outline', onclick: hideModal }, '关闭'),
    ]),
  ]));
}

// ---- 提供商卡片上的小徽标 ----
function keyBadgeEl(name) {
  const info = state.keysInfo[name];
  const n = info ? (info.count || 0) : 0;
  return el('span', {
    class: `mini-badge key-${n > 0 ? 'ok' : 'missing'}`,
    'data-tip': n > 0 ? `已配置 ${n} 个 API Key` : '未配置 API Key（点 🔑 添加）',
  }, n > 0 ? `🔑 ${n}` : '🔑 未配置');
}

// 卡片内的失败原因行（默认隐藏；测速失败才显示）
function probeReasonEl(name) {
  const pr = state.probe[name];
  const first = pr ? ((pr.models || []).find(m => !m.ok) || {}) : {};
  const msg = (pr && !pr.ok) ? String(first.error || pr.error || '不可用') : '';
  return el('div', {
    class: 'probe-reason',
    style: msg ? '' : 'display:none',
    'data-tip': msg,
  }, msg ? '⚠️ ' + msg.replace(/\s+/g, ' ').slice(0, 96) : '');
}

function latencyBadgeEl(name) {
  const pr = state.probe[name];
  if (!pr) return el('span', { class: 'mini-badge probe-none', 'data-tip': '尚未测速，点「📡 测速」' }, '📡 —');
  if (!pr.ok) {
    const err = ((pr.models || []).find(m => !m.ok) || {}).error || pr.error || '不可用';
    return el('span', { class: 'mini-badge probe-fail', 'data-tip': `最近测速失败：${err}` }, '📡 失败');
  }
  const ms = pr.best_latency_ms || 0;
  const cls = ms < 1500 ? 'probe-fast' : ms < 4000 ? 'probe-mid' : 'probe-slow';
  return el('span', {
    class: `mini-badge ${cls}`,
    'data-tip': `最快 ${Math.round(ms)}ms · 平均 ${Math.round(pr.avg_latency_ms || ms)}ms · ${pr.ok_count}/${pr.tested} 个模型可用`,
  }, `📡 ${Math.round(ms)}ms`);
}

async function probeProvider(name) {
  showToast(`正在测速 ${providerDisplayName(name)} …`, 'info', 2000);
  try {
    const r = await api(`/providers/${name}/probe`, { method: 'POST', body: { limit: 3 } });
    state.probe[name] = r;
    renderProviders();
    if (r.ok) {
      showToast(`${providerDisplayName(name)} 可用，最快 ${Math.round(r.best_latency_ms)}ms`, 'success');
    } else {
      const first = (r.models || []).find(m => !m.ok) || r;
      showToast(`${providerDisplayName(name)} 失败：${(first.error || '不可用').slice(0, 180)}`, 'danger', 12000);
    }
  } catch (e) { showToast('测速失败: ' + e.message, 'danger'); }
}

async function probeAllProviders() {
  const btn = $('#btnProbeAll');
  if (btn) { btn.disabled = true; btn.textContent = '📡 测速中…'; }
  showToast('正在并发测速全部提供商…', 'info', 3000);
  try {
    const r = await api('/probe_all', { method: 'POST', body: { limit: 2 } });
    state.probe = r.probe || state.probe;
    renderProviders();
    const all = Object.values(state.probe);
    const ok = all.filter(x => x.ok).length;
    showToast(`测速完成：${ok}/${all.length} 个提供商可用`, ok ? 'success' : 'warning');
  } catch (e) {
    showToast('全部测速失败: ' + e.message, 'danger');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📡 全部测速'; }
  }
}

function renderProviderCatalog() {
  const grid = $('#providerCatalog');
  if (!grid) return;
  grid.innerHTML = '';
  const configured = new Set(state.providers.map(p => p.provider || p));
  const entries = Object.entries(PROVIDER_META)
    .filter(([name, meta]) => {
      if (!meta.baseUrl || configured.has(name)) return false;
      if (state.catalogKind && providerKind(name) !== state.catalogKind) return false;
      if (state.catalogHideProxy && meta.needProxy) return false;
      if (state.catalogOnlyFree && quotaRank(name).q <= 0) return false;
      return true;
    })
    .sort((a, b) => {
      const rank = k => ({ cn: 0, local: 1, gw: 2, intl: 3 }[providerKind(k)] ?? 9);
      return rank(a[0]) - rank(b[0]) || a[0].localeCompare(b[0]);
    });
  const countEl = $('#catalogCount');
  if (countEl) countEl.textContent = entries.length ? `${entries.length} 个未接入` : '已全部接入';
  if (!entries.length) {
    grid.innerHTML = '<div class="empty-state">✅<br>主流提供商已全部接入</div>';
    return;
  }
  entries.forEach(([name, meta]) => {
    const kind = providerKind(name);
    const kindLabel = { cn: '国内', intl: '国外', gw: '网关', local: '本地' }[kind] || '';
    const connectable = meta.compat === 'openai';
    grid.appendChild(el('div', { class: 'provider-card catalog-card', 'data-tip': buildCatalogTooltip(name, meta) }, [
      el('div', { class: 'provider-card-header' }, [
        providerNameEl(name),
        el('span', { class: `kind-badge kind-${kind}` }, kindLabel),
      ]),
      el('div', { class: 'provider-card-stats catalog-desc' }, [
        el('span', { style: 'display:block; flex:1 1 100%' }, meta.desc),
        meta.sampleModels
          ? el('span', { class: 'catalog-models' }, '代表模型：' + meta.sampleModels.slice(0, 3).join('、'))
          : null,
      ].filter(Boolean)),
      el('div', { class: 'provider-card-actions' }, [
        connectable
          ? el('button', { class: 'btn primary', onclick: e => { e.stopPropagation(); openAddProviderModal(name); } }, '＋ 接入')
          : el('span', { class: 'not-connectable' }, '需网关转接'),
      ]),
    ]));
  });
}

// 一键接入弹窗
function openAddProviderModal(name) {
  const meta = PROVIDER_META[name] || {};
  const keyInput = el('input', { type: 'text', id: 'newProviderKey', placeholder: `粘贴 ${meta.keyEnv || 'API Key'}（可留空，接入后点 🔑 补充）` });
  async function submit() {
    const btn = this;
    try {
      await api('/providers', { method: 'POST', body: { name, base_url: meta.baseUrl, key_env: meta.keyEnv, type: 'free', free_only: true } });
      const key = keyInput.value.trim();
      if (key) {
        try { await api('/keys', { method: 'POST', body: { provider: name, action: 'add', key } }); }
        catch (e) { console.warn('设置 Key 失败:', e); }
      }
      try { await api(`/providers/${name}/refresh`, { method: 'POST' }); }
      catch (e) { console.warn('刷新模型失败:', e); }
      hideModal();
      await loadDashboard(); await loadModels(); await loadKeys();
      renderAll();
      showToast(`已接入 ${providerDisplayName(name)}`, 'success');
      if (!key) showToast('未填 Key，请点该提供商卡片的 🔑 补充', 'warning');
    } catch (e) { showToast('接入失败: ' + e.message, 'danger'); }
  }
  const modal = el('div', { class: 'modal' }, [
    el('div', { class: 'modal-header' }, [
      el('h3', { class: 'modal-title' }, `接入 ${providerDisplayName(name)}`),
      el('button', { class: 'modal-close', onclick: hideModal }, '✕'),
    ]),
    el('div', { class: 'modal-body' }, [
      el('div', { class: 'apikey-meta', style: 'white-space: pre-line; line-height: 1.8' },
        `端点: ${meta.baseUrl}\n环境变量: ${meta.keyEnv || '-'}\n获取方式: ${meta.auth}\n免费额度: ${meta.freeQuota}${meta.needProxy ? '\n⚠️ 该提供商需要代理/VPN 才能访问' : ''}`),
      keyInput,
    ]),
    el('div', { class: 'modal-footer' }, [
      el('button', { class: 'btn outline', onclick: hideModal }, '取消'),
      el('button', { class: 'btn primary', onclick: submit }, '接入并拉取模型'),
    ]),
  ]);
  showModal(modal);
}

async function refreshProvider(provider) {
  try {
    await api(`/providers/${provider}/refresh`, { method: 'POST' });
    showToast(`${provider} 模型列表已刷新`, 'success');
    await loadModels(); await loadDashboard(); renderAll();
  } catch (e) { showToast(e.message, 'danger'); }
}

function showProviderModels(provider) {
  const models = state.models.filter(m => m.provider === provider);
  const content = el('div', {}, [
    el('h3', {}, `${providerDisplayName(provider)} 的模型 (${models.length})`),
    el('div', { class: 'models-grid', style: 'max-height: 400px; overflow: auto' }, models.map(m => {
      const st = getModelStatus(m);
      return el('div', { class: `model-card ${st.class}` }, [
        el('div', { class: 'model-card-header' }, [
          el('strong', { 'data-tip': buildModelTooltip(m) }, m.model),
          el('span', { class: `status-badge ${st.badge}` }, st.text),
        ]),
        el('div', { class: 'model-card-meta' }, `类型: ${m.type} | Tier: ${getTierStars(m.tier)} | 优先级: ${m.priority}`),
        el('div', { class: 'model-card-tags' }, (m.tags || []).map(t => el('span', { class: `tag ${getTagClass(t)}` }, t))),
      ]);
    })),
  ]);
  showModal(el('div', { class: 'modal' }, [
    el('div', { class: 'modal-header' }, [el('h3', { class: 'modal-title' }, `${providerDisplayName(provider)} 模型列表`), el('button', { class: 'modal-close', onclick: hideModal }, '✕')]),
    el('div', { class: 'modal-body' }, content),
    el('div', { class: 'modal-footer' }, [el('button', { class: 'btn primary', onclick: hideModal }, '关闭')]),
  ]));
}

// ============ Section 4: 额度分析 ============
function renderQuota() {
  const usage = state.quota.usage || {};
  const predicted = state.quota.predicted_exhausted || {};

  $('#btnQuotaRefresh')?.addEventListener('click', async () => { await loadDashboard(); renderQuota(); showToast('已刷新', 'success'); });

  const totalRpd = Object.values(usage).reduce((a, b) => a + (b.rpd || 0), 0);
  const totalRpm = Object.values(usage).reduce((a, b) => a + (b.rpm || 0), 0);
  const totalTpd = Object.values(usage).reduce((a, b) => a + (b.tpd || 0), 0);
  const preCool = Object.keys(predicted).length;

  $('#quotaSummary').innerHTML = '';
  [{ label: '今日总请求 (RPD)', value: totalRpd, icon: '📊', class: 'cyan' },
   { label: '当前分钟请求 (RPM)', value: totalRpm, icon: '⚡', class: 'amber' },
   { label: '总 Token (TPD)', value: totalTpd.toLocaleString(), icon: '🔤', class: 'green' },
   { label: '预冷却模型', value: preCool, icon: '⚠️', class: preCool ? 'red' : 'green' }].forEach(k => {
    $('#quotaSummary').appendChild(el('div', { class: 'metric-card' }, [
      el('div', { class: `metric-icon ${k.class}` }, k.icon),
      el('div', { class: 'metric-value' }, k.value),
      el('div', { class: 'metric-label' }, k.label),
    ]));
  });

  const wrap = $('#quotaTableWrap');
  if (Object.keys(usage).length) {
    wrap.innerHTML = '';
    
    // 按提供商分组
    const grouped = {};
    Object.entries(usage).forEach(([k, v]) => {
      const provider = k.split('/')[0];
      if (!grouped[provider]) grouped[provider] = [];
      grouped[provider].push({ model: k, ...v });
    });

    const providers = Object.keys(grouped).sort();
  
      providers.forEach((provider, pi) => {
        const models = grouped[provider].sort((a, b) => (b.rpd || 0) - (a.rpd || 0));
        const isOpen = state.providerSections[provider] === true;
    
        // 初始化折叠状态为默认收起
        if (state.providerSections[provider] === undefined) {
          state.providerSections[provider] = false;
        }
    
        const section = el('div', { class: 'provider-section' }, [
          el('div', { class: 'provider-section-header', onclick: () => toggleProviderSection(provider + '-quota'), 'data-tip': buildProviderTooltip(provider) }, [
            el('span', { class: 'provider-section-title' }, `${providerDisplayName(provider)} (${models.length} 个模型)`),
            el('div', { class: 'provider-section-actions' }, [
              el('span', { class: `provider-section-toggle ${isOpen ? 'open' : ''}` }, isOpen ? '▲' : '▼'),
            ]),
          ]),
          el('div', { class: 'provider-section-content', id: provider + '-quota', style: isOpen ? '' : 'display: none' }, [
          el('table', { class: 'data-table' }, [
            el('thead', {}, el('tr', {}, ['模型', 'RPM', 'RPD', 'TPM', 'TPD', '状态'].map(t => el('th', {}, t)))),
            el('tbody', {}, models.map(m => {
              const k = m.model;
              const isPreCool = predicted[k];
              return el('tr', { class: isPreCool ? 'warning-row' : '' }, [
                el('td', {}, `<code>${k}</code>`),
                el('td', {}, m.rpm || 0), el('td', {}, m.rpd || 0), el('td', {}, m.tpm || 0), el('td', {}, m.tpd || 0),
                el('td', {}, isPreCool ? el('span', { class: 'status-badge warning' }, '预冷却') : el('span', { class: 'status-badge success' }, '正常')),
              ]);
            }))
          ])
        ])
      ]);
      wrap.appendChild(section);
    });
  } else {
    wrap.innerHTML = '<div class="empty-state">📈<br>暂无额度使用数据</div>';
  }
}

// ============ Section 5: 冷却中心 ============
function renderCooldown() {
  const cds = state.cooldowns || {};

  $('#btnResetAllCooldown')?.addEventListener('click', resetAllCooldowns);
  $('#btnCooldownRefresh')?.addEventListener('click', async () => { await loadDashboard(); renderCooldown(); showToast('已刷新', 'success'); });

  const wrap = $('#cooldownTableWrap');
  const empty = $('#cooldownEmpty');
  if (!Object.keys(cds).length) {
    wrap.innerHTML = ''; empty.hidden = false; return;
  }
  empty.hidden = true;
  wrap.innerHTML = '';
  const table = el('table', { class: 'data-table' }, [
    el('thead', {}, el('tr', {}, ['模型', '原因', '剩余时间', '命中次数', '操作'].map(t => el('th', {}, t)))),
    el('tbody', {}, Object.entries(cds).map(([k, v]) => {
      const [provider, model] = k.split('/');
      const remaining = Math.max(0, Math.ceil(v.until - Date.now() / 1000));
      const reasonClass = v.reason === 'key_unhealthy' ? 'danger' : v.reason === '429' ? 'warning' : 'info';
      return el('tr', {}, [
        el('td', {}, `<code>${k}</code>`),
        el('td', {}, el('span', { class: `status-badge ${reasonClass}` }, v.reason)),
        el('td', {}, formatDuration(remaining)),
        el('td', {}, v.hits),
        el('td', {}, el('button', { class: 'btn ghost', onclick: () => clearSingleCooldown(provider, model) }, '解除')),
      ]);
    }))
  ]);
  wrap.appendChild(table);
}

async function clearSingleCooldown(provider, model) {
  showToast('单个冷却解除需后端支持，暂用"重置所有冷却"', 'warning');
}

async function resetAllCooldowns() {
  if (!confirm('确定重置所有模型的冷却状态？')) return;
  try {
    await api('/reset_cooldowns', { method: 'POST' });
    await loadDashboard(); renderAll(); showToast('所有冷却已重置', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

// ============ Section 6: 缓存控制 ============
function renderCache() {
  const c = state.cache || {};

  $('#btnToggleCache')?.addEventListener('click', toggleCache);
  $('#btnClearCache')?.addEventListener('click', clearCache);
  $('#btnCacheRefresh')?.addEventListener('click', async () => { await loadCache(); renderCache(); showToast('已刷新', 'success'); });

  $('#cacheMetrics').innerHTML = '';
  [{ label: '缓存状态', value: c.enabled ? '启用' : '禁用', icon: c.enabled ? '✅' : '⛔', class: c.enabled ? 'green' : 'red' },
   { label: '缓存条目', value: c.entries || 0, icon: '📦', class: 'cyan' },
   { label: 'TTL', value: `${(c.ttl_seconds || 86400) / 3600}h`, icon: '⏱️', class: 'amber' },
   { label: '命中服务', value: c.serve_hits ? '是' : '否', icon: c.serve_hits ? '✅' : '❌', class: c.serve_hits ? 'green' : 'amber' }].forEach(k => {
    $('#cacheMetrics').appendChild(el('div', { class: 'metric-card' }, [
      el('div', { class: `metric-icon ${k.class}` }, k.icon),
      el('div', { class: 'metric-value' }, k.value),
      el('div', { class: 'metric-label' }, k.label),
    ]));
  });

  const eventsEl = $('#cacheEvents');
  if (c.events && Object.keys(c.events).length) {
    eventsEl.hidden = false;
    eventsEl.textContent = JSON.stringify(c.events, null, 2);
  } else { eventsEl.hidden = true; }
}

async function toggleCache() {
  try {
    await api('/cache', { method: 'POST', body: { enabled: !state.cache.enabled } });
    await loadCache(); renderCache(); showToast(`缓存已${state.cache.enabled ? '禁用' : '启用'}`, 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

async function clearCache() {
  if (!confirm('确定清空所有缓存？')) return;
  try {
    await api('/cache', { method: 'POST', body: { clear: true } });
    await loadCache(); renderCache(); showToast('缓存已清空', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

// ============ Section 7: 策略中心 ============
function renderPolicy() {
  const p = state.policy || {};

  $('#btnResetPolicy')?.addEventListener('click', resetPolicy);
  $('#policyForm')?.addEventListener('submit', e => { e.preventDefault(); hotLoadPolicy(); });

  $('#policyCurrent').innerHTML = '';
  const dl = $('#policyCurrent');
  if (dl) {
    dl.appendChild(el('dt', {}, '策略类')); dl.appendChild(el('dd', {}, `<code>${p.policy || 'BuiltinPolicy'}</code>`));
    dl.appendChild(el('dt', {}, '模块路径')); dl.appendChild(el('dd', {}, `<code>${p.module || 'app.core.policy'}</code>`));
    dl.appendChild(el('dt', {}, '内置策略')); dl.appendChild(el('dd', {}, p.builtin ? '是' : '否'));
    dl.appendChild(el('dt', {}, '配置参数')); dl.appendChild(el('dd', {}, `<pre style="font-size:0.75rem;max-height:200px;overflow:auto">${JSON.stringify(p.config || {}, null, 2)}</pre>`));
  }
}

async function hotLoadPolicy() {
  const module = $('#policyModule').value.trim();
  const cls = $('#policyClass').value.trim();
  const args = $('#policyArgs').value.trim();
  if (!module || !cls) { showToast('模块名和类名必填', 'danger'); return; }
  let parsedArgs = {};
  try { parsedArgs = args ? JSON.parse(args) : {}; } catch { showToast('参数 JSON 格式错误', 'danger'); return; }
  try {
    await api('/policy', { method: 'POST', body: { module, class: cls, args: parsedArgs } });
    await loadPolicy(); renderPolicy(); showToast('策略已加载', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

async function resetPolicy() {
  try {
    await api('/policy', { method: 'POST', body: { reset: true } });
    await loadPolicy(); renderPolicy(); showToast('已恢复内置策略', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

// ============ Section 8: 系统设置 ============
function renderSettings() {
  $('#btnHotRefresh')?.addEventListener('click', async () => { await api('/refresh', { method: 'POST' }); await loadDashboard(); renderSettings(); showToast('配置已热刷新', 'success'); });
  $('#btnStatusRefresh')?.addEventListener('click', async () => { await loadDashboard(); renderSettings(); showToast('状态已刷新', 'success'); });
  $('#btnDiag2')?.addEventListener('click', runDiagnostics);
  $('#btnDiag')?.addEventListener('click', runDiagnostics);

  $('#configPreview').textContent = JSON.stringify(state.settings, null, 2);
}

async function runDiagnostics() {
  showToast('正在运行诊断...', 'info');
  try {
    const health = await api('/../health');
    const status = await api('/status');
    showToast(`诊断完成: 服务健康, 模型 ${status.models?.active}/${status.models?.total} 活跃`, 'success');
  } catch (e) { showToast(`诊断失败: ${e.message}`, 'danger'); }
}

// ============ 模型 CRUD ============
async function deleteModel(provider, model) {
  if (!confirm(`确定删除 ${provider}/${model}？`)) return;
  try {
    await api('/models', { method: 'DELETE', body: { provider, model } });
    await loadModels(); renderModelsView(); showToast('已删除', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

async function forceSwitchModel(provider, model) {
  try {
    await api('/switch', { method: 'POST', body: { model: `${provider}/${model}` } });
    showToast(`已切换到 ${provider}/${model}`, 'success');
    await loadDashboard(); renderOverview();
  } catch (e) { showToast(e.message, 'danger'); }
}

function showAddModelModal() { showModelEditModal(null); }

function showEditModelModal(model) {
  const isEdit = !!model;
  const providers = state.providers.map(p => p.provider);
  const modal = el('div', { class: 'modal' }, [
    el('div', { class: 'modal-header' }, [
      el('h3', { class: 'modal-title' }, isEdit ? '编辑模型' : '添加模型'),
      el('button', { class: 'modal-close', onclick: hideModal }, '✕'),
    ]),
    el('div', { class: 'modal-body' }, [
      el('form', { id: 'modelForm' }, [
        el('div', { class: 'form-group' }, [
          el('label', {}, '提供商'),
          el('select', { id: 'editProvider', required: true }, [
            el('option', { value: '' }, '选择提供商'),
            ...providers.map(p => el('option', { value: p }, p)),
          ]),
        ]),
        el('div', { class: 'form-group' }, [el('label', {}, '模型 ID'), el('input', { type: 'text', id: 'editModel', placeholder: '如: Qwen/Qwen3.8-Flash', required: true })]),
        el('div', { class: 'form-group' }, [el('label', {}, '类型'), el('select', { id: 'editType' }, [el('option', { value: 'free' }, '免费'), el('option', { value: 'paid' }, '付费')])]),
        el('div', { class: 'form-group' }, [el('label', {}, 'Tier'), el('select', { id: 'editTier' }, [el('option', { value: 'high' }, '高'), el('option', { value: 'medium' }, '中'), el('option', { value: 'low' }, '低')])]),
        el('div', { class: 'form-group' }, [el('label', {}, '标签 (逗号分隔)'), el('input', { type: 'text', id: 'editTags', placeholder: 'reasoning, code, vision' })]),
        el('div', { class: 'form-group' }, [el('label', {}, '优先级'), el('input', { type: 'number', id: 'editPriority', min: 0, value: 0 })]),
        el('div', { class: 'form-group' }, [el('label', {}, '启用'), el('input', { type: 'checkbox', id: 'editEnabled', checked: true })]),
      ]),
    ]),
    el('div', { class: 'modal-footer' }, [
      el('button', { class: 'btn outline', onclick: hideModal }, '取消'),
      el('button', { class: `btn ${isEdit ? 'primary' : 'success'}`, onclick: () => { const f = $('#modelForm'); if (f.checkValidity()) { isEdit ? updateModel(model) : addModel(); } else f.reportValidity(); } }, isEdit ? '保存' : '添加'),
    ]),
  ]);
  showModal(modal);

  if (isEdit) {
    $('#editProvider').value = model.provider;
    $('#editModel').value = model.model;
    $('#editType').value = model.type;
    $('#editTier').value = model.tier;
    $('#editTags').value = (model.tags || []).join(', ');
    $('#editPriority').value = model.priority || 0;
    $('#editEnabled').checked = !model.disabled;
  }
}

async function addModel() {
  const provider = $('#editProvider').value;
  const modelId = $('#editModel').value.trim();
  const type = $('#editType').value;
  const tier = $('#editTier').value;
  const tags = $('#editTags').value.split(',').map(t => t.trim()).filter(t => t);
  const priority = parseInt($('#editPriority').value) || 0;

  if (!provider || !modelId) { showToast('提供商和模型不能为空', 'danger'); return; }
  if (!modelId.includes('/')) { showToast('模型 ID 应为 provider/name 格式', 'danger'); return; }
  const [, modelName] = modelId.split('/');
  try {
    await api('/models', { method: 'POST', body: { provider, model: modelName, type, tags, priority } });
    await loadModels(); renderModelsView(); hideModal(); showToast('已添加', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

async function updateModel(oldModel) {
  const [provider, modelName] = [$('#editProvider').value, $('#editModel').value.trim().split('/')[1]];
  const type = $('#editType').value;
  const tier = $('#editTier').value;
  const tags = $('#editTags').value.split(',').map(t => t.trim()).filter(t => t);
  const priority = parseInt($('#editPriority').value) || 0;
  try {
    await api(`/models/${encodeURIComponent(provider)}/${encodeURIComponent(modelName)}`, { method: 'PATCH', body: { type, tags, priority } });
    await loadModels(); renderModelsView(); hideModal(); showToast('已更新', 'success');
  } catch (e) { showToast(e.message, 'danger'); }
}

// ============ 主题切换 ============
function applyTheme() {
  // 深色主题已是默认，此处可扩展浅色主题
  document.documentElement.setAttribute('data-theme', state.theme);
  $('#btnTheme').textContent = state.theme === 'dark' ? '☀️' : '🌙';
}

$('#btnTheme')?.addEventListener('click', () => {
  state.theme = state.theme === 'dark' ? 'light' : 'dark';
  localStorage.setItem('theme', state.theme);
  applyTheme();
});

// ============ 粒子背景 ============
function initParticles() {
  const container = $('#particles');
  if (!container) return;
  // CSS 已处理基础粒子，这里可用 JS 生成更多
  for (let i = 0; i < 12; i++) {
    const p = document.createElement('div');
    p.style.cssText = `
      position: absolute; width: 3px; height: 3px; border-radius: 50%;
      background: ${Math.random() > 0.5 ? 'var(--accent-cyan)' : 'var(--accent-violet)'};
      box-shadow: 0 0 6px currentColor;
      left: ${Math.random() * 100}%; top: ${Math.random() * 100}%;
      opacity: ${0.2 + Math.random() * 0.4};
      animation: particleFloat ${8 + Math.random() * 4}s ease-in-out infinite;
      animation-delay: ${-Math.random() * 10}s;
    `;
    container.appendChild(p);
  }
}

// ---- 目录筛选 / 全部测速 等控件初始化 ----
function initCatalogControls() {
  const chips = $$('#catalogKindChips .chip');
  chips.forEach(ch => ch.addEventListener('click', () => {
    chips.forEach(c => c.classList.toggle('active', c === ch));
    state.catalogKind = ch.getAttribute('data-kind') || '';
    renderProviderCatalog();
  }));
  const hp = $('#catalogHideProxy');
  if (hp) hp.addEventListener('change', () => { state.catalogHideProxy = hp.checked; renderProviderCatalog(); });
  const of = $('#catalogOnlyFree');
  if (of) of.addEventListener('change', () => { state.catalogOnlyFree = of.checked; renderProviderCatalog(); });

  const btn = $('#btnProbeAll');
  if (btn) btn.addEventListener('click', probeAllProviders);
  const kb = $('#btnKeyManager');
  if (kb) kb.addEventListener('click', openKeyManager);
  const na = $('#btnNotifyAckAll');
  if (na) na.addEventListener('click', ackAllNotifications);
}

// ============ 模型更新通知（免费模型新增/下线）============
const TAG_LABEL = {
  vision: '👁 视觉理解', audio: '🔊 语音音频', embedding: '🧬 向量嵌入',
  rerank: '📐 重排序', code: '💻 代码', math: '➗ 数学',
  reasoning: '🧠 深度推理', long: '📚 长上下文', fast: '⚡ 轻量快速',
  flagship: '🏆 旗舰高能', chat: '💬 通用对话',
};
const TAG_TASKS = {
  vision: '看图问答、OCR、图表理解、截图分析', audio: '语音转写、音频理解、语音合成',
  embedding: '文本向量化、语义检索、RAG 召回', rerank: '检索结果重排序、提升 RAG 精度',
  code: '代码生成与补全、调试改错、单测编写', math: '数学解题、公式推导',
  reasoning: '复杂逻辑推理、多步规划、难题攻坚', long: '长文档总结、整本书/代码库分析',
  fast: '高并发问答、分类抽取、摘要改写', flagship: '通用复杂任务、写作、Agent 主控',
  chat: '日常问答、文本生成',
};

async function loadNotifications() {
  try {
    const r = await api('/notifications');
    state.notifications = { unread: r.unread || 0, events: r.events || [] };
    renderNotifications();
  } catch (e) {
    console.warn('[free-router] 加载通知失败:', e);
  }
}

function notifyEventEl(ev) {
  const isAdd = ev.kind === 'model_added';
  const tags = (ev.tags && ev.tags.length) ? ev.tags : ['chat'];
  const when = ev.ts ? new Date(ev.ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
  return el('div', { class: `notify-item ${isAdd ? 'add' : 'del'}` }, [
    el('div', { class: 'notify-icon' }, isAdd ? '➕' : '➖'),
    el('div', { class: 'notify-body' }, [
      el('div', { class: 'notify-head' }, [
        el('strong', { 'data-tip': isAdd ? '自动发现新增的模型' : '该模型已从提供商的模型列表中消失' }, ev.model),
        el('span', { class: `mini-badge kind-${providerKind(ev.provider)}` }, providerDisplayName(ev.provider)),
        el('span', { class: `mini-badge ${ev.free ? 'key-ok' : 'key-missing'}` }, ev.free ? '免费' : '付费'),
        ev.tier ? el('span', { class: 'mini-badge' }, `Tier ${getTierStars(ev.tier)}`) : null,
        (ev.count > 1) ? el('span', { class: 'mini-badge' }, `×${ev.count}`) : null,
        el('span', { class: 'notify-time' }, when),
      ].filter(Boolean)),
      el('div', { class: 'notify-caps' }, tags.map(t =>
        el('span', { class: `cap-badge cap-${t}`, 'data-tip': `${TAG_LABEL[t] || t}：${TAG_TASKS[t] || '通用对话'}` },
          TAG_LABEL[t] || `💬 ${t}`))),
      el('div', { class: 'notify-tasks' }, isAdd
        ? `✅ 可处理：${tags.map(t => TAG_TASKS[t]).filter(Boolean).join('；') || '通用对话'}`
        : '⚠️ 已下线或改名，相关请求会自动路由到其它模型'),
    ]),
    el('div', { class: 'notify-actions' }, isAdd ? [
      el('button', { class: 'btn primary', title: '保留在池中', onclick: () => ackNotification(ev) }, '✓ 保留'),
      el('button', { class: 'btn outline', title: '从池中移除（记入墓碑，不再自动加回）', onclick: () => removeNotifiedModel(ev) }, '✕ 移除'),
    ] : [
      el('button', { class: 'btn outline', onclick: () => ackNotification(ev) }, '知道了'),
    ]),
  ]);
}

function renderNotifications() {
  const sec = $('#sec-notify');
  const list = $('#notifyList');
  if (!sec || !list) return;
  const evs = (state.notifications.events || []).filter(e => !e.ack);
  const badge = $('#notifyCount');
  if (badge) badge.textContent = String(state.notifications.unread || evs.length || 0);
  if (!evs.length) { sec.hidden = true; list.innerHTML = ''; return; }
  sec.hidden = false;
  list.innerHTML = '';
  evs.slice(0, 50).forEach(ev => list.appendChild(notifyEventEl(ev)));
}

async function ackNotification(ev) {
  try {
    await api('/notifications/ack', { method: 'POST', body: { ids: [ev.id] } });
    ev.ack = true;
    await loadNotifications();
  } catch (e) { showToast('操作失败: ' + e.message, 'danger'); }
}

async function ackAllNotifications() {
  try {
    await api('/notifications/ack', { method: 'POST', body: { all: true } });
    await loadNotifications();
    showToast('已全部标记为已读', 'success');
  } catch (e) { showToast('操作失败: ' + e.message, 'danger'); }
}

async function removeNotifiedModel(ev) {
  try {
    await api('/models', { method: 'DELETE', body: { provider: ev.provider, model: ev.model } });
    await ackNotification(ev);
    await loadModels(); await loadDashboard();
    renderAll();
    showToast(`已移除 ${ev.model}（不会自动加回）`, 'success');
  } catch (e) { showToast('移除失败: ' + e.message, 'danger'); }
}

function startNotifyPolling() {
  let last = state.notifications.unread || 0;
  setInterval(async () => {
    await loadNotifications();
    const now = state.notifications.unread || 0;
    if (now > last) {
      showToast(`🔔 模型库有更新：新增 ${now - last} 项（见「模型更新通知」）`, 'info', 9000);
    }
    last = now;
  }, 60000);
}

// ============ Section 9: Autofree 榜单 ============
async function loadAutofree() {
  try {
    const r = await api('/autofree/ranking');
    state.autofree = r;
    return r;
  } catch (e) {
    console.warn('[free-router] 加载 Autofree 榜单失败:', e);
    state.autofree = { top: [], fallback: [], top_n: 10, fallback_n: 10, recompute_interval_hours: 1 };
    return state.autofree;
  }
}

async function renderAutofree() {
  if (!state.autofree) await loadAutofree();
  const af = state.autofree;
  $('#autofreeTopN').textContent = af.top_n || 10;
  $('#autofreeFallbackN').textContent = af.fallback_n || 10;
  $('#autofreeLastUpdate').textContent = af.last_recompute
    ? new Date(af.last_recompute * 1000).toLocaleString('zh-CN')
    : '—';

  // 概览卡片
  const top = af.top || [];
  const fallback = af.fallback || [];
  const total = top.length + fallback.length;
  const cooling = top.filter(m => m.tier === 'cooling').length +
                  fallback.filter(m => m.tier === 'cooling').length;
  $('#autofreeSummary').innerHTML = `
    <div class="metric-card"><span class="metric-value">${top.length}</span><span class="metric-label">Top${af.top_n}</span></div>
    <div class="metric-card"><span class="metric-value">${fallback.length}</span><span class="metric-label">Fallback</span></div>
    <div class="metric-card"><span class="metric-value">${cooling}</span><span class="metric-label">冷却中</span></div>
    <div class="metric-card"><span class="metric-value">${total}</span><span class="metric-label">总榜单</span></div>
  `;

  // 表格
  const all = [...top, ...fallback];
  const tbody = $('#autofreeTbody');
  tbody.innerHTML = '';
  all.forEach((m, idx) => {
    const row = document.createElement('tr');
    row.dataset.provider = m.provider;
    row.dataset.model = m.model;
    row.className = `tier-${m.tier || 'top10'}`;
    row.innerHTML = `
      <td class="rank">${m.rank || idx + 1}</td>
      <td class="model-name"><span class="model-title">${m.model}</span></td>
      <td><span class="provider-badge kind-${providerKind(m.provider)}">${providerDisplayName(m.provider)}</span></td>
      <td><span class="tier-badge tier-${m.tier || 'top10'}">${tierLabel(m.tier)}</span></td>
      <td class="score-final">${m.final_score?.toFixed(2) || '—'}</td>
      <td class="score-static">${m.static_score?.toFixed(2) || '—'}</td>
      <td class="score-runtime">${m.runtime_score?.toFixed(2) || '—'}</td>
      <td class="success-rate">${pct(m.success_rate)}</td>
      <td class="latency">${m.avg_latency_ms ? (m.avg_latency_ms.toFixed(0) + 'ms') : '—'}</td>
      <td class="requests">${m.requests_total || 0}</td>
      <td><span class="status-chip ${m.tier || 'top10'}">${tierLabel(m.tier)}</span></td>
      <td class="actions">
        <button class="icon-btn" title="强制刷新该模型统计" onclick="refreshAutofreeModel('${m.provider}', '${m.model}')">🔄</button>
        <button class="icon-btn" title="标记收藏" onclick="toggleAutofreeFavorite('${m.provider}', '${m.model}')">⭐</button>
      </td>
    `;
    tbody.appendChild(row);
  });
}

function tierLabel(tier) {
  const labels = { top10: '🏆 Top10', fallback: '🛡 Fallback', cooling: '❄️ 冷却', recovered: '🔄 恢复', none: '—' };
  return labels[tier] || tier;
}

function pct(rate) {
  if (!rate && rate !== 0) return '—';
  return (rate * 100).toFixed(1) + '%';
}

async function refreshAutofreeRanking(force = false) {
  try {
    showToast(force ? '强制重算中...' : '重算榜单中...', 'info');
    await api('/autofree/recompute', { method: 'POST', body: { force } });
    await loadAutofree();
    renderAutofree();
    showToast(force ? '强制重算完成' : '榜单已重算', 'success');
  } catch (e) { showToast('重算失败: ' + e.message, 'danger'); }
}

async function refreshAutofreeModel(provider, model) {
  try {
    showToast(`刷新 ${provider}/${model} 统计...`, 'info');
    await api('/autofree/refresh-model', { method: 'POST', body: { provider, model } });
    await loadAutofree();
    renderAutofree();
    showToast('统计已刷新', 'success');
  } catch (e) { showToast('刷新失败: ' + e.message, 'danger'); }
}

async function toggleAutofreeFavorite(provider, model) {
  try {
    await api('/autofree/favorite', { method: 'POST', body: { provider, model } });
    await loadAutofree();
    renderAutofree();
    showToast('收藏已切换', 'success');
  } catch (e) { showToast('操作失败: ' + e.message, 'danger'); }
}

$('#btnAutofreeRefresh')?.addEventListener('click', () => refreshAutofreeRanking(false));
$('#btnAutofreeForceRefresh')?.addEventListener('click', () => refreshAutofreeRanking(true));

// 在 renderAll() 里调用 renderAutofree()
const _originalRenderAll = renderAll;
function renderAll() {
  _originalRenderAll();
  renderAutofree();
}
async function init() {
  console.log('[free-router] Initializing...');
  applyTheme();
  initTips(); // 悬停说明

  // 模态框背景点击关闭
  $('#modalLayer')?.querySelector('.modal-backdrop')?.addEventListener('click', hideModal);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hideModal(); });

  try {
    await loadDashboard();
    await loadModels();
    await loadPolicy();
    await loadCache();
    await loadKeys();
    await loadFavorites();
    await loadNotifications();
    initCatalogControls();
    renderAll();
    initScrollButtons(); // 初始化滚动按钮
    startNotifyPolling();
    console.log('[free-router] Initialization complete');
  } catch (e) {
    console.error('[free-router] 初始化失败:', e);
    showToast(`初始化失败: ${e.message}`, 'danger');
  }
}

// DOM ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}

// ============ 模型通知弹窗系统 ============
let notificationQueue = [];
let notificationTimers = {};
let lastNotificationId = 0;

function startNotifyPolling() {
  // 每 30 秒检查一次新通知
  setInterval(async () => {
    try {
      const data = await api('/admin/notifications?limit=50');
      if (data && data.events) {
        const newEvents = data.events.filter(e => e.id > lastNotificationId);
        if (newEvents.length > 0) {
          lastNotificationId = Math.max(...newEvents.map(e => e.id));
          newEvents.forEach(showNotificationPopup);
        }
      }
    } catch (e) {
      console.warn('[free-router] 通知轮询失败:', e);
    }
  }, 30000);
}

function showNotificationPopup(event) {
  const container = document.getElementById('notificationContainer');
  if (!container) return;

  const popup = document.createElement('div');
  popup.className = `notification-popup type-${event.type}`;
  popup.innerHTML = `
    <div class="notification-popup-header">
      <span class="notification-popup-icon">${getNotificationIcon(event.type)}</span>
      <span class="notification-popup-title">${escapeHtml(event.title)}</span>
      <button class="notification-popup-close" onclick="closeNotificationPopup(this)">×</button>
    </div>
    <div class="notification-popup-body">
      <div class="notification-popup-model">${escapeHtml(event.model || '')}</div>
      <div class="notification-popup-provider">${escapeHtml(event.provider || '')}</div>
      <div class="notification-popup-detail">${escapeHtml(event.detail || '')}</div>
      <div class="notification-popup-time">${formatTime(event.timestamp)}</div>
    </div>
    <div class="notification-popup-footer">
      <button class="btn primary small" onclick="expandNotification(${event.id}, this)">查看详情</button>
    </div>
  `;

  container.appendChild(popup);

  // 5 秒后自动关闭
  const timer = setTimeout(() => {
    closeNotificationPopup(popup.querySelector('.notification-popup-close'));
  }, 5000);
  notificationTimers[event.id] = timer;

  // 鼠标悬停时暂停自动关闭
  popup.addEventListener('mouseenter', () => {
    clearTimeout(notificationTimers[event.id]);
  });
  popup.addEventListener('mouseleave', () => {
    notificationTimers[event.id] = setTimeout(() => {
      closeNotificationPopup(popup.querySelector('.notification-popup-close'));
    }, 5000);
  });
}

function closeNotificationPopup(btn) {
  const popup = btn.closest('.notification-popup');
  if (!popup) return;
  const eventId = parseInt(popup.querySelector('.notification-popup-footer button').onclick.toString().match(/expandNotification\((\d+)/)?.[1]);
  if (eventId) clearTimeout(notificationTimers[eventId]);
  popup.style.animation = 'slideOut 0.3s ease';
  setTimeout(() => popup.remove(), 300);
}

function expandNotification(eventId, btn) {
  const popup = btn.closest('.notification-popup');
  if (!popup) return;

  // 从 API 获取详情
  api(`/admin/notifications?ids=${eventId}&limit=1`).then(data => {
    const event = data.events?.[0];
    if (!event) return;

    const modal = document.getElementById('notificationModal');
    const modalTitle = document.getElementById('notificationModalTitle');
    const modalBody = document.getElementById('notificationModalBody');

    modalTitle.textContent = event.title || '通知详情';
    modalBody.innerHTML = `
      <div class="notification-detail-row">
        <span class="notification-detail-label">类型</span>
        <span class="notification-detail-value">${getNotificationIcon(event.type)} ${event.type}</span>
      </div>
      <div class="notification-detail-row">
        <span class="notification-detail-label">模型</span>
        <span class="notification-detail-value">${escapeHtml(event.model || '—')}</span>
      </div>
      <div class="notification-detail-row">
        <span class="notification-detail-label">提供商</span>
        <span class="notification-detail-value">${escapeHtml(event.provider || '—')}</span>
      </div>
      <div class="notification-detail-row">
        <span class="notification-detail-label">时间</span>
        <span class="notification-detail-value">${formatTime(event.timestamp)}</span>
      </div>
      <div class="notification-detail-row">
        <span class="notification-detail-label">详情</span>
        <span class="notification-detail-value">${escapeHtml(event.detail || '—')}</span>
      </div>
      ${event.metadata ? `
      <div class="notification-detail-row">
        <span class="notification-detail-label">元数据</span>
        <span class="notification-detail-value"><pre>${escapeHtml(JSON.stringify(event.metadata, null, 2))}</pre></span>
      </div>
      ` : ''}
    `;

    modal.style.display = 'flex';
    closeNotificationPopup(btn);
  }).catch(e => {
    console.error('[free-router] 获取通知详情失败:', e);
    showToast('获取通知详情失败', 'danger');
  });
}

function getNotificationIcon(type) {
  const icons = {
    'added': '🆕',
    'updated': '🔄',
    'removed': '🗑️',
    'deprecated': '⚠️',
    'model_added': '🆕',
    'model_updated': '🔄',
    'model_removed': '🗑️',
  };
  return icons[type] || '📢';
}

function formatTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit'
  });
}

function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

// 关闭模态框
document.getElementById('notificationModalClose')?.addEventListener('click', () => {
  document.getElementById('notificationModal').style.display = 'none';
});

// 点击模态框背景关闭
document.getElementById('notificationModal')?.addEventListener('click', (e) => {
  if (e.target.id === 'notificationModal') {
    document.getElementById('notificationModal').style.display = 'none';
  }
});