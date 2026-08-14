/**
 * Client-side password policy validation matching the Cognito User Pool policy
 * (Req 9.4): 8–128 characters with at least one uppercase letter, one lowercase
 * letter, one digit, and one special character.
 *
 * This mirrors the server-side policy so we can show inline errors before the
 * sign-up request is sent; Cognito remains the source of truth and re-enforces
 * the same rules server-side.
 */

export const PASSWORD_MIN_LENGTH = 8
export const PASSWORD_MAX_LENGTH = 128

/** Human-readable summary shown under the password field. */
export const PASSWORD_POLICY_HINT =
  'Password must be 8–128 characters and include an uppercase letter, a ' +
  'lowercase letter, a digit, and a special character.'

/**
 * Validate a candidate password against the policy.
 *
 * @returns an array of human-readable error messages; empty when the password
 *   satisfies every rule.
 */
export function validatePassword(password: string): string[] {
  const errors: string[] = []

  if (password.length < PASSWORD_MIN_LENGTH) {
    errors.push(`Must be at least ${PASSWORD_MIN_LENGTH} characters.`)
  }
  if (password.length > PASSWORD_MAX_LENGTH) {
    errors.push(`Must be at most ${PASSWORD_MAX_LENGTH} characters.`)
  }
  if (!/[A-Z]/.test(password)) {
    errors.push('Must include an uppercase letter.')
  }
  if (!/[a-z]/.test(password)) {
    errors.push('Must include a lowercase letter.')
  }
  if (!/[0-9]/.test(password)) {
    errors.push('Must include a digit.')
  }
  // "Special" = any character that is not a letter or digit (matches Cognito's
  // broad special-character set, including spaces and symbols).
  if (!/[^A-Za-z0-9]/.test(password)) {
    errors.push('Must include a special character.')
  }

  return errors
}

/** Convenience predicate: true when the password meets the full policy. */
export function isPasswordValid(password: string): boolean {
  return validatePassword(password).length === 0
}
