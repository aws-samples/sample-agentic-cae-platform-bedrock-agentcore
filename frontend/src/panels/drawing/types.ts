/** A drawing that has been uploaded in the current session. */
export interface UploadedDrawing {
  /** Server-generated unique drawing identifier (Req 1.1). */
  drawingId: string
  /** Original filename shown in the list. */
  filename: string
  /** MIME type of the file (drives PDF vs SVG rendering). */
  contentType: string
  /** Object URL for local, library-free preview of the just-uploaded file. */
  objectUrl: string
}
