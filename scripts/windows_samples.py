"""Create known synthetic samples, never read any user documents."""
from pathlib import Path
import zipfile


def create(root):
    from PIL import Image, ImageDraw, ImageFont
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    root.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (1200, 700), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 44)
    draw.text((60, 70), '知识蒸馏器 Windows 离线测试', fill='black', font=font)
    draw.text((60, 160), '测试数据：苹果 12 个，梨 8 个。', fill='black', font=font)
    draw.text((60, 250), 'Offline document sample 2026', fill='black', font=font)
    image.save(root / 'ocr.png')
    image.save(root / 'scan.pdf', resolution=120)
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    font_dict = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font_dict)})})
    lines = ['BT /F1 22 Tf 50 780 Td (Windows Offline Test) Tj ET',
             'BT /F1 12 Tf 50 740 Td (A synthetic source. Apples cost 12 and pears cost 8.) Tj ET',
             'BT /F1 12 Tf 50 710 Td (The total is 20. This document contains no personal data.) Tj ET']
    for y, left, right in [(620, 'Fruit', 'Count'), (590, 'Apples', '12'), (560, 'Pears', '8')]:
        lines.append(f'BT /F1 14 Tf 70 {y} Td ({left}) Tj ET BT /F1 14 Tf 260 {y} Td ({right}) Tj ET')
    for y in (645, 610, 580, 545):
        lines.append(f'50 {y} m 400 {y} l S')
    for x in (50, 230, 400):
        lines.append(f'{x} 545 m {x} 645 l S')
    lines.append('BT /F1 24 Tf 220 450 Td (E = mc2) Tj ET')
    stream = DecodedStreamObject()
    stream.set_data('\n'.join(lines).encode('ascii'))
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(root / 'native.pdf')
    # Scientific-page layout is required to exercise formula enrichment.
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font_dict)})})
    stream = DecodedStreamObject()
    stream.set_data((Path(__file__).resolve().parents[1] / 'tests/fixtures/windows_formula_content.txt').read_bytes())
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(root / 'formula.pdf')
    with zipfile.ZipFile(root / 'sample.epub', 'w') as book:
        book.writestr('mimetype', 'application/epub+zip')
        book.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        book.writestr('content.opf', '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">windows-synthetic</dc:identifier><dc:title>Offline sample</dc:title><dc:language>en</dc:language></metadata><manifest><item id="c1" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>')
        book.writestr('chapter.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Sample</title></head><body><h1>Offline source</h1><p>Apples cost 12 and pears cost 8. The total is 20.</p></body></html>')


if __name__ == '__main__':
    create(Path(__file__).resolve().parents[1] / '.windows-build/samples')
