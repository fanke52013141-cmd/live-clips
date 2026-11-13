import { useEffect } from 'react'
import { Routes, Route, useLocation } from 'react-router-dom'
import { Layout } from 'antd'
import HomePage from './pages/HomePage'
import ProjectDetailPage from './pages/ProjectDetailPage'
import PublishClipPage from './pages/PublishClipPage'
import PublishListPage from './pages/PublishListPage'
import PublishWeekPage from './pages/PublishWeekPage'
import ImportReview from './features/studio/ImportReview'
import StudioEditor from './features/studio/StudioEditor'
import SettingsPage from './pages/SettingsPage'
// SliceStudio 缝合点（UPSTREAM_PATCHES #2）：切片审核台/逐字稿路由
import StudioReviewPage from './pages/StudioReviewPage'
import StudioTranscriptPage from './pages/StudioTranscriptPage'
// SliceStudio 缝合点（UPSTREAM_PATCHES #3）：成片审阅/钩子工坊/合规发布路由
import StudioRenderPage from './pages/StudioRenderPage'
import StudioCompliancePage from './pages/StudioCompliancePage'
import SettingsReturnListener from './features/studio/SettingsReturnListener'
import KeyGuidePrompt from './features/settings/KeyGuidePrompt'
import QuitGuard from './features/studio/quitGuard'
import KeepAwakeGuard from './features/studio/KeepAwakeGuard'
import { startKeyGuideWatch } from './features/settings/keyGuideWatch'
import Header from './components/Header'
import { UpdateProvider } from './desktop/UpdatePrompt'
import { trackPageview } from './analytics/posthog'
import { startWorkflowObserver } from './analytics/observer'

const { Content } = Layout

// HashRouter 下手动上报 pageview（init 时已关闭自动 pageview）
function usePageviewTracking() {
  const location = useLocation()
  useEffect(() => {
    trackPageview(location.pathname + location.search)
  }, [location.pathname, location.search])
}

function App() {
  console.log('🎬 App组件已加载');
  usePageviewTracking()
  useEffect(() => startWorkflowObserver(), [])
  useEffect(() => startKeyGuideWatch(), [])

  return (
    <UpdateProvider>
      <Layout>
        <Header />
        <SettingsReturnListener />
        <KeyGuidePrompt />
        <QuitGuard />
        <KeepAwakeGuard />
        <Content>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/import/:id" element={<ImportReview />} />
            <Route path="/project/:id/studio/:draftId" element={<StudioEditor />} />
            <Route path="/project/:id/publish/week" element={<PublishWeekPage />} />
            <Route path="/project/:id/publish/:clipId" element={<PublishClipPage />} />
            <Route path="/project/:id/publish" element={<PublishListPage />} />
            <Route path="/project/:id" element={<ProjectDetailPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            {/* SliceStudio 缝合点（UPSTREAM_PATCHES #2） */}
            <Route path="/studio/review" element={<StudioReviewPage />} />
            <Route path="/studio/review/:id" element={<StudioReviewPage />} />
            <Route path="/studio/transcript/:id" element={<StudioTranscriptPage />} />
            {/* SliceStudio 缝合点（UPSTREAM_PATCHES #3） */}
            <Route path="/studio/render" element={<StudioRenderPage />} />
            <Route path="/studio/compliance" element={<StudioCompliancePage />} />
          </Routes>
        </Content>
      </Layout>
    </UpdateProvider>
  )
}

export default App
