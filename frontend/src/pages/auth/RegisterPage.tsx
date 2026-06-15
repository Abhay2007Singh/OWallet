import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { Link, useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/store/authStore'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Card } from '@/components/ui/Card'
import { Alert } from '@/components/ui/Alert'
import { extractApiError } from '@/utils/formatters'

const schema = z.object({
  full_name: z.string().min(2, 'Name must be at least 2 characters').max(100),
  email: z.string().email('Enter a valid email'),
  password: z
    .string()
    .min(8, 'Password must be at least 8 characters')
    .regex(/[A-Za-z]/, 'Must contain at least one letter')
    .regex(/\d/, 'Must contain at least one number'),
  phone_number: z.string().optional(),
})

type FormData = z.infer<typeof schema>

export function RegisterPage() {
  const navigate = useNavigate()
  const setAuth = useAuthStore((s) => s.setAuth)
  const [apiError, setApiError] = useState('')

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<FormData>({ resolver: zodResolver(schema) })

  async function onSubmit(data: FormData) {
    setApiError('')
    try {
      const payload = {
        ...data,
        phone_number: data.phone_number || undefined,
      }
      const res = await authApi.register(payload)
      setAuth(res.user, res.tokens)
      navigate('/dashboard')
    } catch (err) {
      setApiError(extractApiError(err))
    }
  }

  return (
    <Card className="p-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-slate-900">Create your account</h1>
        <p className="mt-1 text-sm text-slate-500">Free to join. Get your wallet in seconds.</p>
      </div>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
        {apiError && <Alert variant="error">{apiError}</Alert>}

        <Input
          label="Full name"
          type="text"
          autoComplete="name"
          placeholder="Alice Smith"
          error={errors.full_name?.message}
          {...register('full_name')}
        />
        <Input
          label="Email"
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          error={errors.email?.message}
          {...register('email')}
        />
        <Input
          label="Password"
          type="password"
          autoComplete="new-password"
          placeholder="Min 8 chars, one letter + one digit"
          error={errors.password?.message}
          {...register('password')}
        />
        <Input
          label="Phone number (optional)"
          type="tel"
          autoComplete="tel"
          placeholder="+1234567890"
          error={errors.phone_number?.message}
          {...register('phone_number')}
        />

        <Button type="submit" loading={isSubmitting} className="w-full mt-2" size="lg">
          Create account
        </Button>
      </form>

      <p className="mt-6 text-center text-sm text-slate-500">
        Already have an account?{' '}
        <Link to="/login" className="font-medium text-indigo-600 hover:text-indigo-700">
          Sign in
        </Link>
      </p>
    </Card>
  )
}
