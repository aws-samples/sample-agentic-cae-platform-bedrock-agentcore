import { useContext } from 'react'
import { SessionContext } from './context'
import type { SessionContextValue } from './context'

/**
 * Access session persistence + restoration state (session list, active session
 * detail, load/error states, and actions). Must be used within a
 * {@link SessionProvider}.
 */
export function useSession(): SessionContextValue {
  const value = useContext(SessionContext)
  if (value === null) {
    throw new Error('useSession must be used within a <SessionProvider>')
  }
  return value
}
