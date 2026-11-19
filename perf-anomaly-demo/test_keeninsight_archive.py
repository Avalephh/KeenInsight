"""Presentation contract with the operator's original English.zip."""
from html.parser import HTMLParser
from pathlib import Path
import os
import re
import unittest
import zipfile

WEB=Path(__file__).resolve().parent/"keeninsight_web"
ARCHIVE=Path(os.environ.get("KEENINSIGHT_ORIGINAL_ZIP","/root/English.zip"))


class Headers(HTMLParser):
    def __init__(self,source):
        super().__init__()
        self.values=[]
        self.depth=0
        self.text=[]
        self.feed(source)

    def handle_starttag(self,tag,attrs):
        if tag=="th":
            self.depth+=1
            self.text=[]

    def handle_data(self,text):
        if self.depth:self.text.append(text)

    def handle_endtag(self,tag):
        if tag=="th" and self.depth:
            self.values.append(" ".join("".join(self.text).split()))
            self.depth-=1


@unittest.skipUnless(ARCHIVE.is_file(),"Original operator archive is not installed")
class ArchivePresentationTests(unittest.TestCase):
    def test_all_retained_inline_styles_are_exact_archive_styles(self):
        with zipfile.ZipFile(ARCHIVE) as archive:
            names=[n for n in archive.namelist() if n.endswith(".html") and "realtime-monitor" not in n]
            self.assertEqual(len(names),11)
            for name in names:
                with self.subTest(page=name):
                    original=archive.read(name).decode("utf-8-sig").replace("\r\n","\n")
                    current=(WEB/Path(name).name).read_text()
                    styles=lambda value:re.findall(r"<style[^>]*>(.*?)</style>",value,re.S)
                    self.assertEqual(styles(original),styles(current))
                    self.assertNotIn("realtime-monitor",current)

    def test_original_list_headers_are_not_silently_renamed(self):
        with zipfile.ZipFile(ARCHIVE) as archive:
            for name in ("host-list","alert-list","alert-rules","sql-log","dba-rules","manual","code-source"):
                with self.subTest(page=name):
                    original=archive.read("English/"+name+".html").decode("utf-8-sig")
                    current=(WEB/(name+".html")).read_text()
                    self.assertEqual(Headers(original).values,Headers(current).values)


if __name__=="__main__":unittest.main()
