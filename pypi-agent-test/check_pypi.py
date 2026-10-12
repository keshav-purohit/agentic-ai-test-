
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

PYPI_URL = "https://pypi.org/pypi"
TIMEOUT = 15
RETRIES = 3


def normalize_name(name):
    """Normalize package names according to PyPI conventions."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_requirements(filename):
    """Parse simple exact pins such as requests==2.31.0."""
    dependencies = []
    unsupported = []

    pattern = re.compile(
        r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)"
    )

    with open(filename, encoding="utf-8") as file:
        for line_number, raw_line in enumerate(file, start=1):
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            match = pattern.match(line)

            if not match:
                unsupported.append((line_number, line))
                continue

            dependencies.append({
                "name": match.group(1),
                "version": match.group(2),
                "line": line_number,
            })

    return dependencies, unsupported


def get_pypi_json(url):
    """Fetch JSON from PyPI; distinguish 404 from network errors."""
    for attempt in range(RETRIES):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "PyPI-Dependency-Checker/1.0"},
        )

        try:
            with urllib.request.urlopen(
                request, timeout=TIMEOUT
            ) as response:
                return json.load(response)

        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None

            if exc.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(
                    f"PyPI returned HTTP {exc.code}"
                ) from exc

            if attempt == RETRIES - 1:
                raise RuntimeError(
                    f"PyPI returned HTTP {exc.code} after retries"
                ) from exc

        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == RETRIES - 1:
                raise RuntimeError(f"Network error: {exc}") from exc

        time.sleep(2 ** attempt)

    raise RuntimeError("Could not retrieve PyPI metadata")


def get_package_metadata(name):
    encoded_name = urllib.parse.quote(normalize_name(name), safe="")
    return get_pypi_json(f"{PYPI_URL}/{encoded_name}/json")


def get_release_metadata(name, version):
    encoded_name = urllib.parse.quote(normalize_name(name), safe="")
    encoded_version = urllib.parse.quote(version, safe="")
    return get_pypi_json(
        f"{PYPI_URL}/{encoded_name}/{encoded_version}/json"
    )


def first_file_upload_time(release_metadata):
    """
    Return upload_time from the first file in the release's urls list.
    Do not sort the list or substitute a different file.
    """
    urls = release_metadata.get("urls", [])

    if not urls:
        return None

    upload_time = urls[0].get("upload_time")

    if not upload_time:
        return None

    try:
        return datetime.fromisoformat(
            upload_time.replace("Z", "+00:00")
        )
    except ValueError:
        return None


def check_file(filename):
    # Requirement 1: count every physical line, including comments/blanks.
    with open(filename, encoding="utf-8") as file:
        total_lines = sum(1 for _ in file)

    dependencies, unsupported = parse_requirements(filename)

    missing_packages = []
    missing_versions = []
    valid_releases = []
    errors = []
    package_cache = {}

    for dependency in dependencies:
        name = dependency["name"]
        version = dependency["version"]
        line = dependency["line"]
        normalized = normalize_name(name)

        print(f"Checking line {line}: {name}=={version}")

        try:
            # Requirement 2: does the package exist?
            if normalized not in package_cache:
                package_cache[normalized] = get_package_metadata(name)

            package_data = package_cache[normalized]

            if package_data is None:
                missing_packages.append(f"{name}=={version}")
                print("  Package does not exist on PyPI")
                continue

            # Requirement 3: does this exact pinned release exist?
            release_data = get_release_metadata(name, version)

            if release_data is None:
                missing_versions.append(f"{name}=={version}")
                print("  Package exists, but version does not")
                continue

            # Requirement 4: use the first file's upload_time.
            timestamp = first_file_upload_time(release_data)

            if timestamp is None:
                print("  Valid release, but no first-file upload_time")
                continue

            valid_releases.append({
                "name": name,
                "version": version,
                "timestamp": timestamp,
                "line": line,
            })

            print("  Package and version exist")

        except RuntimeError as exc:
            errors.append(f"{name}=={version}: {exc}")
            print(f"  Could not verify: {exc}")

    # Find the oldest valid pinned release by first-file upload_time.
    oldest = min(
        valid_releases,
        key=lambda item: item["timestamp"],
        default=None,
    )

    print("\n" + "=" * 55)
    print("PYPI VALIDATION REPORT")
    print("=" * 55)

    print(f"\n1. Dependencies checked (physical lines): {total_lines}")

    print("\n2. Packages not on PyPI:")
    if missing_packages:
        for item in missing_packages:
            print(f"   {item}")
    else:
        print("   None")

    print("\n3. Versions not on PyPI:")
    if missing_versions:
        for item in missing_versions:
            print(f"   {item}")
    else:
        print("   None")

    print("\n4. Oldest pinned release:")
    if oldest:
        year = oldest["timestamp"].year
        print(f"   {oldest['name']}=={oldest['version']}({year})")
    else:
        print("   No eligible release with an upload timestamp")

    if unsupported:
        print("\nUnsupported dependency lines:")
        for line, content in unsupported:
            print(f"   Line {line}: {content}")

    if errors:
        print("\nDependencies that could not be verified:")
        for error in errors:
            print(f"   {error}")

    print("=" * 55)

    # Do not silently treat an incomplete run as a successful check.
    return 1 if errors or unsupported else 0


if __name__ == "__main__":
    filename = sys.argv[1] if len(sys.argv) > 1 else "requirements.txt"

    if not Path(filename).is_file():
        print(f"File not found: {filename}")
        sys.exit(2)

    sys.exit(check_file(filename))
