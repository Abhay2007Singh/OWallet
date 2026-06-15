import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { walletApi } from '@/api/wallet'
import { Card } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { SkeletonRow } from '@/components/ui/Skeleton'
import { EmptyState } from '@/components/ui/EmptyState'
import { TransactionItem } from '@/components/shared/TransactionItem'
import type { TransactionFilters, TransactionStatus } from '@/types/wallet'

const STATUS_OPTIONS: Array<{ label: string; value: TransactionStatus | '' }> = [
  { label: 'All', value: '' },
  { label: 'Completed', value: 'completed' },
  { label: 'Pending', value: 'pending' },
  { label: 'Failed', value: 'failed' },
  { label: 'Reversed', value: 'reversed' },
]

export function TransactionsPage() {
  const [page, setPage] = useState(1)
  const [statusFilter, setStatusFilter] = useState<TransactionStatus | ''>('')

  const filters: TransactionFilters = {
    page,
    page_size: 20,
    status: statusFilter || undefined,
  }

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['wallet', 'transactions', filters],
    queryFn: () => walletApi.getTransactions(filters),
    placeholderData: (prev) => prev,
  })

  const transactions = data?.items ?? []
  const pagination = data?.pagination

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Transaction History</h1>
        <p className="text-sm text-slate-500">All your wallet activity in one place</p>
      </div>

      {/* Filters */}
      <Card className="p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-slate-500 mr-1">Status:</span>
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => { setStatusFilter(opt.value as TransactionStatus | ''); setPage(1) }}
              className={`rounded-full px-3 py-1 text-xs font-medium transition-colors ${
                statusFilter === opt.value
                  ? 'bg-indigo-600 text-white'
                  : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </Card>

      {/* Transactions List */}
      <Card>
        {pagination && (
          <div className="border-b border-slate-100 px-4 py-3 text-xs text-slate-500">
            {pagination.total} transaction{pagination.total !== 1 ? 's' : ''}
          </div>
        )}

        {isLoading ? (
          <div className="divide-y divide-slate-50">
            {[...Array(6)].map((_, i) => <SkeletonRow key={i} />)}
          </div>
        ) : transactions.length === 0 ? (
          <EmptyState
            title="No transactions found"
            description={statusFilter ? `No ${statusFilter} transactions` : 'Your transactions will appear here'}
          />
        ) : (
          <div className={`divide-y divide-slate-50 ${isFetching ? 'opacity-60' : ''}`}>
            {transactions.map((tx) => (
              <TransactionItem key={tx.id} tx={tx} />
            ))}
          </div>
        )}

        {/* Pagination */}
        {pagination && pagination.pages > 1 && (
          <div className="flex items-center justify-between border-t border-slate-100 px-4 py-3">
            <Button
              variant="secondary"
              size="sm"
              disabled={!pagination.has_prev}
              onClick={() => setPage((p) => p - 1)}
            >
              Previous
            </Button>
            <span className="text-xs text-slate-500">
              Page {pagination.page} of {pagination.pages}
            </span>
            <Button
              variant="secondary"
              size="sm"
              disabled={!pagination.has_next}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        )}
      </Card>
    </div>
  )
}
