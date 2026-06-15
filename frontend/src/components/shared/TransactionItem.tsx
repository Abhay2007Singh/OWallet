import type { Transaction } from '@/types/wallet'
import { Badge } from '@/components/ui/Badge'
import { formatCurrency, formatDateShort } from '@/utils/formatters'

interface TransactionItemProps {
  tx: Transaction
}

const typeLabel: Record<string, string> = {
  deposit: 'Deposit',
  withdraw: 'Withdrawal',
  transfer: 'Transfer',
  debit: 'Sent',
  credit: 'Received',
}

const statusVariant = {
  completed: 'green',
  pending: 'yellow',
  failed: 'red',
  reversed: 'slate',
} as const

function TxIcon({ type }: { type: string }) {
  if (type === 'deposit' || type === 'credit') {
    return (
      <div className="flex h-10 w-10 items-center justify-center rounded-full bg-emerald-50 text-emerald-600">
        <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 11l5-5m0 0l5 5m-5-5v12" />
        </svg>
      </div>
    )
  }
  return (
    <div className="flex h-10 w-10 items-center justify-center rounded-full bg-red-50 text-red-500">
      <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 13l-5 5m0 0l-5-5m5 5V6" />
      </svg>
    </div>
  )
}

export function TransactionItem({ tx }: TransactionItemProps) {
  const isCredit = tx.transaction_type === 'deposit' || tx.transaction_type === 'credit'

  return (
    <div className="flex items-center gap-3 px-4 py-3 hover:bg-slate-50 transition-colors">
      <TxIcon type={tx.transaction_type} />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-slate-800 truncate">
          {typeLabel[tx.transaction_type] ?? tx.transaction_type}
        </p>
        <p className="text-xs text-slate-500 truncate">
          {tx.description ?? formatDateShort(tx.created_at)}
        </p>
      </div>
      <div className="flex flex-col items-end gap-1">
        <span className={`text-sm font-semibold ${isCredit ? 'text-emerald-600' : 'text-slate-800'}`}>
          {isCredit ? '+' : '-'}{formatCurrency(tx.amount)}
        </span>
        <Badge variant={statusVariant[tx.status] ?? 'slate'}>
          {tx.status}
        </Badge>
      </div>
    </div>
  )
}
