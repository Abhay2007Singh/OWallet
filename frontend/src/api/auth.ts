import { api } from './axios'
import type {
  LoginRequest,
  LoginResponse,
  LogoutResponse,
  MeResponse,
  RefreshRequest,
  RefreshResponse,
  RegisterRequest,
  RegisterResponse,
} from '@/types/auth'

export const authApi = {
  register: (body: RegisterRequest) =>
    api.post<RegisterResponse>('/auth/register', body).then((r) => r.data),

  login: (body: LoginRequest) =>
    api.post<LoginResponse>('/auth/login', body).then((r) => r.data),

  refresh: (body: RefreshRequest) =>
    api.post<RefreshResponse>('/auth/refresh', body).then((r) => r.data),

  logout: () =>
    api.post<LogoutResponse>('/auth/logout').then((r) => r.data),

  me: () =>
    api.get<MeResponse>('/auth/me').then((r) => r.data),
}
