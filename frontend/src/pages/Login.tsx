import { useState, type FormEvent } from 'react'
import { Fingerprint, KeyRound, ShieldCheck } from 'lucide-react'
import { useAdminLogin } from '../api/admin'
import { isWebAuthnSupported, useLoginWithPasskey, usePasskeyStatus } from '../api/passkey'
import { useTotpLogin, useTotpStatus } from '../api/totp'

type Mode = 'choose' | 'password' | 'totp'

export default function Login() {
  const passkeyStatus = usePasskeyStatus()
  const totpStatus = useTotpStatus()
  const passkeyLogin = useLoginWithPasskey()
  const passkeyAvailable =
    isWebAuthnSupported() &&
    passkeyStatus.data?.enabled === true &&
    passkeyStatus.data.has_credentials === true
  const totpAvailable =
    totpStatus.data?.enabled === true && totpStatus.data.has_verified === true

  const [mode, setMode] = useState<Mode>('choose')
  const [error, setError] = useState<string | null>(null)

  function tryPasskey() {
    setError(null)
    passkeyLogin.mutate(undefined, {
      onError: (err: unknown) => {
        const msg = err instanceof Error ? err.message : 'Passkey 登录失败'
        setError(msg)
      },
    })
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center"
      style={{ background: 'var(--bg)' }}
    >
      <div
        className="w-[340px] p-8 rounded-lg flex flex-col gap-4"
        style={{ background: 'var(--bg-elevated)', border: '1px solid var(--border)' }}
      >
        <div>
          <div className="text-lg font-medium" style={{ color: 'var(--text)' }}>
            nous-engine
          </div>
          <div className="text-xs mt-1" style={{ color: 'var(--muted)' }}>
            管理员登录
          </div>
        </div>

        {mode === 'choose' && (
          <ChooseMethod
            passkeyAvailable={passkeyAvailable}
            totpAvailable={totpAvailable}
            passkeyPending={passkeyLogin.isPending}
            onPasskey={tryPasskey}
            onTotp={() => {
              setError(null)
              setMode('totp')
            }}
            onPassword={() => {
              setError(null)
              setMode('password')
            }}
          />
        )}
        {mode === 'password' && <PasswordForm onBack={() => setMode('choose')} />}
        {mode === 'totp' && <TotpForm onBack={() => setMode('choose')} />}

        {error && (
          <div className="text-xs" style={{ color: 'var(--error, #ef4444)' }}>
            {error}
          </div>
        )}
      </div>
    </div>
  )
}

function ChooseMethod({
  passkeyAvailable,
  totpAvailable,
  passkeyPending,
  onPasskey,
  onTotp,
  onPassword,
}: {
  passkeyAvailable: boolean
  totpAvailable: boolean
  passkeyPending: boolean
  onPasskey: () => void
  onTotp: () => void
  onPassword: () => void
}) {
  return (
    <div className="flex flex-col gap-2">
      {passkeyAvailable && (
        <BigButton
          icon={<Fingerprint size={16} />}
          label={passkeyPending ? '等待 Passkey 验证…' : '使用 Passkey 登录'}
          onClick={onPasskey}
          disabled={passkeyPending}
          primary
        />
      )}
      {totpAvailable && (
        <BigButton
          icon={<ShieldCheck size={16} />}
          label="使用动态码 (TOTP)"
          onClick={onTotp}
        />
      )}
      <BigButton icon={<KeyRound size={16} />} label="使用密码" onClick={onPassword} />
      {!passkeyAvailable && !totpAvailable && (
        <div className="text-[11px] mt-1" style={{ color: 'var(--muted)' }}>
          首次登录请用密码，进入后可在「设置」绑定 Passkey 或 TOTP。
        </div>
      )}
    </div>
  )
}

function PasswordForm({ onBack }: { onBack: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const login = useAdminLogin()

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    login.mutate(password, {
      onError: (err: unknown) => {
        const msg = err instanceof Error ? err.message : '登录失败'
        setError(msg.includes('401') ? '密码错误' : msg)
      },
    })
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3">
      <input
        type="password"
        autoFocus
        name="password"
        autoComplete="current-password"
        value={password}
        onChange={(e) => setPassword(e.target.value)}
        placeholder="管理员密码"
        className="w-full px-3 py-2 rounded text-sm outline-none"
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          color: 'var(--text)',
        }}
      />
      {error && (
        <div className="text-xs" style={{ color: 'var(--error, #ef4444)' }}>
          {error}
        </div>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onBack}
          className="px-3 py-2 rounded text-xs"
          style={{
            background: 'transparent',
            border: '1px solid var(--border)',
            color: 'var(--muted)',
          }}
        >
          返回
        </button>
        <button
          type="submit"
          disabled={login.isPending || !password}
          className="flex-1 px-3 py-2 rounded text-sm font-medium disabled:opacity-50"
          style={{ background: 'var(--accent)', color: 'var(--accent-fg, #fff)' }}
        >
          {login.isPending ? '登录中…' : '登录'}
        </button>
      </div>
    </form>
  )
}

function TotpForm({ onBack }: { onBack: () => void }) {
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const login = useTotpLogin()

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    login.mutate(code, {
      onError: (err: unknown) => {
        const msg = err instanceof Error ? err.message : '登录失败'
        setError(msg.includes('401') ? '动态码不匹配' : msg)
      },
    })
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3">
      <input
        autoFocus
        name="otp"
        autoComplete="one-time-code"
        inputMode="numeric"
        pattern="[0-9]*"
        maxLength={8}
        value={code}
        onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
        placeholder="6 位动态码"
        className="w-full px-3 py-2 rounded text-sm outline-none tracking-widest text-center"
        style={{
          background: 'var(--bg)',
          border: '1px solid var(--border)',
          color: 'var(--text)',
          letterSpacing: '0.3em',
        }}
      />
      {error && (
        <div className="text-xs" style={{ color: 'var(--error, #ef4444)' }}>
          {error}
        </div>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={onBack}
          className="px-3 py-2 rounded text-xs"
          style={{
            background: 'transparent',
            border: '1px solid var(--border)',
            color: 'var(--muted)',
          }}
        >
          返回
        </button>
        <button
          type="submit"
          disabled={login.isPending || code.length < 6}
          className="flex-1 px-3 py-2 rounded text-sm font-medium disabled:opacity-50"
          style={{ background: 'var(--accent)', color: 'var(--accent-fg, #fff)' }}
        >
          {login.isPending ? '验证中…' : '登录'}
        </button>
      </div>
    </form>
  )
}

function BigButton({
  icon,
  label,
  onClick,
  disabled,
  primary,
}: {
  icon: React.ReactNode
  label: string
  onClick: () => void
  disabled?: boolean
  primary?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="w-full px-3 py-2 rounded text-sm font-medium flex items-center justify-center gap-2 disabled:opacity-50"
      style={{
        background: primary ? 'var(--accent)' : 'transparent',
        border: primary ? 'none' : '1px solid var(--border)',
        color: primary ? 'var(--accent-fg, #fff)' : 'var(--text)',
      }}
    >
      {icon}
      {label}
    </button>
  )
}
