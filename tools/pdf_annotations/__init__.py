"""PDF annotation extraction Lambda tool package.

Exposes the ``extract_pdf_annotations`` AgentCore Gateway tool (see design
section "7. AgentCore Gateway" and the Gateway target table). The tool reads a
PDF from the drawings S3 bucket and uses PyMuPDF to extract text annotations,
dimensions, and callouts from the PDF structure.
"""
