export const EXECUTOR_OPTIONS = [
  { value: 'protocol', label: '纯协议' },
  { value: 'headless', label: '无头浏览器' },
  { value: 'headed', label: '有头浏览器' },
] as const

const PLATFORM_EXECUTORS: Record<string, string[]> = {
  chatgpt: ['protocol', 'headless', 'headed'],
  cursor: ['protocol', 'headless', 'headed'],
  grok: ['protocol', 'headed', 'headless'],
  kiro: ['protocol', 'headless', 'headed'],
  deepseek: ['headless', 'headed'],
  nvidia: ['headless', 'headed'],
  cerebras: ['headless', 'headed'],
  tavily: ['protocol', 'headless', 'headed'],
  trae: ['protocol', 'headless', 'headed'],
  qwen: ['headless', 'headed'],
  openblocklabs: ['protocol'],
  github: ['headed', 'headless'],
  claude: ['headed', 'headless'],
  zai: ['headless', 'headed'],
}

export function getSupportedExecutors(platform?: string) {
  if (!platform) return ['protocol']
  return PLATFORM_EXECUTORS[platform] || ['protocol']
}

export function getExecutorOptions(platform?: string) {
  const supported = new Set(getSupportedExecutors(platform))
  return EXECUTOR_OPTIONS.filter((option) => supported.has(option.value))
}

export function normalizeExecutorForPlatform(platform: string | undefined, executor: string | undefined) {
  const supported = getSupportedExecutors(platform)
  if (executor && supported.includes(executor)) return executor
  return supported[0] || 'protocol'
}
