import React from 'react'
import ReactDOM from 'react-dom/client'
import { HashRouter } from 'react-router-dom'
import { ConfigProvider } from 'antd'
import { theme as antdTheme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import jaJP from 'antd/locale/ja_JP'
import koKR from 'antd/locale/ko_KR'
import esES from 'antd/locale/es_ES'
import ptBR from 'antd/locale/pt_BR'
import ruRU from 'antd/locale/ru_RU'
import frFR from 'antd/locale/fr_FR'
import { useTranslation } from 'react-i18next'
import i18n from './i18n'
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn'
import 'dayjs/locale/en'
import 'dayjs/locale/ja'
import 'dayjs/locale/ko'
import 'dayjs/locale/es'
import 'dayjs/locale/pt-br'
import 'dayjs/locale/ru'
import 'dayjs/locale/fr'
import relativeTime from 'dayjs/plugin/relativeTime'
import timezone from 'dayjs/plugin/timezone'
import utc from 'dayjs/plugin/utc'
import App from './App.tsx'
import ErrorBoundary from './components/ErrorBoundary'
import { ThemeProvider, useTheme } from './context/ThemeContext'
import { initAnalytics } from './analytics/posthog'
import './analytics/flags'
import { trackLaunch } from './analytics/lifecycle'
import { installDomTranslationGuard } from './utils/domTranslationGuard'
import { initSentry } from './desktop/sentry'
import './index.css'

// 必须在 React 挂载前装好：浏览器网页翻译会移动文本节点，否则 React 更新时整页崩（#100）
installDomTranslationGuard()

initSentry()
// 初始化产品分析 / 埋点（无 key 时自动 no-op，不发任何网络请求）
initAnalytics()
// 注册全局属性 + 上报启动/安装/更新事件
void trackLaunch()

// 配置dayjs插件
dayjs.extend(relativeTime)
dayjs.extend(timezone)
dayjs.extend(utc)

const antdLocales = { zh: zhCN, en: enUS, ja: jaJP, ko: koKR, es: esES, pt: ptBR, ru: ruRU, fr: frFR }
const dateLocales: Record<string, string> = { zh: 'zh-cn', pt: 'pt-br' }
const updateDateLocale = () => dayjs.locale(dateLocales[i18n.language] || i18n.language)
updateDateLocale()
i18n.on('languageChanged', updateDateLocale)

// Native tray follows the same resolved language as the React interface.
const updateTrayLanguage = async () => {
  if (!('__TAURI_INTERNALS__' in window) && !('__TAURI__' in window)) return
  try {
    const { invoke } = await import('@tauri-apps/api/core')
    await invoke('set_tray_language', { language: i18n.language })
  } catch (error) { console.warn('Could not update tray language', error) }
}
void updateTrayLanguage()
i18n.on('languageChanged', () => { void updateTrayLanguage() })

function Root() {
  // 统一在根节点接入错误边界，避免运行时异常导致白屏
  return (
    <ErrorBoundary showDetails={import.meta.env.DEV}>
      <App />
    </ErrorBoundary>
  )
}

function ThemedApp() {
  useTranslation()
  const { theme } = useTheme()
  const isDark = theme === 'dark'

  return (
    <ConfigProvider
      locale={antdLocales[i18n.language as keyof typeof antdLocales] || enUS}
      theme={{
      algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
      token: {
        // Krepling Editorial Minimal tokens — see frontend/DESIGN.md
        colorPrimary: isDark ? '#A78BFA' : '#8B5CF6',
        colorText: isDark ? '#ECEAE6' : '#0A0A0A',
        colorTextSecondary: isDark ? '#C4C2BC' : '#3F3F46',
        colorBgBase: isDark ? '#19181A' : '#FAFAF8',
        colorBgContainer: isDark ? '#211F22' : '#FFFFFF',
        colorBorder: isDark ? '#2C2A2D' : '#E7E5E4',
        colorBorderSecondary: isDark ? '#232124' : '#F0EEEA',
        borderRadius: 12,
        fontFamily: '"Inter","Geist","PingFang SC","Noto Sans SC",system-ui,-apple-system,sans-serif',
        controlHeight: 38,
      },
      components: {
        Button: { borderRadius: 999, controlHeight: 40, fontWeight: 500, primaryShadow: 'none' },
        Select: { borderRadius: 12 },
        Card: { borderRadiusLG: 20 },
      },
    }}
    >
    <React.StrictMode>
      <HashRouter>
        <Root />
      </HashRouter>
    </React.StrictMode>
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <ThemeProvider>
    <ThemedApp />
  </ThemeProvider>,
)
