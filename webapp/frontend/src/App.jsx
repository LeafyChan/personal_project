import React, { useState } from 'react'
import {
  SignedIn,
  SignedOut,
  SignInButton,
  UserButton,
  useAuth,
  useOrganization,
  OrganizationSwitcher,
  CreateOrganization,
} from '@clerk/clerk-react'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

function ApiTester() {
  const { getToken } = useAuth()
  const { organization } = useOrganization()
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  async function callEndpoint(path) {
    setError(null)
    setResult(null)
    try {
      const token = await getToken()
      const res = await fetch(`${API_BASE}${path}`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      const body = await res.json()
      if (!res.ok) {
        setError(`HTTP ${res.status}: ${JSON.stringify(body)}`)
      } else {
        setResult(body)
      }
    } catch (e) {
      setError(String(e))
    }
  }

  if (!organization) {
    return (
      <div>
        <p>No active organization yet. Create one to get an org-scoped token:</p>
        <CreateOrganization />
      </div>
    )
  }

  return (
    <div>
      <p>Active org: <b>{organization.name}</b> ({organization.id})</p>
      <button onClick={() => callEndpoint('/me')}>Call /me</button>{' '}
      <button onClick={() => callEndpoint('/invoices/_smoke_test')}>
        Call /invoices/_smoke_test
      </button>
      {result && <pre>{JSON.stringify(result, null, 2)}</pre>}
      {error && <pre style={{ color: 'red' }}>{error}</pre>}
    </div>
  )
}

export default function App() {
  return (
    <div style={{ fontFamily: 'monospace', padding: 24, maxWidth: 600 }}>
      <h2>Auth chain smoke test</h2>
      <p>Diagnostic only — not the product UI.</p>

      <SignedOut>
        <SignInButton mode="modal" />
      </SignedOut>

      <SignedIn>
        <UserButton />
        <div style={{ margin: '16px 0' }}>
          <OrganizationSwitcher />
        </div>
        <ApiTester />
      </SignedIn>
    </div>
  )
}
