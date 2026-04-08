# tools/research/pdf_engine.py
"""
PDF Generation Engine for Research Reports.

Converts AI-generated XML into institutional-grade PDFs using ReportLab.
Applies the "Minimalist Gray" design system and handles XML parsing with grace.
"""

import os
import re
import xml.etree.ElementTree as ET
from typing import List

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# Import config for logo path
try:
    from config import RESEARCH_PDF_LOGO_PATH
except ImportError:
    RESEARCH_PDF_LOGO_PATH = None

# Import centralized text processing utilities
from utils.text_processing import sanitize_for_xml


class ReportEngine:
    """
    Handles XML-to-PDF conversion with ReportLab.

    The engine sanitizes LLM output, parses XML structure, and maps tags
    to ReportLab flowables while applying the Minimalist Gray design system.
    """
    
    def __init__(self):
        self.styles = self._setup_custom_styles()
    
    def _setup_custom_styles(self) -> getSampleStyleSheet:
        """Define custom ParagraphStyles for the Minimalist Gray design system."""
        styles = getSampleStyleSheet()
        
        # Color palette
        PRIMARY_COLOR = colors.HexColor("#2C3E50")  # Dark gray-blue
        SECONDARY_COLOR = colors.HexColor("#7F8C8D")  # Medium gray
        ACCENT_COLOR = colors.HexColor("#E74C3C")  # Red accent
        
        # Heading styles
        styles.add(ParagraphStyle(
            name='H1',
            parent=styles['Heading1'],
            fontSize=24,
            textColor=PRIMARY_COLOR,
            spaceBefore=20,
            spaceAfter=12,
            leading=28,
            fontWeight='Bold'
        ))
        
        styles.add(ParagraphStyle(
            name='H2',
            parent=styles['Heading2'],
            textColor=PRIMARY_COLOR,
            fontSize=18,
            spaceBefore=15,
            spaceAfter=8,
            borderPadding=(4, 0, 4, 8),
            borderWidth=1,
            borderColor=PRIMARY_COLOR
        ))
        
        styles.add(ParagraphStyle(
            name='H3',
            parent=styles['Heading3'],
            textColor=SECONDARY_COLOR,
            fontSize=14,
            spaceBefore=10,
            spaceAfter=5,
            fontWeight='Bold'
        ))
        
        # Body text
        styles.add(ParagraphStyle(
            name='Body',
            parent=styles['Normal'],
            fontSize=11,
            leading=14,
            alignment=TA_JUSTIFY,
            textColor=colors.black
        ))
        
        # List items
        styles.add(ParagraphStyle(
            name='ListItem',
            parent=styles['Normal'],
            fontSize=11,
            leading=14,
            leftIndent=18,
            bulletIndent=8,
            spaceBefore=2,
            textColor=colors.black
        ))
        
        return styles
    
    @staticmethod
    def _sanitize_xml(raw: str) -> str:
        """
        Fix common AI-generated XML issues before parsing.
        Uses centralized sanitization for consistency.
        """
        # Strip markdown fences
        raw = re.sub(r'```(?:xml)?\s*', '', raw)
        # Remove any remaining markdown artifacts
        raw = re.sub(r'^\s*```.*$', '', raw, flags=re.MULTILINE)
        # Use centralized sanitizer for typographic issues and ampersands
        return sanitize_for_xml(raw)
    
    @staticmethod
    def _iter_content(root):
        """
        Yield content-level elements by unwrapping step-level sections.
        """
        for elem in root:
            if elem.tag == 'section':
                yield from elem
            elif elem.tag in ['heading', 'paragraph', 'list', 'li', 'table', 'page_break']:
                # Skip elements that are just whitespace containers
                if elem.tag in ['paragraph', 'li'] and not elem.text and not len(elem):
                    continue
                yield elem

    @staticmethod
    def _header_footer(canvas, doc):
        """Draw header and footer on every page."""
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        
        # Header - Title
        canvas.setFillColor(colors.HexColor("#2C3E50"))
        canvas.drawString(2*cm, A4[1] - 1.2*cm, "SumAnal Research Report")
        
        # Header - Logo (top-right, if file exists)
        if RESEARCH_PDF_LOGO_PATH and os.path.exists(RESEARCH_PDF_LOGO_PATH):
            try:
                canvas.drawImage(
                    RESEARCH_PDF_LOGO_PATH,
                    A4[0] - 3.5*cm, A4[1] - 1.6*cm,
                    width=1.2*cm, height=1.2*cm,
                    preserveAspectRatio=True, mask='auto'
                )
            except Exception:
                # If logo fails to load, silently continue without it
                pass
        
        # Footer - Page number
        canvas.setFillColor(colors.HexColor("#7F8C8D"))
        canvas.drawRightString(A4[0] - 2*cm, 1.2*cm, f"Page {canvas.getPageNumber()}")
        
        canvas.restoreState()
    
    def generate(self, xml_content: str, output_path: str) -> str:
        """
        Generate a PDF from XML content.
        
        Args:
            xml_content: XML string from the 8-step CoT pipeline
            output_path: Full path where PDF should be saved
        
        Returns:
            The output_path string (for caller reference)
        """
        # Setup document
        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            rightMargin=2*cm,
            leftMargin=2*cm,
            topMargin=2.5*cm,
            bottomMargin=2*cm
        )
        
        story = []
        usable_width = A4[0] - 4*cm  # Available width for tables
        
        try:
            # Sanitize and parse XML
            clean = self._sanitize_xml(xml_content)
            root = ET.fromstring(f"<root>{clean}</root>")
            
            # Process each content element
            for elem in self._iter_content(root):
                
                if elem.tag == 'heading':
                    h_type = elem.get('type', 'h2').upper()
                    style = h_type if h_type in ('H1', 'H2', 'H3') else 'H2'
                    text = elem.text or ""
                    if text.strip():
                        story.append(Paragraph(text, self.styles[style]))
                
                elif elem.tag == 'paragraph':
                    text = elem.text or ""
                    if text.strip():
                        story.append(Paragraph(text, self.styles['Body']))
                        story.append(Spacer(1, 0.3*cm))
                
                elif elem.tag == 'list':
                    for li in elem.findall('li'):
                        li_text = li.text or ""
                        if li_text.strip():
                            story.append(Paragraph(f"\u2022 {li_text}", self.styles['ListItem']))
                    story.append(Spacer(1, 0.2*cm))
                
                elif elem.tag == 'li':
                    # Handle bare <li> outside a <list> wrapper
                    li_text = elem.text or ""
                    if li_text.strip():
                        story.append(Paragraph(f"\u2022 {li_text}", self.styles['ListItem']))
                
                elif elem.tag == 'table':
                    data = []
                    for row in elem.findall('row'):
                        cells = []
                        # Collect cell content - handle both <cell> tags and direct text
                        for cell in row:
                            cell_text = cell.text or cell.tail or ""
                            if cell_text and cell_text.strip():
                                cells.append(Paragraph(cell_text.strip(), self.styles['Body']))
                        if cells:
                            data.append(cells)
                    
                    if data:
                        # Calculate column widths
                        n_cols = max(len(r) for r in data)
                        col_w = min(usable_width / max(n_cols, 1), 6*cm)
                        
                        # Create table
                        t = Table(
                            data,
                            colWidths=[col_w]*n_cols,
                            hAlign='LEFT',
                            repeatRows=1  # Repeat header row
                        )
                        
                        # Apply styling
                        t.setStyle(TableStyle([
                            ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
                            ('BACKGROUND', (0,0), (-1,0), colors.whitesmoke),
                            ('VALIGN', (0,0), (-1,-1), 'TOP'),
                            ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                        ]))
                        
                        story.append(t)
                        story.append(Spacer(1, 0.3*cm))
                
                elif elem.tag == 'page_break':
                    story.append(PageBreak())
        
        except ET.ParseError as exc:
            # Graceful degradation on XML parse errors
            story.append(Paragraph(
                f"\u26a0 XML parse error \u2014 raw content appended below. ({exc})",
                self.styles['Body']
            ))
            story.append(Spacer(1, 0.2*cm))
            story.append(Paragraph(xml_content[:3000], self.styles['Body']))
        
        # Build the PDF
        doc.build(story, onFirstPage=self._header_footer, onLaterPages=self._header_footer)
        
        return output_path
