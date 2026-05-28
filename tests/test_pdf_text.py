from extraction.pdf_text import extract_text_from_pdf_bytes, safe_extract_text_from_pdf_bytes


MINIMAL_TEXT_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources <<
/Font << /F1 5 0 R >>
>> >>
endobj
4 0 obj
<< /Length 44 >>
stream
BT
/F1 18 Tf
72 96 Td
(Hello PDF) Tj
ET
endstream
endobj
5 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
xref
0 6
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
0000000241 00000 n 
0000000335 00000 n 
trailer
<< /Root 1 0 R /Size 6 >>
startxref
405
%%EOF
"""


def test_extract_text_from_pdf_bytes() -> None:
    assert "Hello PDF" in extract_text_from_pdf_bytes(MINIMAL_TEXT_PDF)


def test_safe_extract_text_from_pdf_bytes_handles_none() -> None:
    assert safe_extract_text_from_pdf_bytes(None) is None
