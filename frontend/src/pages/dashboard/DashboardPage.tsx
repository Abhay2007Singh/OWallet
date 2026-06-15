import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { walletApi } from '@/api/wallet'
import { useAuthStore } from '@/store/authStore'
import { Card } from '@/components/ui/Card'
import { SkeletonCard, SkeletonRow } from '@/components/ui/Skeleton'
import { EmptyState } from '@/components/ui/EmptyState'
import { TransactionItem } from '@/components/shared/TransactionItem'
import { formatCurrency } from '@/utils/formatters'

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card className="p-5">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-bold text-slate-900">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-slate-400">{sub}</p>}
    </Card>
  )
}

export function DashboardPage() {
  const { user } = useAuthStore()

  const { data: balance, isLoading: loadingBalance } = useQuery({
    queryKey: ['wallet', 'balance'],
    queryFn: walletApi.getBalance,
    refetchInterval: 30_000,
  })

  const { data: txData, isLoading: loadingTx } = useQuery({
    queryKey: ['wallet', 'transactions', { page: 1, page_size: 5 }],
    queryFn: () => walletApi.getTransactions({ page: 1, page_size: 5 }),
  })

  const transactions = txData?.items ?? []

  const totalCredits = transactions
    .filter((t) => t.transaction_type === 'deposit' || t.transaction_type === 'credit')
    .reduce((sum, t) => sum + parseFloat(t.amount), 0)

  const totalDebits = transactions
    .filter((t) => t.transaction_type === 'debit' || t.transaction_type === 'withdraw')
    .reduce((sum, t) => sum + parseFloat(t.amount), 0)

  return (
    <div className="space-y-6">
      {/* Greeting */}
      <div>
        <h1 className="text-2xl font-bold text-slate-900">
          Good day, {user?.full_name?.split(' ')[0]} 👋
        </h1>
        <p className="text-sm text-slate-500">Here's your financial overview</p>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {loadingBalance ? (
          <>
            <SkeletonCard />
            <SkeletonCard />
            <SkeletonCard />
          </>
        ) : (
          <>
            <StatCard
              label="Current Balance"
              value={balance ? formatCurrency(balance.balance, balance.currency) : '—'}
              sub={balance?.cached ? 'Cached · updates every 30s' : 'Live balance'}
            />
            <StatCard
              label="Total Credits (recent)"
              value={formatCurrency(totalCredits)}
              sub="Deposits + received transfers"
            />
            <StatCard
              label="Total Debits (recent)"
              value={formatCurrency(totalDebits)}
              sub="Sent transfers"
            />
          </>
        )}
      </div>

      {/* Quick Actions */}
      <Card className="p-5">
        <h2 className="mb-4 text-sm font-semibold text-slate-700">Quick Actions</h2>
        <div className="flex flex-wrap gap-3">
          <Link
            to="/wallet"
            className="inline-flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 transition-colors"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6v6m0 0v6m0-6h6m-6 0H6" />
            </svg>
            Deposit
          </Link>
          <Link
            to="/transfer"
            className="inline-flex items-center gap-2 rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 transition-colors"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 8l4 4m0 0l-4 4m4-4H3" />
            </svg>
            Send Money
          </Link>
          <Link
            to="/transactions"
            className="inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium text-slate-600 hover:bg-slate-100 transition-colors"
          >
            View All Transactions
          </Link>
        </div>
      </Card>

      {/* Recent Transactions */}
      <Card>
        <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-700">Recent Transactions</h2>
          <Link to="/transactions" className="text-xs text-indigo-600 hover:text-indigo-700 font-medium">
            View all
          </Link>
        </div>

        {loadingTx ? (
          <div className="divide-y divide-slate-50">
            {[...Array(4)].map((_, i) => <SkeletonRow key={i} />)}
          </div>
        ) : transactions.length === 0 ? (
          <EmptyState
            title="No transactions yet"
            description="Make a deposit or transfer to get started"
            action={
              <Link
                to="/wallet"
                className="inline-flex items-center rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 transition-colors"
              >
                Make a deposit
              </Link>
            }
          />
        ) : (
          <div className="divide-y divide-slate-50">
            {transactions.map((tx) => (
              <TransactionItem key={tx.id} tx={tx} />
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
