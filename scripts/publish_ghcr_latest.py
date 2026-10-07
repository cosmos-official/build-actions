"""Mirror a multi-platform image as a version and latest, retaining its manifest graph."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import time


def publish_latest(source_image: str, destination_image: str, version_tag: str) -> dict:
    source_repository, separator, expected_digest = source_image.partition("@")
    if not source_repository or separator != "@" or not re.fullmatch(r"sha256:[a-f0-9]{64}", expected_digest):
        raise ValueError("The source image must be pinned to a sha256 digest")
    ghcr_image = os.environ["GHCR_IMAGE"]
    destination_match = re.fullmatch(r"ghcr\.io/([a-z0-9-]+)/([a-z0-9][a-z0-9._-]*)", ghcr_image)
    if not destination_match:
        raise ValueError("GHCR_IMAGE must be an untagged organization-scoped image")
    if destination_image != f"{ghcr_image}:latest":
        raise ValueError("Only the GHCR_IMAGE latest mirror configured in secrets may be pruned")
    if not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", version_tag):
        raise ValueError("An existing SemVer tag is required")
    package_owner, package_name = destination_match.groups()
    destination_images = (f"{ghcr_image}:{version_tag}", destination_image)

    copy_started = time.monotonic()
    for tagged_image in destination_images:
        subprocess.run(
            [
                "skopeo", "copy", "--all", "--preserve-digests", "--retry-times", "3",
                f"docker://{source_image}", f"docker://{tagged_image}",
            ],
            check=True,
        )
    copy_seconds = time.monotonic() - copy_started

    destination_repository = destination_image.removesuffix(":latest")
    pending = [(expected_digest, image) for image in destination_images]
    protected_digests = set()
    platforms = set()
    while pending:
        digest, image_reference = pending.pop()
        manifest_bytes = subprocess.run(
            ["skopeo", "inspect", "--raw", f"docker://{image_reference}"],
            check=True, stdout=subprocess.PIPE,
        ).stdout
        actual_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        if actual_digest != digest:
            raise ValueError(f"GHCR manifest digest mismatch: expected {digest}, got {actual_digest}")
        if digest in protected_digests:
            continue
        manifest = json.loads(manifest_bytes)
        protected_digests.add(digest)
        for child in manifest.get("manifests", []):
            child_digest = child["digest"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", child_digest):
                raise ValueError("Invalid digest in the GHCR manifest graph")
            platform = child.get("platform", {})
            if "os" in platform and "architecture" in platform:
                platforms.add((platform["os"], platform["architecture"]))
            pending.append((child_digest, f"{destination_repository}@{child_digest}"))

    if not {("linux", "arm64"), ("linux", "amd64")} <= platforms:
        raise ValueError("GHCR latest must contain both linux/arm64 and linux/amd64")

    package_api = f"orgs/{package_owner}/packages/container/{package_name}"
    version_pages = json.loads(subprocess.run(
        [
            "gh", "api", "--paginate", "--slurp",
            "-H", "X-GitHub-Api-Version: 2026-03-10",
            f"{package_api}/versions?per_page=100",
        ],
        check=True, stdout=subprocess.PIPE, text=True,
    ).stdout)
    versions = [version for page in version_pages for version in page]
    for image_tag in (version_tag, "latest"):
        tagged_versions = [
            version for version in versions
            if image_tag in version["metadata"]["container"]["tags"]
        ]
        if len(tagged_versions) != 1 or tagged_versions[0]["name"] != expected_digest:
            raise ValueError("GHCR package versions do not identify the published tag digest; refusing cleanup")

    # Check both tags again immediately before deletion. Workflow concurrency
    # serializes our writers; this also detects a manual update during verification.
    for tagged_image in destination_images:
        tagged_bytes = subprocess.run(
            ["skopeo", "inspect", "--raw", f"docker://{tagged_image}"],
            check=True, stdout=subprocess.PIPE,
        ).stdout
        if "sha256:" + hashlib.sha256(tagged_bytes).hexdigest() != expected_digest:
            raise ValueError("GHCR tag changed during verification; refusing cleanup")

    deleted_versions = 0
    for version in versions:
        if version["name"] in protected_digests:
            continue
        version_id = version["id"]
        if not isinstance(version_id, int) or isinstance(version_id, bool) or version_id <= 0:
            raise ValueError("Invalid GHCR package version ID")
        subprocess.run(
            [
                "gh", "api", "--method", "DELETE",
                "-H", "X-GitHub-Api-Version: 2026-03-10",
                f"{package_api}/versions/{version_id}",
            ],
            check=True,
        )
        deleted_versions += 1

    return {
        "digest": expected_digest,
        "copy_seconds": copy_seconds,
        "deleted_versions": deleted_versions,
        "preserved_manifests": len(protected_digests),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--destination-image", required=True)
    parser.add_argument("--version-tag", required=True)
    arguments = parser.parse_args()
    result = publish_latest(arguments.source_image, arguments.destination_image, arguments.version_tag)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(
            f"- GHCR tags: `{arguments.version_tag}`, `latest`\n"
            f"- GHCR digest: `{result['digest']}`\n"
            f"- GHCR copy duration: {result['copy_seconds']:.1f}s\n"
            f"- Previous GHCR versions removed: {result['deleted_versions']}\n"
            f"- Current manifests preserved: {result['preserved_manifests']}\n"
        )
