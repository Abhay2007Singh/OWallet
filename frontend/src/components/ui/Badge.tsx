type BadgeVariant = 'green' | 'red' | 'yellow' | 'blue' | 'slate'

interface BadgeProps {
  variant?: BadgeVariant
  children: React.ReactNode
}

const styles: Record<BadgeVariant, string> = {
  green: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  red: 'bg-red-50 text-red-700 ring-red-200',
  yellow: 'bg-amber-50 text-amber-700 ring-amber-200',
  blue: 'bg-blue-50 text-blue-700 ring-blue-200',
  slate: 'bg-slate-100 text-slate-600 ring-slate-200',
}

export function Badge({ variant = 'slate', children }: BadgeProps) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${styles[variant]}`}>
      {children}
    </span>
  )
}
