import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/bridge.css'
import './styles/conductor.css'
import './styles/main.css'
import './styles/setup.css'
import './styles/overview.css'
import './styles/settings.css'
import './styles/glass.css'   // K-054: the glass layer loads last
import App from './App.tsx'
import { lang } from './i18n'

document.documentElement.lang = lang

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
