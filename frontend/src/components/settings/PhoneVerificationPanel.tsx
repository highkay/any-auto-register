import { Alert, Card, Form, Input, Select, Switch, Tag, Typography } from 'antd'

export type PhoneVerificationProviderValue = 'auto' | 'smstome' | 'five_sim' | 'hero_sms' | 'free_sms_tool'

export const PHONE_VERIFICATION_PROVIDER_OPTIONS = [
  { label: '自动判断（推荐）', value: 'auto' },
  { label: 'SMSToMe', value: 'smstome' },
  { label: '5sim', value: 'five_sim' },
  { label: 'HeroSMS', value: 'hero_sms' },
  { label: 'Free SMS Tool', value: 'free_sms_tool' },
]

type PhoneFieldType = 'input' | 'select' | 'boolean'

interface PhoneFieldConfig {
  key: string
  label: string
  placeholder?: string
  type?: PhoneFieldType
  secret?: boolean
  help?: string
}

interface PhoneProviderSection {
  key: Exclude<PhoneVerificationProviderValue, 'auto'>
  title: string
  badge: string
  description: string
  accent: string
  cardBackground: string
  fields: PhoneFieldConfig[]
}

const PHONE_PROVIDER_SECTIONS: PhoneProviderSection[] = [
  {
    key: 'smstome',
    title: 'SMSToMe',
    badge: '本地池 / Cookie 同步',
    description: '适合本地维护号码池，支持 Cookie 自动同步和按国家筛选。',
    accent: '#d97706',
    cardBackground: 'linear-gradient(180deg, rgba(217, 119, 6, 0.12), rgba(255, 255, 255, 0.92) 64%)',
    fields: [
      { key: 'smstome_global_file', label: '号码池文件', placeholder: 'smstome_all_numbers.txt' },
      { key: 'smstome_used_numbers_dir', label: '已用号码目录', placeholder: 'smstome_used' },
      { key: 'smstome_task_name', label: '任务名', placeholder: 'chatgpt_add_phone' },
      { key: 'smstome_cookie', label: 'Cookie', secret: true },
      { key: 'smstome_country_slugs', label: '国家列表', placeholder: 'united-kingdom,poland' },
      { key: 'smstome_phone_attempts', label: '手机号尝试次数', placeholder: '3' },
      { key: 'smstome_otp_timeout_seconds', label: '短信等待秒数', placeholder: '45' },
      { key: 'smstome_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '5' },
      { key: 'smstome_sync_max_pages_per_country', label: '每国同步页数', placeholder: '5' },
    ],
  },
  {
    key: 'five_sim',
    title: '5sim',
    badge: '低价 / 地区策略',
    description: '适合按国家、运营商和产品做细粒度控制。',
    accent: '#2563eb',
    cardBackground: 'linear-gradient(180deg, rgba(37, 99, 235, 0.12), rgba(255, 255, 255, 0.92) 64%)',
    fields: [
      { key: 'five_sim_api_key', label: 'API Key', secret: true },
      { key: 'five_sim_product', label: '产品', placeholder: 'other / openai / wechat' },
      { key: 'five_sim_country', label: '国家', placeholder: '留空自动选最低价，也可填 netherlands / england' },
      { key: 'five_sim_operator', label: '运营商', placeholder: '留空自动选最低价，也可填 virtual59 / any' },
      { key: 'five_sim_max_price', label: '最高价格', placeholder: '留空按最低价国家当前报价' },
      { key: 'five_sim_phone_attempts', label: '手机号尝试次数', placeholder: '3' },
      { key: 'five_sim_otp_timeout_seconds', label: '短信等待秒数', placeholder: '120' },
      { key: 'five_sim_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '5' },
    ],
  },
  {
    key: 'hero_sms',
    title: 'HeroSMS',
    badge: '服务名 / 国家 / 价格',
    description: '适合按服务名和目标国家做精确取号。',
    accent: '#be123c',
    cardBackground: 'linear-gradient(180deg, rgba(190, 18, 60, 0.12), rgba(255, 255, 255, 0.92) 64%)',
    fields: [
      { key: 'hero_sms_api_key', label: 'API Key', secret: true },
      { key: 'hero_sms_service', label: '服务', placeholder: 'Kimi 或 ayz' },
      { key: 'hero_sms_country', label: '国家', placeholder: '留空自动选择最低价，也可填 16 / United Kingdom' },
      { key: 'hero_sms_operator', label: '运营商', placeholder: '可选，例如 any / vodafone' },
      { key: 'hero_sms_max_price', label: '最高价格', placeholder: '留空按最低价国家当前报价' },
      { key: 'hero_sms_phone_attempts', label: '手机号尝试次数', placeholder: '3' },
      { key: 'hero_sms_otp_timeout_seconds', label: '短信等待秒数', placeholder: '120' },
      { key: 'hero_sms_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '5' },
    ],
  },
  {
    key: 'free_sms_tool',
    title: 'Free SMS Tool',
    badge: '本地服务 / Claim',
    description: '适合接入本地 Free SMS Tool 服务，统一认领、轮询和释放。',
    accent: '#0f766e',
    cardBackground: 'linear-gradient(180deg, rgba(15, 118, 110, 0.12), rgba(255, 255, 255, 0.92) 64%)',
    fields: [
      { key: 'free_sms_tool_base_url', label: 'Base URL', placeholder: 'http://127.0.0.1:18000' },
      { key: 'free_sms_tool_api_key', label: 'API Key', secret: true },
      { key: 'free_sms_tool_app_slug', label: 'App Slug', placeholder: 'chatgpt / kimi' },
      { key: 'free_sms_tool_app_name', label: 'App Name', placeholder: 'ChatGPT / Kimi' },
      { key: 'free_sms_tool_country_name', label: '国家', placeholder: 'United Kingdom / Thailand' },
      { key: 'free_sms_tool_provider_id', label: 'Provider ID', placeholder: '留空自动挑选，如 receive_smss / sms24' },
      { key: 'free_sms_tool_claim_ttl_minutes', label: 'Claim TTL 分钟', placeholder: '10' },
      { key: 'free_sms_tool_include_cooling', label: '包含 cooling 号码', type: 'boolean' },
      { key: 'free_sms_tool_phone_attempts', label: '手机号尝试次数', placeholder: '3' },
      { key: 'free_sms_tool_otp_timeout_seconds', label: '短信等待秒数', placeholder: '120' },
      { key: 'free_sms_tool_poll_interval_seconds', label: '轮询间隔秒数', placeholder: '5' },
    ],
  },
]

function PhoneField({ field }: { field: PhoneFieldConfig }) {
  const options = field.type === 'select' && field.key === 'phone_verification_provider' ? PHONE_VERIFICATION_PROVIDER_OPTIONS : undefined
  const isBoolean = field.type === 'boolean'

  return (
    <Form.Item
      label={field.label}
      name={field.key}
      valuePropName={isBoolean ? 'checked' : undefined}
      extra={field.help}
      style={{ marginBottom: 12 }}
    >
      {options ? (
        <Select options={options} style={{ width: '100%' }} />
      ) : isBoolean ? (
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      ) : field.secret ? (
        <Input.Password placeholder={field.placeholder} />
      ) : (
        <Input placeholder={field.placeholder} />
      )}
    </Form.Item>
  )
}

function ProviderCard({
  provider,
  active,
}: {
  provider: PhoneProviderSection
  active: boolean
}) {
  return (
    <Card
      style={{
        borderRadius: 20,
        border: `1px solid ${active ? provider.accent : 'rgba(148, 163, 184, 0.18)'}`,
        boxShadow: active ? `0 18px 42px -30px ${provider.accent}` : '0 16px 40px -32px rgba(15, 23, 42, 0.24)',
        background: provider.cardBackground,
      }}
      styles={{ body: { padding: 20 } }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 12, marginBottom: 14 }}>
        <div>
          <Typography.Title level={4} style={{ margin: 0, marginBottom: 6 }}>
            {provider.title}
          </Typography.Title>
          <Typography.Text type="secondary">{provider.description}</Typography.Text>
        </div>
        <Tag color={active ? 'blue' : 'default'} style={{ margin: 0, alignSelf: 'flex-start' }}>
          {active ? '当前激活' : provider.badge}
        </Tag>
      </div>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
          gap: 12,
        }}
      >
        {provider.fields.map((field) => (
          <PhoneField key={field.key} field={field} />
        ))}
      </div>
    </Card>
  )
}

interface PhoneVerificationPanelProps {
  title?: string
  description?: string
}

export default function PhoneVerificationPanel({
  title = '手机验证控制台',
  description = '把全局 provider 和各家短信配置分开管理，避免混在一屏里难以排查。',
}: PhoneVerificationPanelProps) {
  const providerValue = String(Form.useWatch('phone_verification_provider') || 'auto')
  const selectedLabel =
    PHONE_VERIFICATION_PROVIDER_OPTIONS.find((item) => item.value === providerValue)?.label || '自动判断（推荐）'
  const selectedDescription =
    providerValue === 'auto'
      ? '自动判断会按后端可用性选择 provider；如果你明确知道当前链路，就直接手动指定。'
      : `当前选择 ${selectedLabel}，会覆盖自动判断逻辑。`

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <Card
        style={{
          borderRadius: 22,
          border: '1px solid rgba(37, 99, 235, 0.12)',
          background: 'linear-gradient(135deg, rgba(37, 99, 235, 0.12), rgba(15, 23, 42, 0.02) 55%, rgba(255, 255, 255, 0.96))',
          boxShadow: '0 24px 70px -46px rgba(37, 99, 235, 0.65)',
        }}
        styles={{ body: { padding: 22 } }}
      >
        <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 16, marginBottom: 16 }}>
          <div>
            <Typography.Title level={3} style={{ margin: 0, marginBottom: 8 }}>
              {title}
            </Typography.Title>
            <Typography.Text type="secondary">{description}</Typography.Text>
          </div>
          <Tag color={providerValue === 'auto' ? 'geekblue' : 'blue'} style={{ margin: 0, height: 28, lineHeight: '28px' }}>
            {selectedLabel}
          </Tag>
        </div>
        <div style={{ display: 'grid', gap: 12, gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))' }}>
          <div>
            <Form.Item name="phone_verification_provider" label="手机服务" style={{ marginBottom: 0 }}>
              <Select options={PHONE_VERIFICATION_PROVIDER_OPTIONS} />
            </Form.Item>
          </div>
          <Alert showIcon type="info" message={selectedDescription} />
        </div>
      </Card>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
          gap: 16,
        }}
      >
        {PHONE_PROVIDER_SECTIONS.map((provider) => (
          <ProviderCard
            key={provider.key}
            provider={provider}
            active={providerValue === provider.key}
          />
        ))}
      </div>
    </div>
  )
}
