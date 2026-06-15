export type TransactionType = 'deposit' | 'withdraw' | 'transfer' | 'debit' | 'credit'
export type TransactionStatus = 'pending' | 'completed' | 'failed' | 'reversed'
export type WalletCurrency = 'USD' | 'EUR' | 'GBP'

export interface WalletBalanceResponse {
  wallet_id: string
  balance: string
  currency: WalletCurrency
  is_active: boolean
  cached: boolean
}

export interface Transaction {
  id: string
  wallet_id: string
  amount: string
  balance_before: string
  balance_after: string
  transaction_type: TransactionType
  status: TransactionStatus
  description: string | null
  external_reference: string | null
  counterpart_wallet_id: string | null
  transfer_reference_id: string | null
  created_at: string
  updated_at: string
}

export interface PaginationMeta {
  total: number
  page: number
  page_size: number
  pages: number
  has_next: boolean
  has_prev: boolean
}

export interface PaginatedTransactionResponse {
  items: Transaction[]
  pagination: PaginationMeta
}

export interface DepositRequest {
  amount: number
  description?: string
}

export interface DepositResponse {
  message: string
  transaction: Transaction
  new_balance: string
}

export interface TransferRequest {
  receiver_email: string
  amount: number
  description?: string
}

export interface TransferResponse {
  transfer_reference_id: string
  amount: string
  sender_new_balance: string
  debit_transaction_id: string
  timestamp: string
  message: string
}

export interface TransactionFilters {
  page?: number
  page_size?: number
  status?: TransactionStatus
  date_from?: string
  date_to?: string
}
