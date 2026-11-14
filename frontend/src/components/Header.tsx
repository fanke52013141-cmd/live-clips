import { t } from '../i18n'
import { useTranslation } from 'react-i18next'
import React from 'react'
import LanguageSelect from './LanguageSelect'
import { Layout, Button } from 'antd'
import { SettingOutlined, BulbOutlined, MoonOutlined } from '@ant-design/icons'
import { useNavigate, useLocation } from 'react-router-dom'
import { useTheme } from '../context/ThemeContext'
import { UpdateToast, useAppUpdate } from '../desktop/UpdatePrompt'
import { Icon } from '../ui'
import { useKeepAwakeHeld } from '../features/studio/KeepAwakeGuard'

const { Header: AntHeader } = Layout

// Calm Premium header — see DESIGN.md
const Header: React.FC = () => {
  useTranslation()
  const navigate = useNavigate()
  const location = useLocation()
  const isSettings = location.pathname === '/settings'
  const { theme, toggleTheme } = useTheme()
  const appUpdate = useAppUpdate()
  const updatePending = appUpdate.phase === 'downloading' || appUpdate.phase === 'ready' || appUpdate.phase === 'failed' || appUpdate.phase === 'restarting'
  const keepAwakeHeld = useKeepAwakeHeld()

  return (
    <AntHeader
      style={{
        padding: '0 clamp(12px, 4vw, 56px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        height: '72px',
        lineHeight: 'normal',
        overflow: 'visible',
        position: 'sticky',
        top: 0,
        zIndex: 1000,
        backdropFilter: 'blur(10px)',
        background: 'color-mix(in srgb, var(--ac-bg) 78%, transparent)',
        borderBottom: '1px solid var(--ac-line-2)',
      }}
    >
      {/* Wordmark — Inter 粗体，Krepling 编辑感 */}
      <div
        style={{ display: 'flex', alignItems: 'center', cursor: 'pointer', gap: '10px' }}
        onClick={() => navigate('/')}
      >
        <span
          aria-hidden
          style={{
            width: '28px', height: '28px', borderRadius: '50%',
            background: 'var(--ac-ink)', color: 'var(--ac-cta-fg)',
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '13px', fontWeight: 700, letterSpacing: '-0.5px',
          }}
        >
          A
        </span>
        <span
          style={{
            fontFamily: 'var(--ac-font-sans)',
            fontSize: '19px',
            fontWeight: 700,
            color: 'var(--ac-ink)',
            letterSpacing: '-0.02em',
          }}
        >
          AutoClip
        </span>
      </div>

      {/* Right side */}
      <div className="ac-header-actions">
        {keepAwakeHeld && (
          <span
            className="ac-keep-awake-pill"
            title={t('制作中，已阻止休眠')}
            aria-label={t('制作中，已阻止休眠')}
            style={{
              fontSize: '12px',
              color: 'var(--ac-sub)',
              border: '1px solid var(--ac-line)',
              borderRadius: '999px',
              padding: '4px 10px',
              background: 'var(--ac-card)',
              whiteSpace: 'nowrap',
              maxWidth: '42vw',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
            }}
          >{t('制作中，已阻止休眠')}</span>
        )}
        {updatePending && (
          <div className="ac-update-anchor">
            <button
              type="button"
              className="ac-update-icon"
              aria-expanded={appUpdate.toastVisible}
              aria-label={t('有新版本可用')}
              onClick={() => appUpdate.toastVisible ? appUpdate.snooze() : appUpdate.openUpdate()}
            >
              <Icon.Up size={16} />
            </button>
            <UpdateToast />
          </div>
        )}
        <div className="ac-header-permanent">
        <LanguageSelect />
        {/* 返回入口由各页面页头承担（见 DESIGN.md App Layer），顶栏只留全局动作 */}
        <Button
          type="text"
          icon={theme === 'dark' ? <BulbOutlined /> : <MoonOutlined />}
          onClick={toggleTheme}
          aria-label={theme === 'dark' ? t("切换到亮色模式") : t("切换到暗色模式")}
          title={theme === 'dark' ? t("切换到亮色模式") : t("切换到暗色模式")}
          style={{
            color: 'var(--ac-sub)',
            border: '1px solid var(--ac-line)',
            borderRadius: '999px',
            width: '36px',
            height: '36px',
            padding: 0,
            background: 'var(--ac-card)',
          }}
        />
        <Button
          type="text"
          icon={<SettingOutlined />}
          onClick={() => navigate('/settings')}
          aria-current={isSettings ? 'page' : undefined}
          style={{
            color: isSettings ? 'var(--ac-accent)' : 'var(--ac-cta-fg)',
            borderRadius: '999px',
            height: '40px',
            padding: '0 18px',
            fontWeight: 500,
            background: isSettings ? 'var(--ac-sel)' : 'var(--ac-cta-bg)',
          }}
        >{t("设置")}</Button>
        </div>
      </div>
    </AntHeader>
  )
}

export default Header
