import os

from cullspeed import xmp

LIGHTROOM = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   xmp:Rating="2"
   crs:Exposure2012="+0.35">
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


def names(folder):
    return {name.lower(): name for name in os.listdir(folder)}


def test_parse_attribute_and_element_forms():
    assert xmp.parse(LIGHTROOM) == (2, None)
    assert xmp.parse("<rdf:Description><xmp:Rating>4</xmp:Rating><xmp:Label>Red</xmp:Label></rdf:Description>") == (4, "Red")
    assert xmp.parse('<rdf:Description xmp:Rating="-1"/>') == (0, None)  # Lightroom "rejected"


def test_update_keeps_everything_else():
    text = xmp.update(LIGHTROOM, 5, "Green")

    assert xmp.parse(text) == (5, "Green")
    assert 'crs:Exposure2012="+0.35"' in text
    assert text.count("xmlns:xmp=") == 1
    assert text.count("xmp:Rating") == 1


def test_update_declares_the_namespace_when_missing():
    text = xmp.update('<rdf:RDF><rdf:Description rdf:about=""/></rdf:RDF>', 3, None)
    assert 'xmlns:xmp="http://ns.adobe.com/xap/1.0/"' in text
    assert xmp.parse(text) == (3, None)


def test_update_clears_and_refuses_unknown_documents():
    assert xmp.parse(xmp.update(LIGHTROOM, 0, None)) == (0, None)
    assert xmp.update("<something-else/>", 3, None) is None


def test_write_creates_a_sidecar_for_raws_only(tmp_path):
    raw, jpg = str(tmp_path / "IMG.CR2"), str(tmp_path / "IMG.JPG")
    for path in (raw, jpg):
        open(path, "w").close()

    xmp.write([raw, jpg], 3, "Red", names(tmp_path))

    assert sorted(os.listdir(tmp_path)) == ["IMG.CR2", "IMG.JPG", "IMG.xmp"]
    assert xmp.read([raw], names(tmp_path)) == (3, "Red")


def test_write_nothing_creates_nothing(tmp_path):
    raw = str(tmp_path / "IMG.CR2")
    open(raw, "w").close()
    xmp.write([raw], 0, None, names(tmp_path))
    assert os.listdir(tmp_path) == ["IMG.CR2"]


def test_write_updates_existing_sidecars_whatever_their_case(tmp_path):
    jpg = str(tmp_path / "IMG.jpg")
    open(jpg, "w").close()
    (tmp_path / "IMG.JPG.XMP").write_text(LIGHTROOM)  # darktable-style, upper case

    xmp.write([jpg], 4, None, names(tmp_path))

    assert xmp.parse((tmp_path / "IMG.JPG.XMP").read_text()) == (4, None)
    assert sorted(os.listdir(tmp_path)) == ["IMG.JPG.XMP", "IMG.jpg"]
