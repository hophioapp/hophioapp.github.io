#!/usr/bin/env python3
"""FlixBus Europe GTFS -> the small feed Hophio downloads (international coaches, 1.2).

Keeps every trip that stops at least once in Estonia, whole (its stops abroad too), and
nothing else. Standard library only: runs the same on a Mac and in the site's GitHub
Action (site/.github/workflows/build-flix.yml).

What changes on the way:
- Times. Flix publishes them in UTC (agency_timezone=UTC). The app reads every feed in
  Tallinn time, so each trip gets the Tallinn offset of its own day (+2 h winter, +3 h
  summer): a trip that runs on both sides of a clock change becomes two trips with two
  services. Hours past 24 are kept (GTFS allows 25:30 = 01:30 the next morning).
- Shapes are left out (160 MB for all of Europe); the app draws straight lines between
  stops, as for other feeds without shapes.
- pickup_type / drop_off_type and stop_timezone stay: the app needs them (where one may
  board or get off; which country a stop is in).

Usage: flix_filter.py <gtfs_generic_eu.zip> <out.zip>
"""
import csv
import datetime as dt
import io
import sys
import zipfile
from collections import defaultdict
from zoneinfo import ZoneInfo

ESTONIA_TZ = "Europe/Tallinn"
TALLINN = ZoneInfo(ESTONIA_TZ)


def rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig", newline=""))


def seconds(t):
    h, m, s = (int(x) for x in t.split(":"))
    return h * 3600 + m * 60 + s


def hms(sec):
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def ymd(s):
    return dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def offset_hours(day, utc_seconds):
    """Tallinn's UTC offset at that instant (whole hours)."""
    instant = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=utc_seconds)
    return int(instant.astimezone(TALLINN).utcoffset().total_seconds() // 3600)


def write(zout, name, header, data):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(data)
    zout.writestr(name, buf.getvalue())


def main(src, dst):
    z = zipfile.ZipFile(src)

    stops = {r["stop_id"]: r for r in rows(z, "stops.txt")}
    estonian = {sid for sid, r in stops.items() if r.get("stop_timezone") == ESTONIA_TZ}
    if not estonian:
        sys.exit("no stop in Estonia - the source changed its format")

    # Which trips touch Estonia, and all their stop_times.
    by_trip = defaultdict(list)
    touches = set()
    for r in rows(z, "stop_times.txt"):
        t = r["trip_id"]
        by_trip[t].append(r)
        if r["stop_id"] in estonian:
            touches.add(t)
    by_trip = {t: sorted(v, key=lambda r: int(r["stop_sequence"])) for t, v in by_trip.items() if t in touches}

    trips = {r["trip_id"]: r for r in rows(z, "trips.txt") if r["trip_id"] in touches}
    services = {t["service_id"] for t in trips.values()}

    # Dates of each service.
    dates = defaultdict(set)
    for r in rows(z, "calendar.txt"):
        if r["service_id"] not in services:
            continue
        d, end = ymd(r["start_date"]), ymd(r["end_date"])
        days = [r[k] == "1" for k in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")]
        while d <= end:
            if days[d.weekday()]:
                dates[r["service_id"]].add(d)
            d += dt.timedelta(days=1)
    if "calendar_dates.txt" in z.namelist():
        for r in rows(z, "calendar_dates.txt"):
            if r["service_id"] not in services:
                continue
            d = ymd(r["date"])
            if r["exception_type"] == "1":
                dates[r["service_id"]].add(d)
            elif r["exception_type"] == "2":
                dates[r["service_id"]].discard(d)

    out_trips, out_times, out_cal = [], [], []
    used_stops, used_routes = set(), set()
    for tid, trip in trips.items():
        st = by_trip.get(tid) or []
        if len(st) < 2:
            continue
        first = seconds(st[0]["departure_time"] or st[0]["arrival_time"])
        by_offset = defaultdict(list)
        for d in dates.get(trip["service_id"], ()):
            by_offset[offset_hours(d, first)].append(d)
        for off, ds in sorted(by_offset.items()):
            new_tid = f"{tid}~{off}"
            new_svc = f"{trip['service_id']}~{off}"
            shift = off * 3600
            out_trips.append([trip["route_id"], new_tid, new_svc, trip.get("trip_headsign", ""), trip.get("block_id", "")])
            for d in sorted(ds):
                out_cal.append([new_svc, d.strftime("%Y%m%d"), 1])
            for r in st:
                a = seconds(r["arrival_time"] or r["departure_time"]) + shift
                b = seconds(r["departure_time"] or r["arrival_time"]) + shift
                out_times.append([new_tid, r["stop_id"], hms(a), hms(b), r["stop_sequence"],
                                  r.get("pickup_type") or "0", r.get("drop_off_type") or "0"])
                used_stops.add(r["stop_id"])
            used_routes.add(trip["route_id"])

    # The same service id can serve several trips: one calendar row per (service, date).
    out_cal = sorted({tuple(r) for r in out_cal})

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zo:
        write(zo, "agency.txt", ["agency_id", "agency_name", "agency_url", "agency_timezone", "agency_lang"],
              [[r["agency_id"], "FlixBus" if r["agency_id"].startswith("FLIXBUS") else "FlixTrain",
                r["agency_url"], ESTONIA_TZ, r.get("agency_lang", "en")] for r in rows(z, "agency.txt")])
        # "FlixBus N1234" -> "N1234": the line badge holds the number; the carrier is
        # written beside it (a badge "FlixBus N1234" did not fit, 27.11).
        def short_name(r):
            n = r["route_short_name"]
            for p in ("FlixBus ", "FlixTrain "):
                if n.startswith(p):
                    return n[len(p):]
            return n

        write(zo, "routes.txt", ["agency_id", "route_id", "route_short_name", "route_long_name", "route_type",
                                 "route_color", "route_text_color"],
              [[r["agency_id"], r["route_id"], short_name(r), r["route_long_name"], r["route_type"],
                r.get("route_color", ""), r.get("route_text_color", "")]
               for r in rows(z, "routes.txt") if r["route_id"] in used_routes])
        write(zo, "stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon", "stop_code", "stop_timezone"],
              [[s, stops[s]["stop_name"], stops[s]["stop_lat"], stops[s]["stop_lon"], stops[s].get("stop_code", ""),
                stops[s].get("stop_timezone", "")] for s in sorted(used_stops)])
        write(zo, "trips.txt", ["route_id", "trip_id", "service_id", "trip_headsign", "block_id"], out_trips)
        write(zo, "stop_times.txt", ["trip_id", "stop_id", "arrival_time", "departure_time", "stop_sequence",
                                     "pickup_type", "drop_off_type"], out_times)
        write(zo, "calendar_dates.txt", ["service_id", "date", "exception_type"], out_cal)
        fi = next(rows(z, "feed_info.txt"), {})
        write(zo, "feed_info.txt", ["feed_publisher_name", "feed_publisher_url", "feed_lang", "feed_start_date",
                                    "feed_end_date", "feed_version"],
              [[fi.get("feed_publisher_name", "FlixMobility"), fi.get("feed_publisher_url", ""), "en",
                fi.get("feed_start_date", ""), fi.get("feed_end_date", ""),
                dt.date.today().strftime("%Y%m%d")]])

    print(f"trips {len(out_trips)} (from {len(trips)}), stops {len(used_stops)} "
          f"(Estonia {len(used_stops & estonian)}), routes {len(used_routes)}, stop_times {len(out_times)}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
