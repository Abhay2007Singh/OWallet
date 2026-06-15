import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/store/authStore'
import { authApi } from '@/api/auth'

const navLinks = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/wallet', label: 'Wallet' },
  { to: '/transfer', label: 'Transfer' },
  { to: '/transactions', label: 'History' },
]

export function Navbar() {
  const location = useLocation()
  const navigate = useNavigate()
  const { user, logout } = useAuthStore()

  async function handleLogout() {
    try {
      await authApi.logout()
    } catch {
      // ignore — still clear local state
    }
    logout()
    navigate('/login')
  }

  return (
    <header className="sticky top-0 z-40 border-b border-slate-100 bg-white/95 backdrop-blur">
      <nav className="mx-auto flex max-w-7xl items-center justify-between px-4 py-3 sm:px-6">
        {/* Logo */}
        <Link to="/dashboard" className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-indigo-600">
            <svg className="h-5 w-5 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.8">
              <rect x="2" y="7" width="20" height="14" rx="3" strokeLinejoin="round"/>
              <path d="M2 11h20" strokeLinecap="round"/>
              <rect x="15" y="13.5" width="5" height="4" rx="1.5" fill="currentColor" stroke="none"/>
            </svg>
          </div>
          <span className="text-base font-bold text-slate-900">OWallet</span>
        </Link>

        {/* Nav Links — desktop */}
        <div className="hidden sm:flex items-center gap-1">
          {navLinks.map((link) => (
            <Link
              key={link.to}
              to={link.to}
              className={`rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                location.pathname === link.to
                  ? 'bg-indigo-50 text-indigo-700'
                  : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900'
              }`}
            >
              {link.label}
            </Link>
          ))}
        </div>

        {/* Right side */}
        <div className="flex items-center gap-2">
          <Link
            to="/profile"
            className="hidden sm:flex items-center gap-2 rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-slate-100 transition-colors"
          >
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-indigo-100 text-indigo-600 font-semibold text-xs">
              {user?.full_name?.[0]?.toUpperCase() ?? 'U'}
            </div>
            <span className="hidden md:block truncate max-w-[100px]">{user?.full_name}</span>
          </Link>
          <button
            onClick={handleLogout}
            className="rounded-lg px-3 py-2 text-sm text-slate-600 hover:bg-red-50 hover:text-red-600 transition-colors"
          >
            Sign out
          </button>
        </div>
      </nav>

      {/* Bottom nav — mobile */}
      <div className="flex sm:hidden border-t border-slate-100 bg-white">
        {navLinks.map((link) => (
          <Link
            key={link.to}
            to={link.to}
            className={`flex-1 py-2 text-center text-xs font-medium transition-colors ${
              location.pathname === link.to ? 'text-indigo-600' : 'text-slate-500'
            }`}
          >
            {link.label}
          </Link>
        ))}
      </div>
    </header>
  )
}
