"""gcws.learn: parsing human NIAS evaluation workbooks (synthetic workbooks, no client data)."""
from learn_fixtures import FID_HEADER, FID_PEAKS, TIC_HEADER, TIC_PEAKS


def _rohdaten_rows():
    return [("[contents]",), ("count=2",), (r"[INT TIC: 05_X_A.D\data.ms]",), ("Time=", "x"), tuple(TIC_HEADER),
            *map(tuple, TIC_PEAKS), (r"[INT 05_X_A.D\FID1A.ch]",), tuple(FID_HEADER), *map(tuple, FID_PEAKS)]


def test_parse_rohdaten_sections():
    from gcws.learn.model import RawPeak
    from gcws.learn.workbook import parse_rohdaten
    peaks = parse_rohdaten(_rohdaten_rows())
    tic = [p for p in peaks if p.signal == "TIC"]
    fid = [p for p in peaks if p.signal == "FID"]
    assert len(tic) == 2 and len(fid) == len(FID_PEAKS)
    assert fid[3] == RawPeak(signal="FID", number=4, rt=7.07, area=119966715, height=3238076, start=6.938,
                             end=7.313, peak_type="M", manual=True)
    t = tic[1]
    assert (t.number, t.rt, t.area, t.start, t.end, t.peak_type, t.manual) == (2, 8.203, 40534814, None, None,
                                                                                "VV", False)


def test_parse_rohdaten_keeps_column_gaps():
    """Empty cells inside a row (e.g. a blank peak type) must not shift the columns."""
    from gcws.learn.workbook import parse_rohdaten
    rows = [(r"[INT 05_X_A.D\FID1A.ch]",), tuple(FID_HEADER), ("1=", 1, 5.0, 4.9, 5.1, None, 100, 2000, 0, 0)]
    (p,) = parse_rohdaten(rows)
    assert (p.peak_type, p.height, p.area, p.manual) == ("", 100, 2000, False)


def test_normalise_cas():
    from gcws.learn.model import normalise_cas
    assert normalise_cas("000097-88-1") == "97-88-1"
    assert normalise_cas(" 128-37-0 ") == "128-37-0"
    assert normalise_cas("-") == ""
    assert normalise_cas(None) == ""
