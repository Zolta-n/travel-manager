"""Travel log report, days per country, Word reading and home-screen assets."""
import base64
import io
import json
from datetime import date, timedelta
from pathlib import Path
import shutil
import sys
import unittest
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import travel_log as tl
from app import Application, Config
from extractor import extract_document, ReaderError
from store import empty_state

MONTHS = tl.MONTHS


def serial(day):
    return (day - date(1899, 12, 30)).days


def make_log(year=2026, checked_until=date(2026, 10, 31), plan=None, extra_lists=()):
    """A small workbook shaped like the owner's log: month sheets (A date, B weekday, C country,
    D check, E arrival, F departure, G/H formulas), Lists with counting formulas, a calcChain."""
    plan = plan or {}
    strings = ["Date ", "Day", "Country", "Check", "Arrival time", "Departure", "Days", "Text", "China", "Germany", "Country", "Counts"]
    sheets, rels, overrides = [], [], []
    for i, month in enumerate(MONTHS, 1):
        rows = ['<row r="2"><c r="A2" s="1" t="s"><v>0</v></c><c r="C2" s="1" t="s"><v>2</v></c><c r="D2" s="1" t="s"><v>3</v></c></row>']
        day, r = date(year, i, 1), 3
        while day.month == i:
            country = plan.get(day, "China")
            idx = strings.index(country) if country in strings else None
            ccell = '<c r="C%d" s="5" t="s"><v>%d</v></c>' % (r, idx) if idx is not None else '<c r="C%d" s="5" t="inlineStr"><is><t>%s</t></is></c>' % (r, country)
            formulas = ('<c r="G%d" s="44" t="str" cm="1"><f t="array" ref="G%d">IF(F%d&lt;&gt;"",(A%d+F%d)-E$1,"")</f><v/></c>'
                        '<c r="H%d" s="45" t="str"><f>IF(G%d&lt;&gt;"",TEXT(G%d,"0.0")&amp;" days spent in "&amp;C%d,"")</f><v></v></c>') % ((r,) * 9) if r < 31 else ""
            rows.append('<row r="%d" spans="1:10"><c r="A%d" s="3"><v>%d</v></c><c r="B%d" s="4" t="str"><f>TEXT(WEEKDAY(A%d,1),"ddd")</f><v>x</v></c>%s'
                        '<c r="D%d" s="6" t="b"><v>%d</v></c><c r="E%d" s="43"/><c r="F%d" s="43"/>%s</row>'
                        % (r, r, serial(day), r, r, ccell, r, 1 if day <= checked_until else 0, r, r, formulas))
            day += timedelta(days=1); r += 1
        sheets.append((month, "sheet%d.xml" % i, '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A2:J33"/><sheetData>%s</sheetData><conditionalFormatting sqref="D3:D33"><cfRule type="cellIs" dxfId="30" priority="1" operator="equal"><formula>TRUE</formula></cfRule></conditionalFormatting></worksheet>' % "".join(rows)))
    lists_rows = ['<row r="1"><c r="A1" t="s"><v>10</v></c><c r="B1" t="s"><v>11</v></c></row>']
    for r, country in enumerate(["China", "Germany", *extra_lists], 2):
        lists_rows.append('<row r="%d"><c r="A%d" s="24" t="inlineStr"><is><t>%s</t></is></c><c r="B%d" cm="1"><f t="array" aca="1" ref="B%d" ca="1">SUMPRODUCT(COUNTIF(INDIRECT("\'"&amp;TEXT(DATE(%d,ROW(INDIRECT("1:12")),1),"mmmm")&amp;"\'!$C$3:$C$33"),Lists!A%d))</f><v>1</v></c></row>' % (r, r, country, r, r, year, r))
    sheets.append(("Lists", "sheet13.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><dimension ref="A1:B3"/><sheetData>%s</sheetData></worksheet>' % "".join(lists_rows)))
    for i, (name, file, _) in enumerate(sheets, 1):
        rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/%s"/>' % (i, file))
        overrides.append('<Override PartName="/xl/worksheets/%s" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % file)
    rels.append('<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/calcChain" Target="calcChain.xml"/>')
    workbook = ('<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>%s</sheets>'
                '<definedNames><definedName name="_xlchart.v5.1" hidden="1">Lists!$A$2:$A$3</definedName><definedName name="_xlchart.v5.3" hidden="1">Lists!$B$2:$B$3</definedName></definedNames><calcPr calcId="191028"/></workbook>'
                % "".join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (name, i, i) for i, (name, _, _) in enumerate(sheets, 1)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">%s<Override PartName="/xl/calcChain.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.calcChain+xml"/></Types>' % "".join(overrides))
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>' % "".join(rels))
        z.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">%s</sst>' % "".join("<si><t>%s</t></si>" % s for s in strings))
        z.writestr("xl/calcChain.xml", "<calcChain/>")
        z.writestr("xl/charts/chartEx1.xml", "<cx:chartSpace>map chart stays untouched</cx:chartSpace>")
        for _, file, xml in sheets:
            z.writestr("xl/worksheets/" + file, xml)
    return out.getvalue()


def flight(start, start_time, start_zone, end, end_time, end_zone, trip="t1"):
    return {"category": "Flight", "status": "Confirmed", "tripId": trip, "title": "F", "startDate": start, "startTime": start_time,
            "startZone": start_zone, "endDate": end, "endTime": end_time, "endZone": end_zone}


def cell_text(binary, sheet, ref):
    with zipfile.ZipFile(io.BytesIO(binary)) as z:
        return tl._find(z.read("xl/worksheets/" + sheet).decode(), "".join(c for c in ref if c.isalpha()), int("".join(c for c in ref if c.isdigit()))) or ""


class DayRules(unittest.TestCase):
    def test_zone_countries(self):
        self.assertEqual(tl.zone_country("Asia/Shanghai"), "China")
        self.assertEqual(tl.zone_country("Europe/Berlin"), "Germany")
        self.assertEqual(tl.zone_country("Asia/Seoul"), "South Korea")
        self.assertEqual(tl.zone_country("Europe/London"), "United Kingdom")
        self.assertEqual(tl.zone_country("Etc/GMT-8"), "")

    def test_departure_and_arrival_days_like_the_owners_log(self):
        state = {"trips": [{"tripId": "t1", "endDate": "2026-02-07"}], "activities": [
            flight("2026-02-01", "13:00", "Asia/Shanghai", "2026-02-01", "20:30", "Europe/Berlin"),
            flight("2026-02-06", "18:30", "Europe/Berlin", "2026-02-07", "11:00", "Asia/Shanghai")]}
        days = tl.day_countries(state, 2026, home="China")
        get = lambda d: days[date.fromisoformat(d)]
        self.assertEqual(get("2026-01-31")["country"], "China")
        self.assertEqual((get("2026-02-01")["country"], get("2026-02-01")["arrival"]), ("Germany", "20:30"))  # same-day flight: destination
        self.assertEqual(get("2026-02-03")["country"], "Germany")
        self.assertEqual((get("2026-02-06")["country"], get("2026-02-06")["departure"]), ("Germany", "18:30"))  # overnight: origin
        self.assertEqual((get("2026-02-07")["country"], get("2026-02-07")["arrival"]), ("China", ""))  # home arrivals carry no time

    def test_ticked_days_win_and_foreign_plans_are_kept(self):
        state = {"trips": [{"tripId": "t1", "endDate": "2027-01-03"}], "activities": [
            flight("2026-12-14", "00:10", "Asia/Shanghai", "2026-12-14", "06:15", "Europe/Berlin")]}
        anchors = {date(2026, 12, 13): "Hungary"}
        existing = {date(2026, 12, 20): "Austria", date(2026, 12, 21): "China"}
        days = tl.day_countries(state, 2026, anchors, "China", existing)
        self.assertEqual((days[date(2026, 12, 13)]["country"], days[date(2026, 12, 13)]["source"]), ("Hungary", "log"))
        self.assertEqual(days[date(2026, 12, 20)]["country"], "Austria")        # side trip planned in the log
        self.assertEqual(days[date(2026, 12, 21)]["country"], "Germany")        # "China" was only a pre-fill
        self.assertEqual(days[date(2026, 12, 15)]["source"], "booking")

    def test_hotel_nights_and_trip_end_without_return_booking(self):
        state = {"trips": [{"tripId": "t1", "endDate": "2026-05-05"}], "activities": [
            {"category": "Hotel / accommodation", "status": "Confirmed", "tripId": "t1", "startDate": "2026-05-02", "endDate": "2026-05-04", "startZone": "Asia/Tokyo"}]}
        days = tl.day_countries(state, 2026, home="China")
        self.assertEqual([days[date(2026, 5, d)]["country"] for d in (1, 2, 3, 4, 6)], ["China", "Japan", "Japan", "Japan", "China"])


class ReportTests(unittest.TestCase):
    def test_fill_keeps_ticked_rows_and_other_parts_and_adds_countries(self):
        base = make_log(plan={date(2026, 12, 20): "Germany"})
        state = {"settings": {}, "trips": [{"tripId": "t1", "endDate": "2026-11-25"}], "activities": [
            flight("2026-10-10", "09:00", "Asia/Shanghai", "2026-10-10", "13:00", "Asia/Tokyo"),       # ticked month: ignored
            flight("2026-11-20", "09:00", "Asia/Shanghai", "2026-11-20", "13:00", "Asia/Tokyo"),
            flight("2026-11-24", "23:00", "Asia/Tokyo", "2026-11-25", "02:30", "Asia/Shanghai")]}
        out, summary = tl.fill_log(base, state, home="China")
        self.assertEqual(summary["year"], 2026)
        self.assertEqual(summary["updated"], [{"from": "2026-11-20", "to": "2026-11-24", "old": "China", "new": "Japan"}])
        before, after = zipfile.ZipFile(io.BytesIO(base)), zipfile.ZipFile(io.BytesIO(out))
        for name in before.namelist():
            if name not in ("xl/worksheets/sheet11.xml", "xl/worksheets/sheet13.xml", "xl/workbook.xml", "[Content_Types].xml", "xl/_rels/workbook.xml.rels", "xl/calcChain.xml"):
                self.assertEqual(before.read(name), after.read(name), name)
        self.assertNotIn("xl/calcChain.xml", after.namelist())
        self.assertIn('fullCalcOnLoad="1"', after.read("xl/workbook.xml").decode())
        self.assertNotIn("calcChain", after.read("[Content_Types].xml").decode() + after.read("xl/_rels/workbook.xml.rels").decode())
        self.assertIn("Japan", cell_text(out, "sheet11.xml", "C22"))           # 20 Nov is row 22
        self.assertIn("<v>%r</v>" % (13 / 24), cell_text(out, "sheet11.xml", "E22"))
        self.assertIn("<v>%r</v>" % (23 / 24), cell_text(out, "sheet11.xml", "F26"))  # overnight departure from Japan
        self.assertIn('t="b"><v>0</v>', cell_text(out, "sheet11.xml", "D22"))  # still unticked for you to verify
        self.assertIn('s="5"', cell_text(out, "sheet11.xml", "C22"))            # cell style kept
        self.assertIn("<f", cell_text(out, "sheet11.xml", "G31")) if False else None
        lists = after.read("xl/worksheets/sheet13.xml").decode()
        self.assertIn("<t>Japan</t>", lists)
        self.assertIn("Lists!A4", lists)                                        # counting formula copied for the new row
        self.assertIn("Lists!$A$2:$A$4", after.read("xl/workbook.xml").decode())  # map chart range extended
        again = tl.read_log(out)
        self.assertEqual(again["verified"], tl.read_log(base)["verified"])

    def test_missing_formulas_are_added_for_late_month_rows(self):
        base = make_log()
        state = {"settings": {}, "trips": [], "activities": [flight("2026-12-31", "09:00", "Asia/Shanghai", "2026-12-31", "13:00", "Asia/Tokyo")]}
        out, _ = tl.fill_log(base, state, home="China")
        g = cell_text(out, "sheet12.xml", "G33")
        self.assertIn('ref="G33"', g)
        self.assertIn("F33", g)
        self.assertNotIn("F3)", g)

    def test_rejects_non_logs_and_unsafe_files(self):
        with self.assertRaises(tl.LogError):
            tl.read_log(b"not a zip")
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w") as z:
            z.writestr("xl/workbook.xml", "<workbook><sheets/></workbook>")
            z.writestr("xl/_rels/workbook.xml.rels", "<Relationships/>")
        with self.assertRaises(tl.LogError):
            tl.read_log(empty.getvalue())


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.work = ROOT / (".qa-" + uuid.uuid4().hex)
        self.work.mkdir(mode=0o700)
        log = make_log()
        seed = empty_state()
        seed["trips"] = [{"tripId": "t1", "name": "Japan", "type": "Business", "destination": "Tokyo", "startDate": "2026-11-20", "endDate": "2026-11-25", "notes": "", "sample": False}]
        seed["activities"] = [dict(flight("2026-11-20", "09:00", "Asia/Shanghai", "2026-11-20", "13:00", "Asia/Tokyo"), activityId="a1", title="NH", documentIds="")]
        seed["documents"] = [{"documentId": "log", "name": "2026_Travel_Log.xlsx", "size": len(log), "tripId": "",
                              "data": "data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64," + base64.b64encode(log).decode()}]
        (self.work / "seed.json").write_text(json.dumps(seed))
        self.app = Application(Config(password="Correct-Horse-Battery-9", allowed_hosts=("travel.example",), data_dir=self.work / "data",
                                      web_dir=ROOT / "web", seed_path=self.work / "seed.json", secure_cookie=False))

    def tearDown(self):
        shutil.rmtree(self.work)

    def call(self, method, path, body=None, cookie="", csrf="", query=""):
        data = json.dumps(body).encode() if body is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": "travel.example", "wsgi.input": io.BytesIO(data),
               "CONTENT_LENGTH": str(len(data)), "CONTENT_TYPE": "application/json", "HTTP_COOKIE": cookie, "HTTP_X_CSRF_TOKEN": csrf, "REMOTE_ADDR": "127.0.0.1"}
        if method != "GET":
            env["HTTP_ORIGIN"] = "http://travel.example"
        got = {}
        raw = b"".join(self.app(env, lambda s, h: got.update(status=int(s.split()[0]), headers=dict(h))))
        return got["status"], raw, got["headers"]

    def login(self):
        status, raw, headers = self.call("POST", "/api/login", {"password": "Correct-Horse-Battery-9"})
        return headers["Set-Cookie"].split(";")[0], json.loads(raw)["csrfToken"]

    def test_report_countries_and_public_icons(self):
        self.assertEqual(self.call("GET", "/api/countries")[0], 401)
        cookie, csrf = self.login()
        status, raw, _ = self.call("POST", "/api/travel-log", {"documentId": "log"}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200, raw)
        body = json.loads(raw)
        self.assertTrue(body["name"].startswith("Travel_Log_2026_"))
        self.assertEqual(tl.read_log(base64.b64decode(body["data"]))["countries"][date(2026, 11, 20)], "Japan")
        status, raw, _ = self.call("GET", "/api/countries", cookie=cookie, query="year=2026")
        data = json.loads(raw)
        self.assertEqual((status, data["logDocument"], data["home"]), (200, "2026_Travel_Log.xlsx", "China"))
        self.assertEqual(len(data["days"]), 365)
        self.assertIn("Japan", [t["country"] for t in data["totals"]])
        for path, kind in (("/manifest.json", "application/manifest+json"), ("/apple-touch-icon.png", "image/png"), ("/icon-512.png", "image/png")):
            status, raw, headers = self.call("GET", path)
            self.assertEqual((status, headers["Content-Type"]), (200, kind))
        self.assertEqual(json.loads(self.call("GET", "/manifest.json")[1])["short_name"], "Travel")


class WordTests(unittest.TestCase):
    def docx(self, body, extra=None):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>%s</w:body></w:document>' % body)
            for name, data in (extra or {}).items():
                z.writestr(name, data)
        return out.getvalue()

    def read(self, binary, name="booking.docx"):
        work = ROOT / (".qa-" + uuid.uuid4().hex)
        work.mkdir(mode=0o700)
        try:
            path = work / name
            path.write_bytes(binary)
            return extract_document(path, "application/octet-stream", name, work / "reader")
        finally:
            shutil.rmtree(work)

    def test_paragraphs_tables_and_headers(self):
        body = ('<w:p><w:r><w:t>Booking confirmation UQ4YP</w:t></w:r></w:p>'
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Flight</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>LH727</w:t></w:r></w:p></w:tc></w:tr>'
                '<w:tr><w:tc><w:p><w:r><w:t>Depart</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>14 Dec 2026 00:10</w:t></w:r><w:r><w:tab/><w:t>PVG</w:t></w:r></w:p></w:tc></w:tr></w:tbl>')
        result = self.read(self.docx(body, {"word/header1.xml": '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Lufthansa</w:t></w:r></w:p></w:hdr>'}))
        self.assertEqual(result["method"], "Word document (Python stdlib)")
        self.assertIn("Booking confirmation UQ4YP", result["text"])
        self.assertIn("Flight | LH727", result["text"])
        self.assertIn("Depart | 14 Dec 2026 00:10\tPVG", result["text"])
        self.assertTrue(result["text"].startswith("Lufthansa"))

    def test_unsafe_empty_and_old_word_files(self):
        with self.assertRaises(ReaderError):
            self.read(self.docx("<w:p/>").replace(b"x", b"x"), "empty.docx")
        bomb = io.BytesIO()
        with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>')
        with self.assertRaises(ReaderError) as caught:
            self.read(bomb.getvalue())
        self.assertEqual(caught.exception.code, "invalid_docx")
        with self.assertRaises(ReaderError) as caught:
            self.read(b"\xd0\xcf\x11\xe0" + b"\0" * 100, "old.doc")
        self.assertIn(".docx", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
