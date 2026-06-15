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
import { Badge } from '@/components/ui/Badge'
import { SkeletonCard } from '@/components/ui/Skeleton'
import { formatCurrency, extractApiError } from '@/utils/formatters'

const depositSchema = z.object({
  amount: z
    .string()
    .min(1, 'Amount is required')
    .refine((v) => !isNaN(parseFloat(v)) && parseFloat(v) > 0, 'Amount must be positive')
    .refine((v) => parseFloat(v) <= 1_000_000, 'Maximum $1,000,000 per deposit'),
  description: z.string().max(255).optional(),
})

type DepositForm = z.infer<typeof depositSchema>

export function WalletPage() {
  const queryClient = useQueryClient()
  const [successMsg, setSuccessMsg] = useState('')
  const [errorMsg, setErrorMsg] = useState('')

  const { data: balance, isLoading } = useQuery({
    queryKey: ['wallet', 'balance'],
    queryFn: walletApi.getBalance,
    refetchInterval: 30_000,
  })

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<DepositForm>({ resolver: zodResolver(depositSchema) })

  const depositMutation = useMutation({
    mutationFn: (data: DepositForm) =>
      walletApi.deposit({ amount: parseFloat(data.amount), description: data.description }),
    onSuccess: (data) => {
      setSuccessMsg(
        `Deposit received! New balance: ${formatCurrency(data.new_balance)}`,
      )
      setErrorMsg('')
      reset()
      queryClient.invalidateQueries({ queryKey: ['wallet'] })
    },
    onError: (err) => {
      setErrorMsg(extractApiError(err))
      setSuccessMsg('')
    },
  })

  function onDeposit(data: DepositForm) {
    setSuccessMsg('')
    setErrorMsg('')
    depositMutation.mutate(data)
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-slate-900">Wallet</h1>
        <p className="text-sm text-slate-500">Manage your balance and deposit funds</p>
      </div>

      {/* Balance Card */}
      {isLoading ? (
        <SkeletonCard />
      ) : balance ? (
        <Card className="p-6 bg-gradient-to-br from-indigo-600 to-indigo-700 text-white border-0">
          <div className="flex items-start justify-between">
            <div>
              <p className="text-sm text-indigo-200">Available Balance</p>
              <p className="mt-1 text-4xl font-bold">{formatCurrency(balance.balance, balance.currency)}</p>
              <p className="mt-1 text-xs text-indigo-300">{balance.currency} account</p>
            </div>
            <div className="flex flex-col items-end gap-2">
              <Badge variant={balance.is_active ? 'green' : 'red'}>
                {balance.is_active ? 'Active' : 'Frozen'}
              </Badge>
              {balance.cached && (
                <span className="text-xs text-indigo-300">Cached</span>
              )}
            </div>
          </div>
          <p className="mt-4 text-xs text-indigo-300 font-mono">
            ID: {balance.wallet_id.slice(0, 16)}...
          </p>
        </Card>
      ) : null}

      {/* Deposit Form */}
      <Card className="p-6">
        <h2 className="mb-4 text-base font-semibold text-slate-800">Deposit Funds</h2>

        {successMsg && <Alert variant="success" className="mb-4">{successMsg}</Alert>}
        {errorMsg && <Alert variant="error" className="mb-4">{errorMsg}</Alert>}

        <form onSubmit={handleSubmit(onDeposit)} className="space-y-4">
          <Input
            label="Amount (USD)"
            type="number"
            step="0.01"
            min="0.01"
            placeholder="100.00"
            error={errors.amount?.message}
            {...register('amount')}
          />
          <Input
            label="Description (optional)"
            type="text"
            placeholder="Salary, bank transfer…"
            error={errors.description?.message}
            {...register('description')}
          />
          <Button
            type="submit"
            loading={depositMutation.isPending}
            size="lg"
            className="w-full"
          >
            Deposit Funds
          </Button>
        </form>
        <p className="mt-3 text-xs text-slate-400">
          Deposits are processed in the background. Status may show Pending briefly.
        </p>
      </Card>
    </div>
  )
}
