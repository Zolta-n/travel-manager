"""Country per day, and the owner's Excel travel log report.

Days are assigned like the owner's own log: the day you leave a country counts for that country,
the day you arrive counts for the new one (an overnight flight leaving Germany on 6 Feb and landing in
China on 7 Feb gives Germany for 6 Feb and China for 7 Feb). Countries come from the IANA time zone of
each flight end or hotel, using the zone table shipped with tzdata.

The report keeps the owner's workbook intact: it edits only the cells of rows that are not ticked in
the "Check" column, adds missing countries to the "Lists" sheet (which feeds the map chart) and asks
Excel to recalculate on opening. Everything else - calendar, map chart, formulas, formatting, labels -
is copied byte for byte.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
import importlib.resources
import io
from pathlib import Path
import re
import zipfile
from xml.sax.saxutils import escape

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
TRANSPORT = ("Flight", "Rail / travel", "Transfer")
MAX_PART = 30 * 1024 * 1024
MAX_TOTAL = 120 * 1024 * 1024
EXCEL_EPOCH = date(1899, 12, 30)
NAME_OVERRIDES = {"SZ": "Eswatini", "KR": "South Korea", "KP": "North Korea", "GB": "United Kingdom", "MM": "Myanmar",
                  "CD": "Democratic Republic of the Congo", "CG": "Republic of the Congo",
                  "VA": "Vatican City", "PS": "Palestine", "TL": "Timor-Leste", "CI": "Côte d'Ivoire"}


class LogError(ValueError):
    pass


# ---------------------------------------------------------------- zones -> countries

def _tz_table(name):
    try:
        return importlib.resources.files("tzdata").joinpath("zoneinfo", name).read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        return Path("/usr/share/zoneinfo", name).read_text(encoding="utf-8")


def _nice_name(code, name):
    if code in NAME_OVERRIDES:
        return NAME_OVERRIDES[code]
    return re.sub(r"^St ", "Saint ", name.replace(" & ", " and "))


@lru_cache(maxsize=1)
def _zones():
    names = {}
    for line in _tz_table("iso3166.tab").splitlines():
        if line and not line.startswith("#"):
            code, name = line.split("\t")[:2]
            names[code] = _nice_name(code, name)
    zones = {}
    for line in _tz_table("zone.tab").splitlines():
        if line and not line.startswith("#"):
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0] in names:
                zones[parts[2]] = names[parts[0]]
    return zones


def zone_country(zone):
    """Country name for an IANA zone (Europe/Berlin -> Germany); '' for UTC/Etc/unknown."""
    return _zones().get(str(zone or ""), "")


# ---------------------------------------------------------------- itinerary -> country per day

def _valid(day):
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


def _events(state):
    """Arrivals into a country (transport legs), hotel stays, oldest first."""
    events, stays = [], []
    for a in state.get("activities", []):
        if a.get("status") == "Cancelled":
            continue
        start, end = _valid(a.get("startDate")), _valid(a.get("endDate")) or _valid(a.get("startDate"))
        if not start:
            continue
        if a.get("category") in TRANSPORT:
            origin, target = zone_country(a.get("startZone")), zone_country(a.get("endZone") or a.get("startZone"))
            if origin and target and origin != target:
                events.append({"date": end, "time": a.get("endTime") or "", "country": target, "from": origin,
                               "departDate": start, "departTime": a.get("startTime") or "", "trip": a.get("tripId", "")})
        elif a.get("category") == "Hotel / accommodation":
            country = zone_country(a.get("startZone"))
            if country:
                stays.append({"start": start, "end": end if end and end > start else start + timedelta(days=1),
                              "country": country, "trip": a.get("tripId", "")})
    return sorted(events, key=lambda e: (e["date"], e["time"])), stays


def day_countries(state, year, anchors=None, home="", existing=None):
    """{date: {"country", "arrival", "departure", "source", "booking"}} for every day of `year`.

    anchors:  {date: country} verified days (ticked rows of the owner's log); always kept.
    existing: {date: country} unticked rows already in the log; kept unless a booking places you
              elsewhere that very day.
    source:   "log" verified · "booking" a flight arrives/departs or a hotel night is that day ·
              "planned" the log's own unticked entry · "assumed" carried over from the last known place.
    booking:  the country the itinerary alone suggests (for listing disagreements), or "".
    """
    anchors, existing = anchors or {}, existing or {}
    trips = {t.get("tripId"): _valid(t.get("endDate")) for t in state.get("trips", [])}
    events, stays = _events(state)
    by_arrival, by_departure = {}, {}
    for e in events:
        by_arrival.setdefault(e["date"], []).append(e)
        by_departure.setdefault(e["departDate"], []).append(e)
    first = date(year, 1, 1)
    current, last_trip, stay = home, None, ""
    for e in events:  # where the bookings put you on 1 January
        if e["date"] < first:
            current, last_trip = e["country"], e["trip"]
            stay = e["country"] if e["country"] != home else ""
    out, day = {}, first
    while day.year == year:
        arrival = departure = ""
        booked = ""
        for e in by_departure.get(day, []):
            if e["from"] == stay:
                stay = ""
            if e["date"] > day:  # overnight: the departure day stays with the country you leave
                booked = e["from"]
                if e["from"] != home:
                    departure = e["departTime"]
        for e in by_arrival.get(day, []):
            booked, last_trip = e["country"], e["trip"]
            stay = e["country"] if e["country"] != home else ""
            if e["country"] != home:
                arrival = e["time"]
        if not booked:
            night = next((s for s in stays if s["start"] <= day < s["end"]), None)
            if night:
                booked, last_trip = night["country"], night["trip"]
        if booked:
            current = booked
        elif last_trip and home and current != home and trips.get(last_trip) and day > trips[last_trip]:
            current, last_trip, stay = home, None, ""  # a trip ended without a return booking
        if day in anchors:
            out[day] = {"country": anchors[day], "arrival": "", "departure": "", "source": "log", "booking": booked}
            current = anchors[day]
            if anchors[day] != stay:  # your verified log places you elsewhere: that booked stay is over
                stay, last_trip = "", None
        elif booked:
            out[day] = {"country": booked, "arrival": arrival, "departure": departure, "source": "booking", "booking": booked}
        elif stay and (not existing.get(day) or existing[day] in (home, stay)):
            # Between a flight into a country and the next flight out of it. A log entry saying "home"
            # here is only a pre-fill; a different foreign country (a side trip by car) is kept.
            out[day] = {"country": stay, "arrival": "", "departure": "", "source": "booking", "booking": stay}
        elif existing.get(day):
            out[day] = {"country": existing[day], "arrival": "", "departure": "", "source": "planned", "booking": "", "app": stay}
            if not last_trip:
                current = existing[day]
        else:
            out[day] = {"country": current, "arrival": "", "departure": "", "source": "assumed", "booking": ""}
        day += timedelta(days=1)
    return out


def totals(days):
    counts = {}
    for info in days.values():
        if info["country"]:
            entry = counts.setdefault(info["country"], {"country": info["country"], "days": 0, "verified": 0})
            entry["days"] += 1
            entry["verified"] += info["source"] == "log"
            entry["planned"] = entry.get("planned", 0) + (info["source"] != "log")
    return sorted(counts.values(), key=lambda c: (-c["days"], c["country"]))


# ---------------------------------------------------------------- workbook access (zip + XML text)

def _open(binary):
    try:
        archive = zipfile.ZipFile(io.BytesIO(binary))
    except zipfile.BadZipFile as exc:
        raise LogError("This file is not an Excel .xlsx workbook.") from exc
    total = 0
    for info in archive.infolist():
        total += info.file_size
        if info.file_size > MAX_PART or total > MAX_TOTAL or (info.compress_size and info.file_size / info.compress_size > 300):
            raise LogError("This workbook is too large to process safely.")
    return archive


def _read(archive, name):
    data = archive.read(name).decode("utf-8")
    if "<!DOCTYPE" in data[:2000] or "<!ENTITY" in data:
        raise LogError("This workbook contains unsupported XML declarations.")
    return data


def _sheet_files(archive):
    workbook = _read(archive, "xl/workbook.xml")
    rels = _read(archive, "xl/_rels/workbook.xml.rels")
    targets = {m[0]: m[1] for m in re.findall(r'<Relationship [^>]*?Id="([^"]+)"[^>]*?Target="([^"]+)"', rels)}
    targets.update({m[1]: m[0] for m in re.findall(r'<Relationship [^>]*?Target="([^"]+)"[^>]*?Id="([^"]+)"', rels)})
    sheets = {}
    for tag in re.findall(r"<sheet [^>]*/>", workbook):
        name = re.search(r'name="([^"]+)"', tag)
        rid = re.search(r'r:id="([^"]+)"', tag)
        if name and rid and rid.group(1) in targets:
            target = targets[rid.group(1)].lstrip("/")
            sheets[_unescape(name.group(1))] = target if target.startswith("xl/") else "xl/" + target
    return sheets


def _unescape(text):
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")


def _shared_strings(archive):
    try:
        data = _read(archive, "xl/sharedStrings.xml")
    except KeyError:
        return []
    return [_unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))) for si in re.findall(r"<si>(.*?)</si>", data, re.S)]


ROW_RX = re.compile(r'<row r="(\d+)"[^>]*?(?:/>|>.*?</row>)', re.S)


def _cell_rx(col, row):
    return re.compile(r'<c r="%s%d"(?=[ />])[^>]*?(?:/>|>.*?</c>)' % (col, row), re.S)


def _cell_value(cell, strings):
    if cell is None:
        return None
    kind = re.search(r' t="([^"]+)"', cell.split(">", 1)[0])
    kind = kind.group(1) if kind else ""
    value = re.search(r"<v>(.*?)</v>", cell, re.S)
    if kind == "s" and value:
        index = int(value.group(1))
        return strings[index] if index < len(strings) else ""
    if kind == "inlineStr":
        return _unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", cell, re.S)))
    if kind == "b":
        return bool(value and value.group(1).strip() == "1")
    if kind in ("str", "e"):
        return _unescape(value.group(1)) if value else ""
    return float(value.group(1)) if value and value.group(1).strip() else None


def _month_rows(archive, strings):
    """{month: [(row_number, date, country, checked)]} for month sheets that look like the log."""
    files = _sheet_files(archive)
    out = {}
    for month in MONTHS:
        if month not in files:
            continue
        data = _read(archive, files[month])
        rows = []
        for match in ROW_RX.finditer(data):
            number, xml = int(match.group(1)), match.group(0)
            if number < 3:
                continue
            serial = _cell_value(_find(xml, "A", number), strings)
            if not isinstance(serial, float) or not 20000 < serial < 80000:
                continue
            rows.append((number, EXCEL_EPOCH + timedelta(days=int(serial)), _cell_value(_find(xml, "C", number), strings) or "",
                         _cell_value(_find(xml, "D", number), strings) is True))
        out[month] = rows
    return out


def _find(row_xml, col, row):
    match = _cell_rx(col, row).search(row_xml)
    return match.group(0) if match else None


def read_log(binary):
    """Year, verified days and all days of the owner's travel log workbook."""
    archive = _open(binary)
    with archive:
        rows = _month_rows(archive, _shared_strings(archive))
    if len(rows) < 6:
        raise LogError("This workbook does not have the month sheets of the travel log (January … December).")
    days = [r for month in rows.values() for r in month]
    if not days:
        raise LogError("No dated rows were found in the month sheets.")
    year = max({d.year for _, d, _, _ in days}, key=lambda y: sum(1 for _, d, _, _ in days if d.year == y))
    return {"year": year,
            "verified": {d: c for _, d, c, checked in days if checked and c and d.year == year},
            "countries": {d: c for _, d, c, _ in days if c and d.year == year}}


# ---------------------------------------------------------------- writing the report

def _col_index(col):
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def _set_cell(row_xml, col, row, inner, kind=None, style=None):
    """Replace or insert one cell, keeping its style. inner=None empties the cell."""
    old = _find(row_xml, col, row)
    if old is not None and style is None:
        found = re.search(r' s="(\d+)"', old.split(">", 1)[0])
        style = found.group(1) if found else None
    attrs = ' r="%s%d"' % (col, row) + (' s="%s"' % style if style else "") + (' t="%s"' % kind if kind and inner is not None else "")
    cell = "<c%s/>" % attrs if inner is None else "<c%s>%s</c>" % (attrs, inner)
    if old is not None:
        return row_xml.replace(old, cell, 1)
    if row_xml.endswith("/>"):
        return row_xml[:-2] + ">" + cell + "</row>"
    for match in re.finditer(r'<c r="([A-Z]+)%d"' % row, row_xml):
        if _col_index(match.group(1)) > _col_index(col):
            return row_xml[:match.start()] + cell + row_xml[match.start():]
    return row_xml.replace("</row>", cell + "</row>")


def _time_fraction(text):
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", text or "")
    return None if not match else (int(match.group(1)) * 60 + int(match.group(2))) / 1440


def _shift_formula_cell(cell, old_row, new_row, col):
    cell = re.sub(r'r="%s%d"' % (col, old_row), 'r="%s%d"' % (col, new_row), cell, count=1)
    cell = re.sub(r'ref="%s%d"' % (col, old_row), 'ref="%s%d"' % (col, new_row), cell)
    cell = re.sub(r'<v>.*?</v>|<v/>', "<v/>", cell, flags=re.S)
    return re.sub(r"(?<![$A-Za-z0-9_.])([A-Z]{1,3})%d(?!\d)" % old_row, lambda m: "%s%d" % (m.group(1), new_row), cell)


def _column_style(sheet_xml, col):
    found = re.search(r'<c r="%s(?:[3-9]|\d\d)" s="(\d+)"' % col, sheet_xml)
    return found.group(1) if found else None


def fill_log(binary, state, home=""):
    """Return (xlsx bytes, summary) with every unticked day filled from the itinerary."""
    log = read_log(binary)
    year = log["year"]
    days = day_countries(state, year, log["verified"], home, log["countries"])
    source = _open(binary)
    changed, countries, months_touched, updated_days, kept = 0, set(), [], [], []
    with source:
        strings = _shared_strings(source)
        files = _sheet_files(source)
        parts = {info.filename: source.read(info.filename) for info in source.infolist()}
        for month in MONTHS:
            name = files.get(month)
            if not name:
                continue
            sheet = parts[name].decode("utf-8")
            template = {col: next(((int(m.group(1)), m.group(0)) for m in re.finditer(r'<c r="%s(\d+)"[^>]*>\s*<f[^>]*>.*?</c>' % col, sheet, re.S)), None) for col in ("G", "H")}
            styles = {col: _column_style(sheet, col) for col in "CEF"}
            month_changes = 0

            def edit(match):
                nonlocal month_changes
                number, row_xml = int(match.group(1)), match.group(0)
                if number < 3:
                    return row_xml
                serial = _cell_value(_find(row_xml, "A", number), strings)
                if not isinstance(serial, float):
                    return row_xml
                day = EXCEL_EPOCH + timedelta(days=int(serial))
                info = days.get(day)
                country_now = _cell_value(_find(row_xml, "C", number), strings) or ""
                if country_now:
                    countries.add(country_now)
                if info is None or _cell_value(_find(row_xml, "D", number), strings) is True:
                    return row_xml
                if info["source"] == "planned" and info.get("app") and info["app"] != country_now:
                    kept.append((day, country_now, info["app"]))
                if info["source"] not in ("booking", "assumed") or (info["source"] == "assumed" and country_now):
                    return row_xml
                new = row_xml
                if info["country"] and info["country"] != country_now:
                    if country_now:
                        updated_days.append((day, country_now, info["country"]))
                    new = _set_cell(new, "C", number, "<is><t>%s</t></is>" % escape(info["country"]), "inlineStr", styles["C"])
                    countries.add(info["country"])
                for col, key in (("E", "arrival"), ("F", "departure")):
                    fraction = _time_fraction(info[key])
                    if fraction is not None and _cell_value(_find(new, col, number), strings) != fraction:
                        new = _set_cell(new, col, number, "<v>%s</v>" % repr(fraction), None, styles[col])
                for col in ("G", "H"):
                    if template[col] and _find(new, col, number) is None:
                        new = new.replace("</row>", _shift_formula_cell(template[col][1], template[col][0], number, col) + "</row>")
                if new != row_xml:
                    month_changes += 1
                return new

            updated = ROW_RX.sub(edit, sheet)
            if updated != sheet:
                parts[name] = updated.encode("utf-8")
                changed += month_changes
                months_touched.append(month)
        if "Lists" in files:
            parts[files["Lists"]] = _update_lists(parts[files["Lists"]].decode("utf-8"), countries, strings, year, parts).encode("utf-8")
        _recalculate_on_open(parts)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    planned = sum(1 for d in days.values() if d["source"] != "log")
    return output.getvalue(), {"year": year, "rowsChanged": changed, "months": months_touched,
                               "verifiedDays": len(log["verified"]), "filledDays": planned, "totals": totals(days),
                               "updated": _ranges(updated_days), "keptPlan": _ranges(kept)}


def _ranges(items):
    """[(day, old, new)] -> compact list like [{"from": "2026-12-14", "to": "2026-12-14", "old": "China", "new": "Germany"}]."""
    out = []
    for day, old, new in sorted(items):
        if out and out[-1]["old"] == old and out[-1]["new"] == new and date.fromisoformat(out[-1]["to"]) + timedelta(days=1) == day:
            out[-1]["to"] = day.isoformat()
        else:
            out.append({"from": day.isoformat(), "to": day.isoformat(), "old": old, "new": new})
    return out


def _update_lists(sheet, countries, strings, year, parts):
    """Add countries missing from Lists!A2:A… (the map chart's data) with the same counting formula."""
    rows = {int(m.group(1)): m.group(0) for m in ROW_RX.finditer(sheet)}
    listed = {}
    for number, xml in rows.items():
        if number >= 2:
            value = _cell_value(_find(xml, "A", number), strings)
            if value:
                listed[number] = value
    missing = sorted(c for c in countries if c and c not in listed.values())
    if not missing or 2 not in rows:
        return sheet
    template = rows[2]
    a_style = re.search(r'<c r="A2" s="(\d+)"', template)
    b_cell = _find(template, "B", 2)
    next_row = max(listed) + 1 if listed else 2
    additions = []
    for country in missing:
        cell_a = '<c r="A%d"%s t="inlineStr"><is><t>%s</t></is></c>' % (next_row, ' s="%s"' % a_style.group(1) if a_style else "", escape(country))
        cell_b = _shift_formula_cell(b_cell, 2, next_row, "B").replace("Lists!A%d" % next_row, "Lists!A%d" % next_row) if b_cell else ""
        additions.append((next_row, '<row r="%d">%s%s</row>' % (next_row, cell_a, cell_b)))
        next_row += 1
    for number, xml in additions:
        if number in rows:
            sheet = sheet.replace(rows[number], xml, 1)
        else:
            later = [n for n in rows if n > number]
            anchor = rows[min(later)] if later else None
            sheet = sheet.replace(anchor, xml + anchor, 1) if anchor else sheet.replace("</sheetData>", xml + "</sheetData>")
            rows[number] = xml
    last = next_row - 1
    sheet = re.sub(r'<dimension ref="A1:B\d+"/>', '<dimension ref="A1:B%d"/>' % last, sheet)
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    workbook = re.sub(r"(Lists!\$[AB]\$2:\$[AB]\$)(\d+)", lambda m: m.group(1) + str(max(int(m.group(2)), last)), workbook)
    parts["xl/workbook.xml"] = workbook.encode("utf-8")
    return sheet


def _recalculate_on_open(parts):
    """Formulas were edited, so cached results are stale: drop the calculation chain and recalc on open."""
    parts.pop("xl/calcChain.xml", None)
    if "xl/_rels/workbook.xml.rels" in parts:
        rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
        parts["xl/_rels/workbook.xml.rels"] = re.sub(r'<Relationship [^>]*?Target="calcChain\.xml"[^>]*/>', "", rels).encode("utf-8")
    types = parts["[Content_Types].xml"].decode("utf-8")
    parts["[Content_Types].xml"] = re.sub(r'<Override [^>]*?PartName="/xl/calcChain\.xml"[^>]*/>', "", types).encode("utf-8")
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    if "<calcPr" in workbook:
        workbook = re.sub(r"<calcPr([^>]*?)/>", lambda m: "<calcPr%s fullCalcOnLoad=\"1\"/>" % re.sub(r' fullCalcOnLoad="[^"]*"', "", m.group(1)), workbook, count=1)
    else:
        workbook = workbook.replace("</workbook>", '<calcPr fullCalcOnLoad="1"/></workbook>')
    parts["xl/workbook.xml"] = workbook.encode("utf-8")


def report_name(year):
    return "Travel_Log_%d_%s.xlsx" % (year, datetime.now().strftime("%Y%m%d"))
