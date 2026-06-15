import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { walletApi } from '@/api/wallet'
import { Card } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Alert } from '@/components/ui/Alert'
import { SkeletonCard } from '@/components/ui/Skeleton'
import { formatCurrency, extractApiError } from '@/utils/formatters'
import type { TransferResponse } from '@/types/wallet'

const schema = z.object({
  receiver_email: z.string().email('Enter a valid recipient email'),
  amount: z
    .string()
    .min(1, 'Amount is required')
    .refine((v) => !isNaN(parseFloat(v)) && parseFloat(v) > 0, 'Amount must be positive')
    .refine((v) => parseFloat(v) <= 1_000_000, 'Maximum $1,000,000 per transfer'),
  description: z.string().max(255).optional(),
})

type FormData = z.infer<typeof schema>

export function TransferPage() {
  const queryClient = useQueryClient()
  const [receipt, setReceipt] = useState<TransferResponse | null>(null)
  const [errorMsg, setErrorMsg] = useState('')

  const { data: balance, isLoading: loadingBalance } = useQuery({
    queryKey: ['wallet', 'balance'],
    queryFn: walletApi.getBalance,
  })

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormData>({ resolver: zodResolver(schema) })

  const transferMutation = useMutation({
    mutationFn: (data: FormData) =>
      walletApi.transfer({
        receiver_email: data.receiver_email,
        amount: parseFloat(data.amount),
        description: data.description,
      }),
    onSuccess: (data) => {
      setReceipt(data)
      setErrorMsg('')
      reset()
      queryClient.invalidateQueries({ queryKey: ['wallet'] })
    },
    onError: (err) => {
      setErrorMsg(extractApiError(err))
      setReceipt(null)
    },
  })

  function onSubmit(data: FormData) {
    setErrorMsg('')
    setReceipt(null)
    transferMutation.mutate(data)
  }

  return (
    <div className="space-y-6 max-w-xl">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Send Money</h1>
        <p className="text-sm text-slate-500">Transfer funds to another OWallet user instantly</p>
      </div>

      {/* Available balance */}
      {loadingBalance ? (
        <SkeletonCard />
      ) : balance ? (
        <Card className="p-4 bg-slate-50">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500">Your balance</span>
            <span className="text-lg font-bold text-slate-900">
              {formatCurrency(balance.balance, balance.currency)}
            </span>
          </div>
        </Card>
      ) : null}

      {/* Transfer form */}
      <Card className="p-6">
        <h2 className="mb-4 text-base font-semibold text-slate-800">Transfer Details</h2>

        {errorMsg && <Alert variant="error" className="mb-4">{errorMsg}</Alert>}

        {receipt && (
          <Alert variant="success" className="mb-4">
            <p className="font-semibold">Transfer complete!</p>
            <p className="text-xs mt-1">Amount: {formatCurrency(receipt.amount)}</p>
            <p className="text-xs">New balance: {formatCurrency(receipt.sender_new_balance)}</p>
            <p className="text-xs text-slate-500 mt-1 truncate">Ref: {receipt.transfer_reference_id}</p>
          </Alert>
        )}

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
          <Input
            label="Recipient email"
            type="email"
            placeholder="recipient@example.com"
            error={errors.receiver_email?.message}
            {...register('receiver_email')}
          />
          <Input
            label="Amount (USD)"
            type="number"
            step="0.01"
            min="0.01"
            placeholder="50.00"
            error={errors.amount?.message}
            {...register('amount')}
          />
          <Input
            label="Note (optional)"
            type="text"
            placeholder="Dinner, rent, gift…"
            error={errors.description?.message}
            {...register('description')}
          />

          <div className="rounded-lg bg-amber-50 border border-amber-200 px-3 py-2 text-xs text-amber-700">
            Rate limit: 5 transfers per minute. Transfers are instant and atomic.
          </div>

          <Button
            type="submit"
            loading={transferMutation.isPending}
            size="lg"
            className="w-full"
          >
            Send Money
          </Button>
        </form>
      </Card>
    </div>
  )
}
