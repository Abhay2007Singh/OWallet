import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { useAuthStore } from '@/store/authStore'
import { authApi } from '@/api/auth'
import { Card } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Alert } from '@/components/ui/Alert'

export function SettingsPage() {
  const navigate = useNavigate()
  const { user, logout } = useAuthStore()
  const [loggingOut, setLoggingOut] = useState(false)
  const [msg, setMsg] = useState('')

  async function handleLogout() {
    setLoggingOut(true)
    try {
      await authApi.logout()
    } catch {
      // still clear local state
    }
    logout()
    navigate('/login')
  }

  return (
    <div className="space-y-6 max-w-xl">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Settings</h1>
        <p className="text-sm text-slate-500">Manage your account preferences</p>
      </div>

      {msg && <Alert variant="success">{msg}</Alert>}

      {/* Account Info */}
      <Card className="p-6">
        <h2 className="mb-4 text-sm font-semibold text-slate-700 uppercase tracking-wide">Account</h2>
        <div className="space-y-3">
          <div className="flex items-center justify-between py-2 border-b border-slate-100">
            <div>
              <p className="text-sm font-medium text-slate-800">Email address</p>
              <p className="text-xs text-slate-500">{user?.email}</p>
            </div>
            <Badge label="Primary" />
          </div>
          <div className="flex items-center justify-between py-2 border-b border-slate-100">
            <div>
              <p className="text-sm font-medium text-slate-800">Account status</p>
              <p className="text-xs text-slate-500">{user?.is_active ? 'Active' : 'Disabled'}</p>
            </div>
          </div>
          <div className="flex items-center justify-between py-2">
            <div>
              <p className="text-sm font-medium text-slate-800">Verification</p>
              <p className="text-xs text-slate-500">{user?.is_verified ? 'Email verified' : 'Email not verified'}</p>
            </div>
          </div>
        </div>
      </Card>

      {/* API Info */}
      <Card className="p-6">
        <h2 className="mb-4 text-sm font-semibold text-slate-700 uppercase tracking-wide">Technical</h2>
        <div className="space-y-2 text-xs text-slate-500 font-mono bg-slate-50 rounded-lg p-3">
          <p>API: {import.meta.env.VITE_API_BASE_URL}</p>
          <p>JWT: Access token (15 min) + Refresh (7 days)</p>
          <p>Payments: Idempotency-Key per request</p>
        </div>
      </Card>

      {/* Danger Zone */}
      <Card className="p-6 border-red-100">
        <h2 className="mb-4 text-sm font-semibold text-red-600 uppercase tracking-wide">Session</h2>
        <p className="text-sm text-slate-500 mb-4">
          Signing out will invalidate your refresh token. You'll need to log in again.
        </p>
        <Button
          variant="danger"
          onClick={handleLogout}
          loading={loggingOut}
        >
          Sign out of all sessions
        </Button>
      </Card>
    </div>
  )
}

function Badge({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
      {label}
    </span>
  )
}
