export type UserRole = 'user' | 'admin'

export interface User {
  id: string
  email: string
  full_name: string
  phone_number: string | null
  role: UserRole
  is_active: boolean
  is_verified: boolean
  created_at: string
  updated_at: string
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface RegisterRequest {
  email: string
  full_name: string
  password: string
  phone_number?: string
}

export interface LoginRequest {
  email: string
  password: string
}

export interface RefreshRequest {
  refresh_token: string
}

export interface RegisterResponse {
  message: string
  user: User
  tokens: TokenPair
}

export interface LoginResponse {
  message: string
  user: User
  tokens: TokenPair
}

export interface RefreshResponse {
  message: string
  tokens: TokenPair
}

export interface LogoutResponse {
  message: string
}

export interface MeResponse {
  user: User
}
