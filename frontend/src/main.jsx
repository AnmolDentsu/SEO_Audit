import React from 'react'

import {
  createRoot,
} from 'react-dom/client'

import App from './App.jsx'
import AuthGate from './AuthGate.jsx'

import './styles.css'
import './auth.css'


createRoot(
  document.getElementById('root')
).render(
  <React.StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </React.StrictMode>
)