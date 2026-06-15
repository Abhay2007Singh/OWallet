import { v4 as uuidv4 } from 'uuid'
import { api } from './axios'
import type {
  DepositRequest,
  DepositResponse,
  PaginatedTransactionResponse,
  Transaction,
  TransactionFilters,
  TransferRequest,
  TransferResponse,
  WalletBalanceResponse,
} from '@/types/wallet'

export const walletApi = {
  getBalance: () =>
    api.get<WalletBalanceResponse>('/wallet/balance').then((r) => r.data),

  getTransactions: (filters?: TransactionFilters) =>
    api
      .get<PaginatedTransactionResponse>('/wallet/transactions', { params: filters })
      .then((r) => r.data),

  getTransaction: (id: string) =>
    api.get<Transaction>(`/wallet/transactions/${id}`).then((r) => r.data),

  deposit: (body: DepositRequest) =>
    api
      .post<DepositResponse>('/wallet/deposit', body, {
        headers: { 'Idempotency-Key': uuidv4() },
      })
      .then((r) => r.data),

  transfer: (body: TransferRequest) =>
    api
      .post<TransferResponse>('/wallet/transfer', body, {
        headers: { 'Idempotency-Key': uuidv4() },
      })
      .then((r) => r.data),
}
