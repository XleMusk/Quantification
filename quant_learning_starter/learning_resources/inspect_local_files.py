"""Check local links and render downloaded PDFs without changing the originals."""

import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageOps
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            url = dict(attrs).get("href", "")
            if url and not urlsplit(url).scheme:
                self.paths.append(unquote(urlsplit(url).path))


if __name__ == "__main__":
    links = Links()
    links.feed((ROOT / "RESOURCE_NAVIGATION.html").read_text(encoding="utf-8"))
    missing = [path for path in links.paths if not (ROOT / path).exists()]
    if missing:
        raise FileNotFoundError(missing)
    previews = ROOT / "previews"
    previews.mkdir(exist_ok=True)
    files = sorted((ROOT / "courses").glob("*.pdf"))
    sheet = Image.new("RGB", (510 * len(files), 740), "white")
    draw = ImageDraw.Draw(sheet)
    records = []
    for index, path in enumerate(files):
        reader = PdfReader(path)
        document = pdfium.PdfDocument(path)
        page = document[0]
        bitmap = page.render(scale=1.2)
        picture = bitmap.to_pil().convert("RGB")
        picture.save(previews / (path.stem + ".png"))
        preview = ImageOps.contain(picture, (490, 690))
        sheet.paste(preview, (index * 510 + 10, 38))
        draw.text((index * 510 + 10, 8), path.name, fill="black")
        records.append({"file": str(path.relative_to(ROOT)), "pages": len(reader.pages),
                        "first_page_rendered": True, "last_page_readable": bool(reader.pages[-1].extract_text().strip())})
        bitmap.close()
        page.close()
        document.close()
    sheet.save(previews / "pdf_preview.png")
    result = {"local_links_checked": len(links.paths), "missing_local_links": missing, "pdfs": records}
    (ROOT / "local_validation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
