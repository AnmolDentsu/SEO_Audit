from __future__ import annotations

import csv
import gzip
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urljoin

import httpx
from lxml import etree

from app import models
from app.database import SessionLocal
from app.services.sitemap_fetcher import normalize_url


PROJECT_NAME = "tab3"

OUTPUT_DIR = Path("reports")

BLOCK_STATUSES = {401, 403, 406, 429, 503}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "application/xml,text/xml,text/html;q=0.9,*/*;q=0.1",
    "Accept-Language": "en-IN,en;q=0.9",
}


def local_name(node) -> str:
    if not getattr(node, "tag", None):
        return ""
    return etree.QName(node.tag).localname.lower()


def child_text(node, wanted: str) -> str | None:
    wanted = wanted.lower()

    for child in node:
        if local_name(child) == wanted:
            value = (child.text or "").strip()
            return value or None

    return None


def decode_body(url: str, body: bytes, headers: dict) -> bytes:
    encoding = str(headers.get("content-encoding") or "").lower()

    if (
        "gzip" in encoding
        or url.lower().endswith(".gz")
        or body[:2] == b"\x1f\x8b"
    ):
        try:
            return gzip.decompress(body)
        except Exception:
            pass

    return body


def fetch_xml(client: httpx.Client, url: str) -> tuple[str, bytes]:
    response = client.get(url)

    if response.status_code in BLOCK_STATUSES:
        try:
            from curl_cffi import requests as curl_requests

            recovered = curl_requests.get(
                url,
                impersonate="chrome",
                timeout=30,
                allow_redirects=True,
                headers=HEADERS,
            )

            if recovered.status_code < 400:
                return (
                    str(recovered.url),
                    decode_body(
                        str(recovered.url),
                        recovered.content,
                        dict(recovered.headers),
                    ),
                )
        except Exception:
            pass

    response.raise_for_status()

    return (
        str(response.url),
        decode_body(
            str(response.url),
            response.content,
            dict(response.headers),
        ),
    )


def source_rows_for_run(db, run):
    snapshot = dict(
        (run.config_json or {}).get("_input_snapshot") or {}
    )

    rows = list(snapshot.get("sources") or [])

    if rows:
        return rows

    return [
        {
            "id": row.id,
            "label": row.label,
            "role": row.role,
            "root_url": row.root_url,
        }
        for row in (
            db.query(models.SitemapSource)
            .filter(
                models.SitemapSource.project_id == run.project_id
            )
            .order_by(models.SitemapSource.id)
            .all()
        )
    ]


def collect_occurrences(client: httpx.Client, source: dict):
    root_sitemap = str(source["root_url"])

    queue = deque([root_sitemap])
    visited = set()

    occurrences = []

    while queue:
        sitemap_url = queue.popleft()
        sitemap_key = normalize_url(sitemap_url)

        if sitemap_key in visited:
            continue

        visited.add(sitemap_key)

        final_url, body = fetch_xml(client, sitemap_url)

        parser = etree.XMLParser(
            recover=True,
            no_network=True,
            resolve_entities=False,
            huge_tree=True,
        )

        root = etree.fromstring(body, parser=parser)
        kind = local_name(root)

        if kind == "sitemapindex":
            for node in root:
                if local_name(node) != "sitemap":
                    continue

                loc = child_text(node, "loc")

                if loc:
                    queue.append(urljoin(final_url, loc))

        elif kind == "urlset":
            for node in root:
                if local_name(node) != "url":
                    continue

                loc = child_text(node, "loc")

                if not loc:
                    continue

                raw_url = urljoin(final_url, loc)
                normalized = normalize_url(raw_url)

                occurrences.append(
                    {
                        "source_id": source.get("id"),
                        "site": source.get("label"),
                        "role": source.get("role"),
                        "root_sitemap": root_sitemap,
                        "child_sitemap": final_url,
                        "raw_url": raw_url,
                        "normalized_url": normalized,
                        "lastmod": child_text(node, "lastmod"),
                    }
                )

        else:
            print(
                f"WARNING: unsupported XML root "
                f"{kind!r}: {final_url}"
            )

    return occurrences


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()

    try:
        project = (
            db.query(models.Project)
            .filter(models.Project.name == PROJECT_NAME)
            .first()
        )

        if not project:
            raise SystemExit(
                f"Project {PROJECT_NAME!r} not found"
            )

        run = (
            db.query(models.SitemapRun)
            .filter(
                models.SitemapRun.project_id == project.id
            )
            .order_by(models.SitemapRun.id.desc())
            .first()
        )

        if not run:
            raise SystemExit(
                f"No sitemap run found for {PROJECT_NAME!r}"
            )

        sources = source_rows_for_run(db, run)

        print(f"Project : {project.name}")
        print(f"Run ID  : {run.id}")
        print(f"Sources : {len(sources)}")

        all_occurrences = []

        with httpx.Client(
            timeout=30,
            headers=HEADERS,
            follow_redirects=True,
        ) as client:

            for source in sources:
                print(
                    f"\nReading {source.get('label')} "
                    f"-> {source.get('root_url')}"
                )

                rows = collect_occurrences(
                    client,
                    source,
                )

                print(
                    f"Raw URL records: {len(rows):,}"
                )

                all_occurrences.extend(rows)

        grouped = defaultdict(list)

        for row in all_occurrences:
            key = (
                row["source_id"],
                row["normalized_url"],
            )
            grouped[key].append(row)

        duplicate_groups = {
            key: rows
            for key, rows in grouped.items()
            if len(rows) > 1
        }

        output_rows = []

        group_number = 0

        for (_source_id, normalized), rows in sorted(
            duplicate_groups.items(),
            key=lambda item: (
                str(item[1][0]["site"]),
                item[0][1],
            ),
        ):
            group_number += 1

            raw_variants = {
                row["raw_url"]
                for row in rows
            }

            duplicate_kind = (
                "EXACT_XML_DUPLICATE"
                if len(raw_variants) == 1
                else "NORMALIZED_VARIANT_DUPLICATE"
            )

            for occurrence_number, row in enumerate(
                rows,
                start=1,
            ):
                output_rows.append(
                    {
                        "duplicate_group": group_number,
                        "site": row["site"],
                        "role": row["role"],
                        "duplicate_kind": duplicate_kind,
                        "normalized_url": normalized,
                        "occurrence_count": len(rows),
                        "duplicate_records": len(rows) - 1,
                        "occurrence_number": occurrence_number,
                        "is_first_occurrence": (
                            occurrence_number == 1
                        ),
                        "raw_url": row["raw_url"],
                        "child_sitemap": row[
                            "child_sitemap"
                        ],
                        "root_sitemap": row[
                            "root_sitemap"
                        ],
                        "lastmod": row.get("lastmod"),
                    }
                )

        output_file = (
            OUTPUT_DIR
            / f"sitemap-duplicates-run-{run.id}.csv"
        )

        columns = [
            "duplicate_group",
            "site",
            "role",
            "duplicate_kind",
            "normalized_url",
            "occurrence_count",
            "duplicate_records",
            "occurrence_number",
            "is_first_occurrence",
            "raw_url",
            "child_sitemap",
            "root_sitemap",
            "lastmod",
        ]

        with output_file.open(
            "w",
            newline="",
            encoding="utf-8-sig",
        ) as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=columns,
            )
            writer.writeheader()
            writer.writerows(output_rows)

        duplicate_record_count = sum(
            len(rows) - 1
            for rows in duplicate_groups.values()
        )

        print("\n" + "=" * 100)
        print("DUPLICATE REPORT")
        print("=" * 100)

        print(
            "Raw sitemap URL records :",
            f"{len(all_occurrences):,}",
        )

        print(
            "Duplicate URL groups     :",
            f"{len(duplicate_groups):,}",
        )

        print(
            "Duplicate records        :",
            f"{duplicate_record_count:,}",
        )

        print(
            "CSV                       :",
            output_file.resolve(),
        )

        print("\nBY SITE")

        per_site = defaultdict(
            lambda: {
                "groups": 0,
                "duplicate_records": 0,
            }
        )

        for rows in duplicate_groups.values():
            site = str(rows[0]["site"])

            per_site[site]["groups"] += 1
            per_site[site]["duplicate_records"] += (
                len(rows) - 1
            )

        for site, stats in sorted(per_site.items()):
            print(
                f"{site:20} "
                f"groups={stats['groups']:,} "
                f"duplicate_records="
                f"{stats['duplicate_records']:,}"
            )

    finally:
        db.close()


if __name__ == "__main__":
    main()