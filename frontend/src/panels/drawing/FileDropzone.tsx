import { useId, useRef, useState } from 'react'

interface FileDropzoneProps {
  /** Called with the chosen file(s). The panel handles validation + upload. */
  onFiles: (files: File[]) => void
  /** Disables interaction while an upload is in flight. */
  disabled?: boolean
}

/**
 * Accessible drag-and-drop + file-picker dropzone for drawing uploads (Req 8.2).
 *
 * The whole region is a button that opens the native file picker; files can
 * also be dropped onto it. A visually-hidden `<input type="file">` backs the
 * picker so keyboard and assistive-tech users get the same flow.
 */
function FileDropzone({ onFiles, disabled = false }: FileDropzoneProps) {
  const inputId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const [isDragging, setIsDragging] = useState(false)

  function openPicker() {
    if (!disabled) {
      inputRef.current?.click()
    }
  }

  function handleDrop(event: React.DragEvent<HTMLButtonElement>) {
    event.preventDefault()
    setIsDragging(false)
    if (disabled) {
      return
    }
    const files = Array.from(event.dataTransfer.files)
    if (files.length > 0) {
      onFiles(files)
    }
  }

  return (
    <div>
      <button
        type="button"
        onClick={openPicker}
        onDragOver={(event) => {
          event.preventDefault()
          if (!disabled) {
            setIsDragging(true)
          }
        }}
        onDragLeave={() => setIsDragging(false)}
        onDrop={handleDrop}
        disabled={disabled}
        aria-describedby={`${inputId}-hint`}
        className={`flex w-full flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-8 text-center transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-500 ${
          isDragging
            ? 'border-sky-500 bg-sky-50'
            : 'border-slate-300 bg-slate-50 hover:border-sky-400 hover:bg-slate-100'
        } ${disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer'}`}
      >
        <span className="text-base font-medium text-slate-800">
          Drop a PDF drawing here, or click to browse
        </span>
        <span id={`${inputId}-hint`} className="text-sm text-slate-500">
          PDF only · 1 KB to 100 MB
        </span>
      </button>
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        accept="application/pdf,.pdf"
        className="sr-only"
        disabled={disabled}
        onChange={(event) => {
          const files = Array.from(event.target.files ?? [])
          if (files.length > 0) {
            onFiles(files)
          }
          // Reset so selecting the same file again re-triggers onChange.
          event.target.value = ''
        }}
      />
    </div>
  )
}

export default FileDropzone
