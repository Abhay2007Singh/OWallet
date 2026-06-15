import { useQuery } from '@tanstack/react-query'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/store/authStore'
import { Card } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import { formatDate } from '@/utils/formatters'

export function ProfilePage() {
  const setUser = useAuthStore((s) => s.setUser)

  const { data, isLoading } = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: async () => {
      const res = await authApi.me()
      setUser(res.user)
      return res.user
    },
  })

  const user = data

  return (
    <div className="space-y-6 max-w-xl">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Profile</h1>
        <p className="text-sm text-slate-500">Your account information</p>
      </div>

      <Card className="p-6">
        {/* Avatar */}
        <div className="flex items-center gap-4 mb-6">
          <div className="flex h-16 w-16 items-center justify-center rounded-full bg-indigo-100 text-indigo-600 text-2xl font-bold">
            {user?.full_name?.[0]?.toUpperCase() ?? '?'}
          </div>
          <div>
            {isLoading ? (
              <Skeleton className="h-5 w-36 mb-1" />
            ) : (
              <h2 className="text-lg font-semibold text-slate-900">{user?.full_name}</h2>
            )}
            <div className="flex items-center gap-2 mt-1">
              <Badge variant={user?.is_active ? 'green' : 'red'}>
                {user?.is_active ? 'Active' : 'Inactive'}
              </Badge>
              <Badge variant={user?.is_verified ? 'blue' : 'yellow'}>
                {user?.is_verified ? 'Verified' : 'Unverified'}
              </Badge>
              <Badge variant="slate">{user?.role}</Badge>
            </div>
          </div>
        </div>

        {/* Details */}
        <div className="divide-y divide-slate-100">
          {[
            { label: 'Email', value: user?.email },
            { label: 'Phone', value: user?.phone_number ?? 'Not set' },
            { label: 'Member since', value: user?.created_at ? formatDate(user.created_at) : undefined },
            { label: 'Last updated', value: user?.updated_at ? formatDate(user.updated_at) : undefined },
            { label: 'User ID', value: user?.id },
          ].map(({ label, value }) => (
            <div key={label} className="flex items-start justify-between py-3">
              <span className="text-sm text-slate-500 font-medium">{label}</span>
              {isLoading ? (
                <Skeleton className="h-4 w-36" />
              ) : (
                <span className="text-sm text-slate-800 text-right max-w-[60%] break-all font-mono">
                  {value ?? '—'}
                </span>
              )}
            </div>
          ))}
        </div>
      </Card>
    </div>
  )
}
