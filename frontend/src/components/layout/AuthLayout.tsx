import { Outlet } from 'react-router-dom'

export function AuthLayout() {
  return (
    <div className="min-h-screen bg-gradient-to-br from-indigo-50 via-white to-slate-50 flex flex-col items-center justify-center px-4">
      <div className="mb-8 flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-600 shadow-md">
          <svg className="h-6 w-6 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
            <rect x="2" y="7" width="20" height="14" rx="3" strokeLinejoin="round"/>
            <path d="M2 11h20" strokeLinecap="round"/>
            <rect x="15" y="13.5" width="5" height="4" rx="1.5" fill="currentColor" stroke="none"/>
          </svg>
        </div>
        <span className="text-2xl font-bold text-slate-900">OWallet</span>
      </div>
      <div className="w-full max-w-md">
        <Outlet />
      </div>
    </div>
  )
}
