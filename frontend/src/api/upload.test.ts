import { describe, expect, it } from 'vitest'
import fc from 'fast-check'
import {
  MAX_UPLOAD_SIZE_BYTES,
  MIN_UPLOAD_SIZE_BYTES,
  type UploadFileLike,
  validateUploadFile,
} from './upload'

/** A fully-valid reference file: valid size, .pdf extension, application/pdf. */
function validFile(overrides: Partial<UploadFileLike> = {}): UploadFileLike {
  return {
    name: 'drawing.pdf',
    type: 'application/pdf',
    size: 2048,
    ...overrides,
  }
}

/**
 * Independent reference of the acceptance rules (Req 1.2, 1.3): a file is
 * acceptable iff its size is within [1 KB, 100 MB], its name ends in `.pdf`
 * (case-insensitive), and its content type is exactly `application/pdf`.
 */
function referenceValid(file: UploadFileLike): boolean {
  const sizeOk = file.size >= MIN_UPLOAD_SIZE_BYTES && file.size <= MAX_UPLOAD_SIZE_BYTES
  const extOk = file.name.toLowerCase().endsWith('.pdf')
  const typeOk = file.type === 'application/pdf'
  return sizeOk && extOk && typeOk
}

describe('validateUploadFile (Req 1.2, 1.3)', () => {
  it('accepts a valid PDF within the size range', () => {
    expect(validateUploadFile(validFile())).toEqual([])
  })

  it('rejects files smaller than 1 KB with a size-range message', () => {
    const errors = validateUploadFile(validFile({ size: MIN_UPLOAD_SIZE_BYTES - 1 }))
    expect(errors).toContain('File is too small. Accepted size is 1 KB to 100 MB.')
  })

  it('rejects files larger than 100 MB with a size-range message', () => {
    const errors = validateUploadFile(validFile({ size: MAX_UPLOAD_SIZE_BYTES + 1 }))
    expect(errors).toContain('File is too large. Accepted size is 1 KB to 100 MB.')
  })

  it('accepts the exact boundary sizes (1 KB and 100 MB)', () => {
    expect(validateUploadFile(validFile({ size: MIN_UPLOAD_SIZE_BYTES }))).toEqual([])
    expect(validateUploadFile(validFile({ size: MAX_UPLOAD_SIZE_BYTES }))).toEqual([])
  })

  it('rejects a non-.pdf extension', () => {
    const errors = validateUploadFile(validFile({ name: 'drawing.png' }))
    expect(errors).toContain('Only PDF files are accepted (a .pdf extension is required).')
  })

  it('accepts an uppercase .PDF extension', () => {
    expect(validateUploadFile(validFile({ name: 'DRAWING.PDF' }))).toEqual([])
  })

  it('rejects a non-application/pdf content type', () => {
    const errors = validateUploadFile(validFile({ type: 'image/png' }))
    expect(errors).toContain(
      'Only PDF files are accepted (application/pdf content type is required).',
    )
  })

  it('reports multiple errors at once', () => {
    const errors = validateUploadFile({ name: 'notes.txt', type: 'text/plain', size: 10 })
    expect(errors.length).toBeGreaterThanOrEqual(3)
  })

  it('property: validity agrees with the acceptance-criteria definition', () => {
    const nameArb = fc.oneof(
      fc.constantFrom('drawing.pdf', 'DRAWING.PDF', 'a.PdF', 'file.png', 'file.svg', 'noext', 'x.pdf.txt'),
      fc.string({ maxLength: 20 }),
    )
    const typeArb = fc.constantFrom('application/pdf', 'image/png', 'image/svg+xml', 'text/plain', '')
    const sizeArb = fc.integer({ min: 0, max: MAX_UPLOAD_SIZE_BYTES + 5000 })

    fc.assert(
      fc.property(nameArb, typeArb, sizeArb, (name, type, size) => {
        const file: UploadFileLike = { name, type, size }
        expect(validateUploadFile(file).length === 0).toBe(referenceValid(file))
      }),
    )
  })
})
