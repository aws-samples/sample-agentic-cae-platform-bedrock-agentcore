import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import {
  PASSWORD_MAX_LENGTH,
  PASSWORD_MIN_LENGTH,
  isPasswordValid,
  validatePassword,
} from './password'

/**
 * Independent reference implementation of the Cognito password policy (Req 9.4):
 * 8–128 chars with at least one uppercase, lowercase, digit, and special
 * (non-alphanumeric) character. Used to cross-check `isPasswordValid` across a
 * wide input space without duplicating its regex logic.
 */
function referencePolicy(pw: string): boolean {
  const lengthOk = pw.length >= PASSWORD_MIN_LENGTH && pw.length <= PASSWORD_MAX_LENGTH
  const hasUpper = [...pw].some((c) => c >= 'A' && c <= 'Z')
  const hasLower = [...pw].some((c) => c >= 'a' && c <= 'z')
  const hasDigit = [...pw].some((c) => c >= '0' && c <= '9')
  const hasSpecial = [...pw].some((c) => !/[A-Za-z0-9]/.test(c))
  return lengthOk && hasUpper && hasLower && hasDigit && hasSpecial
}

describe('validatePassword (Req 9.4)', () => {
  it('accepts a fully-compliant password', () => {
    expect(validatePassword('Abcdef1!')).toEqual([])
    expect(isPasswordValid('Abcdef1!')).toBe(true)
  })

  it('rejects passwords shorter than 8 characters', () => {
    expect(isPasswordValid('Ab1!')).toBe(false)
    expect(validatePassword('Ab1!')).toContain('Must be at least 8 characters.')
  })

  it('rejects passwords longer than 128 characters', () => {
    const tooLong = 'Aa1!' + 'a'.repeat(125) // 129 chars
    expect(tooLong.length).toBe(129)
    expect(isPasswordValid(tooLong)).toBe(false)
    expect(validatePassword(tooLong)).toContain('Must be at most 128 characters.')
  })

  it('flags each missing character class', () => {
    expect(validatePassword('abcdef1!')).toContain('Must include an uppercase letter.')
    expect(validatePassword('ABCDEF1!')).toContain('Must include a lowercase letter.')
    expect(validatePassword('Abcdefg!')).toContain('Must include a digit.')
    expect(validatePassword('Abcdefg1')).toContain('Must include a special character.')
  })

  it('accepts the boundary lengths (8 and 128)', () => {
    expect(isPasswordValid('Abcde1!x')).toBe(true) // exactly 8
    const at128 = 'Aa1!' + 'a'.repeat(124) // exactly 128
    expect(at128.length).toBe(128)
    expect(isPasswordValid(at128)).toBe(true)
  })

  it('property: isPasswordValid agrees with the policy definition for arbitrary strings', () => {
    fc.assert(
      fc.property(fc.string({ maxLength: 140 }), (pw) => {
        expect(isPasswordValid(pw)).toBe(referencePolicy(pw))
      }),
    )
  })

  it('property: compliant passwords (built from all four classes) always pass', () => {
    const upper = fc.constantFrom(...'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
    const lower = fc.constantFrom(...'abcdefghijklmnopqrstuvwxyz')
    const digit = fc.constantFrom(...'0123456789')
    const special = fc.constantFrom(...'!@#$%^&*()-_=+[]{};:,.?/')
    const filler = fc.string({ maxLength: 120 })
    fc.assert(
      fc.property(
        upper,
        lower,
        digit,
        special,
        filler,
        (u, l, d, s, f) => {
          // Compose a candidate then keep it within the length bound.
          const candidate = (u + l + d + s + f).slice(0, PASSWORD_MAX_LENGTH)
          // Guaranteed to contain all four classes and be >= 4 chars; only pass
          // the assertion when the length floor is also met.
          if (candidate.length >= PASSWORD_MIN_LENGTH) {
            expect(isPasswordValid(candidate)).toBe(true)
          }
        },
      ),
    )
  })
})
